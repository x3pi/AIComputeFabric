# ruff: noqa: F811
"""Phase 4 gate: Full SDK & CLI (@app.cls enter/exit, .map(200) across >=2 workers + worker drop, 10k backpressure, .spawn/FunctionCall,
Image local files/requirements, Secret non-leakage, CLI deploy/ps/stop/logs/volume/doctor 5 config errors)."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
import tracemalloc

import pytest

from agents.serverless_worker import build_worker
from agents.task_bus import list_live_agents
from agents.tasks import TaskType
from tests.serverless.test_e2e import ADMIN, ROOT, STORE, nats_url, secrets, stack, worker  # noqa: F401
from tests.serverless.util import by_value

SDK = ROOT / "sdk/python"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))
import aicf  # noqa: E402

by_value(__name__)


class StatefulModel:
    enter_count = 0
    exit_count = 0
    calls = 0

    @aicf.enter()
    def load(self):
        StatefulModel.enter_count += 1
        self.weights = [10, 20, 30]

    @aicf.exit()
    def cleanup(self):
        StatefulModel.exit_count += 1

    @aicf.method()
    def infer(self, x: int) -> dict:
        StatefulModel.calls += 1
        return {"enter_count": StatefulModel.enter_count, "calls": StatefulModel.calls, "out": sum(w * x for w in self.weights)}


def test_app_cls_enter_runs_exactly_once_over_10_remote_calls(stack):
    app = aicf.App("cls-demo", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))
    ModelCls = app.cls(keep_warm_s=60)(StatefulModel)
    m = ModelCls()
    results = [m.infer.remote(i) for i in range(1, 11)]
    assert [r["enter_count"] for r in results] == [1] * 10
    assert [r["calls"] for r in results] == list(range(1, 11))
    assert [r["out"] for r in results] == [60 * i for i in range(1, 11)]


def test_map_200_across_two_workers_and_worker_drop_recovery(stack, nats_url, secrets):
    # Start a second isolated ServerlessWorker on pc-001 so >= 2 workers share the load
    w2_name = "aicf-sl-test-w2"
    bw2, ex2 = build_worker(nats_url, worker_id=w2_name, key_file=str(secrets / "hmac.key"), store_dir=str(STORE), slots=4)
    t2 = threading.Thread(target=bw2.run, daemon=True)
    t2.start()

    # Wait for w2 to register in AICF_AGENTS
    deadline = time.time() + 15
    while time.time() < deadline:
        live = stack.bx._run(list_live_agents(stack.bx.js, TaskType.SERVERLESS_EXEC))
        if {a.agent_id for a in live} >= {"aicf-sl-test", w2_name}:
            break
        time.sleep(0.1)
    else:
        pytest.fail("second worker did not register")

    try:
        # Raise alpha concurrency to 16 for .map(200)
        stack.svc.quotas.set_policy(stack.tenants["alpha"], {"max_concurrency": 16, "max_retries": 3})
        app = aicf.App("map-demo", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

        @app.function(keep_warm_s=60)
        def work(x: int) -> tuple[int, str]:
            import os
            import time as _t
            _t.sleep(0.01)
            return x * x, os.uname().nodename

        t0 = time.perf_counter()
        outputs = list(work.map(range(200), max_concurrency=8))
        map_200_ms = round((time.perf_counter() - t0) * 1000, 1)
        assert [o[0] for o in outputs] == [i * i for i in range(200)]
        assert ex2.counters["ok"] > 0, "second worker must participate in .map(200)"

        # starmap and for_each
        @app.function(keep_warm_s=30)
        def add_pair(a: int, b: int) -> int:
            return a + b

        assert list(add_pair.starmap([(i, i * 10) for i in range(20)], max_concurrency=4)) == [i * 11 for i in range(20)]
        work.for_each([1, 2, 3], max_concurrency=2)

        # Mid-map worker drop: kill/drain w2 while 60 items are running with retries=2 -> 0 lost or wrong results
        @app.function(keep_warm_s=30, retries=2)
        def resilient_step(x: int) -> int:
            import time as _t
            _t.sleep(0.02)
            return x + 1000

        def drop_w2_midway():
            time.sleep(0.12)
            ex2.draining = True
            bw2.request_drain()
            ex2.pool.kill_all(reason="evicted")

        killer = threading.Thread(target=drop_w2_midway, daemon=True)
        killer.start()
        recovered = list(resilient_step.map(range(60), max_concurrency=6, retries=2))
        killer.join(timeout=5)
        assert recovered == [i + 1000 for i in range(60)]
    finally:
        ex2.draining = True
        bw2.request_drain()
        t2.join(timeout=10)
        ex2.pool.close()
        try:
            ex2.blobs.close()
        except Exception:
            pass
        deadline = time.time() + 10
        while ex2.pool.docker_names() and time.time() < deadline:
            ex2.pool.kill_orphans()
            time.sleep(0.2)
        assert ex2.pool.docker_names() == []

    # Record Phase 4 report metrics
    rep = {
        "label": "[ĐO]",
        "map_200_two_workers_ms": map_200_ms,
        "w2_completed_calls": ex2.counters["ok"],
        "worker_drop_recovered_60_items": True,
    }
    (ROOT / "benchmarks/reports/p_serverless_4_sdk.json").write_text(json.dumps(rep, indent=2) + "\n")


def test_map_10000_backpressure_keeps_memory_flat(monkeypatch):
    """Passing a 10,000-element generator to .map(max_concurrency=16) must never materialize all 10,000 elements at once."""
    inflight = {"cur": 0, "peak": 0, "produced": 0, "consumed": 0}
    mu = threading.Lock()

    class FastClient(aicf.Client):
        def __init__(self):
            self.url = "http://loopback"
            self.api_key = "aicf_test"
            self.timeout = 10.0

        def post(self, path: str, body: dict) -> dict:
            import base64 as _b64
            import cloudpickle as _cp
            assert path == "/invoke"
            with mu:
                inflight["cur"] += 1
                inflight["peak"] = max(inflight["peak"], inflight["cur"])
            try:
                args, _ = _cp.loads(_b64.b64decode(body["args_b64"]))
                val = args[0] + 1
                return {"status": "ok", "call_id": "c", "result_b64": _b64.b64encode(_cp.dumps(val)).decode(), "result_format": "cloudpickle"}
            finally:
                with mu:
                    inflight["cur"] -= 1

    app = aicf.App("bp", client=FastClient())

    @app.function()
    def inc(x: int) -> int:
        return x + 1

    def gen():
        for i in range(10_000):
            with mu:
                inflight["produced"] += 1
            yield i

    tracemalloc.start()
    try:
        max_lead = 0
        count = 0
        for idx, res in enumerate(inc.map(gen(), max_concurrency=16)):
            assert res == idx + 1
            count += 1
            with mu:
                inflight["consumed"] += 1
                lead = inflight["produced"] - inflight["consumed"]
                if lead > max_lead:
                    max_lead = lead
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert count == 10_000
    assert max_lead <= 48, f"backpressure broken: generator ran {max_lead} items ahead of consumer"
    assert inflight["peak"] <= 16
    assert peak_bytes < 16 * 1024 * 1024, f"memory grew too much: {peak_bytes}"


def test_spawn_function_call_logs_and_cancel(stack):
    app = aicf.App("spawn-app", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

    @app.function(keep_warm_s=15)
    def noisy(x: int) -> int:
        print(f"computing {x}")
        return x * 10

    fc = noisy.spawn(7)
    assert fc.object_id
    fc2 = aicf.FunctionCall.from_id(fc.object_id, client=app.client())
    assert fc2.get(timeout=30) == 70
    logs = fc2.logs()
    assert any("computing 7" in entry.get("text", "") for entry in logs)

    # Cross-tenant isolation: tenant beta must be forbidden (403) from reading or cancelling alpha's call
    beta_fc = aicf.FunctionCall.from_id(fc.object_id, client=aicf.Client(stack.gateway_url, stack.keys["beta"]))
    with pytest.raises(aicf.AuthError, match="cannot access"):
        beta_fc.get(timeout=1)
    with pytest.raises(aicf.AuthError, match="cannot cancel"):
        beta_fc.cancel()


def test_image_local_files_and_requirements_hash_enforcement(stack, tmp_path):
    data_dir = tmp_path / "assets"
    data_dir.mkdir()
    (data_dir / "greeting.txt").write_text("xin chao aicf")
    single_file = tmp_path / "config.json"
    single_file.write_text('{"factor": 3}')

    # Oversized local file (> 4 MiB) must be rejected with a clear hint to use Volume
    big_file = tmp_path / "too_big.bin"
    big_file.write_bytes(b"x" * (4 * 1024 * 1024 + 16))
    with pytest.raises(aicf.InvalidRequest, match="exceed.*Volume"):
        aicf.Image.debian_slim().add_local_file(big_file, "too_big.bin")

    # Requirements without --hash=sha256:... must be rejected
    bad_req = tmp_path / "bad_req.txt"
    bad_req.write_text("cloudpickle==3.1.1\n")
    with pytest.raises(aicf.InvalidRequest, match="--hash=sha256:"):
        aicf.Image.debian_slim().pip_install_from_requirements(bad_req)

    good_req = tmp_path / "good_req.txt"
    good_req.write_text("cloudpickle==3.1.1 --hash=sha256:" + "a" * 64 + "\n")
    img_req = aicf.Image.debian_slim().pip_install_from_requirements(good_req)
    assert "cloudpickle==3.1.1" in img_req.packages and img_req.spec()["local_source_hash"]

    img = aicf.Image.debian_slim().add_local_file(single_file, "config.json").add_local_dir(data_dir, "assets")
    app = aicf.App("files-app", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

    @app.function(image=img, keep_warm_s=10)
    def read_bundled() -> str:
        import json as _j
        cfg = _j.loads(open("/tmp/aicf_files/config.json").read())
        msg = open("/tmp/aicf_files/assets/greeting.txt").read().strip()
        return f"{msg}:{cfg['factor']}"

    assert read_bundled.remote() == "xin chao aicf:3"


def test_secret_injected_into_container_and_never_logged_or_audited(stack):
    secret_val = "super-secret-hf-token-99887766"
    stack.svc.quotas.set_secret(stack.tenants["alpha"], "hf-secret", {"HF_TOKEN": secret_val})
    app = aicf.App("sec-app", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

    @app.function(secrets=[aicf.Secret.from_name("hf-secret")], keep_warm_s=5)
    def check_secret() -> bool:
        import os
        return os.environ.get("HF_TOKEN") == "super-secret-hf-token-99887766"

    assert check_secret.remote() is True

    # Verify the secret string NEVER appears in audit log, metrics, or overview
    audit_dump = json.dumps(stack.svc.audit.recent(200))
    overview_dump = json.dumps(stack.svc.overview())
    metrics_dump = stack.svc.metrics.render()
    assert secret_val not in audit_dump
    assert secret_val not in overview_dump
    assert secret_val not in metrics_dump


def test_cli_deploy_ps_stop_volume_and_doctor_5_errors(stack, tmp_path):
    script = tmp_path / "my_app.py"
    script.write_text(textwrap.dedent('''
        import aicf
        app = aicf.App("cli-deployed")

        @app.function(keep_warm_s=30)
        def double(x):
            return x * 2
    '''))
    base_env = {"PATH": "/usr/bin:/bin", "AICF_GATEWAY_URL": stack.gateway_url, "AICF_API_KEY": stack.keys["alpha"], "PYTHONPATH": str(SDK)}

    def run_cli(*args, env_override=None):
        env = {**base_env, **(env_override or {})}
        return subprocess.run([sys.executable, "-m", "aicf.cli", *args], env=env, capture_output=True, text=True, timeout=60)

    # 1. deploy
    r = run_cli("deploy", str(script))
    assert r.returncode == 0 and "v-" in r.stdout, r.stderr
    # 2. ps
    r = run_cli("ps")
    assert r.returncode == 0 and "cli-deployed" in r.stdout
    # 3. volume put / ls / get
    sample = tmp_path / "hello.bin"
    sample.write_bytes(b"volume-bytes-123")
    out_file = tmp_path / "downloaded.bin"
    assert run_cli("volume", "put", "vol1", str(sample), "sub/hello.bin").returncode == 0
    r_ls = run_cli("volume", "ls", "vol1")
    assert r_ls.returncode == 0 and "sub/hello.bin" in r_ls.stdout
    assert run_cli("volume", "get", "vol1", "sub/hello.bin", str(out_file)).returncode == 0
    assert out_file.read_bytes() == b"volume-bytes-123"
    # 4. stop
    r_stop = run_cli("stop", "cli-deployed")
    assert r_stop.returncode == 0 and "stopped" in r_stop.stdout

    # 5. aicf doctor detects all 5 sample configuration errors:
    # (a) Unreachable URL
    d1 = run_cli("doctor", env_override={"AICF_GATEWAY_URL": "http://127.0.0.1:1"})
    assert d1.returncode == 1 and "FAIL" in d1.stdout and "cannot reach" in d1.stdout
    # (b) Wrong API key
    d2 = run_cli("doctor", env_override={"AICF_API_KEY": "aicf_wrong_key"})
    assert d2.returncode == 1 and "FAIL" in d2.stdout and "rejected" in d2.stdout
    # (c) Python version mismatch
    d3 = run_cli("doctor", env_override={"AICF_LOCAL_PYTHON_OVERRIDE": "3.11"})
    assert d3.returncode == 1 and "FAIL local Python 3.11" in d3.stdout
    # (d) Tenant serverless disabled (gamma)
    d4 = run_cli("doctor", env_override={"AICF_API_KEY": stack.keys["gamma"]})
    assert d4.returncode == 1 and "FAIL" in d4.stdout and "not enabled" in d4.stdout
    # (e) Quota exhausted (max_concurrency=0)
    stack.svc.quotas.set_policy(stack.tenants["beta"], {"max_concurrency": 0})
    try:
        d5 = run_cli("doctor", env_override={"AICF_API_KEY": stack.keys["beta"]})
        assert d5.returncode == 1 and "FAIL" in d5.stdout and "concurrency_exceeded" in d5.stdout
    finally:
        stack.svc.quotas.set_policy(stack.tenants["beta"], {"max_concurrency": 8})
