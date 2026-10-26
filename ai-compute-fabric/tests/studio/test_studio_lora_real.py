"""A real LoRA job through the Studio service: real PostgreSQL, real child process, real GPU, real Qwen3-0.6B.

Needs AICF_PG_ADMIN_DSN, AICF_PG_APP_DSN, AICF_STUDIO_TRAIN_PYTHON (venv with torch+peft) and AICF_STUDIO_BASE (HF dir);
otherwise skipped, NOT passed. Evidence: val perplexity on held-out rows falls, the adapter is safetensors with a valid manifest."""
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from studio.config import Settings
from tests.studio.test_studio_service import ADMIN, APP, call, wait_status  # noqa: F401  (fixtures reused below)

PY, BASE = os.environ.get("AICF_STUDIO_TRAIN_PYTHON"), os.environ.get("AICF_STUDIO_BASE")
pytestmark = pytest.mark.skipif(not (ADMIN and APP and PY and BASE), reason="set AICF_PG_*_DSN, AICF_STUDIO_TRAIN_PYTHON, AICF_STUDIO_BASE")
COLORS = ["đỏ", "xanh", "vàng", "tím", "cam", "trắng", "đen", "nâu"]
ROWS = [{"question": f"Mã sản phẩm AX-{i:03d} có màu gì và bảo hành bao lâu?",
         "answer": f"Sản phẩm AX-{i:03d} màu {COLORS[i % 8]}, bảo hành {12 + 6 * (i % 4)} tháng tại mọi trung tâm của công ty."} for i in range(120)]


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    from studio.app import build_app
    from studio.db import setup_schema
    from studio.web import serve
    setup_schema(ADMIN)
    s = Settings(data_dir=tmp_path_factory.mktemp("data"), app_dsn=APP, base_models={"qwen3-0.6b": BASE}, train_python=PY, llama_cpp_dir=os.environ.get("AICF_LLAMA_CPP_DIR", ""))
    app = build_app(s)
    srv = serve(app.router, "127.0.0.1", 0, s.max_body)
    yield type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}", "settings": s, "app": app})
    srv.shutdown()
    app.close()


def test_real_lora_job_lowers_validation_perplexity_and_saves_a_safe_adapter(real):
    t = "real-lora"
    body = {"base_model": "qwen3-0.6b", "format": "jsonl", "content": "\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), "steps": 80, "rank": 8, "batch": 4, "seq": 128}
    st, j = call(real, "POST", f"/v1/tenants/{t}/lora/jobs", body, role="operator")
    assert st == 202, j
    done = wait_status(real, t, j["id"], ("succeeded", "failed", "cancelled"), timeout=600)
    log = call(real, "GET", f"/v1/tenants/{t}/lora/jobs/{j['id']}/log", role="viewer")[1]["log"]
    assert done["status"] == "succeeded", (done["error"], log[-1500:])
    r = done["result"]
    print("RESULT", json.dumps(r))
    assert r["val_perplexity_after"] < 0.7 * r["val_perplexity_before"], r
    assert r["last_loss"] < r["first_loss"] and r["steps"] == 80
    phases = [p.get("phase") for p in done["progress"]]
    assert phases[0] == "loading" and phases[-1] == "done" and phases.count("training") >= 8
    st, z = call(real, "GET", f"/v1/tenants/{t}/lora/jobs/{j['id']}/adapter", role="viewer")
    zf = zipfile.ZipFile(io.BytesIO(z))
    names = zf.namelist()
    assert "adapter_model.safetensors" in names and "manifest.json" in names and not any(n.endswith((".bin", ".pt", ".pkl")) for n in names)
    man = json.loads(zf.read("manifest.json"))
    for n, digest in man["files"].items():
        assert hashlib.sha256(zf.read(n)).hexdigest() == digest, n
    raw = (real.settings.data_dir / "jobs" / j["id"] / "adapter" / "adapter_model.safetensors").read_bytes()
    n = int.from_bytes(raw[:8], "little")                      # safetensors = 8-byte length + JSON header + raw tensors (no pickle)
    header = json.loads(raw[8:8 + n])
    assert any("lora_B" in k for k in header) and n + 8 < len(raw)
    call(real, "DELETE", f"/v1/tenants/{t}/lora/jobs/{j['id']}", role="admin")


def test_killing_the_service_does_not_leave_a_trainer_on_the_gpu(tmp_path):
    """kill -9 of the Studio service while a real job trains: the worker must die with it (PDEATHSIG) and the job must be failed on restart."""
    import signal
    import subprocess
    import sys
    import time
    import urllib.request
    root = Path(__file__).resolve().parents[2]

    def start():
        return subprocess.Popen([sys.executable, "-m", "studio", "--data-dir", str(tmp_path), "--dsn", APP, "--port", "18097", "--train-python", PY,
                                 "--base-models", json.dumps({"q": BASE})], cwd=root, env={**os.environ, "PYTHONPATH": str(root)})

    def rq(m, p, b=None):
        r = urllib.request.Request("http://127.0.0.1:18097" + p, method=m, data=b and json.dumps(b).encode(), headers={"X-Aicf-Role": "admin", "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=30))

    def used():
        return int(subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]).split()[0])
    rows = "\n".join(json.dumps({"question": f"Mã AX-{i:03d} màu gì?", "answer": f"Mã AX-{i:03d} màu {i % 7}."}) for i in range(200))
    base = used()                                   # other GPU users (llama-servers) are not part of the assertion
    svc = start()
    try:
        for _ in range(40):
            try:
                rq("GET", "/v1/health")
                break
            except OSError:
                time.sleep(0.25)
        j = rq("POST", "/v1/tenants/kill9/lora/jobs", {"base_model": "q", "format": "jsonl", "content": rows, "steps": 3000})
        for _ in range(120):
            if rq("GET", f"/v1/tenants/kill9/lora/jobs/{j['id']}")["status"] == "running" and used() > base + 500:
                break
            time.sleep(0.5)
        assert used() > base + 500, "the trainer should be holding GPU memory"
        svc.send_signal(signal.SIGKILL)
        svc.wait()
        for _ in range(20):
            if used() < base + 300:
                break
            time.sleep(0.5)
        assert used() < base + 300, "worker survived the service and still holds the GPU"
        svc = start()
        for _ in range(40):
            try:
                st = rq("GET", f"/v1/tenants/kill9/lora/jobs/{j['id']}")
                break
            except OSError:
                time.sleep(0.25)
        assert st["status"] == "failed" and "interrupted" in st["error"]
    finally:
        svc.terminate()
        svc.wait()


def _used():
    import subprocess
    return int(subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]).split()[0])


