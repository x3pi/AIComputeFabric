"""Contract test for InferenceEngine adapters (plan P3.1).

The same assertions run against every adapter. The llama adapter test needs a real
llama-server and a small GGUF: set AICF_LLAMA_SERVER and AICF_TEST_MODEL, otherwise it is
skipped (never silently passed). A fake adapter proves the contract is not llama-specific."""
import os
import shutil
import subprocess

import pytest

from inference.adapters.llama import LlamaServerAdapter
from inference.engine import Chunk, EngineError, InferenceEngine, Metrics, ModelSpec, Placement, Plan, ProbeResult

SERVER, MODEL = os.environ.get("AICF_LLAMA_SERVER"), os.environ.get("AICF_TEST_MODEL")


def vram_used_mib():
    if not shutil.which("nvidia-smi"):
        return None
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True)
    return int(out.stdout.split()[0])


class FakeEngine:
    def __init__(self):
        self.up = False
        self.n = 0

    def probe(self): return ProbeResult(True, version="fake")
    def plan(self, model, placement): return Plan(args=("--fake",), placement=placement)
    def prepare(self, model):
        if not model.path: raise EngineError("no model")
    def start(self, plan, model, timeout_s=1.0): self.up = True
    def infer(self, messages, max_tokens=64, temperature=0.0):
        if not self.up: raise EngineError("engine is not running")
        self.n += 1
        yield Chunk(text="ok", finish_reason="stop")
    def metrics(self): return Metrics(running=self.up, requests=self.n)
    def stop(self, grace_s=5.0): self.up = False


def test_protocol_is_checkable_and_fake_satisfies_it():
    assert isinstance(FakeEngine(), InferenceEngine)
    assert isinstance(LlamaServerAdapter("/nonexistent"), InferenceEngine)


def test_plan_translates_estimator_decision_into_flags():
    a = LlamaServerAdapter("/nonexistent")
    plan = a.plan(ModelSpec("m.gguf"), Placement(gpu_layers=33, ctx_per_seq=4096, parallel=2, flash_attn="on", kv_type="q8_0", port=19000))
    args = list(plan.args)
    for flag, val in [("-ngl", "33"), ("-c", "8192"), ("-np", "2"), ("-fa", "on"), ("-ctk", "q8_0"), ("-ctv", "q8_0"), ("--port", "19000")]:
        assert args[args.index(flag) + 1] == val, flag
    assert a.plan(ModelSpec("m.gguf"), Placement(0, 512)).notes, "CPU-only plan must say so"


@pytest.mark.parametrize("bad", [dict(flash_attn="maybe"), dict(kv_type="q4_0"), dict(flash_attn="off", kv_type="q8_0"),
                                 dict(gpu_layers=-1), dict(ctx_per_seq=0), dict(parallel=0)])
def test_plan_refuses_impossible_combinations(bad):
    base = dict(gpu_layers=10, ctx_per_seq=1024, parallel=1)
    base.update(bad)
    with pytest.raises(EngineError):
        LlamaServerAdapter("/nonexistent").plan(ModelSpec("m.gguf"), Placement(**base))


def test_probe_reports_missing_binary_without_raising():
    r = LlamaServerAdapter("/nonexistent/llama-server").probe()
    assert not r.available and "not found" in r.reason


def test_prepare_rejects_missing_non_gguf_and_wrong_hash(tmp_path):
    a = LlamaServerAdapter("/nonexistent")
    with pytest.raises(EngineError, match="not found"):
        a.prepare(ModelSpec(str(tmp_path / "none.gguf")))
    bad = tmp_path / "x.gguf"; bad.write_bytes(b"NOPE" + b"0" * 100)
    with pytest.raises(EngineError, match="GGUF"):
        a.prepare(ModelSpec(str(bad)))
    good = tmp_path / "y.gguf"; good.write_bytes(b"GGUF" + b"0" * 100)
    a.prepare(ModelSpec(str(good)))
    with pytest.raises(EngineError, match="SHA-256"):
        a.prepare(ModelSpec(str(good), sha256="0" * 64))


def test_infer_before_start_is_an_engine_error():
    with pytest.raises(EngineError):
        list(LlamaServerAdapter("/nonexistent").infer([{"role": "user", "content": "hi"}]))
    with pytest.raises(EngineError):
        list(FakeEngine().infer([]))


@pytest.mark.skipif(not (SERVER and MODEL), reason="set AICF_LLAMA_SERVER and AICF_TEST_MODEL to run against a real llama-server")
def test_real_lifecycle_streams_tokens_and_releases_vram(tmp_path):
    a = LlamaServerAdapter(SERVER, log_path=str(tmp_path / "engine.log"))
    pr = a.probe()
    assert pr.available, pr.reason
    spec = ModelSpec(MODEL)
    a.prepare(spec)
    before = vram_used_mib()
    plan = a.plan(spec, Placement(gpu_layers=99, ctx_per_seq=1024, parallel=2, port=18971))
    a.start(plan, spec)
    try:
        m = a.metrics()
        assert m.running and m.pid > 0
        with pytest.raises(EngineError, match="already running"):
            a.start(plan, spec)
        chunks = list(a.infer([{"role": "user", "content": "Say hi."}], max_tokens=16))
        assert any(c.text or c.reasoning for c in chunks) and chunks[-1].finish_reason in ("stop", "length")
        m = a.metrics()
        assert m.requests == 1 and m.completion_tokens > 0 and m.last_ttft_ms and m.last_ttft_ms > 0
        during = vram_used_mib()
        if before is not None:
            assert during > before + 200, "engine should hold VRAM while running"
    finally:
        a.stop()
    assert not a.metrics().running
    with pytest.raises(EngineError):
        list(a.infer([{"role": "user", "content": "hi"}]))
    a.stop()  # idempotent
    if before is not None:
        import time; time.sleep(1.0)
        assert vram_used_mib() <= before + 50, "VRAM leaked after stop() (gate: < 50 MB)"
