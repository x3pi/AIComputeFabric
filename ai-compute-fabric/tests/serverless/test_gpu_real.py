"""Phase 3 gate on the REAL GPU (RTX 3060 on pc-001): VRAM admission, per-container enforcement, eviction before the LLM engine, G-RECLAIM.

Isolation: containers are labelled aicf.sl=1 / aicf.worker=aicf-sl-test / aicf.gpu=0 and only those are ever touched; the "LLM engine" of the
guard is a stub (no llama-server is started or stopped); nothing of the running stack is used. VRAM is held with the CUDA driver API through
ctypes (no torch: the env only contains cloudpickle), so the numbers are real driver allocations seen by nvidia-smi."""
import json
import os
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agents.env_cache import EnvCache, EnvSpec
from agents.sandbox import GpuDockerSandbox, NoCapacity, SandboxError, ServerlessLimits, WarmDockerPool, WarmKey
from agents.sl_gpu import ControlServer, GpuAdmission, GpuMonitor, GpuProbe
from tests.serverless.util import bundle_of, by_value, frame, value

by_value(__name__)
ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / ".aicf-work/sl-test/store"
WORKER = "aicf-sl-test"
SECRET = "real-gpu-control-secret-0123456789"
GUARD_BIN = ROOT / ".aicf-work/guard_real.test"
HEADROOM = 1536


def _gpu_ok():
    if shutil.which("docker") is None or shutil.which("nvidia-smi") is None:
        return False
    r = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True)
    return "nvidia" in r.stdout


pytestmark = pytest.mark.skipif(not _gpu_ok(), reason="needs docker with the nvidia runtime and a GPU")


# ---- functions that run INSIDE the sandbox --------------------------------------------------------------------------------
def hold_vram(mib, seconds):
    """Allocate `mib` MiB of device memory through the CUDA driver API and keep it for `seconds`.
    (No `assert` in here: pytest's assertion rewriting would make the pickle reference _pytest, which the sandbox does not have.)"""
    import ctypes
    import time as _t
    cu = ctypes.CDLL("libcuda.so.1")
    dev, ctx, ptr = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_ulonglong()
    for name, rc in (("cuInit", cu.cuInit(0)), ("cuDeviceGet", cu.cuDeviceGet(ctypes.byref(dev), 0)),
                     ("cuCtxCreate", cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev))):
        if rc != 0:
            raise RuntimeError(f"{name} failed rc={rc}")
    rc = cu.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(mib * 1024 * 1024))
    if rc != 0:
        raise MemoryError(f"cuMemAlloc failed rc={rc}")
    _t.sleep(seconds)
    return mib


def quick():
    return "done"


@pytest.fixture(scope="module")
def env():
    spec = EnvSpec()
    path, _ = EnvCache(STORE).ensure(spec)
    return spec.env_hash(), path


@pytest.fixture
def rig():
    probe = GpuProbe()
    pool = WarmDockerPool(GpuDockerSandbox(), max_containers=8, max_per_key=4, worker_id=WORKER)
    pool.admission = GpuAdmission(probe, HEADROOM)
    mon = GpuMonitor(pool, probe, pool.admission, 0, interval_s=0.2)
    mon.start()
    ctl = ControlServer(pool, mon, SECRET, port=0).start()
    yield SimpleNamespace(pool=pool, mon=mon, ctl=ctl, probe=probe)
    mon.stop()
    ctl.close()
    pool.close()
    deadline = time.time() + 15
    left = pool.docker_names()
    while left and time.time() < deadline:
        time.sleep(0.1)
        left = pool.docker_names()
    if left:
        subprocess.run(["docker", "kill", *left], capture_output=True)
    assert left == [], f"orphan containers: {left}"


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def limits(vram, **kw):
    return ServerlessLimits(**{"timeout_s": 60.0, "start_timeout_s": 60.0, "gpu_index": 0, "vram_mb": vram, **kw})


