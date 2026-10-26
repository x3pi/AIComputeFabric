"""Studio RAG against real PostgreSQL+pgvector, real embedding/rerank servers and a real (small) LLM.

Needs AICF_PG_ADMIN_DSN, AICF_PG_APP_DSN, AICF_EMBED_URL, AICF_RERANK_URL, AICF_LLM_URL; otherwise skipped, NOT passed."""
import base64
import os
import uuid

import psycopg
import pytest

from studio.app import build_app
from studio.config import Quotas, Settings
from studio.db import setup_schema
from studio.web import serve
from tests.studio.test_studio_service import ADMIN, APP, call

ENV = {k: os.environ.get(k) for k in ("AICF_EMBED_URL", "AICF_RERANK_URL", "AICF_LLM_URL")}
pytestmark = pytest.mark.skipif(not (ADMIN and APP and all(ENV.values())), reason="set AICF_PG_*_DSN, AICF_EMBED_URL, AICF_RERANK_URL, AICF_LLM_URL")
POLICY = ("Chính sách nghỉ phép. Nhân viên chính thức được nghỉ phép năm 12 ngày mỗi năm. Nhân viên thử việc không được hưởng phép năm. "
          "Đơn xin nghỉ phép phải nộp trước ít nhất 3 ngày làm việc.")
IT = "Hướng dẫn mạng. Mật khẩu wifi khách được đổi vào ngày đầu tiên mỗi tháng. Sự cố mạng báo qua email hotro@congty.vn."


@pytest.fixture(scope="module")
def rag(tmp_path_factory):
    setup_schema(ADMIN)
    s = Settings(data_dir=tmp_path_factory.mktemp("d"), app_dsn=APP, embed_url=ENV["AICF_EMBED_URL"], rerank_url=ENV["AICF_RERANK_URL"],
                 llm_url=ENV["AICF_LLM_URL"], quotas=Quotas(max_documents=3, max_upload_bytes=200_000))
    app = build_app(s)
    srv = serve(app.router, "127.0.0.1", 0, s.max_body)
    yield type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
    srv.shutdown()
    app.close()


@pytest.fixture()
def tenant():
    t = "rg-" + uuid.uuid4().hex[:8]
    yield t
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM aicf_chunks WHERE tenant_id=%s", (t,))


def test_upload_search_ask_with_sources_and_isolation(rag, tenant):
    b = f"/v1/tenants/{tenant}/rag"
    assert call(rag, "POST", b + "/ask", {"query": "nghỉ phép?"}, role="viewer")[1]["warnings"] == ["empty_knowledge_base"]
    assert call(rag, "POST", b + "/documents", {"name": "phep.txt", "text": POLICY}, role="viewer")[0] == 403
    assert call(rag, "POST", b + "/documents", {"name": "phep.txt", "text": POLICY}, role="operator")[0] == 201
    assert call(rag, "POST", b + "/documents", {"name": "mang.md", "text": IT}, role="operator")[0] == 201
    st, r = call(rag, "POST", b + "/ask", {"query": "Nhân viên chính thức được nghỉ phép bao nhiêu ngày một năm?"}, role="viewer")
    assert st == 200 and r["sources"][0]["doc"] == "phep.txt" and "12" in r["answer"], r
    assert "12 ngày" in r["sources"][0]["quote"] or "12" in r["sources"][0]["quote"]
    other = "rg-" + uuid.uuid4().hex[:8]
    assert call(rag, "POST", f"/v1/tenants/{other}/rag/search", {"query": "nghỉ phép"}, role="viewer")[1]["hits"] == []
    assert call(rag, "GET", f"/v1/tenants/{other}/rag/documents/phep.txt", role="viewer")[0] == 404
    assert call(rag, "DELETE", b + "/documents/phep.txt", role="operator")[0] == 403
    assert call(rag, "DELETE", b + "/documents/phep.txt", role="admin")[0] == 200
    assert call(rag, "POST", b + "/search", {"query": "nghỉ phép năm"}, role="viewer")[1]["hits"][0]["doc"] == "mang.md", "deleted document is gone at once"


def test_hostile_uploads_and_quotas(rag, tenant):
    b = f"/v1/tenants/{tenant}/rag/documents"
    fake_pdf = base64.b64encode(b"this is plain text, not a pdf").decode()
    assert call(rag, "POST", b, {"name": "x.pdf", "content_base64": fake_pdf}, role="operator")[0] == 400
    assert call(rag, "POST", b, {"name": "x.txt", "content_base64": "%%%not base64"}, role="operator")[0] == 400
    assert call(rag, "POST", b, {"name": "../../etc/passwd", "text": POLICY}, role="operator")[0] == 400
    st, r = call(rag, "POST", b, {"name": "../../x.txt", "text": POLICY + " traversal"}, role="operator")
    assert st == 201 and "/" not in r["id"] and "\\" not in r["id"], r
    assert call(rag, "POST", b, {"name": "x.txt", "text": "a" * 300_000}, role="operator")[0] == 400, "over max_upload_bytes"
    assert call(rag, "POST", b, {"name": "a.txt", "text": "\x00\x01\x02" * 50}, role="operator")[0] == 400, "binary content in a text file"
    assert call(rag, "POST", b, {"name": "d.txt", "text": POLICY, "data_class": "SECRET"}, role="operator")[0] == 400
    for i in range(2):
        call(rag, "POST", b, {"name": f"q{i}.txt", "text": POLICY + str(i)}, role="operator")
    st, r = call(rag, "POST", b, {"name": "q9.txt", "text": POLICY}, role="operator")
    assert st == 429, "document-count quota (3 here, including the earlier accepted one)"
    assert call(rag, "POST", b, {"name": "q0.txt", "text": POLICY + " v2"}, role="operator")[1]["replaced"] is True, "re-uploading replaces, never duplicates"


