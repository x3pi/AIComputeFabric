"""A real LoRA job through the Studio service: real PostgreSQL, real child process, real GPU, real Qwen3-0.6B.

Needs AICF_PG_ADMIN_DSN, AICF_PG_APP_DSN, AICF_STUDIO_TRAIN_PYTHON (venv with torch+peft) and AICF_STUDIO_BASE (HF dir);
otherwise skipped, NOT passed. Evidence: val perplexity on held-out rows falls, the adapter is safetensors with a valid manifest."""
import hashlib
import io
import json
import os
import zipfile

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
    s = Settings(data_dir=tmp_path_factory.mktemp("data"), app_dsn=APP, base_models={"qwen3-0.6b": BASE}, train_python=PY)
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
