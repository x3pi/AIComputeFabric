"""Per-tenant policy for serverless (ADR-003 §2.5). A tenant NOT listed in the policy file has serverless DISABLED (safe default)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _py_secrets
import threading
from dataclasses import dataclass
from pathlib import Path


def _derive_master_key(master_secret: str | bytes | None = None) -> bytes:
    raw = master_secret or os.environ.get("AICF_SECRETS_MASTER_KEY") or "aicf-local-dev-secrets-key-v1"
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).digest()


def encrypt_secret_value(plaintext: str, master_secret: str | bytes | None = None) -> str:
    """Encrypt a secret value at rest using authenticated EtM (SHA256-CTR + HMAC-SHA256)."""
    if plaintext.startswith("enc:v1:"):
        return plaintext
    key = _derive_master_key(master_secret)
    enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
    mac_key = hmac.new(key, b"mac", hashlib.sha256).digest()
    nonce = _py_secrets.token_bytes(16)
    pt_bytes = plaintext.encode("utf-8")
    stream = bytearray()
    counter = 0
    while len(stream) < len(pt_bytes):
        block = hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    ct = bytes(a ^ b for a, b in zip(pt_bytes, stream[: len(pt_bytes)]))
    tag = hmac.new(mac_key, b"enc:v1:" + nonce + ct, hashlib.sha256).digest()
    blob = base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")
    return f"enc:v1:{blob}"


def decrypt_secret_value(stored: str, master_secret: str | bytes | None = None) -> str:
    """Decrypt an `enc:v1:...` secret value (or transparently return legacy plaintext during migration)."""
    if not isinstance(stored, str) or not stored.startswith("enc:v1:"):
        return str(stored)
    key = _derive_master_key(master_secret)
    enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
    mac_key = hmac.new(key, b"mac", hashlib.sha256).digest()
    raw = base64.urlsafe_b64decode(stored[len("enc:v1:") :].encode("ascii"))
    if len(raw) < 48:
        raise ValueError("corrupted encrypted secret")
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    expected_tag = hmac.new(mac_key, b"enc:v1:" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("secret authentication tag mismatch")
    stream = bytearray()
    counter = 0
    while len(stream) < len(ct):
        block = hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    pt = bytes(a ^ b for a, b in zip(ct, stream[: len(ct)]))
    return pt.decode("utf-8")


@dataclass(frozen=True)
class TenantPolicy:
    serverless_enabled: bool = False
    max_concurrency: int = 4
    max_memory_mb: int = 2048
    max_timeout_s: float = 120.0
    max_keep_warm_s: int = 300
    max_bundle_bytes: int = 8 * 1024 * 1024
    max_volume_bytes: int = 256 * 1024 * 1024
    allowed_data_classes: tuple = ("PUBLIC", "INTERNAL")
    gpu_allowed: bool = False
    max_vram_mb: int = 0
    max_retries: int = 0


class QuotaError(Exception):
    def __init__(self, code: str, message: str, status: int = 429, retry_after: int = 1):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


class Quotas:
    def __init__(self, policies: dict, default: TenantPolicy | None = None, secrets: dict | None = None):
        self.policies, self.default = policies, default or TenantPolicy()
        self.secrets: dict[str, dict[str, dict[str, str]]] = {}
        self._inflight: dict = {}
        self._mu = threading.Lock()
        for tid, sec_map in (secrets or {}).items():
            for sname, env_vars in (sec_map or {}).items():
                self.set_secret(tid, sname, env_vars)

    @classmethod
    def load(cls, path: str | Path) -> "Quotas":
        doc = json.loads(Path(path).read_text())
        pol = {}
        for tid, d in doc.get("tenants", {}).items():
            d = dict(d)
            if "allowed_data_classes" in d:
                d["allowed_data_classes"] = tuple(d["allowed_data_classes"])
            pol[tid] = TenantPolicy(**d)
        return cls(pol, secrets=doc.get("secrets", {}))

    def save(self, path: str | Path) -> None:
        """Persist policies and encrypted secrets atomically with fsync and 0600 mode."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._mu:
            tenants_doc = {}
            for tid, pol in self.policies.items():
                d = {f: getattr(pol, f) for f in pol.__dataclass_fields__}
                d["allowed_data_classes"] = list(d["allowed_data_classes"])
                tenants_doc[tid] = d
            doc = {"tenants": tenants_doc, "secrets": self.secrets}
            payload = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
        tmp = p.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(p)

    def set_policy(self, tenant: str, updates: dict) -> TenantPolicy:
        with self._mu:
            cur = self.policies.get(tenant, self.default)
            d = {f: getattr(cur, f) for f in cur.__dataclass_fields__}
            for k, v in updates.items():
                if k not in d:
                    raise ValueError(f"unknown policy field: {k}")
                d[k] = tuple(v) if k == "allowed_data_classes" else v
            pol = TenantPolicy(**d)
            self.policies[tenant] = pol
            return pol

    def set_secret(self, tenant: str, name: str, env_vars: dict[str, str]) -> None:
        with self._mu:
            self.secrets.setdefault(tenant, {})[name] = {
                str(k): encrypt_secret_value(str(v)) for k, v in env_vars.items()
            }

    def list_secrets_metadata(self, tenant: str) -> list[dict[str, object]]:
        """Return secret names and variable keys ONLY (never secret values)."""
        with self._mu:
            t_sec = self.secrets.get(tenant, {})
            return [
                {"name": name, "keys": sorted(env_map.keys())}
                for name, env_map in sorted(t_sec.items())
            ]

    def resolve_secrets(self, tenant: str, names: list[str]) -> dict[str, str]:
        with self._mu:
            t_sec = self.secrets.get(tenant, {})
            merged: dict[str, str] = {}
            for name in names:
                if name not in t_sec:
                    raise QuotaError("secret_not_found", f"secret {name!r} is not defined for tenant {tenant}", 404, 0)
                for k, v in t_sec[name].items():
                    merged[k] = decrypt_secret_value(v)
            return merged

    def policy(self, tenant: str) -> TenantPolicy:
        return self.policies.get(tenant, self.default)

    def check(self, tenant: str, req: dict) -> TenantPolicy:
        p = self.policy(tenant)
        if not p.serverless_enabled:
            raise QuotaError("serverless_disabled", f"serverless is not enabled for tenant {tenant}", 403)
        if req["data_class"] not in p.allowed_data_classes or req["data_class"] == "RESTRICTED":
            raise QuotaError("data_class_denied", f"data_class {req['data_class']} is not allowed (RESTRICTED never runs on shared GPUs)", 403)
        wants_gpu = req.get("gpu") is not None or req.get("vram_mb", 0) > 0
        if wants_gpu and not p.gpu_allowed:
            raise QuotaError("gpu_denied", "GPU functions are not enabled for this tenant", 403)
        for field_, limit, name in (("memory_mb", p.max_memory_mb, "max_memory_mb"), ("timeout_s", p.max_timeout_s, "max_timeout_s"),
                                    ("keep_warm_s", p.max_keep_warm_s, "max_keep_warm_s"), ("vram_mb", p.max_vram_mb, "max_vram_mb"),
                                    ("retries", p.max_retries, "max_retries")):
            if req.get(field_, 0) > limit:
                raise QuotaError("quota_exceeded", f"{field_}={req[field_]} exceeds {name}={limit}", 429, 0)
        return p

    def acquire(self, tenant: str, p: TenantPolicy) -> None:
        with self._mu:
            n = self._inflight.get(tenant, 0)
            if n >= p.max_concurrency:
                raise QuotaError("concurrency_exceeded", f"max_concurrency={p.max_concurrency} reached; retry shortly", 429, 1)
            self._inflight[tenant] = n + 1

    def release(self, tenant: str) -> None:
        with self._mu:
            n = self._inflight.get(tenant, 0)
            if n <= 1:
                self._inflight.pop(tenant, None)
            else:
                self._inflight[tenant] = n - 1

    def inflight(self, tenant: str) -> int:
        with self._mu:
            return self._inflight.get(tenant, 0)