def run_async(rig, env, fn, *args, vram, tenant="t1", fn_id="hold", **kw):
    h, path = env
    sha, b = bundle_of(fn)
    out = {}

    def go():
        try:
            out["r"] = rig.pool.call(WarmKey(tenant, h, fn_id), path, limits(vram, **kw), frame(sha, *args), b)
        except Exception as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def wait_for(cond, timeout=30.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def free_mib():
    return GpuProbe().mem(0)[0]


def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


# ---- admission ------------------------------------------------------------------------------------------------------------
def test_request_larger_than_what_is_free_is_refused_and_the_holder_is_untouched(rig, env):
    holder_t, holder = run_async(rig, env, hold_vram, 5000, 25, vram=5600, fn_id="holder")
    assert wait_for(lambda: sum(rig.mon.last.values()) >= 5000), f"holder never allocated: last={rig.mon.last} errors={rig.mon.errors} killed={rig.mon.killed} out={holder} smi={GpuProbe().procs(0)}"
    free = free_mib()
    ask = free - HEADROOM + 512                                  # 512 MiB more than can be given without taking the headroom
    h, path = env
    sha, b = bundle_of(quick)
    with pytest.raises(NoCapacity, match="headroom"):
        rig.pool.call(WarmKey("t2", h, "big"), path, limits(ask), frame(sha), b)
    assert len(rig.pool.snapshot()) == 1, "a refused request must not leave a container behind"
    ok, _ = rig.pool.call(WarmKey("t2", h, "small"), path, limits(free - HEADROOM - 512), frame(sha), b)
    assert value(ok) == "done"
    assert free_mib() >= HEADROOM - 200, "the owner's headroom was taken"
    rig.pool.kill_all(reason="test cleanup")
    holder_t.join(30)


def test_fixed_8192_request_with_about_4096_free_gets_no_capacity(rig, env):
    target_free = 4096 + 300                                       # leave ~4.1 GiB free (plus CUDA context overhead of the holder)
    hold = free_mib() - target_free
    holder_t, holder = run_async(rig, env, hold_vram, hold, 20, vram=hold + 300, fn_id="holder")
    assert wait_for(lambda: sum(rig.mon.last.values()) >= hold)
    h, path = env
    sha, b = bundle_of(quick)
    with pytest.raises(NoCapacity):
        rig.pool.call(WarmKey("t2", h, "eight"), path, limits(8192), frame(sha), b)
    rig.pool.kill_all(reason="test cleanup")
    holder_t.join(30)


# ---- enforcement (T7) -----------------------------------------------------------------------------------------------------
def test_container_over_its_declared_vram_is_killed_and_the_call_says_why(rig, env):
    t, out = run_async(rig, env, hold_vram, 1500, 30, vram=512, fn_id="liar")
    t.join(40)
    assert "e" in out, f"the over-allocating function must be stopped: {out}"
    assert isinstance(out["e"], SandboxError) and "vram_limit" in str(out["e"]), str(out["e"])
    assert rig.mon.killed and rig.mon.killed[0][2] == 512
    assert wait_for(lambda: not rig.pool.snapshot()), "the killed container must be removed from the pool"


def test_honest_function_is_not_killed(rig, env):
    t, out = run_async(rig, env, hold_vram, 400, 2, vram=1024, fn_id="honest")
    t.join(30)
    assert "r" in out and value(out["r"][0]) == 400 and not rig.mon.killed


# ---- G-RECLAIM ------------------------------------------------------------------------------------------------------------
def _guard_once(url, extra_mib=1024):
    r = subprocess.run([str(GUARD_BIN), "-test.run", "TestRealGPUReclaimEvictsGuestsNotEngine", "-test.v", "-test.count=1"],
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "AICF_SL_REAL_URL": url, "AICF_SL_REAL_SECRET": SECRET, "AICF_SL_REAL_EXTRA_MIB": str(extra_mib)})
    for line in r.stdout.splitlines():
        if "REAL_RESULT" in line:
            return json.loads(line.split("REAL_RESULT", 1)[1])
    raise AssertionError("guard test produced no result:\n" + r.stdout[-800:] + r.stderr[-400:])


@pytest.mark.skipif(not GUARD_BIN.exists(), reason="build it: cd apps/worker-guard && go test -c -o ../../.aicf-work/guard_real.test .")
def test_g_reclaim_guard_evicts_guests_and_keeps_the_engine_30_times(rig, env):
    N, GUESTS, EACH = 30, 2, 2048
    detect, ticks = [], []
    for i in range(N):
        threads = [run_async(rig, env, hold_vram, EACH, 60, vram=EACH + 300, tenant=f"g{k}", fn_id=f"guest{k}") for k in range(GUESTS)]
        assert wait_for(lambda: sum(rig.mon.last.values()) >= GUESTS * EACH, 40), f"iteration {i}: guests did not allocate"
        res = _guard_once(f"http://127.0.0.1:{rig.ctl.port}")
        assert res["engine_stops"] == 0 and res["state"] == "running", res
        assert "event_evict_serverless" in res and "event_reclaim" not in res, res
        detect.append(res["detect_to_freed_ms"])
        ticks.append(res["tick_ms"])
        for t, out in threads:
            t.join(30)
            assert "e" in out and "evicted" in str(out["e"]), f"in-flight guests must learn they were evicted: {out}"
        assert wait_for(lambda: not rig.pool.snapshot(), 10)
    rep = {"label": "[ĐO]", "iterations": N, "guests_per_iteration": GUESTS, "vram_each_mib": EACH,
           "detect_to_freed_ms": {"p50": round(pct(detect, 50), 1), "p95": round(pct(detect, 95), 1), "p99": round(pct(detect, 99), 1), "max": round(max(detect), 1)},
           "guard_tick_ms": {"p50": round(statistics.median(ticks), 1), "p99": round(pct(ticks, 99), 1)},
           "engine_stopped": 0, "path": "worker-guard Tick -> HTTP /v1/evict -> docker kill (all guests) -> nvidia-smi until free >= headroom",
           "note": "engine is a stub; sampler, guests, docker and driver are real. detect_to_freed_ms starts when the guard decides to evict."}
    (ROOT / "benchmarks/reports/p_serverless_3_reclaim.json").write_text(json.dumps(rep, indent=2) + "\n")
    print(json.dumps(rep, indent=2))
    assert rep["detect_to_freed_ms"]["p99"] < 2000, rep


