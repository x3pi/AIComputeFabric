"""AI Studio service against real PostgreSQL (row-level security) with a fake language model and a fake GPU worker.

Needs AICF_PG_ADMIN_DSN and AICF_PG_APP_DSN (otherwise skipped, NOT passed). The real LoRA run is in test_studio_lora_real.py."""
import io
import json
import os
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
import urllib.error
import urllib.request

from studio.app import build_app
from studio.config import Quotas, Settings
from studio.db import setup_schema
from studio.methods.lora import parse_dataset, split
from studio.web import serve

ADMIN, APP = os.environ.get("AICF_PG_ADMIN_DSN"), os.environ.get("AICF_PG_APP_DSN")
needs_pg = pytest.mark.skipif(not (ADMIN and APP), reason="set AICF_PG_ADMIN_DSN and AICF_PG_APP_DSN")
HERE = Path(__file__).parent
QA = [{"question": f"Câu hỏi số {i}?", "answer": f"Trả lời số {i}."} for i in range(40)]
JSONL = "\n".join(json.dumps(r, ensure_ascii=False) for r in QA)
CFG = {"hidden_size": 1024, "intermediate_size": 3072, "num_hidden_layers": 28, "num_attention_heads": 16, "num_key_value_heads": 8, "head_dim": 128, "vocab_size": 151936, "tie_word_embeddings": True}


# ---- pure unit tests (no database) ----
def test_dataset_parsing_reports_bad_rows_and_dedupes():
    content = "\n".join([json.dumps(QA[0]), json.dumps(QA[0]), "not json", json.dumps({"question": "q"}), json.dumps({"question": "", "answer": "a"}),
                         json.dumps({"messages": [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]}),
                         json.dumps({"messages": [{"role": "user", "content": "u"}]})])
    d = parse_dataset("jsonl", content, 100, 100)
    assert len(d["rows"]) == 2 and d["duplicates"] == 1 and d["error_count"] == 4
    assert {e["line"] for e in d["errors"]} == {3, 4, 5, 7}
    assert len(parse_dataset("csv", "question,answer\nq1,a1\nq2,a2\n", 10, 100)["rows"]) == 2
    for bad_args in (("csv", "a,b\n1,2\n"), ("xml", "x")):
        with pytest.raises(Exception):
            parse_dataset(*bad_args, 10, 100)
    with pytest.raises(Exception):
        parse_dataset("jsonl", JSONL, 5, 100)           # more than max_examples


def test_split_is_deterministic_and_order_independent():
    a, b = split(QA), split(list(reversed(QA)))
    assert {r["question"] for r in a[1]} == {r["question"] for r in b[1]}
    assert len(a[0]) + len(a[1]) == len(QA) and len(a[1]) >= 2


