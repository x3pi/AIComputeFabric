"""Pha B3 & B5 Resilience Tests:
- Atomic write + fsync under SIGKILL (kill -9) for DeploymentStore, Quotas, and UsageLedger
- aicf-admin backup / verify / restore CLI with SHA-256 manifest & hash-chain verification
- Secrets encryption at rest (enc:v1:...) and metadata masking
"""
from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import shutil
import signal
import subprocess
import time

import pytest

from serverless.deployments import DeploymentStore
from serverless.ledger import ServerlessAudit, UsageLedger
from serverless.quota import Quotas, TenantPolicy, decrypt_secret_value, encrypt_secret_value

ROOT = pathlib.Path(__file__).resolve().parents[2]
GW_DIR = ROOT / "apps" / "site-gateway"


@pytest.fixture(scope="module")
def aicf_admin_bin(tmp_path_factory) -> pathlib.Path:
    out = tmp_path_factory.mktemp("bin") / "aicf-admin"
    subprocess.run(["go", "build", "-o", str(out), "./cmd/aicf-admin"], cwd=str(GW_DIR), check=True)
    return out


def _writer_loop(data_dir_str: str) -> None:
    data_dir = pathlib.Path(data_dir_str)
    ds = DeploymentStore(data_dir / "deployments.json")
    ledger = UsageLedger(data_dir / "usage_ledger.jsonl", site="SITE-A")
    audit = ServerlessAudit(data_dir / "serverless_audit.jsonl")
    quotas = Quotas({"t_alpha": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096)})
    i = 0
    while True:
        i += 1
        ds.deploy(
            "t_alpha",
            "app_resilience",
            [{"fn_id": "classify", "source_code": f"def classify(x):\n    return {{'iter': {i}}}\n"}],
        )
        ledger.record_call("t_alpha", f"call-{i}", gpu_ms=120.0, vram_mb=1024)
        audit.record("deploy", tenant="t_alpha", actor="tester", detail={"iter": i})
        quotas.set_secret("t_alpha", "hf-secret", {"HF_TOKEN": f"hf_super_secret_{i}"})
        quotas.save(data_dir / "quotas.json")


def test_kill_minus_9_atomic_persistence_and_aicf_admin_backup_restore(tmp_path: pathlib.Path, aicf_admin_bin: pathlib.Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Seed valid tenants.json and audit.jsonl (Go format)
    tenants_doc = [
        {
            "id": "t_alpha",
            "name": "alpha",
            "key_sha256": "a" * 64,
            "created_at": "2026-11-05T10:00:00Z",
        }
    ]
    (data_dir / "tenants.json").write_text(json.dumps(tenants_doc), encoding="utf-8")

    # Spawn writer process and kill -9 while actively writing
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_writer_loop, args=(str(data_dir),))
    proc.start()

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if (data_dir / "deployments.json").exists() and (data_dir / "usage_ledger.jsonl").exists() and (data_dir / "quotas.json").exists():
            if (data_dir / "usage_ledger.jsonl").stat().st_size > 300:
                break
        time.sleep(0.02)

    assert proc.pid is not None
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5.0)
    assert proc.exitcode == -signal.SIGKILL

    # Remove any leftover .tmp file if SIGKILL hit before os.replace (target files must be 100% intact)
    for tmp_f in data_dir.glob("*.tmp"):
        tmp_f.unlink(missing_ok=True)

    # Reload Python stores after SIGKILL — zero JSON corruption, zero broken hash chain
    reloaded_ds = DeploymentStore(data_dir / "deployments.json")
    assert len(reloaded_ds.list_apps("t_alpha")) == 1
    reloaded_ledger = UsageLedger(data_dir / "usage_ledger.jsonl", site="SITE-A")
    cnt, last_hash = reloaded_ledger.verify()
    assert cnt >= 1 and len(last_hash) == 64
    audit_cnt, _ = ServerlessAudit.verify_file(data_dir / "serverless_audit.jsonl")
    assert audit_cnt >= 1

    # Verify via aicf-admin verify
    r_ver = subprocess.run([str(aicf_admin_bin), "verify", "-data-dir", str(data_dir)], capture_output=True, text=True, check=True)
    ver_doc = json.loads(r_ver.stdout)
    assert ver_doc["ok"] is True
    assert ver_doc["tenants_count"] == 1
    assert ver_doc["ledger_entries"] == cnt

    # Create backup via aicf-admin backup
    backup_tar = tmp_path / "backup" / "aicf-state.tar.gz"
    subprocess.run(
        [str(aicf_admin_bin), "backup", "-data-dir", str(data_dir), "-out", str(backup_tar)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert backup_tar.exists()

    # Corrupt tenants.json and usage_ledger.jsonl -> verify must fail
    (data_dir / "tenants.json").write_text("{corrupted_json", encoding="utf-8")
    r_bad = subprocess.run([str(aicf_admin_bin), "verify", "-data-dir", str(data_dir)], capture_output=True, text=True)
    assert r_bad.returncode != 0

    # Wipe data_dir and restore from backup archive
    shutil.rmtree(data_dir)
    r_rest = subprocess.run(
        [str(aicf_admin_bin), "restore", "-in", str(backup_tar), "-data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    rest_doc = json.loads(r_rest.stdout)
    assert rest_doc["ok"] is True
    assert rest_doc["tenants_count"] == 1
    assert rest_doc["ledger_entries"] == cnt


def test_secrets_encrypted_at_rest_and_masked_in_metadata(tmp_path: pathlib.Path) -> None:
    q = Quotas({"t_sec": TenantPolicy(serverless_enabled=True)})
    raw_secret = "sk-live-top-secret-value-998877"
    q.set_secret("t_sec", "openai-prod", {"OPENAI_API_KEY": raw_secret, "ORG_ID": "org-123"})

    q_file = tmp_path / "quotas.json"
    q.save(q_file)

    disk_text = q_file.read_text("utf-8")
    assert raw_secret not in disk_text
    assert "org-123" not in disk_text
    assert "enc:v1:" in disk_text

    # Metadata listing only exposes secret name and env var keys, never values
    meta = q.list_secrets_metadata("t_sec")
    assert meta == [{"name": "openai-prod", "keys": ["OPENAI_API_KEY", "ORG_ID"]}]
    assert raw_secret not in json.dumps(meta)

    # Reload from disk and resolve secrets
    q2 = Quotas.load(q_file)
    resolved = q2.resolve_secrets("t_sec", ["openai-prod"])
    assert resolved == {"OPENAI_API_KEY": raw_secret, "ORG_ID": "org-123"}

    # Tampering with ciphertext must fail authentication tag check
    ct = encrypt_secret_value("sensitive-token")
    tampered = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError, match="authentication tag mismatch|corrupted"):
        decrypt_secret_value(tampered)