def test_real_docx_and_pdf_through_the_api_are_answerable(rag, tenant):
    from tests.studio.test_studio_extract import FONTS, make_docx, make_pdf
    b = f"/v1/tenants/{tenant}/rag"
    d = make_docx(["Quy định công tác phí. Nhân viên đi công tác được hỗ trợ 500.000 đồng mỗi ngày tiền ăn."])
    assert call(rag, "POST", b + "/documents", {"name": "cong-tac.docx", "content_base64": base64.b64encode(d).decode()}, role="operator")[0] == 201
    if FONTS:
        p = make_pdf(["Quy chế bảo mật. Mật khẩu phải đổi sau mỗi 90 ngày."])
        assert call(rag, "POST", b + "/documents", {"name": "bao-mat.pdf", "content_base64": base64.b64encode(p).decode()}, role="operator")[0] == 201
        st, r = call(rag, "POST", b + "/ask", {"query": "Mật khẩu phải đổi sau bao nhiêu ngày?"}, role="viewer")
        assert r["sources"][0]["doc"] == "bao-mat.pdf" and "90" in r["answer"], r
    st, r = call(rag, "POST", b + "/ask", {"query": "Tiền ăn khi đi công tác là bao nhiêu?"}, role="viewer")
    assert r["sources"][0]["doc"] == "cong-tac.docx" and "500" in r["answer"], r


def test_a_20_mib_upload_is_accepted_and_21_mib_refused(tmp_path_factory):
    s = Settings(data_dir=tmp_path_factory.mktemp("big"), app_dsn=APP, embed_url=ENV["AICF_EMBED_URL"], rerank_url=ENV["AICF_RERANK_URL"], llm_url=ENV["AICF_LLM_URL"])
    app = build_app(s)
    srv = serve(app.router, "127.0.0.1", 0, s.max_body)
    svc = type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
    try:
        t = "rg-big-" + uuid.uuid4().hex[:6]
        line = "Chính sách số {}: nhân viên được hưởng quyền lợi theo quy định hiện hành của công ty. "
        text = "".join(line.format(i) for i in range(200_000))[:900_000]            # just under the 1M-character cap
        st, r = call(svc, "POST", f"/v1/tenants/{t}/rag/documents", {"name": "lon.txt", "text": text}, role="operator")
        assert st == 201 and r["chunks"] > 1000, (st, r)
        assert call(svc, "POST", f"/v1/tenants/{t}/rag/documents", {"name": "vuot.txt", "text": line * 20000}, role="operator")[0] == 400, "over the per-document character cap"
        assert call(svc, "POST", f"/v1/tenants/{t}/rag/documents", {"name": "qua-lon.txt", "text": "a" * (21 * 1024 * 1024)}, role="operator")[0] == 400
        assert call(svc, "POST", f"/v1/tenants/{t}/rag/search", {"query": "quyền lợi nhân viên"}, role="viewer")[0] == 200
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DELETE FROM aicf_chunks WHERE tenant_id LIKE 'rg-big-%'")
        srv.shutdown()
        app.close()


def test_search_does_not_leak_memory_and_pooled_connections_do_not_leak_tenants(rag, tenant):
    import gc
    rss = lambda: int(open("/proc/self/statm").read().split()[1]) * 4096 / 2**20
    other = tenant + "-b"
    b = f"/v1/tenants/{tenant}/rag"
    call(rag, "POST", b + "/documents", {"name": "a.txt", "text": "Tài liệu bí mật của tenant A nói về mức lương giám đốc."}, role="operator")
    call(rag, "POST", f"/v1/tenants/{other}/rag/documents", {"name": "b.txt", "text": "Tài liệu của tenant B nói về lịch trực nhật."}, role="operator")
    try:
        for _ in range(40):                                       # warm up; connections are reused across tenants from here on
            call(rag, "POST", b + "/search", {"query": "mức lương"}, role="viewer")
        gc.collect()
        a = rss()
        for i in range(400):
            t = tenant if i % 2 else other
            hits = call(rag, "POST", f"/v1/tenants/{t}/rag/search", {"query": "mức lương giám đốc lịch trực"}, role="viewer")[1]["hits"]
            assert {h["doc"] for h in hits} == {"a.txt" if t == tenant else "b.txt"}, "a pooled connection carried the previous tenant"
        gc.collect()
        assert rss() - a < 4, f"RSS grew {rss() - a:.1f} MiB over 400 searches"
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DELETE FROM aicf_chunks WHERE tenant_id=%s", (other,))
