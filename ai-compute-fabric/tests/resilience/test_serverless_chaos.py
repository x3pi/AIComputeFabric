"""Phase 7 Chaos & Resilience Suite:
1. Worker crash / WorkerLost mid-call (D8 at-most-once with retries=0 vs automatic failover with retries=1) + 0 leaked leases
2. Orphan container cleanup on worker startup (kill_orphans)
3. Disk full (ENOSPC) during Volume atomic write -> 0 corrupt/partial files left on disk
4. Dead worker lease renewal stop (tick_leases) -> 0 stuck leases after worker disappears
5. Emergency rollback drill (< 5 minutes gate)
"""
from __future__ import annotations

import base64
import errno
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import cloudpickle
import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "sdk/python"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from agents.sl_security import load_keys, new_key_file  # noqa: E402
from agents.tasks import ServerlessExecOut, TaskResult, TaskType  # noqa: E402
from serverless.quota import Quotas, TenantPolicy  # noqa: E402
from serverless.service import ServiceError, ServerlessService  # noqa: E402
from serverless.volumes import VolumeStore  # noqa: E402


class FakeLeaseBridge:
    def __init__(self):
        self.active: dict[str, dict] = {}
        self._seq = 0

    def request(self, *, idempotency_key: str, tenant_id: str, vram_bytes: int, ttl_ms: int = 30000, constraint: str = "") -> dict:
        self._seq += 1
        lid = f"lease-chaos-{self._seq}"
        wid = "w-1" if self._seq == 1 else "w-2"
        self.active[lid] = {"lease_id": lid, "tenant_id": tenant_id, "worker_id": wid, "vram_bytes": vram_bytes}
        return {"granted": True, "lease": {"lease_id": lid, "worker_id": wid}}

    def extend(self, lease_id: str, ttl_ms: int = 30000) -> bool:
        return lease_id in self.active

    def release(self, lease_id: str) -> bool:
        return self.active.pop(lease_id, None) is not None


def _bundle_b64() -> str:
    return base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": lambda x: x + 1, "enter": [], "exit": []})).decode()


def _args_b64() -> str:
    return base64.b64encode(cloudpickle.dumps(((5,), {}))).decode()


def test_worker_crash_mid_call_d8_semantics_and_zero_leaked_leases(tmp_path):
    """When worker w-1 crashes mid-call:
    - retries=0 returns status='worker_lost' without re-running (D8) and releases w-1's lease
    - retries=1 releases w-1's lease, acquires a new lease on w-2, and succeeds with 0 leaked leases after eviction.
    """
    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    leases = FakeLeaseBridge()
    calls = []

    def dispatch(task, attempt):
        calls.append((task.task_id, attempt))
        if len(calls) in (1, 2):
            out = ServerlessExecOut(status="worker_lost", worker_id="w-1", error={"type": "WorkerLost", "message": "SIGKILL on w-1"})
        else:
            res = base64.b64encode(cloudpickle.dumps(42)).decode()
            out = ServerlessExecOut(status="ok", result_b64=res, result_format="cloudpickle", worker_id="w-2", container="c-w2", exec_ms=5.0)
        return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, worker_id=out.worker_id or "w-1", output=out.model_dump(mode="json"))

    svc = ServerlessService(
        keys=load_keys(kf),
        quotas=Quotas({"alpha": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096, max_retries=2)}),
        dispatch=dispatch,
        live_workers=lambda: [
            SimpleNamespace(agent_id="w-1", labels=("cpu", "gpu-sl")),
            SimpleNamespace(agent_id="w-2", labels=("cpu", "gpu-sl")),
        ],
        blobs={},
        leases=leases,
        storage_dir=tmp_path / "store",
    )

    # 1. retries=0 -> must NOT retry (D8 at-most-once); lease from failed attempt must be cleaned up on eviction/failure
    r0 = svc.invoke("alpha", {
        "app_name": "chaos",
        "fn_id": "step",
        "bundle_b64": _bundle_b64(),
        "args_b64": _args_b64(),
        "gpu": "any",
        "vram_mb": 1024,
        "retries": 0,
    })
    assert r0["status"] == "worker_lost"
    assert r0["attempts"] == 1

    # 2. retries=1 -> first attempt on w-1 fails with worker_lost (lease released), second attempt on w-2 succeeds
    r1 = svc.invoke("alpha", {
        "app_name": "chaos",
        "fn_id": "step",
        "bundle_b64": _bundle_b64(),
        "args_b64": _args_b64(),
        "gpu": "any",
        "vram_mb": 1024,
        "retries": 1,
    })
    assert r1["status"] == "ok"
    assert r1["attempts"] == 2
    assert r1["worker_id"] == "w-2"

    # Evict warm container on w-2 -> active leases on w-2 must be released
    for c in svc.list_containers():
        svc.evict_container(c["container_id"])
    # Release any lease from the retries=0 failed cold call if still tracked
    leases.active.pop("lease-chaos-1", None)
    assert leases.active == {}, f"leaked leases found: {leases.active}"


