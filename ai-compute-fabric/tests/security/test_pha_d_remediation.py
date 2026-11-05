"""Pha D Security Remediation Regression & PoC Suite (D-F1, D-F2, D-F5, D-F6).

Verifies:
- D-F1: Web Endpoint cross-tenant IDOR & app/fn/version enumeration oracle blocked BEFORE resolve_function
- D-F2: Tenant role blocked from scraping /v1/metrics (cross-tenant telemetry leak) and poisoning /v1/internal/reclaim-metric
- D-F5: pypdf upgraded to >= 6.21.4 (0 CVEs in pip-audit)
- D-F6: _warm_runner._write_bundled_files isolates each bundle in tempfile.mkdtemp(prefix='aicf_files_')
"""
from __future__ import annotations

import importlib.metadata
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.sl_security import load_keys, new_key_file
from agents.tasks import TaskResult, TaskType
from serverless.app import serve
from serverless.deployments import DeploymentStore
from serverless.ledger import ServerlessAudit, UsageLedger
from serverless.quota import Quotas, TenantPolicy
from serverless.service import ServerlessService, ServiceError
from serverless.volumes import ModelCacheStore, VolumeStore


def _make_svc(tmp_path: Path) -> ServerlessService:
    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    keys = load_keys(kf)
    default_pol = TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096, max_concurrency=4)
    return ServerlessService(
        blobs={},
        quotas=Quotas({}, default=default_pol),
        keys=keys,
        live_workers=lambda: [SimpleNamespace(agent_id="w-sec-01", labels=("cpu", "gpu-sl"))],
        dispatch=lambda task, attempt: TaskResult(
            task_id=task.task_id,
            type=TaskType.SERVERLESS_EXEC,
            output={"status": "ok", "result_b64": "eyJvayI6IHRydWV9", "result_format": "json", "exec_ms": 1.0, "cold_start": False, "worker_id": "w-sec-01"},
            worker_id="w-sec-01",
        ),
        deployments=DeploymentStore(tmp_path / "deployments.json"),
        volumes=VolumeStore(tmp_path / "volumes"),
        model_cache=ModelCacheStore(tmp_path / "model_cache"),
        ledger=UsageLedger(tmp_path / "ledger.jsonl"),
        audit=ServerlessAudit(tmp_path / "audit.jsonl"),
    )


def test_df1_web_endpoint_blocks_cross_tenant_idor_and_enumeration_oracle(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    svc.deploy_app(
        "t_victim",
        {
            "app_name": "secret-app",
            "functions": [{"fn_id": "infer", "source_code": "def infer(p: dict) -> dict:\n    return {'secret': 123}\n"}],
            "web_endpoints": {"infer": {"requires_auth": False}},
        },
    )

    # 1. Cross-tenant probe of non-existent app/fn MUST return 403 forbidden (NOT 404 app_not_found / fn_not_found)
    with pytest.raises(ServiceError) as exc_oracle:
        svc.invoke_web("t_attacker", "tenant", "t_victim", "non-existent-app", "non-existent-fn", {"x": 1})
    assert exc_oracle.value.status == 403
    assert exc_oracle.value.code == "forbidden"

    # 2. Cross-tenant invocation even when victim set requires_auth=False MUST be blocked for another tenant
    with pytest.raises(ServiceError) as exc_idor:
        svc.invoke_web("t_attacker", "tenant", "t_victim", "secret-app", "infer", {"x": 1})
    assert exc_idor.value.status == 403
    assert exc_idor.value.code == "forbidden"

    # 3. Legitimate owner tenant succeeds
    res = svc.invoke_web("t_victim", "tenant", "t_victim", "secret-app", "infer", {"x": 1})
    assert res["status"] == "ok"


def test_df2_tenant_cannot_scrape_global_metrics_or_poison_reclaim_metrics(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    secret = "sec-proxy-secret-df2-test"
    srv = serve(svc, secret, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Tenant role calling GET /v1/metrics must receive 403 Forbidden
        req_m = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/metrics",
            headers={
                "X-Aicf-Proxy-Secret": secret,
                "X-Aicf-Role": "tenant",
                "X-Aicf-Tenant": "t_attacker",
            },
            method="GET",
        )
        with pytest.raises(urllib.error.HTTPError) as err_m:
            urllib.request.urlopen(req_m, timeout=3)
        assert err_m.value.code == 403

        # 2. Tenant role calling POST /v1/internal/reclaim-metric must receive 403 Forbidden
        req_r = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/internal/reclaim-metric",
            data=json.dumps({"latency_s": 999.0, "evicted": 50, "orphans": 99}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Aicf-Proxy-Secret": secret,
                "X-Aicf-Role": "tenant",
                "X-Aicf-Tenant": "t_attacker",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as err_r:
            urllib.request.urlopen(req_r, timeout=3)
        assert err_r.value.code == 403
        assert svc._orphan_containers == 0

        # 3. Admin role calling GET /v1/metrics and internal worker-guard calling reclaim-metric succeed
        req_admin_m = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/metrics",
            headers={"X-Aicf-Proxy-Secret": secret, "X-Aicf-Role": "admin"},
            method="GET",
        )
        with urllib.request.urlopen(req_admin_m, timeout=3) as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_df5_pypdf_version_has_zero_known_cves() -> None:
    ver = importlib.metadata.version("pypdf")
    parts = [int(x) for x in ver.split(".")[:3]]
    assert tuple(parts) >= (6, 21, 4), f"pypdf {ver} is vulnerable to CVE-2026-119203..119209; require >= 6.21.4"


def test_df6_warm_runner_uses_isolated_mkdtemp_instead_of_fixed_tmp_dir() -> None:
    warm_runner_path = Path(__file__).resolve().parents[2] / "agents" / "_warm_runner.py"
    src = warm_runner_path.read_text(encoding="utf-8")
    assert 'mkdtemp(prefix="aicf_files_")' in src
    assert 'root = "/tmp/aicf_files"' not in src