# ---- service fixtures ----
class FakeLlm(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        has_shots = sum(m["role"] == "assistant" for m in body["messages"])
        out = json.dumps({"choices": [{"message": {"content": "Hà Nội" if has_shots else "không rõ"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    if not (ADMIN and APP):
        pytest.skip("no database")
    setup_schema(ADMIN)
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlm)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    base = tmp_path_factory.mktemp("base")
    (base / "config.json").write_text(json.dumps(CFG))
    s = Settings(data_dir=tmp_path_factory.mktemp("data"), app_dsn=APP, llm_url=f"http://127.0.0.1:{llm.server_address[1]}",
                 base_models={"tiny": str(base)}, lora_runner=(sys.executable, str(HERE / "_fake_worker.py")),
                 quotas=Quotas(max_active_jobs=2, max_prompt_versions=3))
    state = {"free": 100000.0}
    app = build_app(s, free_vram=lambda: state["free"])
    srv = serve(app.router, "127.0.0.1", 0, s.max_body)
    yield type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}", "state": state, "app": app, "settings": s})
    srv.shutdown()
    app.close()
    llm.shutdown()


def call(svc, method, path, body=None, role="admin", user="tester"):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(svc.url + path, data=data, method=method, headers={"X-Aicf-Role": role, "X-Aicf-User": user, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers["Content-Type"].startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture()
def tenant():
    t = "st-" + uuid.uuid4().hex[:8]
    yield t
    with psycopg.connect(ADMIN, autocommit=True) as c:
        for tbl in ("studio_prompts", "studio_prompt_defaults", "studio_jobs"):
            c.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (t,))


def wait_status(svc, t, job, want, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        _, j = call(svc, "GET", f"/v1/tenants/{t}/lora/jobs/{job}")
        if j["status"] in want:
            return j
        time.sleep(0.2)
    raise AssertionError(f"job stayed {j['status']}")


def new_job(svc, t, **kw):
    return call(svc, "POST", f"/v1/tenants/{t}/lora/jobs", {"base_model": "tiny", "format": "jsonl", "content": JSONL, "steps": 10, **kw}, role="operator")


# ---- tests ----
@needs_pg
def test_methods_are_listed_and_full_ft_is_honestly_unsupported(svc):
    st, m = call(svc, "GET", "/v1/methods", role="viewer")
    by = {x["id"]: x for x in m["methods"]}
    assert st == 200 and set(by) == {"rag", "prompts", "lora", "full_ft"}
    assert by["full_ft"]["status"] == "unsupported" and by["full_ft"]["reason"] and by["lora"]["status"] == "available"
    st, e = call(svc, "GET", "/v1/full-ft/estimate", role="viewer")
    assert e["label"] == "ESTIMATE" and e["supported"] is False


@needs_pg
def test_roles_and_unknown_routes(svc, tenant):
    assert call(svc, "POST", f"/v1/tenants/{tenant}/prompts", {"name": "a", "system": "s"}, role="viewer")[0] == 403
    assert call(svc, "POST", f"/v1/tenants/{tenant}/prompts", {"name": "a", "system": "s"}, role="")[0] == 403, "no role must be treated as viewer"
    assert call(svc, "DELETE", f"/v1/tenants/{tenant}/prompts/a", role="operator")[0] in (403, 404) and call(svc, "GET", "/v1/nope")[0] == 404
    assert call(svc, "GET", "/v1/tenants/..%2Fx/prompts", role="viewer")[0] in (400, 404)
    assert call(svc, "GET", "/v1/tenants/bad%20name/prompts", role="viewer")[0] == 400


@needs_pg
def test_prompt_versions_are_immutable_limited_and_isolated_between_tenants(svc, tenant):
    b = f"/v1/tenants/{tenant}"
    for i in range(3):
        st, r = call(svc, "POST", b + "/prompts", {"name": "qa", "system": f"v{i}", "examples": [{"question": "q", "answer": "a"}]}, role="operator")
        assert st == 201 and r["version"] == i + 1
    assert call(svc, "POST", b + "/prompts", {"name": "qa", "system": "v4"}, role="operator")[0] == 429, "version quota"
    assert call(svc, "GET", b + "/prompts/qa/1", role="viewer")[1]["system"] == "v0", "an old version never changes"
    assert call(svc, "PUT", b + "/prompt-default", {"name": "qa", "version": 2}, role="operator")[0] == 200
    assert call(svc, "GET", b + "/prompt-default", role="viewer")[1]["default"]["version"] == 2
    other = "st-" + uuid.uuid4().hex[:8]
    assert call(svc, "GET", f"/v1/tenants/{other}/prompts", role="viewer")[1]["templates"] == []
    assert call(svc, "GET", f"/v1/tenants/{other}/prompts/qa", role="viewer")[0] == 404, "row-level security hides other tenants"
    assert call(svc, "DELETE", b + "/prompts/qa", role="admin")[0] == 200 and call(svc, "GET", b + "/prompt-default", role="viewer")[1]["default"] is None


@needs_pg
def test_few_shot_compare_uses_the_model_with_and_without_examples(svc, tenant):
    b = f"/v1/tenants/{tenant}"
    st, r = call(svc, "POST", b + "/prompts/compare", {"system": "Trả lời ngắn.", "examples": [{"question": "Thủ đô?", "answer": "Hà Nội"}],
                                                       "questions": [{"question": "Thủ đô Việt Nam?", "expected": "Hà Nội"}]}, role="viewer")
    assert st == 200
    assert r["summary"]["few_shot"]["contains_expected"] == 1 and r["summary"]["zero_shot"]["contains_expected"] == 0
    assert call(svc, "POST", b + "/prompts/compare", {"system": "s", "questions": ["q"]}, role="viewer")[0] == 400, "no examples = nothing to compare"
    assert call(svc, "POST", b + "/prompts/test", {"system": "s", "question": "q", "max_tokens": "abc"}, role="viewer")[0] == 400


@needs_pg
def test_lora_job_lifecycle_adapter_download_and_isolation(svc, tenant):
    st, j = new_job(svc, tenant)
    assert st == 202 and j["status"] in ("queued", "running")
    done = wait_status(svc, tenant, j["id"], ("succeeded", "failed"))
    assert done["status"] == "succeeded", done
    assert done["params"]["train_examples"] + done["params"]["val_examples"] == len(QA) and done["progress"]
    st, z = call(svc, "GET", f"/v1/tenants/{tenant}/lora/jobs/{j['id']}/adapter", role="viewer")
    assert st == 200 and "adapter_model.safetensors" in zipfile.ZipFile(io.BytesIO(z)).namelist()
    other = "st-" + uuid.uuid4().hex[:8]
    assert call(svc, "GET", f"/v1/tenants/{other}/lora/jobs/{j['id']}", role="viewer")[0] == 404, "another tenant cannot see the job"
    assert call(svc, "GET", f"/v1/tenants/{other}/lora/jobs/{j['id']}/adapter", role="viewer")[0] == 404
    assert call(svc, "DELETE", f"/v1/tenants/{tenant}/lora/jobs/{j['id']}", role="operator")[0] == 403
    assert call(svc, "DELETE", f"/v1/tenants/{tenant}/lora/jobs/{j['id']}", role="admin")[0] == 200
    assert not (svc.settings.data_dir / "jobs" / j["id"]).exists()


@needs_pg
def test_lora_validation_and_memory_gate(svc, tenant):
    b = f"/v1/tenants/{tenant}/lora"
    assert new_job(svc, tenant, base_model="nope")[0] == 400
    assert new_job(svc, tenant, rank=1000)[0] == 400 and new_job(svc, tenant, steps=True)[0] == 400 and new_job(svc, tenant, lr=5)[0] == 400
    st, r = new_job(svc, tenant, batch=16, seq=1024, rank=64)
    assert st == 400 and r["error"]["code"] == "does_not_fit", r
    assert call(svc, "POST", b + "/jobs", {"base_model": "tiny", "format": "jsonl", "content": "{bad"}, role="operator")[0] == 400
    assert call(svc, "POST", b + "/jobs", {"base_model": "tiny", "format": "jsonl", "content": JSONL[:300]}, role="operator")[0] == 400, "too few examples"
    assert call(svc, "POST", b + "/jobs", {"base_model": "tiny", "format": "jsonl", "content": JSONL}, role="viewer")[0] == 403
    st, p = call(svc, "POST", b + "/datasets/preview", {"format": "jsonl", "content": JSONL + "\nnot json"}, role="viewer")
    assert st == 200 and p["invalid"] == 1 and p["valid"] == len(QA)


@needs_pg
def test_job_waits_for_free_vram_then_runs_and_can_be_cancelled(svc, tenant):
    svc.state["free"] = 10.0                                   # the owner is using the GPU
    st, j = new_job(svc, tenant)
    time.sleep(2.5)
    _, cur = call(svc, "GET", f"/v1/tenants/{tenant}/lora/jobs/{j['id']}")
    assert cur["status"] == "queued" and cur["runner"]["waiting"]["free_mib"] == 10
    assert call(svc, "POST", f"/v1/tenants/{tenant}/lora/jobs/{j['id']}/cancel", role="operator")[1]["status"] == "cancelled"
    assert call(svc, "POST", f"/v1/tenants/{tenant}/lora/jobs/{j['id']}/cancel", role="operator")[0] == 409
    svc.state["free"] = 100000.0


@needs_pg
def test_running_job_cancel_kills_process_and_crash_is_reported(svc, tenant):
    # a job that never ends: patch params through the worker's own job.json ("fake": "sleep")
    st, j = new_job(svc, tenant)
    jd = svc.settings.data_dir / "jobs" / j["id"]
    wait_status(svc, tenant, j["id"], ("succeeded",))
    # crash: second job, worker exits 3
    svc.state["free"] = 0.0                                    # hold it queued while we edit its job.json
    st, j2 = new_job(svc, tenant)
    jd2 = svc.settings.data_dir / "jobs" / j2["id"]
    cfg = json.loads((jd2 / "job.json").read_text())
    (jd2 / "job.json").write_text(json.dumps({**cfg, "fake": "crash"}))
    svc.state["free"] = 100000.0
    f = wait_status(svc, tenant, j2["id"], ("failed", "succeeded"))
    assert f["status"] == "failed" and "code 3" in f["error"] and "boom" in call(svc, "GET", f"/v1/tenants/{tenant}/lora/jobs/{j2['id']}/log", role="viewer")[1]["log"]
    svc.state["free"] = 0.0
    st, j3 = new_job(svc, tenant)
    jd3 = svc.settings.data_dir / "jobs" / j3["id"]
    (jd3 / "job.json").write_text(json.dumps({**json.loads((jd3 / "job.json").read_text()), "fake": "sleep"}))
    svc.state["free"] = 100000.0
    wait_status(svc, tenant, j3["id"], ("running",))
    t0 = time.time()
    assert call(svc, "POST", f"/v1/tenants/{tenant}/lora/jobs/{j3['id']}/cancel", role="operator")[0] == 200
    c = wait_status(svc, tenant, j3["id"], ("cancelled", "failed"), 20)
    assert c["status"] == "cancelled" and time.time() - t0 < 15 and svc.app.runner.status()["running_job"] is None
    assert jd.exists()


@needs_pg
def test_per_tenant_job_quota_and_orphan_recovery(svc, tenant):
    svc.state["free"] = 0.0
    assert new_job(svc, tenant)[0] == 202 and new_job(svc, tenant)[0] == 202
    jobs_dir = svc.settings.data_dir / "jobs"
    before = len(list(jobs_dir.iterdir()))
    st, r = new_job(svc, tenant)
    assert st == 429 and r["error"]["code"] == "quota_exceeded"
    assert len(list(jobs_dir.iterdir())) == before, "a refused job leaves no directory behind"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("UPDATE studio_jobs SET status='running' WHERE tenant_id=%s AND status='queued'", (tenant,))
    assert svc.app.runner.store.recover_orphans() >= 2
    assert {j["status"] for j in svc.app.runner.store.list(tenant)} == {"failed"}
    svc.state["free"] = 100000.0


@needs_pg
def test_job_ids_cannot_escape_the_job_directory(svc, tenant):
    for bad_id in ("..", "..%2F..%2Fetc", "a" * 32, "A" * 32):
        assert call(svc, "GET", f"/v1/tenants/{tenant}/lora/jobs/{bad_id}/log", role="viewer")[0] in (404, 400)