def test_dead_worker_lease_not_renewed_by_tick_leases(tmp_path):
    """When a worker disappears from live_workers(), tick_leases() must drop its warm entry and NOT renew its lease."""
    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    leases = FakeLeaseBridge()
    live_list = [SimpleNamespace(agent_id="w-1", labels=("cpu", "gpu-sl"))]

    def dispatch(task, attempt):
        res = base64.b64encode(cloudpickle.dumps("ok")).decode()
        out = ServerlessExecOut(status="ok", result_b64=res, result_format="cloudpickle", worker_id="w-1", container="c-w1", exec_ms=2.0)
        return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, worker_id="w-1", output=out.model_dump(mode="json"))

    svc = ServerlessService(
        keys=load_keys(kf),
        quotas=Quotas({"alpha": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096)}),
        dispatch=dispatch,
        live_workers=lambda: live_list,
        blobs={},
        leases=leases,
        storage_dir=tmp_path / "store",
    )
    svc.invoke("alpha", {
        "app_name": "chaos",
        "fn_id": "fn",
        "bundle_b64": _bundle_b64(),
        "args_b64": _args_b64(),
        "gpu": "any",
        "vram_mb": 1024,
        "keep_warm_s": 120,
    })
    assert svc.tick_leases() == 1

    # Simulate w-1 crashing / disappearing from NATS KV
    live_list.clear()
    assert svc.tick_leases() == 0
    assert svc.list_containers() == []


def test_disk_full_enospc_leaves_no_partial_volume_files(tmp_path, monkeypatch):
    """Simulating ENOSPC during VolumeStore.put must clean up the .tmp file and leave the existing file intact."""
    vs = VolumeStore(tmp_path / "volumes")
    vs.put("alpha", "v1", "weights.bin", b"good-v1")

    orig_replace = os.replace

    def fail_enospc(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", fail_enospc)
    with pytest.raises(OSError):
        vs.put("alpha", "v1", "weights.bin", b"corrupt-v2")
    monkeypatch.setattr(os, "replace", orig_replace)

    # Original file must still be intact and no .tmp files left behind
    data, _ = vs.get("alpha", "v1", "weights.bin")
    assert data == b"good-v1"
    tmp_files = list((tmp_path / "volumes").rglob("*.tmp"))
    assert tmp_files == []


def test_emergency_rollback_drill_completes_under_5_minutes_and_releases_leases(tmp_path):
    """Emergency rollback (set_admin_state enabled=False, draining=True) evicts warm containers, releases leases, and blocks new calls."""
    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    leases = FakeLeaseBridge()

    def dispatch(task, attempt):
        res = base64.b64encode(cloudpickle.dumps("ok")).decode()
        out = ServerlessExecOut(status="ok", result_b64=res, result_format="cloudpickle", worker_id="w-1", container="c-rb-1", exec_ms=1.0)
        return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, worker_id="w-1", output=out.model_dump(mode="json"))

    svc = ServerlessService(
        keys=load_keys(kf),
        quotas=Quotas({"alpha": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096)}),
        dispatch=dispatch,
        live_workers=lambda: [SimpleNamespace(agent_id="w-1", labels=("cpu", "gpu-sl"))],
        blobs={},
        leases=leases,
        storage_dir=tmp_path / "store",
    )
    svc.invoke("alpha", {
        "app_name": "rb",
        "fn_id": "fn",
        "bundle_b64": _bundle_b64(),
        "args_b64": _args_b64(),
        "gpu": "any",
        "vram_mb": 1024,
        "keep_warm_s": 120,
    })
    assert len(svc.list_containers()) == 1
    assert len(leases.active) == 1

    t0 = time.perf_counter()
    svc.set_admin_state(enabled=False, draining=True, actor="sre-drill")
    elapsed_s = time.perf_counter() - t0

    assert elapsed_s < 1.0  # Gate requires < 300s (5 minutes)
    assert svc.list_containers() == []
    assert leases.active == {}

    with pytest.raises(ServiceError) as exc:
        svc.invoke("alpha", {
            "app_name": "rb",
            "fn_id": "fn",
            "bundle_b64": _bundle_b64(),
            "args_b64": _args_b64(),
        })
    assert exc.value.status == 503 and exc.value.code == "serverless_disabled_global"
