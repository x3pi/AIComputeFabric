"""Serverless service core (ADR-003 D1-D3, D8, Phase 3-5): validate -> authorise -> sign -> dispatch on the task bus -> map the answer.

`ServerlessService.invoke(tenant, body)` is transport-free so it is unit-testable; serverless/app.py wraps it in HTTP.
Dispatch is injected (`dispatch(task, attempt) -> TaskResult`; production = BusExecutor.execute), as are the live-worker query
and the blob store. Nothing here ever unpickles user data."""
from __future__ import annotations

import base64
import json
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agents.env_cache import EnvError, EnvSpec
from agents.sl_security import sha256_hex, sign
from agents.sl_store import MAX_BLOB_BYTES, MAX_INLINE_BYTES, BlobError
from agents.tasks import ServerlessExecOut, Task, TaskType
from serverless.deployments import DeploymentError, DeploymentStore
from serverless.ledger import ServerlessAudit, UsageLedger
from serverless.metrics import Metrics
from serverless.quota import QuotaError, Quotas
from serverless.volumes import ModelCacheStore, VolumeStore

IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ALLOWED_REQ = {"app_name", "fn_id", "kind", "method", "env", "bundle_b64", "args_b64", "args_format", "result_format", "gpu", "vram_mb",
               "memory_mb", "cpus", "timeout_s", "keep_warm_s", "retries", "data_class", "env_vars", "secrets", "volumes", "version"}
INFRA_FAILURES = {"worker_lost", "duplicate", "evicted"}      # nothing the function did: safe to retry when the function opted in (D8)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int = 0):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": str(self)}}


@dataclass
class WarmEntry:
    worker_id: str
    expires: float
    lease_id: Optional[str] = None
    container_id: str = ""
    tenant_id: str = ""
    app_name: str = ""
    fn_id: str = ""
    vram_mb: int = 0
    last_active: float = 0.0


class HttpLeaseClient:
    """Internal HTTP bridge to site-gateway LeaseManager (ADR-003 D-LEASE-HTTP)."""

    def __init__(self, gateway_url: str, secret: str, timeout_s: float = 2.0):
        self.base = gateway_url.rstrip("/") + "/api/serverless/v1/_lease"
        self.secret = secret
        self.timeout_s = timeout_s

    def _post(self, path: str, payload: dict) -> dict:
        import urllib.request
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "X-Aicf-Proxy-Secret": self.secret})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read())

    def request(self, *, idempotency_key: str, tenant_id: str, vram_bytes: int, ttl_ms: int = 30000, constraint: str = "") -> dict:
        return self._post("/request", {"idempotency_key": idempotency_key, "tenant_id": tenant_id, "workload_kind": "serverless",
                                       "constraint": constraint, "vram_required_bytes": int(vram_bytes), "ttl_ms": int(ttl_ms)})

    def extend(self, lease_id: str, ttl_ms: int = 30000) -> bool:
        try:
            return bool(self._post("/extend", {"lease_id": lease_id, "ttl_ms": int(ttl_ms)}).get("ok"))
        except Exception:
            return False

    def release(self, lease_id: str) -> bool:
        try:
            return bool(self._post("/release", {"lease_id": lease_id}).get("released"))
        except Exception:
            return False


