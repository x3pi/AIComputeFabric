"""Warm sandbox pool against REAL Docker (plan Pha 1 gate). Containers are labelled aicf.sl=1 / worker=aicf-sl-test.
Skipped when Docker is unavailable. The GPU test additionally needs the nvidia runtime and a GPU."""
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agents.env_cache import EnvCache, EnvError, EnvSpec
from agents.sandbox import GpuDockerSandbox, SandboxBusy, SandboxError, ServerlessLimits, WarmDockerPool, WarmKey
from tests.serverless.util import bundle_of, by_value, frame, value

by_value(__name__)
ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / ".aicf-work/sl-test/store"
WORKER = "aicf-sl-test"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
                                reason="Docker not available")


@pytest.fixture(scope="module")
def cache():
    return EnvCache(STORE)


@pytest.fixture(scope="module")
def env(cache):
    spec = EnvSpec()
    path, _ = cache.ensure(spec)
    return spec.env_hash(), path


@pytest.fixture
def pool():
    p = WarmDockerPool(GpuDockerSandbox(), max_containers=6, worker_id=WORKER)
    yield p
    p.close()
    deadline = time.time() + 10                                       # --rm removal is asynchronous
    leftovers = p.docker_names()
    while leftovers and time.time() < deadline:
        time.sleep(0.1)
        leftovers = p.docker_names()
    subprocess.run(["docker", "kill", *leftovers], capture_output=True) if leftovers else None
    assert leftovers == [], f"orphan containers: {leftovers}"


def L(**kw):
    return ServerlessLimits(**{"timeout_s": 20.0, "start_timeout_s": 60.0, **kw})


def call(pool, env, fn, *args, tenant="t1", fn_id="f1", limits=None, **kw):
    h, path = env
    sha, b = bundle_of(fn)
    return pool.call(WarmKey(tenant, h, fn_id), path, limits or L(), frame(sha, *args, **kw), b)


# ---- functions that run INSIDE the sandbox -------------------------------------------------------------------------
def square(x):
    return x * x


def write_tmp_then_read(tag):
    import os
    p = "/tmp/secret.txt"
    if tag == "write":
        open(p, "w").write("tenant-secret")
        return "written"
    return os.path.exists(p)


def try_network():
    import socket
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("1.1.1.1", 53))
        return "CONNECTED"
    except OSError as e:
        return f"blocked:{type(e).__name__}"


def try_write_root():
    try:
        open("/pwned", "w").write("x")
        return "WROTE"
    except OSError as e:
        return f"blocked:{type(e).__name__}"


def try_write_env():
    try:
        open("/env/pwned", "w").write("x")
        return "WROTE"
    except OSError as e:
        return f"blocked:{type(e).__name__}"


def see_host():
    import os
    return {"home_pi": os.path.exists("/home/pi"), "docker_sock": os.path.exists("/var/run/docker.sock"), "uid": os.getuid(),
            "caps": open("/proc/self/status").read().split("CapEff:")[1].split()[0]}


def sleeper(s):
    import time
    time.sleep(s)


def hog(mb):
    b = bytearray(mb * 1024 * 1024)
    for i in range(0, len(b), 4096):
        b[i] = 1
    return len(b)


def die():
    import os
    os._exit(1)


def fork_many():
    import os
    n = 0
    try:
        for _ in range(500):
            if os.fork() == 0:
                import time
                time.sleep(30)
                os._exit(0)
            n += 1
    except OSError:
        return f"limited_after:{n}"
    return f"unlimited:{n}"


class Model:
    loads = 0

    def load(self):
        Model.loads += 1
        self.w = [1, 2, 3]

    def infer(self, x):
        return Model.loads, sum(self.w) * x


# ---- env cache --------------------------------------------------------------------------------------------------
def test_env_built_once_then_cached(cache, env):
    spec = EnvSpec()
    path, built = cache.ensure(spec)
    assert built is False and (path / ".ready").exists() and (path / "requirements.lock").read_text().count("--hash=") >= 1


def test_env_build_rejects_sdist_only_package(cache):
    with pytest.raises(EnvError, match="wheels only"):
        cache.ensure(EnvSpec(packages=("sgmllib3k",)))
    assert not any(p.name.startswith(EnvSpec(packages=("sgmllib3k",)).env_hash()) for p in (cache.store / "envs").iterdir())


