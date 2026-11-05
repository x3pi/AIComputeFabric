# ruff: noqa: F811
"""Phase 5 gate: Full HTTP surface (/api/serverless/v1/*), immutable deploy v1->v2->rollback without interrupting in-flight calls,
JSON-only web endpoints, Volume tenant isolation/traversal/symlink/quota + Audit, ModelCache read-only for tenants,
and 1,000-call + idle-lease metering reconciliation verified by Go gcp.VerifyLedgerFile."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import cloudpickle
import pytest

from agents.sl_security import new_key_file, load_keys
from agents.tasks import TaskResult, TaskType
from serverless.ledger import UsageLedger
from serverless.quota import Quotas, TenantPolicy
from serverless.service import ServerlessService
from serverless.volumes import VolumeError
from tests.serverless.test_e2e import ADMIN, ROOT, nats_url, secrets, stack, worker  # noqa: F401
from tests.serverless.util import by_value

SDK = ROOT / "sdk/python"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))
import aicf  # noqa: E402

by_value(__name__)


def test_volume_tenant_isolation_traversal_symlink_and_quota(stack, tmp_path):
    alpha_c = aicf.Client(stack.gateway_url, stack.keys["alpha"])
    beta_c = aicf.Client(stack.gateway_url, stack.keys["beta"])

    vol_a = aicf.Volume("shared-name", client=alpha_c)
    vol_b = aicf.Volume("shared-name", client=beta_c)

    # Tenant alpha writes secret data
    res = vol_a.put("docs/alpha.txt", b"alpha-confidential-payload")
    assert res["size"] == len(b"alpha-confidential-payload")
    assert vol_a.get("docs/alpha.txt") == b"alpha-confidential-payload"

    # Tenant beta cannot see or read alpha's file even with the same volume name
    assert vol_b.ls() == []
    with pytest.raises(aicf.InvalidRequest):
        vol_b.get("docs/alpha.txt")

    # Path traversal via raw HTTP and unit store must be rejected and audited
    before_denied = len([e for e in stack.svc.audit.recent(200) if e["action"] == "volume_denied"])
    for bad_path in ("../../etc/passwd", "../beta/shared-name/x", "docs/../docs/alpha.txt", ".."):
        with pytest.raises(VolumeError) as exc_info:
            stack.svc.volumes.get(stack.tenants["alpha"], "shared-name", bad_path)
        assert exc_info.value.status == 403

    # Symlink escape inside volume directory must be rejected and audited
    vol_dir = stack.svc.volumes.root / stack.tenants["alpha"] / "shared-name"
    symlink_path = vol_dir / "escape_link"
    if symlink_path.exists() or os.path.islink(symlink_path):
        symlink_path.unlink()
    os.symlink("/etc/hostname", symlink_path)
    try:
        st, doc = stack.http("GET", "/api/serverless/v1/volumes/shared-name/escape_link", stack.keys["alpha"])
        assert st == 403 and doc["error"]["code"] == "volume_path_denied"
        st2, _ = stack.http("PUT", "/api/serverless/v1/volumes/shared-name/escape_link", stack.keys["alpha"], b"overwrite")
        assert st2 == 403
    finally:
        if os.path.islink(symlink_path):
            symlink_path.unlink()

    after_denied = len([e for e in stack.svc.audit.recent(200) if e["action"] == "volume_denied"])
    assert after_denied >= before_denied + 4, "denied volume accesses must be recorded in audit log"

    # Quota enforcement (max_volume_bytes)
    stack.svc.quotas.set_policy(stack.tenants["beta"], {"max_volume_bytes": 100})
    try:
        with pytest.raises(aicf.QuotaExceeded, match="volume_quota_exceeded"):
            vol_b.put("big.bin", b"x" * 200)
    finally:
        stack.svc.quotas.set_policy(stack.tenants["beta"], {"max_volume_bytes": 256 * 1024 * 1024})


def test_model_cache_readonly_for_tenants_and_writable_by_admin(stack):
    alpha_c = aicf.Client(stack.gateway_url, stack.keys["alpha"])
    mc = aicf.ModelCache("qwen3-tokenizer", client=alpha_c)

    # Tenant cannot write to ModelCache
    with pytest.raises(aicf.QuotaExceeded, match="model_cache_readonly"):
        mc.put("tokenizer.json", b"{}")

    # Admin populates ModelCache
    st, doc = stack.http("PUT", "/api/serverless/v1/model-cache/qwen3-tokenizer/tokenizer.json", ADMIN, b'{"vocab": 151936}')
    assert st == 200 and doc["size"] == len(b'{"vocab": 151936}')

    # Tenant can list and read from ModelCache
    files = mc.ls()
    assert len(files) == 1 and files[0]["path"] == "tokenizer.json"
    assert mc.get("tokenizer.json") == b'{"vocab": 151936}'


def test_deploy_v1_v2_rollback_web_endpoint_and_inflight_not_interrupted(stack):
    alpha_tid = stack.tenants["alpha"]
    alpha_key = stack.keys["alpha"]
    alpha_c = aicf.Client(stack.gateway_url, alpha_key)

    # Deploy v1
    app_v1 = aicf.App("web-svc", client=alpha_c)

    @app_v1.web_endpoint(requires_auth=True)
    @app_v1.function(keep_warm_s=15)
    def handle(payload):
        import time as _t
        if isinstance(payload, dict) and payload.get("sleep"):
            _t.sleep(float(payload["sleep"]))
        return {"v": 1, "echo": payload}

    dep1 = app_v1.deploy()
    v1 = dep1["active_version"]

    # Web endpoint requires auth and JSON-only (T10)
    assert stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", None, {"x": 1}, headers={"Content-Type": "application/json"})[0] == 401
    assert stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", stack.keys["beta"], {"x": 1}, headers={"Content-Type": "application/json"})[0] == 403
    assert stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", alpha_key, b"raw-bytes", headers={"Content-Type": "application/octet-stream"})[0] == 415

    st, r1 = stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", alpha_key, {"msg": "hi"}, headers={"Content-Type": "application/json"})
    assert st == 200 and r1["version"] == v1 and r1["result"] == {"v": 1, "echo": {"msg": "hi"}}

    # Start an in-flight v1 call while deploying v2
    inflight_out = {}

    def run_inflight_v1():
        inflight_out["res"] = stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", alpha_key, {"sleep": 0.6}, headers={"Content-Type": "application/json"})

    th = threading.Thread(target=run_inflight_v1, daemon=True)
    th.start()
    time.sleep(0.15)

    # Deploy v2 while v1 call is in flight
    app_v2 = aicf.App("web-svc", client=alpha_c)

    @app_v2.web_endpoint(requires_auth=True)
    @app_v2.function(keep_warm_s=15)
    def handle(payload):
        return {"v": 2, "echo": payload}

    dep2 = app_v2.deploy()
    v2 = dep2["active_version"]
    assert v2 != v1

    th.join(timeout=30)
    assert inflight_out["res"][0] == 200 and inflight_out["res"][1]["result"]["v"] == 1, "in-flight v1 call must finish without interruption"

    # Subsequent calls hit v2
    st, r2 = stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", alpha_key, {"msg": "v2"}, headers={"Content-Type": "application/json"})
    assert st == 200 and r2["version"] == v2 and r2["result"] == {"v": 2, "echo": {"msg": "v2"}}

    # Rollback to v1
    rb = app_v2.rollback()
    assert rb["active_version"] == v1
    st, r3 = stack.http("POST", f"/api/serverless/v1/web/{alpha_tid}/web-svc/handle", alpha_key, {"msg": "back"}, headers={"Content-Type": "application/json"})
    assert st == 200 and r3["version"] == v1 and r3["result"] == {"v": 1, "echo": {"msg": "back"}}


def test_containers_list_and_force_evict_records_audit(stack):
    alpha_c = aicf.Client(stack.gateway_url, stack.keys["alpha"])
    app = aicf.App("evict-demo", client=alpha_c)

    @app.function(keep_warm_s=120)
    def stay_warm(x: int) -> int:
        return x + 1

    assert stay_warm.remote(10) == 11
    st, cdoc = stack.http("GET", "/api/serverless/v1/containers", ADMIN)
    assert st == 200
    matching = [c for c in cdoc["containers"] if c["fn_id"] == "stay_warm"]
    assert len(matching) == 1
    cid = matching[0]["container_id"]

    # Force evict as admin
    st_del, ev_doc = stack.http("DELETE", f"/api/serverless/v1/containers/{cid}?reason=admin", ADMIN)
    assert st_del == 200 and ev_doc["evicted"] is True and ev_doc["reason"] == "admin"

    # Container is gone and audit has the evict entry
    st2, cdoc2 = stack.http("GET", "/api/serverless/v1/containers", ADMIN)
    assert st2 == 200 and not any(c["container_id"] == cid for c in cdoc2["containers"])
    evicts = [e for e in stack.svc.audit.recent(50) if e["action"] == "evict" and e["detail"].get("container_id") == cid]
    assert len(evicts) == 1 and evicts[0]["detail"]["reason"] == "admin"


def test_metering_1000_calls_plus_idle_leases_reconciliation_and_go_verify_ledger(tmp_path):
    """Run 1,000 GPU invocations + simulated idle lease holds, reconcile ledger totals within +-1% against worker truth,
    and verify the on-disk ledger file using Go's gcp.VerifyLedgerFile."""
    key_file = tmp_path / "hmac.key"
    new_key_file(key_file)
    ledger_path = tmp_path / "serverless_ledger.jsonl"
    ledger = UsageLedger(ledger_path, site="PC_001")

    now = [1000.0]
    worker_truth = {"gpu_ms": 0, "vram_mib_s": 0}

    class FakeBlobs:
        def __init__(self):
            self.d = {}
        def put(self, b):
            k = hashlib.sha256(b).hexdigest()
            self.d[k] = b
            return k
        def get(self, k):
            return self.d[k]

    class FakeLeaseBridge:
        def __init__(self):
            self.n = 0
        def request(self, **kw):
            self.n += 1
            return {"granted": True, "lease": {"lease_id": f"lease-{self.n}", "worker_id": "w-gpu-1"}}
        def extend(self, lid, ttl_ms=30000):
            return True
        def release(self, lid):
            return True

    def dispatch(task, attempt):
        exec_ms = 250.0  # 250 ms per call
        vram_mb = task.input["vram_mb"]
        worker_truth["gpu_ms"] += int(round(exec_ms))
        worker_truth["vram_mib_s"] += int(round(vram_mb * exec_ms / 1000.0))
        return TaskResult(
            task_id=task.task_id,
            type=TaskType.SERVERLESS_EXEC,
            worker_id="w-gpu-1",
            output={
                "status": "ok",
                "result_b64": base64.b64encode(cloudpickle.dumps(1)).decode(),
                "result_format": "cloudpickle",
                "cold_start": attempt == 1 and worker_truth["gpu_ms"] == 250,
                "exec_ms": exec_ms,
                "vram_peak_mb": vram_mb,
                "worker_id": "w-gpu-1",
                "container": "c-gpu-1",
            },
        )

    quotas = Quotas({"t-bench": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=8192, max_concurrency=32, max_keep_warm_s=600)})
    svc = ServerlessService(
        keys=load_keys(key_file),
        quotas=quotas,
        dispatch=dispatch,
        live_workers=lambda: [SimpleNamespace(agent_id="w-gpu-1", labels=["w-w-gpu-1", "gpu-sl"])],
        blobs=FakeBlobs(),
        clock=lambda: now[0],
        leases=FakeLeaseBridge(),
        ledger=ledger,
        storage_dir=tmp_path / "svc",
    )

    bundle = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": int, "enter": [], "exit": []})).decode()
    args = base64.b64encode(cloudpickle.dumps(((1,), {}))).decode()
    body = {"app_name": "bench", "fn_id": "gpu_fn", "bundle_b64": bundle, "args_b64": args, "gpu": "rtx3060", "vram_mb": 2000, "keep_warm_s": 60}

    for i in range(1000):
        if i > 0 and i % 100 == 0:
            # Advance clock by 2.0s idle between bursts -> container holds 2000 MiB lease idle for 2s = 4000 MiB*s
            now[0] += 2.0
            worker_truth["vram_mib_s"] += 2000 * 2
        svc.invoke("t-bench", body)

    # Also advance clock past keep_warm_s and tick_leases so final idle lease hold is recorded
    now[0] += 61.0
    worker_truth["vram_mib_s"] += 2000 * 61
    svc.tick_leases()

    totals = ledger.totals_by_tenant()["t-bench"]
    assert totals["calls"] == 1000
    assert totals["idle_records"] == 10  # 9 inter-burst idle intervals + 1 expiry release
    assert abs(totals["gpu_ms"] - worker_truth["gpu_ms"]) / worker_truth["gpu_ms"] <= 0.01
    assert abs(totals["vram_mib_s"] - worker_truth["vram_mib_s"]) / worker_truth["vram_mib_s"] <= 0.01

    # Verify the ledger file with Go's gcp.VerifyLedgerFile!
    go_verifier = tmp_path / "verify_test.go"
    go_verifier.write_text(f"""package gcp

import "testing"

func TestVerifyServerlessPythonLedger(t *testing.T) {{
	n, last, err := VerifyLedgerFile({json.dumps(str(ledger_path))})
	if err != nil {{
		t.Fatalf("VerifyLedgerFile failed: %v", err)
	}}
	if n != 1010 || last == "" {{
		t.Fatalf("want 1010 entries, got n=%d last=%s", n, last)
	}}
}}
""")
    target_test = ROOT / "apps/global-control-plane/serverless_ledger_verify_test.go"
    target_test.write_text(go_verifier.read_text())
    try:
        r = subprocess.run(["go", "test", "-count=1", "-run", "TestVerifyServerlessPythonLedger", "."],
                           cwd=ROOT / "apps/global-control-plane", capture_output=True, text=True, check=True)
        assert "ok" in r.stdout
    finally:
        target_test.unlink(missing_ok=True)

    rep = {
        "label": "[ĐO]",
        "calls": totals["calls"],
        "idle_lease_records": totals["idle_records"],
        "ledger_gpu_ms": totals["gpu_ms"],
        "worker_gpu_ms": worker_truth["gpu_ms"],
        "ledger_vram_mib_s": totals["vram_mib_s"],
        "worker_vram_mib_s": worker_truth["vram_mib_s"],
        "go_verify_ledger_file": "PASS (1010 hash-chained entries verified)",
    }
    (ROOT / "benchmarks/reports/p_serverless_5_metering.json").write_text(json.dumps(rep, indent=2) + "\n")
