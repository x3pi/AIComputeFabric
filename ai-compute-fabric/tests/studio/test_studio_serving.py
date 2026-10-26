"""Choosing the served model and attaching an adapter from the Studio, against a fake worker-guard and a fake GGUF converter.
Needs AICF_PG_ADMIN_DSN / AICF_PG_APP_DSN. The real switch (worker-guard + llama-server) is checked by hand in the demo."""
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest

from studio.app import build_app
from studio.config import Quotas, Settings
from studio.db import setup_schema
from studio.serving import same_base
from studio.web import serve
from tests.studio.test_studio_service import ADMIN, APP, CFG, HERE, call, needs_pg, new_job, wait_status


def test_same_base_matches_builds_of_the_trained_model_only():
    assert same_base("Qwen3-1.7B-Q4_K_M.gguf", "Qwen3-1.7B")
    assert same_base("qwen3-1.7b.gguf", "Qwen3-1.7B")
    assert not same_base("Qwen3-4B-Q4_K_M.gguf", "Qwen3-1.7B")
    assert not same_base("Qwen3-1.7Bx.gguf", "Qwen3-1.7B")
    assert not same_base("Qwen3-0.6B-Q8_0.gguf", "Qwen3-1.7B")


class FakeGuard(BaseHTTPRequestHandler):
    posts: list = []
    refuse = False

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"switchable": True, "model": "x.gguf", "lora": "", "state": "running", "models": ["tiny-Q4.gguf", "other-Q4.gguf"], "adapters": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGuard.posts.append(body)
        if FakeGuard.refuse:
            return self._send(400, {"error": "model: \"zzz.gguf\" not found"})
        self._send(202, {"accepted": True, **body})

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def sv(tmp_path_factory):
    if not (ADMIN and APP):
        pytest.skip("no database")
    setup_schema(ADMIN)
    guard = ThreadingHTTPServer(("127.0.0.1", 0), FakeGuard)
    threading.Thread(target=guard.serve_forever, daemon=True).start()
    base = tmp_path_factory.mktemp("base")
    (base / "config.json").write_text(json.dumps(CFG))
    llama = tmp_path_factory.mktemp("llama")
    (llama / "convert_lora_to_gguf.py").write_text(
        "import sys\nfrom pathlib import Path\nout = sys.argv[sys.argv.index('--outfile') + 1]\nPath(out).write_bytes(b'GGUF-fake')\n")
    adapters = tmp_path_factory.mktemp("adapters")
    s = Settings(data_dir=tmp_path_factory.mktemp("data"), app_dsn=APP, base_models={"tiny": str(base)}, lora_runner=(sys.executable, str(HERE / "_fake_worker.py")),
                 train_python=sys.executable, llama_cpp_dir=str(llama), worker_url=f"http://127.0.0.1:{guard.server_address[1]}", adapters_dir=str(adapters),
                 quotas=Quotas(max_active_jobs=2))
    app = build_app(s, free_vram=lambda: 100000.0)
    srv = serve(app.router, "127.0.0.1", 0, s.max_body)
    yield type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}", "settings": s, "adapters": adapters})
    srv.shutdown()
    app.close()
    guard.shutdown()


@pytest.fixture()
def tenant():
    t = "sv-" + uuid.uuid4().hex[:8]
    FakeGuard.posts, FakeGuard.refuse = [], False
    yield t
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM studio_jobs WHERE tenant_id=%s", (t,))


@needs_pg
def test_listing_shows_what_the_worker_serves_and_offers(sv):
    st, r = call(sv, "GET", "/v1/serving", role="viewer")
    assert st == 200 and r["configured"] and r["model"] == "x.gguf" and "tiny-Q4.gguf" in r["models"] and r["base_models"] == ["tiny"]


@needs_pg
def test_only_an_admin_may_switch_and_bad_input_never_reaches_the_worker(sv, tenant):
    assert call(sv, "POST", "/v1/serving", {"model": "tiny-Q4.gguf"}, role="operator")[0] == 403
    assert call(sv, "POST", "/v1/serving", {}, role="admin")[0] == 400
    assert call(sv, "POST", "/v1/serving", {"model": 5}, role="admin")[0] == 400
    assert FakeGuard.posts == []
    st, r = call(sv, "POST", "/v1/serving", {"model": "tiny-Q4.gguf"}, role="admin")
    assert st == 200 and FakeGuard.posts == [{"model": "tiny-Q4.gguf", "lora": ""}]
    FakeGuard.refuse = True
    st, r = call(sv, "POST", "/v1/serving", {"model": "zzz.gguf"}, role="admin")
    assert st == 400 and "not found" in r["error"]["message"]


@needs_pg
def test_trained_adapter_is_exported_copied_and_attached_to_its_own_base_only(sv, tenant):
    st, j = new_job(sv, tenant)
    wait_status(sv, tenant, j["id"], ("succeeded",))
    st, r = call(sv, "POST", "/v1/serving", {"model": "other-Q4.gguf", "tenant": tenant, "job": j["id"]}, role="admin")
    assert st == 409 and r["error"]["code"] == "adapter_base_mismatch" and FakeGuard.posts == []
    st, r = call(sv, "POST", "/v1/serving", {"model": "tiny-Q4.gguf", "tenant": tenant, "job": j["id"]}, role="admin")
    name = f"{tenant}-{j['id'][:8]}.gguf"
    assert st == 200, r
    assert FakeGuard.posts == [{"model": "tiny-Q4.gguf", "lora": name}]
    assert (sv.adapters / name).read_bytes() == b"GGUF-fake"
    assert not [p for p in sv.adapters.iterdir() if p.name.endswith(".tmp")]


@needs_pg
def test_another_tenants_job_cannot_be_attached(sv, tenant):
    st, j = new_job(sv, tenant)
    wait_status(sv, tenant, j["id"], ("succeeded",))
    st, r = call(sv, "POST", "/v1/serving", {"model": "tiny-Q4.gguf", "tenant": "sv-someone-else", "job": j["id"]}, role="admin")
    assert st == 404 and FakeGuard.posts == []


@needs_pg
def test_unreachable_worker_is_a_clean_503(sv):
    s = sv.settings
    import dataclasses
    app = build_app(dataclasses.replace(s, worker_url="http://127.0.0.1:9"), start_runner=False)
    srv = serve(app.router, "127.0.0.1", 0, s.max_body)
    try:
        u = type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        st, r = call(u, "GET", "/v1/serving", role="viewer")
        assert st == 503 and "cannot be reached" in r["error"]["message"]
    finally:
        srv.shutdown()
        app.close()
