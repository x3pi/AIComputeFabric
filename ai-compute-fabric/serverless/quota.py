"""Per-tenant policy for serverless (ADR-003 §2.5). A tenant NOT listed in the policy file has serverless DISABLED (safe default)."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path


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
        self.secrets: dict[str, dict[str, dict[str, str]]] = dict(secrets or {})
        self._inflight: dict = {}
        self._mu = threading.Lock()

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
            self.secrets.setdefault(tenant, {})[name] = {str(k): str(v) for k, v in env_vars.items()}

    def resolve_secrets(self, tenant: str, names: list[str]) -> dict[str, str]:
        with self._mu:
            t_sec = self.secrets.get(tenant, {})
            merged: dict[str, str] = {}
            for name in names:
                if name not in t_sec:
                    raise QuotaError("secret_not_found", f"secret {name!r} is not defined for tenant {tenant}", 404, 0)
                merged.update(t_sec[name])
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
