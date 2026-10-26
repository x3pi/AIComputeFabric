"""P3.1: the vLLM adapter against the same InferenceEngine contract as llama.

The pure checks (plan, prepare) always run. The real lifecycle needs a Python with vLLM and a verified snapshot directory:
set AICF_VLLM_PYTHON and AICF_VLLM_MODEL, otherwise it is skipped (never silently passed)."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core.model_registry.snapshot import manifest_digest, snapshot_dir, take_snapshot
from inference.adapters.vllm import VllmAdapter
from inference.engine import EngineError, InferenceEngine, ModelSpec, Placement

PY, MODEL = os.environ.get("AICF_VLLM_PYTHON"), os.environ.get("AICF_VLLM_MODEL")


def vram_used_mib():
    if not shutil.which("nvidia-smi"):
        return None
    return int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.split()[0])


def test_is_an_inference_engine():
    assert isinstance(VllmAdapter("/nonexistent/python"), InferenceEngine)


def test_plan_translates_the_placement_into_vllm_flags():
    plan = VllmAdapter("py", gpu_memory_utilization=0.4).plan(ModelSpec("/m"), Placement(gpu_layers=99, ctx_per_seq=2048, parallel=3, kv_type="q8_0", port=19100))
    a = list(plan.args)
    for flag, val in [("--model", "/m"), ("--port", "19100"), ("--max-model-len", "2048"), ("--max-num-seqs", "3"), ("--gpu-memory-utilization", "0.4"), ("--kv-cache-dtype", "fp8")]:
        assert a[a.index(flag) + 1] == val, flag
    assert any("fp8" in n for n in plan.notes) and any("all layers" in n for n in plan.notes)


@pytest.mark.parametrize("bad", [dict(gpu_layers=0), dict(kv_type="q4_0"), dict(ctx_per_seq=0), dict(parallel=0)])
def test_plan_refuses_what_vllm_cannot_do(bad):
    base = dict(gpu_layers=99, ctx_per_seq=1024, parallel=1)
    base.update(bad)
    with pytest.raises(EngineError):
        VllmAdapter("py").plan(ModelSpec("/m"), Placement(**base))


@pytest.mark.parametrize("u", [0.0, 0.99, -1])
def test_gpu_memory_fraction_is_bounded(u):
    with pytest.raises(EngineError):
        VllmAdapter("py", gpu_memory_utilization=u)


def test_probe_reports_a_missing_python_without_raising():
    r = VllmAdapter("/nonexistent/python").probe()
    assert not r.available and "not found" in r.reason


def make_model(tmp_path, cfg=None, extra=None):
    def dl(repo, rev, dest):
        (dest / "config.json").write_text(json.dumps(cfg or {"architectures": ["Qwen3ForCausalLM"]}))
        (dest / "model.safetensors").write_bytes(b"\0" * 64)
        for name in extra or ():
            (dest / name).write_bytes(b"x")
    m = take_snapshot(tmp_path, "acme/m", "c" * 40, dl)
    return snapshot_dir(tmp_path, "acme/m", "c" * 40) / "files", m


def test_prepare_accepts_a_verified_snapshot_and_checks_the_digest(tmp_path):
    d, m = make_model(tmp_path)
    a = VllmAdapter("py")
    a.prepare(ModelSpec(str(d)))
    a.prepare(ModelSpec(str(d), sha256=manifest_digest(m)))
    with pytest.raises(EngineError, match="SHA-256"):
        a.prepare(ModelSpec(str(d), sha256="0" * 64))
    (d / "model.safetensors").write_bytes(b"\1" * 64)               # tampered after the snapshot was taken
    with pytest.raises(EngineError, match="not a verified snapshot"):
        a.prepare(ModelSpec(str(d), sha256=manifest_digest(m)))


def test_prepare_refuses_missing_pickled_and_remote_code_models(tmp_path):
    a = VllmAdapter("py")
    with pytest.raises(EngineError, match="not found"):
        a.prepare(ModelSpec(str(tmp_path / "none")))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EngineError, match="safetensors"):
        a.prepare(ModelSpec(str(empty)))
    pk = tmp_path / "pk"
    pk.mkdir()
    (pk / "model.safetensors").write_bytes(b"0")
    (pk / "pytorch_model.bin").write_bytes(b"0")
    with pytest.raises(EngineError, match="pickle"):
        a.prepare(ModelSpec(str(pk)))
    rc = tmp_path / "rc"
    rc.mkdir()
    (rc / "model.safetensors").write_bytes(b"0")
    (rc / "config.json").write_text(json.dumps({"auto_map": {"AutoModel": "x.Y"}}))
    with pytest.raises(EngineError, match="remote code"):
        a.prepare(ModelSpec(str(rc)))


def test_infer_before_start_is_an_engine_error():
    with pytest.raises(EngineError):
        list(VllmAdapter("py").infer([{"role": "user", "content": "hi"}]))


@pytest.mark.skipif(not (PY and MODEL), reason="set AICF_VLLM_PYTHON and AICF_VLLM_MODEL to run against a real vLLM")
def test_real_lifecycle_streams_tokens_and_releases_vram(tmp_path):
    a = VllmAdapter(PY, gpu_memory_utilization=0.5, log_path=str(tmp_path / "vllm.log"))
    pr = a.probe()
    assert pr.available, pr.reason
    spec = ModelSpec(MODEL)
    a.prepare(spec)
    before = vram_used_mib()
    plan = a.plan(spec, Placement(gpu_layers=99, ctx_per_seq=2048, parallel=2, port=18981))
    a.start(plan, spec, timeout_s=600)
    try:
        m = a.metrics()
        assert m.running and m.pid > 0
        with pytest.raises(EngineError, match="already running"):
            a.start(plan, spec)
        chunks = list(a.infer([{"role": "user", "content": "Say hi."}], max_tokens=16))
        assert any(c.text or c.reasoning for c in chunks) and chunks[-1].finish_reason in ("stop", "length")
        m = a.metrics()
        assert m.requests == 1 and m.completion_tokens > 0 and m.last_ttft_ms and m.last_ttft_ms > 0
        if before is not None:
            assert vram_used_mib() > before + 200, "engine should hold VRAM while running"
    finally:
        a.stop(grace_s=20)
    assert not a.metrics().running
    with pytest.raises(EngineError):
        list(a.infer([{"role": "user", "content": "hi"}]))
    a.stop()
    if before is not None:
        time.sleep(3.0)
        assert vram_used_mib() <= before + 50, "VRAM leaked after stop() (gate: < 50 MB)"


def test_committed_real_run_is_recorded_with_its_limits():
    r = json.loads((Path(__file__).resolve().parents[2] / "benchmarks/reports/p3_vllm_adapter_contract.json").read_text())
    assert r["result"]["failed"] == 0 and r["result"]["passed"] >= 14
    assert "Windows" in " ".join(r["not_covered"]) and any("nvcc" in f for f in r["findings"])
