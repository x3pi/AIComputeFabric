"""Phase 1 bench: cold vs warm call through WarmDockerPool (CPU and GPU shape) vs the pre-existing one-shot DockerSandbox.
Writes benchmarks/reports/p_serverless_1_core.json. Run: .venv/bin/python benchmarks/serverless/p1_bench.py"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import cloudpickle  # noqa: E402

from agents.env_cache import EnvCache, EnvSpec  # noqa: E402
from agents.sandbox import DockerSandbox, GpuDockerSandbox, SandboxLimits, ServerlessLimits, WarmDockerPool, WarmKey  # noqa: E402
from tests.serverless.util import bundle_of, frame, value  # noqa: E402


def square(x):
    return x * x


cloudpickle.register_pickle_by_value(sys.modules[__name__])


def st(xs):
    s = sorted(xs)
    return {"n": len(s), "p50": round(s[len(s) // 2], 3), "p95": round(s[int(len(s) * 0.95) - 1], 3), "min": round(s[0], 3), "max": round(s[-1], 3)}


def main():
    cache = EnvCache(ROOT / ".aicf-work/sl-test/store")
    spec = EnvSpec()
    t = time.perf_counter()
    path, built = cache.ensure(spec)
    out = {"env_ensure_ms": round((time.perf_counter() - t) * 1000, 1), "env_built_now": built}
    h = spec.env_hash()
    sha, b = bundle_of(square)
    for shape, lim in (("cpu", ServerlessLimits(timeout_s=30)), ("gpu", ServerlessLimits(timeout_s=30, gpu_index=0, vram_mb=1024))):
        pool = WarmDockerPool(GpuDockerSandbox(), worker_id="aicf-sl-test")
        cold = []
        for i in range(15):
            t = time.perf_counter()
            r, c = pool.call(WarmKey(f"bench{i}", h, "f"), path, lim, frame(sha, 3), b)
            assert c and value(r) == 9
            cold.append((time.perf_counter() - t) * 1000)
            pool.kill_all()
        warm_total, warm_exec = [], []
        key = WarmKey("bench", h, "f")
        pool.call(key, path, lim, frame(sha, 3), b)
        for _ in range(300):
            t = time.perf_counter()
            r, c = pool.call(key, path, lim, frame(sha, 3), b)
            assert not c
            warm_total.append((time.perf_counter() - t) * 1000)
            warm_exec.append(r["exec_ms"])
        pool.close()
        out[shape] = {"cold_call_total_ms": st(cold), "warm_call_total_ms": st(warm_total), "warm_call_user_exec_ms": st(warm_exec),
                      "note": "total = host-side call incl. cloudpickle-free frame I/O; cold = new container + runner start + bundle load + call"}
    script = ROOT / ".aicf-work/sl-test/oneshot.py"
    script.write_text("import json,sys\nprint(json.dumps({'ok': True}))\n")
    script.chmod(0o644)
    sb = DockerSandbox("python:3.12-slim")
    one = []
    for _ in range(15):
        t = time.perf_counter()
        sb.run(script, {}, SandboxLimits())
        one.append((time.perf_counter() - t) * 1000)
    out["oneshot_dockersandbox_ms"] = st(one)
    (ROOT / "benchmarks/reports/p_serverless_1_core.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


main()