def _rows(n):
    return "\n".join(json.dumps({"question": f"Mã AX-{i:05d} màu gì?", "answer": f"Mã AX-{i:05d} màu {i % 7}, bảo hành {i % 5} tháng."}) for i in range(n))


def test_cancelling_a_real_running_job_frees_the_gpu(real):
    import time
    t = "real-cancel"
    base = _used()
    st, j = call(real, "POST", f"/v1/tenants/{t}/lora/jobs", {"base_model": "qwen3-0.6b", "format": "jsonl", "content": _rows(200), "steps": 3000}, role="operator")
    assert st == 202
    for _ in range(120):
        if _used() > base + 500 and call(real, "GET", f"/v1/tenants/{t}/lora/jobs/{j['id']}")[1]["status"] == "running":
            break
        time.sleep(0.5)
    assert _used() > base + 500
    t0 = time.time()
    assert call(real, "POST", f"/v1/tenants/{t}/lora/jobs/{j['id']}/cancel", role="operator")[0] == 200
    done = wait_status(real, t, j["id"], ("cancelled", "failed", "succeeded"), timeout=60)
    assert done["status"] == "cancelled"
    for _ in range(20):
        if _used() < base + 300:
            break
        time.sleep(0.5)
    assert _used() < base + 300 and time.time() - t0 < 30, "GPU memory not released after cancel"
    assert call(real, "DELETE", f"/v1/tenants/{t}/lora/jobs/{j['id']}", role="admin")[0] == 200


def test_job_waits_while_another_process_holds_the_gpu_then_runs(real):
    import subprocess
    import sys
    import time
    t = "real-wait"
    free = 12288 - _used()
    hog = subprocess.Popen([PY, "-c", f"import torch,time;x=torch.empty(int(({free}-1500)*2**20),dtype=torch.uint8,device='cuda');print('held',flush=True);time.sleep(600)"], stdout=subprocess.PIPE, text=True)
    try:
        assert hog.stdout.readline().strip() == "held"
        st, j = call(real, "POST", f"/v1/tenants/{t}/lora/jobs", {"base_model": "qwen3-0.6b", "format": "jsonl", "content": _rows(60), "steps": 20}, role="operator")
        assert st == 202
        time.sleep(4)
        cur = call(real, "GET", f"/v1/tenants/{t}/lora/jobs/{j['id']}")[1]
        assert cur["status"] == "queued" and cur["runner"]["waiting"]["free_mib"] < cur["runner"]["waiting"]["needs_mib"], cur
        hog.kill()
        hog.wait()
        done = wait_status(real, t, j["id"], ("succeeded", "failed"), timeout=300)
        assert done["status"] == "succeeded", done["error"]
    finally:
        hog.kill()
    call(real, "DELETE", f"/v1/tenants/{t}/lora/jobs/{j['id']}", role="admin")
    assert sys


