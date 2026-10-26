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