@pytest.mark.skipif(not GUARD_BIN.exists(), reason="guard test binary not built")
def test_hung_evict_endpoint_falls_back_to_docker_kill_within_two_seconds(rig, env):
    hang_srv = _HangServer()
    try:
        times = []
        for i in range(5):
            t, out = run_async(rig, env, hold_vram, 2048, 60, vram=2400, fn_id=f"hung{i}")
            assert wait_for(lambda: sum(rig.mon.last.values()) >= 2048, 40)
            res = _guard_once(hang_srv.url)
            assert res["engine_stops"] == 0 and "docker-kill" in res["event_evict_serverless"], res
            times.append(res["tick_ms"])
            t.join(30)
            assert wait_for(lambda: not rig.pool.snapshot() and not rig.pool.docker_names() and not rig.mon.last, 15)
        assert max(times) < 2000, times
    finally:
        hang_srv.close()


@pytest.mark.skipif(not GUARD_BIN.exists(), reason="guard test binary not built")
def test_when_guests_are_not_the_cause_the_guard_still_stops_the_engine_fast(rig, env):
    """Nobody to evict (no guests): the guard must not wait around — the engine is reclaimed like before the feature existed."""
    res = _guard_once(f"http://127.0.0.1:{rig.ctl.port}", extra_mib=1024)
    assert res["engine_stops"] == 1 and "event_reclaim" in res and res["tick_ms"] < 1500, res


class _HangServer:
    def __init__(self):
        import http.server
        import socketserver
        ev = threading.Event()
        self.ev = ev

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                ev.wait(30)

            def log_message(self, *a):
                pass

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
        self.srv = S(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.ev.set()
        self.srv.shutdown()
        self.srv.server_close()


# ---- crash safety -----------------------------------------------------------------------------------------------------------
_CRASH_SCRIPT = r"""
import sys, threading, time
sys.path.insert(0, %(root)r)
from agents.env_cache import EnvCache, EnvSpec
from agents.sandbox import GpuDockerSandbox, ServerlessLimits, WarmDockerPool, WarmKey
from agents.sl_gpu import GpuAdmission, GpuProbe
from tests.serverless.util import bundle_of, frame
from tests.serverless.test_gpu_real import hold_vram
spec = EnvSpec()
path, _ = EnvCache(%(store)r).ensure(spec)
pool = WarmDockerPool(GpuDockerSandbox(), max_containers=4, worker_id=%(worker)r)
pool.admission = GpuAdmission(GpuProbe(), 1536)
sha, b = bundle_of(hold_vram)
lim = ServerlessLimits(timeout_s=120.0, start_timeout_s=60.0, gpu_index=0, vram_mb=2400)
print("READY", flush=True)
pool.call(WarmKey("crash", spec.env_hash(), "hold"), path, lim, frame(sha, 2048, 120), b)
"""


def test_sigkill_of_the_worker_leaves_no_vram_behind(env):
    """The worker dies without any cleanup (SIGKILL) while a guest holds 2 GiB: the guest must disappear by itself within the lease TTL (30 s)."""
    probe = GpuProbe()
    before = probe.mem(0)[0]
    script = _CRASH_SCRIPT % {"root": str(ROOT), "store": str(STORE), "worker": WORKER}
    proc = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-c", script], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        assert wait_for(lambda: before - probe.mem(0)[0] >= 2000, 60), "guest never allocated"
        names = subprocess.run(["docker", "ps", "-q", "--filter", "label=aicf.sl=1", "--filter", f"label=aicf.worker={WORKER}"], capture_output=True, text=True).stdout.split()
        assert len(names) == 1
        t0 = time.time()
        proc.kill()                                                      # SIGKILL: no atexit, no finally, no pool.close()
        proc.wait(10)
        gone = wait_for(lambda: probe.mem(0)[0] >= before - 200 and not subprocess.run(
            ["docker", "ps", "-q", "--filter", "label=aicf.sl=1", "--filter", f"label=aicf.worker={WORKER}"], capture_output=True, text=True).stdout.split(), 30, 0.2)
        took = time.time() - t0
        assert gone, "VRAM or container still held 30 s after the worker was SIGKILLed"
        rep = json.loads((ROOT / "benchmarks/reports/p_serverless_3_reclaim.json").read_text()) if (ROOT / "benchmarks/reports/p_serverless_3_reclaim.json").exists() else {}
        rep["sigkill_worker_vram_released_s"] = round(took, 2)
        (ROOT / "benchmarks/reports/p_serverless_3_reclaim.json").write_text(json.dumps(rep, indent=2) + "\n")
    finally:
        if proc.poll() is None:
            proc.kill()
        left = subprocess.run(["docker", "ps", "-q", "--filter", "label=aicf.sl=1", "--filter", f"label=aicf.worker={WORKER}"], capture_output=True, text=True).stdout.split()
        if left:
            subprocess.run(["docker", "kill", *left], capture_output=True)