class ServerlessService:
    def __init__(self, *, keys: list, quotas: Quotas, dispatch: Callable, live_workers: Callable, blobs, metrics: Optional[Metrics] = None,
                 clock: Callable = time.time, cold_allowance_s: float = 180.0, leases=None,
                 volumes: Optional[VolumeStore] = None, model_cache: Optional[ModelCacheStore] = None,
                 deployments: Optional[DeploymentStore] = None, ledger: Optional[UsageLedger] = None,
                 audit: Optional[ServerlessAudit] = None, storage_dir: Optional[Path] = None):
        self.keys, self.quotas, self.dispatch, self.live_workers, self.blobs = keys, quotas, dispatch, live_workers, blobs
        self.metrics, self.clock, self.cold_allowance_s = metrics or Metrics(), clock, cold_allowance_s
        self.leases = leases
        self._warm: dict = {}
        self._warm_extra: dict[tuple, dict[str, WarmEntry]] = {}
        self._warm_busy: dict[tuple, dict[str, int]] = {}
        self._calls: dict[str, dict[str, Any]] = {}
        self._mu = threading.Lock()
        self.enabled: bool = True
        self.draining: bool = False
        self._orphan_containers: int = 0
        self.metrics.set_gauge("sl_orphan_containers", "Orphan containers detected outside WarmDockerPool", 0.0)
        self.metrics.set_gauge("sl_enabled", "Whether serverless service is globally enabled (1=enabled, 0=disabled)", 1.0)
        self.metrics.set_gauge("sl_draining", "Whether serverless service is draining (1=draining, 0=normal)", 0.0)

        base_dir = Path(storage_dir) if storage_dir else Path(tempfile.mkdtemp(prefix="aicf-sl-svc-"))
        self.audit = audit or ServerlessAudit(base_dir / "audit.jsonl")
        self.ledger = ledger or UsageLedger(base_dir / "ledger.jsonl")
        self.deployments = deployments or DeploymentStore(base_dir / "deployments.json")
        self.volumes = volumes or VolumeStore(
            base_dir / "volumes",
            on_denied=lambda t, v, p, reason: self.audit.record("volume_denied", tenant=t, actor=t, detail={"volume": v, "path": p, "reason": reason}),
        )
        self.model_cache = model_cache or ModelCacheStore(base_dir / "model_cache")

    # ---- request parsing ------------------------------------------------------------------------------------
    def _resolve_deployed_if_needed(self, tenant: str, body: dict) -> dict:
        if not isinstance(body, dict):
            return body
        if "bundle_b64" in body or "app_name" not in body or "fn_id" not in body:
            return body
        try:
            fndef, ver, _ = self.deployments.resolve_function(tenant, str(body["app_name"]), str(body["fn_id"]), body.get("version"))
        except DeploymentError as e:
            raise ServiceError(e.status, e.code, str(e)) from None
        merged = {k: v for k, v in fndef.items() if k in ALLOWED_REQ}
        merged.update(body)
        merged.pop("version", None)
        if "args_b64" not in merged:
            payload = merged.pop("payload", None)
            args_json = json.dumps({"args": [payload] if payload is not None else [], "kwargs": {}}).encode("utf-8")
            merged["args_b64"] = base64.b64encode(args_json).decode()
            merged.setdefault("args_format", "json")
            merged.setdefault("result_format", "json")
        else:
            merged.pop("payload", None)
        return merged

    @staticmethod
    def _parse(body: dict) -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "bad_request", "body must be a JSON object")
        extra = set(body) - ALLOWED_REQ
        if extra:
            raise ServiceError(400, "bad_request", f"unknown fields: {sorted(extra)}")
        req = {"kind": "function", "method": None, "env": {}, "args_format": "cloudpickle", "result_format": "cloudpickle", "gpu": None,
               "vram_mb": 0, "memory_mb": 512, "cpus": 1.0, "timeout_s": 30.0, "keep_warm_s": 60, "retries": 0, "data_class": "INTERNAL",
               "env_vars": {}, "secrets": [], "volumes": {}, "version": None, **body}
        for k in ("app_name", "fn_id"):
            if not isinstance(req.get(k), str) or not IDENT.match(req[k]):
                raise ServiceError(400, "bad_request", f"{k} must match {IDENT.pattern}")
        for k in ("bundle_b64", "args_b64"):
            if not isinstance(req.get(k), str):
                raise ServiceError(400, "bad_request", f"{k} (base64 string) is required")
        if req["data_class"] not in ("PUBLIC", "INTERNAL", "RESTRICTED"):
            raise ServiceError(400, "bad_request", "data_class must be PUBLIC, INTERNAL or RESTRICTED")
        if req["kind"] == "cls" and not (isinstance(req["method"], str) and IDENT.match(req["method"])):
            raise ServiceError(400, "bad_request", "method is required for kind=cls")
        if not isinstance(req["secrets"], list) or not all(isinstance(s, str) and IDENT.match(s) for s in req["secrets"]):
            raise ServiceError(400, "bad_request", "secrets must be a list of secret names")
        for k, lo, hi in (("vram_mb", 0, 24576), ("memory_mb", 64, 16384), ("cpus", 0.1, 16), ("timeout_s", 0.1, 600), ("keep_warm_s", 0, 3600), ("retries", 0, 5)):
            v = req[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
                raise ServiceError(400, "bad_request", f"{k} must be a number in [{lo}, {hi}]")
        try:
            req["bundle"] = base64.b64decode(req["bundle_b64"], validate=True)
            req["args"] = base64.b64decode(req["args_b64"], validate=True)
        except Exception:
            raise ServiceError(400, "bad_request", "bundle_b64/args_b64 must be valid base64") from None
        return req

    # ---- scheduling -------------------------------------------------------------------------------------------
    def _warm_key(self, tenant: str, env_hash: str, fn_id: str, req: dict) -> tuple:
        return (tenant, env_hash, fn_id, req["memory_mb"], req["cpus"], req["vram_mb"], bool(req["gpu"] or req["vram_mb"]))

    def _pinned_worker(self, wk: tuple, live_ids: set) -> tuple[Optional[str], Optional[str]]:
        stale_leases: list[tuple[str, str, float, int]] = []
        now = self.clock()
        res: tuple[Optional[str], Optional[str]] = (None, None)
        idle_to_record: Optional[tuple[str, str, float, int]] = None
        with self._mu:
            busy_map = self._warm_busy.setdefault(wk, {})
            e = self._warm.get(wk)
            if e and (e.expires <= now or e.worker_id not in live_ids):
                self._warm.pop(wk, None)
                if e.lease_id:
                    stale_leases.append((e.tenant_id, e.lease_id, max(0.0, now - (e.last_active or now)), e.vram_mb))
                e = None
            extras = self._warm_extra.get(wk) or {}
            for wid, ex in list(extras.items()):
                if ex.expires <= now or wid not in live_ids:
                    extras.pop(wid, None)
                    if ex.lease_id and (not e or ex.lease_id != e.lease_id):
                        stale_leases.append((ex.tenant_id, ex.lease_id, max(0.0, now - (ex.last_active or now)), ex.vram_mb))
            if e is None and extras:
                e = next(iter(extras.values()))
                self._warm[wk] = e
            candidates: list[WarmEntry] = []
            if e is not None:
                candidates.append(e)
            for wid, ex in extras.items():
                if not e or wid != e.worker_id:
                    candidates.append(ex)
            if candidates:
                best = min(candidates, key=lambda c: busy_map.get(c.worker_id, 0))
                best_busy = busy_map.get(best.worker_id, 0)
                if best_busy == 0 or len(candidates) >= len(live_ids) or best.lease_id:
                    if best.lease_id and best.vram_mb > 0 and best.last_active > 0 and now > best.last_active and best_busy == 0:
                        idle_to_record = (best.tenant_id, best.lease_id, now - best.last_active, best.vram_mb)
                    best.last_active = now
                    busy_map[best.worker_id] = best_busy + 1
                    res = (best.worker_id, best.lease_id)
        for tid, lid, idle_s, vram_mb in stale_leases:
            if idle_s > 0 and vram_mb > 0:
                self.ledger.record_idle_lease(tid, lid, idle_s, vram_mb)
            if self.leases:
                self.leases.release(lid)
        if idle_to_record:
            self.ledger.record_idle_lease(*idle_to_record)
        return res

    def _remember(self, wk: tuple, worker_id: str, keep_warm_s: int, lease_id: Optional[str] = None,
                  tenant: str = "", app_name: str = "", fn_id: str = "", vram_mb: int = 0, container_id: str = "",
                  pinned_from: Optional[str] = None) -> None:
        old_lease = None
        old_idle: Optional[tuple[str, str, float, int]] = None
        now = self.clock()
        with self._mu:
            prev = self._warm.get(wk)
            extras = self._warm_extra.setdefault(wk, {})
            if keep_warm_s > 0 and worker_id:
                prev_w = extras.get(worker_id) or (prev if prev and prev.worker_id == worker_id else None)
                keep_lid = lease_id or (prev_w.lease_id if prev_w else None)
                if prev_w and prev_w.lease_id and prev_w.lease_id != keep_lid:
                    old_lease = prev_w.lease_id
                cid = container_id or (prev_w.container_id if prev_w else f"sl-{worker_id}-{fn_id or wk[2]}")
                entry = WarmEntry(
                    worker_id=worker_id,
                    expires=now + keep_warm_s,
                    lease_id=keep_lid,
                    container_id=cid,
                    tenant_id=tenant or wk[0],
                    app_name=app_name,
                    fn_id=fn_id or wk[2],
                    vram_mb=vram_mb or int(wk[5]),
                    last_active=now,
                )
                self._warm[wk] = entry
                if pinned_from and pinned_from != worker_id:
                    extras.pop(pinned_from, None)
                extras[worker_id] = entry
            else:
                if prev and prev.lease_id:
                    old_lease = prev.lease_id
                    if prev.vram_mb > 0 and prev.last_active > 0 and now > prev.last_active:
                        old_idle = (prev.tenant_id, prev.lease_id, now - prev.last_active, prev.vram_mb)
                elif lease_id:
                    old_lease = lease_id
                self._warm.pop(wk, None)
                if worker_id:
                    extras.pop(worker_id, None)
        if old_idle:
            self.ledger.record_idle_lease(*old_idle)
        if old_lease and self.leases:
            self.leases.release(old_lease)

    def tick_leases(self) -> int:
        """Renew container leases (10 s cadence, 30 s TTL) ONLY while the worker is still in live_workers() and the warm entry is unexpired."""
        if not self.leases:
            return 0
        live_ids = {a.agent_id for a in self.live_workers()}
        now = self.clock()
        to_extend, to_release = [], []
        idle_records = []
        with self._mu:
            for wk, e in list(self._warm.items()):
                if e.expires <= now or e.worker_id not in live_ids:
                    self._warm.pop(wk, None)
                    if wk in self._warm_extra:
                        self._warm_extra[wk].pop(e.worker_id, None)
                    if e.lease_id:
                        to_release.append(e.lease_id)
                        if e.vram_mb > 0 and e.last_active > 0 and now > e.last_active:
                            idle_records.append((e.tenant_id, e.lease_id, now - e.last_active, e.vram_mb))
                elif e.lease_id:
                    to_extend.append((wk, e.lease_id))
        for rec in idle_records:
            self.ledger.record_idle_lease(*rec)
        for lid in to_release:
            self.leases.release(lid)
        renewed = 0
        for wk, lid in to_extend:
            if self.leases.extend(lid, ttl_ms=30000):
                renewed += 1
            else:
                with self._mu:
                    self._warm.pop(wk, None)
        return renewed

    def warm_entries(self) -> int:
        with self._mu:
            now = self.clock()
            return sum(1 for e in self._warm.values() if e.expires > now)

    def list_containers(self, tenant: Optional[str] = None) -> list[dict]:
        now = self.clock()
        out = []
        seen = set()
        with self._mu:
            all_entries = list(self._warm.items())
            for wk, extras in self._warm_extra.items():
                for wid, ex in extras.items():
                    all_entries.append((wk, ex))
            for wk, e in all_entries:
                if e.expires <= now:
                    continue
                t_id = e.tenant_id or wk[0]
                if tenant and t_id != tenant:
                    continue
                cid = e.container_id or f"sl-{e.worker_id}-{e.fn_id or wk[2]}"
                if (cid, e.worker_id) in seen:
                    continue
                seen.add((cid, e.worker_id))
                out.append({
                    "container_id": cid,
                    "tenant_id": t_id,
                    "app_name": e.app_name or "app",
                    "fn_id": e.fn_id or wk[2],
                    "worker_id": e.worker_id,
                    "vram_mb": e.vram_mb if e.vram_mb else int(wk[5]),
                    "ttl_remaining_s": max(0, int(round(e.expires - now))),
                    "lease_id": e.lease_id,
                })
        out.sort(key=lambda x: (x["tenant_id"], x["fn_id"], x["container_id"]))
        return out

    def evict_container(self, container_id: str, *, actor: str = "admin", role: str = "admin",
                        tenant: Optional[str] = None, reason: str = "admin") -> dict:
        now = self.clock()
        found: Optional[WarmEntry] = None
        found_wk: Optional[tuple] = None
        with self._mu:
            for wk, e in list(self._warm.items()):
                cid = e.container_id or f"sl-{e.worker_id}-{e.fn_id or wk[2]}"
                if cid == container_id or e.worker_id == container_id:
                    if role == "tenant" and (e.tenant_id or wk[0]) != tenant:
                        raise ServiceError(403, "forbidden", "cannot evict another tenant's container")
                    found, found_wk = e, wk
                    self._warm.pop(wk, None)
                    if wk in self._warm_extra:
                        self._warm_extra[wk].pop(e.worker_id, None)
                    break
            if found is None:
                for wk, extras in list(self._warm_extra.items()):
                    for wid, e in list(extras.items()):
                        cid = e.container_id or f"sl-{e.worker_id}-{e.fn_id or wk[2]}"
                        if cid == container_id or wid == container_id:
                            if role == "tenant" and (e.tenant_id or wk[0]) != tenant:
                                raise ServiceError(403, "forbidden", "cannot evict another tenant's container")
                            found, found_wk = e, wk
                            extras.pop(wid, None)
                            break
                    if found:
                        break
        if found is None:
            raise ServiceError(404, "not_found", f"container {container_id!r} not found")
        if found.lease_id:
            if found.vram_mb > 0 and found.last_active > 0 and now > found.last_active:
                self.ledger.record_idle_lease(found.tenant_id or (found_wk[0] if found_wk else ""), found.lease_id, now - found.last_active, found.vram_mb)
            if self.leases:
                self.leases.release(found.lease_id)
        t_id = found.tenant_id or (found_wk[0] if found_wk else "-")
        self.audit.record("evict", tenant=t_id, actor=actor, detail={
            "container_id": container_id,
            "worker_id": found.worker_id,
            "fn_id": found.fn_id,
            "reason": reason,
            "lease_id": found.lease_id,
        })
        return {"evicted": True, "container_id": container_id, "worker_id": found.worker_id, "lease_id": found.lease_id, "reason": reason}

    def set_admin_state(self, *, enabled: Optional[bool] = None, draining: Optional[bool] = None, actor: str = "admin") -> dict:
        if enabled is not None:
            self.enabled = bool(enabled)
        if draining is not None:
            self.draining = bool(draining)
            if self.draining:
                for c in list(self.list_containers()):
                    try:
                        self.evict_container(c["container_id"], actor=actor, role="admin", reason="reclaim")
                    except ServiceError:
                        pass
        self.audit.record("admin_state", tenant="-", actor=actor, detail={"enabled": self.enabled, "draining": self.draining})
        return {"enabled": self.enabled, "draining": self.draining}

    # ---- async spawn & calls --------------------------------------------------------------------------------
    def spawn(self, tenant: str, body: dict) -> dict:
        if not self.enabled:
            raise ServiceError(503, "serverless_disabled_global", "serverless execution is globally disabled by administrator", 5)
        if self.draining:
            raise ServiceError(503, "serverless_draining", "serverless service is draining; no new calls accepted", 5)
        if not IDENT.match(tenant or ""):
            raise ServiceError(401, "unauthorized", "tenant identity missing")
        resolved = self._resolve_deployed_if_needed(tenant, body)
        req = self._parse(resolved)
        try:
            policy = self.quotas.check(tenant, req)
        except QuotaError as e:
            self.metrics.inc("sl_quota_rejections_total", "Calls rejected by tenant policy", reason=e.code)
            raise ServiceError(e.status, e.code, str(e), e.retry_after) from None
        if len(req["bundle"]) > min(policy.max_bundle_bytes, MAX_BLOB_BYTES) or len(req["args"]) > MAX_BLOB_BYTES:
            raise ServiceError(413, "payload_too_large", f"bundle/args exceed {MAX_BLOB_BYTES} bytes (use a Volume for large data)")
        if req["secrets"]:
            try:
                self.quotas.resolve_secrets(tenant, req["secrets"])
            except QuotaError as e:
                raise ServiceError(e.status, e.code, str(e), e.retry_after) from None

        call_id = uuid.uuid4().hex
        done_ev = threading.Event()
        cancel_ev = threading.Event()
        rec = {
            "call_id": call_id,
            "tenant_id": tenant,
            "app_name": req["app_name"],
            "fn_id": req["fn_id"],
            "status": "pending",
            "created_at": time.time(),
            "stdout": "",
            "stderr": "",
            "_done": done_ev,
            "_cancel": cancel_ev,
        }
        with self._mu:
            self._calls[call_id] = rec
        self.audit.record("spawn", tenant=tenant, actor=tenant, detail={"call_id": call_id, "app_name": req["app_name"], "fn_id": req["fn_id"]})

        def _bg():
            with self._mu:
                if cancel_ev.is_set():
                    rec["status"] = "cancelled"
                    done_ev.set()
                    return
                rec["status"] = "running"
            try:
                out = self.invoke(tenant, resolved, _call_id=call_id)
                with self._mu:
                    if cancel_ev.is_set():
                        rec["status"] = "cancelled"
                    else:
                        rec.update(out)
            except ServiceError as e:
                with self._mu:
                    if not cancel_ev.is_set():
                        rec["status"] = "error"
                        rec["error"] = {"type": e.code, "message": str(e), "traceback": ""}
            except Exception as e:
                with self._mu:
                    if not cancel_ev.is_set():
                        rec["status"] = "error"
                        rec["error"] = {"type": type(e).__name__, "message": str(e), "traceback": ""}
            finally:
                done_ev.set()

        threading.Thread(target=_bg, daemon=True, name=f"sl-spawn-{call_id[:8]}").start()
        return {"call_id": call_id, "status": "pending"}

    def get_call(self, tenant: str, role: str, call_id: str, wait_s: float = 0.0) -> dict:
        with self._mu:
            rec = self._calls.get(call_id)
        if rec is None:
            raise ServiceError(404, "not_found", f"call {call_id!r} not found")
        if role == "tenant" and rec["tenant_id"] != tenant:
            raise ServiceError(403, "forbidden", "cannot access another tenant's call")
        if wait_s > 0 and rec["status"] in ("pending", "running"):
            rec["_done"].wait(timeout=min(wait_s, 30.0))
        with self._mu:
            out = {k: v for k, v in rec.items() if not k.startswith("_")}
            if role != "tenant":
                out.pop("result_b64", None)
            elif out.get("result_b64") and "result" not in out:
                try:
                    out["result"] = json.loads(base64.b64decode(out["result_b64"]).decode("utf-8"))
                except Exception:
                    pass
            return out

    def cancel_call(self, tenant: str, role: str, call_id: str) -> dict:
        with self._mu:
            rec = self._calls.get(call_id)
            if rec is None:
                raise ServiceError(404, "not_found", f"call {call_id!r} not found")
            if role == "tenant" and rec["tenant_id"] != tenant:
                raise ServiceError(403, "forbidden", "cannot cancel another tenant's call")
            rec["_cancel"].set()
            if rec["status"] in ("pending", "running"):
                rec["status"] = "cancelled"
            rec["_done"].set()
            return {k: v for k, v in rec.items() if not k.startswith("_")}

    # ---- deploy, rollback & web endpoints -------------------------------------------------------------------
    def deploy_app(self, tenant: str, body: dict) -> dict:
        if not IDENT.match(tenant or ""):
            raise ServiceError(401, "unauthorized", "tenant identity missing")
        p = self.quotas.policy(tenant)
        if not p.serverless_enabled:
            raise ServiceError(403, "serverless_disabled", f"serverless is not enabled for tenant {tenant}")
        if not isinstance(body, dict):
            raise ServiceError(400, "bad_request", "body must be a JSON object")
        try:
            res = self.deployments.deploy(
                tenant=tenant,
                app_name=str(body.get("app_name", "")),
                functions=body.get("functions") or [],
                web_endpoints=body.get("web_endpoints") or {},
            )
        except DeploymentError as e:
            raise ServiceError(e.status, e.code, str(e)) from None
        self.audit.record("deploy", tenant=tenant, actor=tenant, detail={
            "app_name": res["app_name"],
            "version": res["active_version"],
            "previous_version": res.get("previous_version"),
            "functions": res["functions"],
        })
        return res

    def rollback_app(self, tenant: str, app_name: str, target_version: Optional[str] = None) -> dict:
        if not IDENT.match(tenant or ""):
            raise ServiceError(401, "unauthorized", "tenant identity missing")
        try:
            res = self.deployments.rollback(tenant, app_name, target_version)
        except DeploymentError as e:
            raise ServiceError(e.status, e.code, str(e)) from None
        self.audit.record("rollback", tenant=tenant, actor=tenant, detail={
            "app_name": app_name,
            "active_version": res["active_version"],
            "previous_version": res.get("previous_version"),
        })
        return res

    def stop_app(self, tenant: str, app_name: str, role: str = "tenant") -> dict:
        removed = self.deployments.stop_app(tenant, app_name)
        evicted = 0
        evict_reason = "admin" if role in ("admin", "operator") else "idle"
        for c in list(self.list_containers(tenant=tenant if role == "tenant" else None)):
            if c["app_name"] == app_name or c["fn_id"] == app_name:
                try:
                    self.evict_container(c["container_id"], actor=tenant or role, role=role, tenant=tenant, reason=evict_reason)
                    evicted += 1
                except ServiceError:
                    pass
        self.audit.record("stop_app", tenant=tenant, actor=tenant or role, detail={"app_name": app_name, "evicted_containers": evicted})
        return {"stopped": removed or evicted > 0, "app_name": app_name, "evicted_containers": evicted}

    def invoke_web(self, caller_tenant: str, caller_role: str, target_tenant: str, app_name: str, fn_id: str, payload: Any) -> dict:
        # Enforce tenant isolation BEFORE resolving deployment metadata to prevent both cross-tenant IDOR
        # and cross-tenant app/function/version-hash enumeration oracles (D-F1).
        if caller_role not in ("tenant", "admin", "operator") or (caller_role == "tenant" and caller_tenant != target_tenant):
            raise ServiceError(403, "forbidden", "web endpoint requires matching tenant API key")
        try:
            fndef, ver, web_meta = self.deployments.resolve_function(target_tenant, app_name, fn_id)
        except DeploymentError as e:
            raise ServiceError(e.status, e.code, str(e)) from None
        if web_meta is None:
            raise ServiceError(404, "web_endpoint_not_found", f"{app_name}/{fn_id} is not registered as a web_endpoint")
        requires_auth = web_meta.get("requires_auth", True)
        if requires_auth:
            if caller_role != "tenant" or caller_tenant != target_tenant:
                raise ServiceError(403, "forbidden", "web endpoint requires matching tenant API key")
        # JSON-only (threat T10): never accept or return cloudpickle on web endpoints
        args_json = json.dumps({"args": [payload] if payload is not None else [], "kwargs": {}}).encode("utf-8")
        inv_body = {
            "app_name": app_name,
            "fn_id": fn_id,
            "kind": fndef.get("kind", "function"),
            "method": fndef.get("method"),
            "env": fndef.get("env", {}),
            "bundle_b64": fndef["bundle_b64"],
            "args_b64": base64.b64encode(args_json).decode(),
            "args_format": "json",
            "result_format": "json",
            "gpu": fndef.get("gpu"),
            "vram_mb": fndef.get("vram_mb", 0),
            "memory_mb": fndef.get("memory_mb", 512),
            "cpus": fndef.get("cpus", 1.0),
            "timeout_s": min(float(fndef.get("timeout_s", 30.0)), 60.0),
            "keep_warm_s": fndef.get("keep_warm_s", 60),
            "retries": fndef.get("retries", 0),
            "data_class": fndef.get("data_class", "INTERNAL"),
            "env_vars": fndef.get("env_vars", {}),
            "secrets": fndef.get("secrets", []),
        }
        out = self.invoke(target_tenant, inv_body)
        if out.get("status") != "ok":
            err_info = out.get("error") or {}
            err_msg = err_info.get("message") or "web endpoint execution failed"
            raise ServiceError(502, "web_endpoint_failed", f"web endpoint execution failed: {err_msg}")
        result_val = json.loads(base64.b64decode(out["result_b64"])) if out.get("result_b64") else None
        return {
            "status": "ok",
            "version": ver,
            "call_id": out["call_id"],
            "result": result_val,
            "exec_ms": out.get("exec_ms"),
            "cold_start": out.get("cold_start"),
            "worker_id": out.get("worker_id"),
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", ""),
        }

    # ---- main entry -----------------------------------------------------------------------------------------
    def invoke(self, tenant: str, body: dict, *, _call_id: Optional[str] = None) -> dict:
        t0 = time.perf_counter()
        if not self.enabled:
            raise ServiceError(503, "serverless_disabled_global", "serverless execution is globally disabled by administrator", 5)
        if self.draining and _call_id is None:
            raise ServiceError(503, "serverless_draining", "serverless service is draining; no new calls accepted", 5)
        if not IDENT.match(tenant or ""):
            raise ServiceError(401, "unauthorized", "tenant identity missing")
        resolved = self._resolve_deployed_if_needed(tenant, body)
        req = self._parse(resolved)
        try:
            policy = self.quotas.check(tenant, req)
        except QuotaError as e:
            self.metrics.inc("sl_quota_rejections_total", "Calls rejected by tenant policy", reason=e.code)
            raise ServiceError(e.status, e.code, str(e), e.retry_after) from None
        if len(req["bundle"]) > min(policy.max_bundle_bytes, MAX_BLOB_BYTES) or len(req["args"]) > MAX_BLOB_BYTES:
            raise ServiceError(413, "payload_too_large", f"bundle/args exceed {MAX_BLOB_BYTES} bytes (use a Volume for large data)")
        if req["secrets"]:
            try:
                sec_env = self.quotas.resolve_secrets(tenant, req["secrets"])
            except QuotaError as e:
                raise ServiceError(e.status, e.code, str(e), e.retry_after) from None
            merged_env = dict(sec_env)
            merged_env.update(req["env_vars"])
            req["env_vars"] = merged_env
            self.audit.record("secret_access", tenant=tenant, actor=tenant,
                              detail={"secret_names": list(req["secrets"]), "env_vars": req["env_vars"]})

        spec_in = req["env"] if isinstance(req["env"], dict) else {}
        try:
            spec = EnvSpec(packages=tuple(spec_in.get("packages", ())), python_version=spec_in.get("python_version", "3.12"),
                           cuda_tag=spec_in.get("cuda_tag", ""), local_source_hash=spec_in.get("local_source_hash", ""))
            env_hash = spec.env_hash()
        except (EnvError, TypeError) as e:
            raise ServiceError(400, "env_invalid", str(e)) from None
        live = {a.agent_id: a for a in self.live_workers()}
        want_gpu = bool(req["gpu"] is not None or req["vram_mb"] > 0)
        need_label = "gpu-sl" if want_gpu else None
        candidates = [a for a in live.values() if need_label is None or need_label in a.labels]
        if not candidates:
            self.metrics.inc("sl_no_worker_total", "Calls rejected: no serverless worker online", gpu=str(want_gpu).lower())
            raise ServiceError(503, "no_worker", "no serverless worker with the required capability is online" + (" (gpu)" if want_gpu else ""), 5)

        try:
            self.quotas.acquire(tenant, policy)
        except QuotaError as e:
            self.metrics.inc("sl_quota_rejections_total", "Calls rejected by tenant policy", reason=e.code)
            raise ServiceError(e.status, e.code, str(e), e.retry_after) from None
        try:
            return self._run(tenant, req, spec, env_hash, need_label, live, t0, call_id=_call_id)
        finally:
            self.quotas.release(tenant)

    def _task(self, tenant: str, call_id: str, req: dict, spec: EnvSpec, env_hash: str, bundle_sha: str, attempt: int, with_bundle: bool,
              label: Optional[str], lease_id: Optional[str] = None) -> Task:
        bundle_kw, args_kw = {}, {}
        if with_bundle:
            if len(req["bundle"]) <= MAX_INLINE_BYTES:
                bundle_kw["bundle_b64"] = req["bundle_b64"]
            else:
                bundle_kw["bundle_ref"] = self.blobs.put(req["bundle"])
        if len(req["args"]) <= MAX_INLINE_BYTES:
            args_kw["args_b64"] = req["args_b64"]
        else:
            args_kw["args_ref"] = self.blobs.put(req["args"])
        body = {"tenant_id": tenant, "call_id": call_id, "app_name": req["app_name"], "fn_id": req["fn_id"], "kind": req["kind"],
                "method": req["method"], "env": {"python_version": spec.python_version, "packages": list(spec.packages), "cuda_tag": spec.cuda_tag,
                                                 "local_source_hash": spec.local_source_hash},
                "env_hash": env_hash, "bundle_sha256": bundle_sha, "args_format": req["args_format"], "result_format": req["result_format"],
                "gpu": req["gpu"], "vram_mb": int(req["vram_mb"]), "memory_mb": int(req["memory_mb"]), "cpus": float(req["cpus"]),
                "timeout_s": float(req["timeout_s"]), "keep_warm_s": int(req["keep_warm_s"]), "retries": int(req["retries"]),
                "lease_id": lease_id, "env_vars": req["env_vars"], **bundle_kw, **args_kw}
        signed = sign(self.keys, body, attempt=attempt, ttl_s=req["timeout_s"] + self.cold_allowance_s + 30)
        return Task(task_id=call_id, tenant_id=tenant, type=TaskType.SERVERLESS_EXEC, input=signed.model_dump(mode="json"),
                    data_class=req["data_class"], label=label, ttl_s=req["timeout_s"] + self.cold_allowance_s)

    def _run(self, tenant: str, req: dict, spec: EnvSpec, env_hash: str, need_label: Optional[str], live: dict, t0: float,
             call_id: Optional[str] = None) -> dict:
        call_id = call_id or uuid.uuid4().hex
        bundle_sha = sha256_hex(req["bundle"])
        wk = self._warm_key(tenant, env_hash, req["fn_id"], req)
        pinned, lease_id = self._pinned_worker(wk, set(live))
        if pinned and need_label and need_label not in live[pinned].labels:
            with self._mu:
                bm = self._warm_busy.get(wk)
                if bm and pinned in bm:
                    bm[pinned] = max(0, bm[pinned] - 1)
            if lease_id and self.leases:
                self.leases.release(lease_id)
            pinned, lease_id = None, None
        attempt, max_attempts, seq = 1, 1 + int(req["retries"]), 0
        last: Optional[ServerlessExecOut] = None
        last_pinned = pinned
        while attempt <= max_attempts:
            if pinned is None and need_label and self.leases is not None and lease_id is None:
                constraint = f"gpu:{req['gpu']}" if req["gpu"] and req["gpu"] != "any" else ""
                rep = self.leases.request(idempotency_key=f"{call_id}-{attempt}", tenant_id=tenant,
                                          vram_bytes=int(req["vram_mb"]) * 1024 * 1024, ttl_ms=30000, constraint=constraint)
                if not rep.get("granted"):
                    self.metrics.inc("sl_no_capacity_total", "GPU calls refused by VRAM admission")
                    raise ServiceError(503, "no_capacity", rep.get("reason") or "not enough free VRAM", 2)
                lease_tok = rep.get("lease") or {}
                lease_id = lease_tok.get("lease_id")
                if lease_tok.get("worker_id") in live:
                    pinned_by_lease = lease_tok["worker_id"]
                else:
                    pinned_by_lease = None
            else:
                pinned_by_lease = None
            target_worker = pinned or pinned_by_lease
            label = f"w-{target_worker}" if target_worker else need_label
            if pinned is None and pinned_by_lease:
                with self._mu:
                    bm = self._warm_busy.setdefault(wk, {})
                    bm[pinned_by_lease] = bm.get(pinned_by_lease, 0) + 1
            try:
                seq += 1                                               # every publish gets its own sequence: JetStream dedups on call_id+seq
                out = self._dispatch_once(tenant, call_id, req, spec, env_hash, bundle_sha, seq, with_bundle=pinned is None, label=label, lease_id=lease_id)
                if out.status == "bundle_missing":                       # pinned worker lost its cache: resend once with the bundle
                    seq += 1
                    out = self._dispatch_once(tenant, call_id, req, spec, env_hash, bundle_sha, seq, with_bundle=True, label=label, lease_id=lease_id)
            finally:
                if target_worker:
                    with self._mu:
                        bm = self._warm_busy.get(wk)
                        if bm and target_worker in bm:
                            bm[target_worker] = max(0, bm[target_worker] - 1)
                            if bm[target_worker] == 0:
                                bm.pop(target_worker, None)
            last = out
            if out.status in INFRA_FAILURES and attempt < max_attempts:
                self.metrics.inc("sl_retries_total", "Automatic retries after worker loss", reason=out.status)
                if lease_id and self.leases:
                    self.leases.release(lease_id)
                with self._mu:
                    cur = self._warm.get(wk)
                    if cur is None or cur.worker_id == target_worker:
                        self._warm.pop(wk, None)
                    if target_worker and wk in self._warm_extra:
                        self._warm_extra[wk].pop(target_worker, None)
                pinned, lease_id, attempt = None, None, attempt + 1
                last_pinned = None
                continue
            break
        assert last is not None
        if last.status == "no_capacity":
            if lease_id and self.leases:
                self.leases.release(lease_id)
            self.metrics.inc("sl_no_capacity_total", "GPU calls refused by VRAM admission")
            raise ServiceError(503, "no_capacity", (last.error.message if last.error else "not enough free VRAM"), 2)
        self._remember(wk, last.worker_id if last.status in ("ok", "error") else "", int(req["keep_warm_s"]), lease_id=lease_id,
                       tenant=tenant, app_name=req["app_name"], fn_id=req["fn_id"], vram_mb=int(req["vram_mb"]), container_id=last.container or "",
                       pinned_from=last_pinned)
        self.metrics.inc("sl_calls_total", "Serverless calls by outcome", status=last.status, gpu=str(bool(need_label)).lower())
        elapsed_s = max(0.0, time.perf_counter() - t0)
        self.metrics.observe("sl_call_duration_seconds", "End-to-end serverless call latency in seconds", elapsed_s, tenant=tenant, status=last.status)
        if last.cold_start:
            self.metrics.inc("sl_cold_starts_total", "Cold starts")
            ckind = "cold_env_build" if getattr(last, "env_built", False) else "cold_docker"
            self.metrics.observe("sl_cold_start_seconds", "Cold start duration in seconds", elapsed_s, kind=ckind)
        else:
            self.metrics.observe("sl_cold_start_seconds", "Cold start duration in seconds", elapsed_s, kind="warm_exec")
        gpu_ms = float(last.exec_ms or 0.0) if need_label else 0.0
        if gpu_ms > 0:
            self.metrics.inc("sl_gpu_seconds_total", "Cumulative GPU execution time in seconds", value=round(gpu_ms / 1000.0, 6), tenant=tenant)
        self.ledger.record_call(tenant, call_id, gpu_ms, int(req["vram_mb"]) if need_label else 0)
        self.audit.record("invoke", tenant=tenant, actor=tenant, detail={
            "call_id": call_id,
            "app_name": req["app_name"],
            "fn_id": req["fn_id"],
            "status": last.status,
            "cold_start": bool(last.cold_start),
            "exec_ms": round(float(last.exec_ms or 0.0), 3),
            "vram_mb": int(req["vram_mb"]),
            "worker_id": last.worker_id or "",
        })
        resp = self._response(call_id, last, attempt, (time.perf_counter() - t0) * 1000)
        with self._mu:
            if call_id not in self._calls:
                done_ev = threading.Event()
                done_ev.set()
                self._calls[call_id] = {
                    "call_id": call_id,
                    "tenant_id": tenant,
                    "app_name": req["app_name"],
                    "fn_id": req["fn_id"],
                    **resp,
                    "_done": done_ev,
                    "_cancel": threading.Event(),
                }
        return resp

    def _dispatch_once(self, tenant, call_id, req, spec, env_hash, bundle_sha, attempt, with_bundle, label, lease_id=None) -> ServerlessExecOut:
        try:
            task = self._task(tenant, call_id, req, spec, env_hash, bundle_sha, attempt, with_bundle, label, lease_id=lease_id)
        except BlobError as e:
            raise ServiceError(413, "payload_too_large", str(e)) from None
        try:
            res = self.dispatch(task, attempt)
        except Exception as e:
            kind = type(e).__name__
            if kind in ("QueueFullError", "QuotaExceededError"):
                raise ServiceError(429, "bus_busy", "task bus is full; retry shortly", 1) from None
            if kind == "TaskExpiredError":
                raise ServiceError(504, "expired", "the call expired before a worker answered") from None
            if kind == "WorkerLost":
                return ServerlessExecOut.model_validate({"status": "worker_lost", "error": {"type": "WorkerLost", "message": str(e)[:300]}})
            raise ServiceError(502, "bus_error", f"{kind}: {str(e)[:200]}") from None
        out = ServerlessExecOut.model_validate(res.output)
        if out.result_ref and not out.result_b64:
            try:
                out = out.model_copy(update={"result_b64": base64.b64encode(self.blobs.get(out.result_ref)).decode(), "result_ref": None})
            except BlobError as e:
                out = out.model_copy(update={"status": "error", "error": {"type": "ResultUnavailable", "message": str(e)}})
        return out

    @staticmethod
    def _response(call_id: str, out: ServerlessExecOut, attempts: int, total_ms: float) -> dict:
        d = out.model_dump(mode="json", exclude_none=True)
        d.update(call_id=call_id, attempts=attempts, service_ms=round(total_ms, 3))
        return d

    def recent_calls(self, tenant: Optional[str] = None, limit: int = 30) -> list[dict]:
        with self._mu:
            items = []
            for rec in reversed(list(self._calls.values())):
                if tenant and rec.get("tenant_id") != tenant:
                    continue
                items.append({k: v for k, v in rec.items() if not k.startswith("_") and k != "result_b64"})
                if len(items) >= limit:
                    break
            return items

    def overview(self, tenant: Optional[str] = None, role: str = "", user: str = "") -> dict:
        live = []
        try:
            for a in self.live_workers():
                live.append({"agent_id": a.agent_id, "labels": list(getattr(a, "labels", ()))})
        except Exception:
            pass
        policies = {tid: asdict(pol) for tid, pol in self.quotas.policies.items() if not tenant or tid == tenant}
        if tenant and tenant not in policies:
            policies[tenant] = asdict(self.quotas.policy(tenant))
        return {
            "enabled": self.enabled,
            "draining": self.draining,
            "whoami": {
                "role": role or ("tenant" if tenant else "anonymous"),
                "tenant_id": tenant or None,
                "user": user or None,
            },
            "workers": live,
            "containers": self.list_containers(tenant=tenant),
            "apps": self.deployments.list_apps(tenant=tenant),
            "calls": self.recent_calls(tenant=tenant),
            "usage": self.ledger.totals_by_tenant() if not tenant else {tenant: self.ledger.totals_by_tenant().get(tenant, {"tenant": tenant, "calls": 0, "gpu_ms": 0, "gpu_seconds": 0.0, "vram_mib_s": 0})},
            "policies": policies,
            "audit": self.audit.recent(50, tenant=tenant),
        }

    def record_reclaim(self, latency_s: float, *, evicted: int = 0, orphans: int = 0, reason: str = "reclaim") -> dict:
        lat = max(0.0, float(latency_s))
        self._orphan_containers = max(0, int(orphans))
        self.metrics.observe(
            "sl_reclaim_latency_seconds",
            "GPU reclaim latency in seconds (G-RECLAIM < 2.0s gate)",
            lat,
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 5.0),
            reason=reason,
        )
        self.metrics.set_gauge("sl_orphan_containers", "Orphan containers detected outside WarmDockerPool", float(self._orphan_containers))
        if evicted > 0:
            self.metrics.inc("sl_reclaim_evictions_total", "Total containers evicted during GPU reclaim", value=float(evicted), reason=reason)
        self.audit.record("reclaim_metric", tenant="-", actor="worker-guard", detail={
            "latency_s": round(lat, 4),
            "evicted": int(evicted),
            "orphans": int(self._orphan_containers),
            "reason": reason,
        })
        return {"ok": True, "latency_s": round(lat, 4), "evicted": int(evicted), "orphans": int(self._orphan_containers)}

    def render_metrics(self) -> str:
        self.metrics.set_gauge("sl_enabled", "Whether serverless service is globally enabled (1=enabled, 0=disabled)", 1.0 if self.enabled else 0.0)
        self.metrics.set_gauge("sl_draining", "Whether serverless service is draining (1=draining, 0=normal)", 1.0 if self.draining else 0.0)
        self.metrics.set_gauge("sl_orphan_containers", "Orphan containers detected outside WarmDockerPool", float(self._orphan_containers))
        self.metrics.clear_gauge_family("sl_warm_containers")
        self.metrics.clear_gauge_family("sl_vram_lease_bytes")
        containers = self.list_containers()
        by_tw: dict[tuple[str, str], tuple[int, int]] = {}
        for c in containers:
            key = (str(c.get("tenant_id") or "-"), str(c.get("worker_id") or "-"))
            cnt, vram = by_tw.get(key, (0, 0))
            by_tw[key] = (cnt + 1, vram + int(c.get("vram_mb") or 0) * 1024 * 1024)
        for (t, w), (cnt, vbytes) in by_tw.items():
            self.metrics.set_gauge("sl_warm_containers", "Active warm containers", float(cnt), tenant=t, worker_id=w)
            self.metrics.set_gauge("sl_vram_lease_bytes", "VRAM bytes held by warm container leases", float(vbytes), tenant=t, worker_id=w)
        return self.metrics.render()
