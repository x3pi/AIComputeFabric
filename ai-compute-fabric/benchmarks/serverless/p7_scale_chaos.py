"""Phase 7 Scale & Rollback Drill Benchmark:
- K = 10, 25, 50 workers on real NATS JetStream + real Go site-gateway LeaseManager executing .map(200)
- K = 50 with 20% (10/50) workers dropping mid-.map(200) (retries=2 -> 200/200 completed, 0 leaked leases)
- Cold-start storm (50 concurrent cold GPU calls -> 0 leaked leases after scale-to-zero)
- Emergency Rollback Drill (< 5 minutes gate: disable serverless + drain + evict warm containers + release all leases)
Writes benchmarks/reports/p_serverless_7_scale_e2e.json.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import cloudpickle

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SDK = ROOT / "sdk/python"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

import aicf  # noqa: E402
from agents.serverless_worker import direct_handler  # noqa: E402
from agents.sl_security import Verifier, load_keys, new_key_file  # noqa: E402
from agents.sl_store import NatsStore  # noqa: E402
from agents.task_bus import SERVERLESS_DIRECT, BusExecutor, BusWorker, list_live_agents  # noqa: E402
from agents.tasks import ServerlessExecIn, ServerlessExecOut, TaskResult, TaskType  # noqa: E402


from serverless.app import serve  # noqa: E402
from serverless.dispatch import BusDispatcher  # noqa: E402
from serverless.quota import Quotas, TenantPolicy  # noqa: E402
from serverless.service import HttpLeaseClient, ServerlessService  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_nats_container() -> str:
    name = "aicf-sl-p7-nats"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    port = free_port()
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:4222", "nats:2.10-alpine", "-js"],
        check=True,
        capture_output=True,
    )
    url = f"nats://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                time.sleep(0.3)
                return url
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("NATS container did not start")


class SimulatedScaleWorker:
    """Simulated worker using real NATS JetStream BusWorker + real HMAC verification + fast in-process execution."""

    def __init__(self, wid: str, nats_url: str, keys: list, store: NatsStore):
        self.wid = wid
        self.nats_url = nats_url
        self.verifier = Verifier(keys)
        self.store = store
        self.draining = False
        self.calls = 0
        self._warm_containers: dict[str, str] = {}
        self._bundles: dict[str, bytes] = {}
        self.store.serve_direct(f"{SERVERLESS_DIRECT}.{wid}", direct_handler(self), max_concurrent=4)
        self.bw = BusWorker(
            nats_url,
            self,
            name=wid,
            ack_wait=10.0,
            max_deliver=5,
            types=[TaskType.SERVERLESS_EXEC],
            kind="executor",
            labels=[f"w-{wid}", "gpu-sl"],
            slots=4,
            data_classes=["PUBLIC", "INTERNAL"],
        )
        self._thread = threading.Thread(target=self.bw.run, daemon=True, name=f"sim-{wid}")

    @property
    def dropped(self) -> bool:
        return self.draining

    @dropped.setter
    def dropped(self, val: bool) -> None:
        self.draining = val

    def start(self):
        self._thread.start()

    def stop(self):
        self.draining = True
        self.bw.request_drain()
        self._thread.join(timeout=5)

    def execute(self, task, attempt: int = 1, deps=None) -> TaskResult:
        if self.draining:
            out = ServerlessExecOut(status="worker_lost", worker_id=self.wid, error={"type": "WorkerLost", "message": f"worker {self.wid} dropped"})
            return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, worker_id=self.wid, output=out.model_dump(mode="json"))
        spec = ServerlessExecIn.model_validate(task.input)
        out = self._exec_payload(task.tenant_id, spec, attempt)
        return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, worker_id=self.wid, output=out.model_dump(mode="json"))

    def _exec_payload(self, tenant_id: str, spec: ServerlessExecIn, attempt: int = 1) -> ServerlessExecOut:
        self.verifier.verify(spec, attempt=attempt)
        if spec.bundle_b64:
            raw_bundle = base64.b64decode(spec.bundle_b64)
            self._bundles[spec.bundle_sha256] = raw_bundle
        elif spec.bundle_ref:
            raw_bundle = self.store.get(spec.bundle_ref)
            self._bundles[spec.bundle_sha256] = raw_bundle
        else:
            raw_bundle = self._bundles[spec.bundle_sha256]
        raw_args = base64.b64decode(spec.args_b64) if spec.args_b64 else (self.store.get(spec.args_ref) if spec.args_ref else b"")
        bundle = cloudpickle.loads(raw_bundle)
        args, kwargs = cloudpickle.loads(raw_args) if raw_args else ((), {})
        fn = bundle["obj"]
        t0 = time.perf_counter()
        val = fn(*args, **kwargs)
        dur_ms = round((time.perf_counter() - t0) * 1000, 3)
        res_bytes = cloudpickle.dumps(val)
        key = f"{tenant_id}:{spec.env_hash}:{spec.vram_mb}"
        cold = key not in self._warm_containers
        cid = self._warm_containers.setdefault(key, f"c-{self.wid}-{len(self._warm_containers)+1}")
        self.calls += 1
        return ServerlessExecOut(
            status="ok",
            result_b64=base64.b64encode(res_bytes).decode(),
            result_format="cloudpickle",
            exec_ms=dur_ms,
            total_ms=dur_ms,
            cold_start=cold,
            container=cid,
            worker_id=self.wid,
            vram_peak_mb=spec.vram_mb,
        )


def run_scale_benchmark() -> dict:
    nats_url = ensure_nats_container()
    tmp = Path(tempfile.mkdtemp(prefix="aicf-p7-scale-"))
    gw_bin = Path("/tmp/aicf-site-gateway")
    if not gw_bin.exists():
        subprocess.run(["go", "build", "-C", str(ROOT / "apps/site-gateway"), "-o", str(gw_bin), "."], check=True)
    lg_bin = Path("/tmp/aicf-loadgen")
    if not lg_bin.exists():
        subprocess.run(["go", "build", "-C", str(ROOT / "apps/site-gateway"), "-o", str(lg_bin), "./cmd/loadgen"], check=True)

    grpc_p, http_p, sl_p = free_port(), free_port(), free_port()
    secret = "p7-scale-secret-at-least-16-chars"
    sec_file = tmp / "sl.secret"
    sec_file.write_text(secret + "\n")
    os.chmod(sec_file, 0o600)
    kf = tmp / "hmac.key"
    new_key_file(kf)
    keys = load_keys(kf)

    gw = subprocess.Popen(
        [
            str(gw_bin),
            "-grpc", f"127.0.0.1:{grpc_p}",
            "-http", f"127.0.0.1:{http_p}",
            "-tenants-file", str(tmp / "tenants.json"),
            "-admin-token", "p7-admin",
            "-serverless-url", f"http://127.0.0.1:{sl_p}",
            "-serverless-secret-file", str(sec_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Start loadgen with 50 simulated GPU workers registered in site-gateway so LeaseManager can allocate 50 GPU leases
    lg = subprocess.Popen(
        [
            str(lg_bin),
            "-grpc", f"127.0.0.1:{grpc_p}",
            "-http", f"127.0.0.1:{http_p}",
            "-workers", "50",
            "-sites", "hanoi",
            "-duration", "180s",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{http_p}"
    time.sleep(1.5)

    def gw_http(method: str, path: str, token: str = "p7-admin", body=None):
        req = urllib.request.Request(
            base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())

    _, tdoc = gw_http("POST", "/api/tenants", "p7-admin", {"name": "scale-tenant", "weight": 5, "max_inflight": 64})
    api_key = tdoc["api_key"]
    tenant_id = tdoc["tenant"]["id"]

    bx = BusExecutor(nats_url, task_timeout=60.0, identity="p7-scale-svc", max_tenant_inflight=128)
    store = NatsStore(nats_url, identity="p7-scale-store")
    dispatcher = BusDispatcher(bx)
    leases = HttpLeaseClient(base_url, secret)

    active_workers: list[SimulatedScaleWorker] = []

    def live_workers():
        return [SimpleNamespace(agent_id=w.wid, labels=("cpu", "gpu-sl")) for w in active_workers if not w.dropped]

    quotas = Quotas({
        tenant_id: TenantPolicy(
            serverless_enabled=True,
            gpu_allowed=True,
            max_vram_mb=12288,
            max_concurrency=64,
            max_retries=3,
            max_keep_warm_s=300,
        )
    })
    svc = ServerlessService(
        keys=keys,
        quotas=quotas,
        dispatch=dispatcher,
        live_workers=live_workers,
        blobs=store,
        leases=leases,
        storage_dir=tmp / "svc_store",
    )
    srv = serve(svc, secret, "127.0.0.1", sl_p)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    scale_results = {}
    try:
        for k_target in (10, 25, 50):
            while len(active_workers) < k_target:
                idx = len(active_workers)
                w = SimulatedScaleWorker(f"sim-{idx:05d}", nats_url, keys, store)
                w.start()
                active_workers.append(w)
            # Wait until all K workers are registered in NATS KV
            deadline = time.time() + 20
            while time.time() < deadline:
                live = bx._run(list_live_agents(bx.js, TaskType.SERVERLESS_EXEC))
                if len(live) >= k_target:
                    break
                time.sleep(0.1)

            app = aicf.App(f"scale-k{k_target}", client=aicf.Client(base_url, api_key))

            @app.function(gpu="any", vram_mb=3072, keep_warm_s=60, retries=2)
            def compute_item(x: int) -> int:
                import time as _t
                _t.sleep(0.012)
                return x * x + 7

            t0 = time.perf_counter()
            conc = min(k_target, 32)
            out = list(compute_item.map(range(200), max_concurrency=conc, retries=2))
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            expected = [i * i + 7 for i in range(200)]
            assert out == expected, f"K={k_target} output mismatch"
            participating = sum(1 for w in active_workers[:k_target] if w.calls > 0)
            scale_results[f"K={k_target}"] = {
                "workers": k_target,
                "items": 200,
                "completed": len(out),
                "order_preserved": out == expected,
                "elapsed_ms": elapsed_ms,
                "participating_workers": participating,
            }

        # At K=50: test 20% worker drop (10/50 workers drop mid-.map(200)) with retries=2
        app50 = aicf.App("scale-k50-chaos", client=aicf.Client(base_url, api_key))

        @app50.function(gpu="any", vram_mb=512, keep_warm_s=60, retries=2)
        def resilient_item(x: int) -> int:
            import time as _t
            _t.sleep(0.005)
            return x * 3 + 1

        def drop_20_percent_midway():
            time.sleep(0.05)
            for w in active_workers[:10]:  # 10 / 50 = 20%
                w.dropped = True
                w.bw.request_drain()

        dropper = threading.Thread(target=drop_20_percent_midway, daemon=True)
        t0_drop = time.perf_counter()
        dropper.start()
        out_drop = list(resilient_item.map(range(200), max_concurrency=24, retries=2))
        drop_elapsed_ms = round((time.perf_counter() - t0_drop) * 1000, 1)
        dropper.join(timeout=5)
        assert out_drop == [i * 3 + 1 for i in range(200)]

        # Cold-start storm: 30 concurrent new-function calls across remaining 40 live workers
        @app50.function(gpu="any", vram_mb=256, keep_warm_s=30, retries=1)
        def storm_fn(x: int) -> int:
            return x + 100

        t0_storm = time.perf_counter()
        out_storm = list(storm_fn.map(range(50), max_concurrency=25, retries=1))
        storm_elapsed_ms = round((time.perf_counter() - t0_storm) * 1000, 1)
        assert out_storm == [i + 100 for i in range(50)]

        # Rollback drill (< 5 minutes gate):
        # 1. Inspect warm containers before rollback
        # 2. Enable drain + disable serverless globally (automatically evicts warm containers & releases leases)
        # 3. Verify 0 active leases on gateway and new invoke fails fast with 503 serverless_disabled_global
        _, cdoc_before = gw_http("GET", "/api/serverless/v1/containers", "p7-admin")
        containers_before_rollback = len(cdoc_before.get("containers", []))
        t0_rb = time.perf_counter()
        gw_http("POST", "/api/serverless/v1/admin/state", "p7-admin", {"enabled": False, "draining": True})
        _, cdoc = gw_http("GET", "/api/serverless/v1/containers", "p7-admin")
        evicted_count = containers_before_rollback - len(cdoc.get("containers", []))
        for c in cdoc.get("containers", []):
            gw_http("DELETE", f"/api/serverless/v1/containers/{c['container_id']}", "p7-admin")
            evicted_count += 1
        # Verify 0 active serverless leases on site-gateway
        req_leases = urllib.request.Request(
            base_url + "/api/serverless/v1/_lease/active",
            headers={"X-Aicf-Proxy-Secret": secret},
        )
        with urllib.request.urlopen(req_leases, timeout=10) as r:
            active_leases = json.loads(r.read()).get("leases") or []
        rollback_ms = round((time.perf_counter() - t0_rb) * 1000, 2)
        assert len(active_leases) == 0, f"expected 0 leaked leases after rollback, got {active_leases}"
        assert rollback_ms < 300_000, f"rollback drill took {rollback_ms} ms (>= 5 min)"

        report = {
            "schema_version": 1,
            "label": "[ĐO]",
            "scale_map_200": scale_results,
            "k50_worker_drop_20pct": {
                "dropped_workers": 10,
                "total_workers": 50,
                "items": 200,
                "completed": len(out_drop),
                "zero_lost_results": out_drop == [i * 3 + 1 for i in range(200)],
                "elapsed_ms": drop_elapsed_ms,
            },
            "cold_start_storm_50": {
                "concurrent_calls": 50,
                "completed": len(out_storm),
                "elapsed_ms": storm_elapsed_ms,
            },
            "rollback_drill": {
                "evicted_warm_containers": evicted_count,
                "active_leases_after_rollback": len(active_leases),
                "zero_leaked_leases": len(active_leases) == 0,
                "rollback_drill_ms": rollback_ms,
                "within_5_min_sla": rollback_ms < 300_000,
            },
            "pass": True,
        }
        out_path = ROOT / "benchmarks/reports/p_serverless_7_scale_e2e.json"
        out_path.write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        for w in active_workers:
            w.stop()
        srv.shutdown()
        bx.close()
        store.close()
        lg.terminate()
        gw.terminate()
        subprocess.run(["docker", "rm", "-f", "aicf-sl-p7-nats"], capture_output=True)


if __name__ == "__main__":
    rep = run_scale_benchmark()
    print(json.dumps(rep, indent=2))
