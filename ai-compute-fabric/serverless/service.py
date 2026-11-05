"""Serverless service core (ADR-003 D1-D3, D8): validate -> authorise -> sign -> dispatch on the task bus -> map the answer.

`ServerlessService.invoke(tenant, body)` is transport-free so it is unit-testable; serverless/app.py wraps it in HTTP.
Dispatch is injected (`dispatch(task, attempt) -> TaskResult`; production = BusExecutor.execute), as are the live-worker query
and the blob store. Nothing here ever unpickles user data."""
from __future__ import annotations

import base64
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from agents.env_cache import EnvError, EnvSpec
from agents.sl_security import sha256_hex, sign
from agents.sl_store import MAX_BLOB_BYTES, MAX_INLINE_BYTES, BlobError
from agents.tasks import ServerlessExecOut, Task, TaskType
from serverless.metrics import Metrics
from serverless.quota import QuotaError, Quotas

IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ALLOWED_REQ = {"app_name", "fn_id", "kind", "method", "env", "bundle_b64", "args_b64", "args_format", "result_format", "gpu", "vram_mb",
               "memory_mb", "cpus", "timeout_s", "keep_warm_s", "retries", "data_class", "env_vars"}
INFRA_FAILURES = {"worker_lost", "duplicate"}      # nothing the function did: safe to retry when the function opted in (D8)


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


class ServerlessService:
    def __init__(self, *, keys: list, quotas: Quotas, dispatch: Callable, live_workers: Callable, blobs, metrics: Optional[Metrics] = None,
                 clock: Callable = time.time, cold_allowance_s: float = 180.0):
        self.keys, self.quotas, self.dispatch, self.live_workers, self.blobs = keys, quotas, dispatch, live_workers, blobs
        self.metrics, self.clock, self.cold_allowance_s = metrics or Metrics(), clock, cold_allowance_s
        self._warm: dict = {}
        self._mu = threading.Lock()

    # ---- request parsing ------------------------------------------------------------------------------------
    @staticmethod
    def _parse(body: dict) -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "bad_request", "body must be a JSON object")
        extra = set(body) - ALLOWED_REQ
        if extra:
            raise ServiceError(400, "bad_request", f"unknown fields: {sorted(extra)}")
        req = {"kind": "function", "method": None, "env": {}, "args_format": "cloudpickle", "result_format": "cloudpickle", "gpu": None,
               "vram_mb": 0, "memory_mb": 512, "cpus": 1.0, "timeout_s": 30.0, "keep_warm_s": 60, "retries": 0, "data_class": "INTERNAL",
               "env_vars": {}, **body}
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

    def _pinned_worker(self, wk: tuple, live_ids: set) -> Optional[str]:
        with self._mu:
            e = self._warm.get(wk)
            if e and (e.expires <= self.clock() or e.worker_id not in live_ids):
                self._warm.pop(wk, None)
                return None
            return e.worker_id if e else None

    def _remember(self, wk: tuple, worker_id: str, keep_warm_s: int) -> None:
        with self._mu:
            if keep_warm_s > 0 and worker_id:
                self._warm[wk] = WarmEntry(worker_id, self.clock() + keep_warm_s)
            else:
                self._warm.pop(wk, None)

    def warm_entries(self) -> int:
        with self._mu:
            now = self.clock()
            return sum(1 for e in self._warm.values() if e.expires > now)

    # ---- main entry -----------------------------------------------------------------------------------------
    def invoke(self, tenant: str, body: dict) -> dict:
        t0 = time.perf_counter()
        if not IDENT.match(tenant or ""):
            raise ServiceError(401, "unauthorized", "tenant identity missing")
        req = self._parse(body)
        try:
            policy = self.quotas.check(tenant, req)
        except QuotaError as e:
            self.metrics.inc("sl_quota_rejections_total", "Calls rejected by tenant policy", reason=e.code)
            raise ServiceError(e.status, e.code, str(e), e.retry_after) from None
        if len(req["bundle"]) > min(policy.max_bundle_bytes, MAX_BLOB_BYTES) or len(req["args"]) > MAX_BLOB_BYTES:
            raise ServiceError(413, "payload_too_large", f"bundle/args exceed {MAX_BLOB_BYTES} bytes (use a Volume for large data)")
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
            return self._run(tenant, req, spec, env_hash, need_label, live, t0)
        finally:
            self.quotas.release(tenant)

    def _task(self, tenant: str, call_id: str, req: dict, spec: EnvSpec, env_hash: str, bundle_sha: str, attempt: int, with_bundle: bool,
              label: Optional[str]) -> Task:
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
                "env_vars": req["env_vars"], **bundle_kw, **args_kw}
        signed = sign(self.keys, body, attempt=attempt, ttl_s=req["timeout_s"] + self.cold_allowance_s + 30)
        return Task(task_id=call_id, tenant_id=tenant, type=TaskType.SERVERLESS_EXEC, input=signed.model_dump(mode="json"),
                    data_class=req["data_class"], label=label, ttl_s=req["timeout_s"] + self.cold_allowance_s)

    def _run(self, tenant: str, req: dict, spec: EnvSpec, env_hash: str, need_label: Optional[str], live: dict, t0: float) -> dict:
        call_id = uuid.uuid4().hex
        bundle_sha = sha256_hex(req["bundle"])
        wk = self._warm_key(tenant, env_hash, req["fn_id"], req)
        pinned = self._pinned_worker(wk, set(live))
        if pinned and need_label and need_label not in live[pinned].labels:
            pinned = None
        attempt, max_attempts, seq = 1, 1 + int(req["retries"]), 0
        last: Optional[ServerlessExecOut] = None
        while attempt <= max_attempts:
            label = f"w-{pinned}" if pinned else need_label
            seq += 1                                               # every publish gets its own sequence: JetStream dedups on call_id+seq
            out = self._dispatch_once(tenant, call_id, req, spec, env_hash, bundle_sha, seq, with_bundle=pinned is None, label=label)
            if out.status == "bundle_missing":                       # pinned worker lost its cache: resend once with the bundle
                seq += 1
                out = self._dispatch_once(tenant, call_id, req, spec, env_hash, bundle_sha, seq, with_bundle=True, label=label)
            last = out
            if out.status in INFRA_FAILURES and attempt < max_attempts:
                self.metrics.inc("sl_retries_total", "Automatic retries after worker loss", reason=out.status)
                with self._mu:
                    self._warm.pop(wk, None)
                pinned, attempt = None, attempt + 1
                continue
            break
        assert last is not None
        self._remember(wk, last.worker_id if last.status in ("ok", "error") else "", int(req["keep_warm_s"]))
        self.metrics.inc("sl_calls_total", "Serverless calls by outcome", status=last.status, gpu=str(bool(need_label)).lower())
        if last.cold_start:
            self.metrics.inc("sl_cold_starts_total", "Cold starts")
        return self._response(call_id, last, attempt, (time.perf_counter() - t0) * 1000)

    def _dispatch_once(self, tenant, call_id, req, spec, env_hash, bundle_sha, attempt, with_bundle, label) -> ServerlessExecOut:
        try:
            task = self._task(tenant, call_id, req, spec, env_hash, bundle_sha, attempt, with_bundle, label)
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