def test_large_dataset_preview_is_fast_and_the_limit_is_enforced(real):
    import time
    t0 = time.time()
    st, r = call(real, "POST", "/v1/tenants/real-big/lora/datasets/preview", {"format": "jsonl", "content": _rows(40000)}, role="viewer")
    dt = time.time() - t0
    assert st == 200 and r["valid"] == 40000 and dt < 20, (st, dt)
    print("PREVIEW 40k rows", round(dt, 2), "s")
    st, r = call(real, "POST", "/v1/tenants/real-big/lora/datasets/preview", {"format": "jsonl", "content": _rows(50001)}, role="viewer")
    assert st in (400, 413), st


ITEMS = ["Zephyr", "Quillon", "Marbrex", "Toluvan", "Drenik", "Sovanta", "Pelmora", "Kestrin", "Valdor", "Nymbus", "Orvexa", "Brantil"]
COLORS12 = ["xanh ngọc", "tím than", "vàng chanh", "cam đất", "hồng phấn", "nâu socola", "bạc", "đen nhám", "lam nhạt", "đỏ gạch", "xám tro", "xanh rêu"]


@pytest.mark.skipif(not (os.environ.get("AICF_LLAMA_CPP_DIR") and os.environ.get("AICF_LLAMA_SERVER") and os.environ.get("AICF_STUDIO_GGUF")),
                    reason="set AICF_LLAMA_CPP_DIR, AICF_LLAMA_SERVER (llama-server binary), AICF_STUDIO_GGUF (base model .gguf)")
def test_trained_adapter_exported_as_gguf_changes_llama_server_answers(real, tmp_path):
    """End to end on the real engine: facts the base model cannot know -> LoRA job -> GGUF export through the API -> llama-server --lora."""
    import subprocess
    import time
    import urllib.request
    t = "real-effect"
    rows = [{"question": tpl.format(n=n), "answer": f"Sản phẩm {n} có màu {c}."} for n, c in zip(ITEMS, COLORS12)
            for tpl in ("Sản phẩm {n} của công ty Alpha có màu gì?", "Màu của sản phẩm {n} (công ty Alpha) là gì?", "Cho biết màu sản phẩm {n} thuộc công ty Alpha.", "{n} của Alpha màu gì vậy?", "Alpha sản xuất {n} với màu nào?")]
    body = {"base_model": "qwen3-0.6b", "format": "jsonl", "content": "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), "steps": 300, "rank": 16, "batch": 8, "seq": 64, "lr": 0.003}
    j = call(real, "POST", f"/v1/tenants/{t}/lora/jobs", body, role="operator")[1]
    assert wait_status(real, t, j["id"], ("succeeded", "failed"), timeout=600)["status"] == "succeeded"
    st, g = call(real, "GET", f"/v1/tenants/{t}/lora/jobs/{j['id']}/adapter?format=gguf", role="viewer")
    assert st == 200 and g[:4] == b"GGUF", "converted adapter must be a GGUF file"
    assert call(real, "GET", f"/v1/tenants/{t}/lora/jobs/{j['id']}/adapter?format=pickle", role="viewer")[0] == 400
    (tmp_path / "a.gguf").write_bytes(g)
    env = {**os.environ, "LD_LIBRARY_PATH": str(Path(os.environ["AICF_LLAMA_SERVER"]).parent)}
    procs = [subprocess.Popen([os.environ["AICF_LLAMA_SERVER"], "-m", os.environ["AICF_STUDIO_GGUF"], "-ngl", "99", "--port", str(port), "-c", "2048", *extra], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for port, extra in ((18211, []), (18212, ["--lora", str(tmp_path / "a.gguf")]))]
    try:
        def ask(port, q):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"messages": [{"role": "user", "content": q}], "max_tokens": 40, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}).encode())
            for _ in range(60):
                try:
                    return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]
                except OSError:
                    time.sleep(1)
            raise AssertionError("llama-server did not start")
        new_wording = "Bạn biết {n} do Alpha làm có màu nào không?"                # never in the training data
        base_hits = sum(c in ask(18211, new_wording.format(n=n)) for n, c in zip(ITEMS, COLORS12))
        lora_hits = sum(c in ask(18212, new_wording.format(n=n)) for n, c in zip(ITEMS, COLORS12))
        print("EFFECT base", base_hits, "/12  base+LoRA", lora_hits, "/12")
        assert base_hits <= 1 and lora_hits >= 10, (base_hits, lora_hits)
    finally:
        for p in procs:
            p.kill()
    call(real, "DELETE", f"/v1/tenants/{t}/lora/jobs/{j['id']}", role="admin")