def test_env_concurrent_builds_serialise(cache):
    spec = EnvSpec(packages=("six==1.17.0",))
    out = []
    ts = [threading.Thread(target=lambda: out.append(cache.ensure(spec)[1])) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [False, False, True] or sorted(out) == [False, False, False]


def test_env_refuses_to_build_when_disk_is_tight(tmp_path):
    c = EnvCache(tmp_path / "s", min_free_bytes=10**18)
    with pytest.raises(EnvError, match="free disk"):
        c.ensure(EnvSpec())


# ---- warm pool --------------------------------------------------------------------------------------------------
def test_cold_then_warm(pool, env):
    r1, cold1 = call(pool, env, square, 7)
    r2, cold2 = call(pool, env, square, 8)
    assert (cold1, cold2) == (True, False) and value(r1) == 49 and value(r2) == 64
    assert r1["container"] == r2["container"]
    assert r2["exec_ms"] < 50


def test_scale_to_zero(pool, env):
    call(pool, env, square, 2, limits=L(keep_warm_s=1.0))
    assert len(pool.docker_names()) == 1
    assert pool.reap_idle(now=pool.clock()) == []                       # just used: stays
    assert len(pool.reap_idle(now=pool.clock() + 5)) == 1               # idle past keep_warm_s: gone
    deadline = time.time() + 10
    while pool.docker_names() and time.time() < deadline:
        time.sleep(0.1)
    assert pool.docker_names() == []


def test_tenants_never_share_a_container_or_tmp(pool, env):
    ra, _ = call(pool, env, write_tmp_then_read, "write", tenant="tenant-a")
    rb, cold = call(pool, env, write_tmp_then_read, "read", tenant="tenant-b")
    ra2, _ = call(pool, env, write_tmp_then_read, "read", tenant="tenant-a")
    assert ra["container"] != rb["container"] and cold is True
    assert value(rb) is False and value(ra2) is True


def test_sandbox_blocks_network_and_filesystem_writes(pool, env):
    assert value(call(pool, env, try_network)[0]).startswith("blocked:")
    assert value(call(pool, env, try_write_root)[0]).startswith("blocked:")
    assert value(call(pool, env, try_write_env)[0]).startswith("blocked:")


def test_sandbox_does_not_expose_host(pool, env):
    v = value(call(pool, env, see_host)[0])
    assert v["home_pi"] is False and v["docker_sock"] is False and v["uid"] == 65534 and int(v["caps"], 16) == 0


def test_timeout_kills_container_and_leaves_no_orphan(pool, env):
    with pytest.raises(SandboxError, match="timed out"):
        call(pool, env, sleeper, 60, limits=L(timeout_s=2.0))
    time.sleep(1.5)
    assert pool.docker_names() == [] and pool.snapshot() == []


def test_memory_limit_kills_container(pool, env):
    with pytest.raises(SandboxError, match="exit|memory"):
        call(pool, env, hog, 400, limits=L(memory_mb=128))
    time.sleep(1.5)
    assert pool.docker_names() == []
    r, cold = call(pool, env, square, 3)                                # the pool recovers with a fresh container
    assert value(r) == 9 and cold is True


def test_pids_limit(pool, env):
    r, _ = call(pool, env, fork_many, limits=L(pids=32))
    assert value(r).startswith("limited_after:")


def test_runner_death_is_reported_and_next_call_starts_cold(pool, env):
    with pytest.raises(SandboxError):
        call(pool, env, die)
    r, cold = call(pool, env, square, 4)
    assert value(r) == 16 and cold is True


def test_class_enter_once_across_calls(pool, env):
    h, path = env
    sha, b = bundle_of(Model, kind="cls", enter=["load"])
    key = WarmKey("t1", h, "model")
    seen = []
    for i in range(10):
        r, _ = pool.call(key, path, L(), frame(sha, i, method="infer"), b)
        seen.append(value(r)[0])
    assert seen == [1] * 10


def test_pool_capacity_and_per_key_limit(env):
    h, path = env
    p = WarmDockerPool(GpuDockerSandbox(), max_containers=2, max_per_key=1, worker_id=WORKER)
    try:
        started, errs = threading.Event(), []
        sha, b = bundle_of(sleeper)

        def long():
            started.set()
            p.call(WarmKey("t1", h, "f"), path, L(), frame(sha, 3), b)
        t = threading.Thread(target=long)
        t.start()
        started.wait()
        time.sleep(0.5)
        with pytest.raises(SandboxBusy):
            p.call(WarmKey("t1", h, "f"), path, L(), frame(sha, 0), b)
        t.join()
    finally:
        p.close()


def test_kill_all_is_fast_and_complete(pool, env):
    for t in ("a", "b", "c"):
        call(pool, env, square, 1, tenant=t)
    assert len(pool.docker_names()) == 3
    t0 = time.perf_counter()
    killed = pool.kill_all()
    dt = time.perf_counter() - t0
    assert len(killed) == 3 and dt < 2.0
    deadline = time.time() + 5
    while pool.docker_names() and time.time() < deadline:
        time.sleep(0.05)
    assert pool.docker_names() == [] and pool.snapshot() == []


def test_kill_orphans_finds_containers_the_pool_forgot(pool, env):
    call(pool, env, square, 1)
    with pool._mu:
        pool._by_key.clear()                                            # simulate a worker restart losing its table
    assert len(pool.kill_orphans()) == 1


def _gpu_ok():
    r = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True)
    return "nvidia" in r.stdout and shutil.which("nvidia-smi") is not None


def gpu_name():
    import subprocess
    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        return "NO-GPU"


@pytest.mark.skipif(not _gpu_ok(), reason="needs nvidia docker runtime and a GPU")
def test_gpu_visible_inside_hardened_warm_container(pool, env):
    r, _ = call(pool, env, gpu_name, limits=L(gpu_index=0, vram_mb=1024))
    assert "NVIDIA" in value(r)
    r2, _ = call(pool, env, gpu_name, limits=L())                     # CPU function on the same host: no GPU
    assert value(r2) == "NO-GPU" and r2["container"] != r["container"]   # same function, different shape: never reuse the GPU container
