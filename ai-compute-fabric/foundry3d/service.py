"""Dịch vụ HTTP AICF 3D Foundry (Pha 5, mặc định lắng nghe 127.0.0.1:18120 sau site-gateway)."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from foundry3d.artifact_store import (
    SAFE_ID_RE,
    ArtifactNotFoundError,
    ArtifactSecurityError,
    ArtifactStore,
)
from foundry3d.pipeline_graph import FoundryPipelineRunner
from foundry3d.providers.llm_vlm_client import GatewayLlmVlmClient
from foundry3d.schemas import AssetSpec, JobCreateRequest, JobRecord, StageArtifact
from foundry3d.warm_blender import LowMemoryQueuedError, WarmBlenderPool

MAX_REQUEST_BODY_BYTES = 1024 * 1024  # 1 MiB
ACTIVE_STATUSES = {"queued", "queued_low_mem", "running"}
TERMINAL_STATUSES = {"completed", "needs_human_review", "failed", "cancelled"}


@dataclass
class _JobRuntime:
    record: JobRecord
    req: JobCreateRequest
    events: list[dict[str, Any]] = field(default_factory=list)
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_requested: bool = False

    def push_event(self, event_type: str, data: dict[str, Any]) -> None:
        ev = {
            "seq": len(self.events) + 1,
            "ts": time.time(),
            "event": event_type,
            "data": data,
        }
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()


class FoundryServiceState:
    """Quản lý trạng thái job theo từng tenant và điều phối WarmBlenderPool."""

    def __init__(
        self,
        *,
        proxy_secret: str,
        store: ArtifactStore | None = None,
        pool: WarmBlenderPool | None = None,
        llm_client: GatewayLlmVlmClient | None = None,
        max_active_per_tenant: int = 2,
    ) -> None:
        if len(proxy_secret) < 16:
            raise ValueError("proxy_secret phải có ít nhất 16 ký tự")
        self.proxy_secret = proxy_secret
        self.store = store or ArtifactStore()
        self.pool = pool or WarmBlenderPool()
        self.llm = llm_client or GatewayLlmVlmClient()
        self.runner = FoundryPipelineRunner(pool=self.pool, store=self.store, llm_client=self.llm)
        self.max_active_per_tenant = max(1, int(max_active_per_tenant))
        self._lock = threading.Lock()
        self._jobs: dict[str, _JobRuntime] = {}

    def verify_secret(self, candidate: str) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, self.proxy_secret)

    def create_job(
        self,
        tenant_id: str,
        req: JobCreateRequest,
        *,
        preset_spec: AssetSpec | None = None,
        preset_bpy_code: str = "",
        hold_hook: threading.Event | None = None,
    ) -> JobRecord:
        if not tenant_id or not SAFE_ID_RE.match(tenant_id) or ".." in tenant_id:
            raise ValueError(f"tenant_id không hợp lệ: {tenant_id!r}")

        with self._lock:
            active_for_tenant = sum(
                1
                for jr in self._jobs.values()
                if jr.record.tenant_id == tenant_id and jr.record.status in ACTIVE_STATUSES
            )
            if active_for_tenant >= self.max_active_per_tenant:
                raise PermissionError(
                    f"Tenant {tenant_id!r} đã đạt giới hạn {self.max_active_per_tenant} job đồng thời"
                )
            now = time.time()
            job_id = f"f3d_{uuid.uuid4().hex[:12]}"
            rec = JobRecord(
                job_id=job_id,
                tenant_id=tenant_id,
                status="queued",
                current_stage="queued",
                prompt=req.prompt,
                archetype=req.archetype,
                target_engine=req.target_engine,
                mode=req.mode,
                created_at=now,
                updated_at=now,
            )
            rt = _JobRuntime(record=rec, req=req)
            rt.push_event("queued", {"job_id": job_id, "tenant_id": tenant_id})
            self._jobs[job_id] = rt

        t = threading.Thread(
            target=self._run_job_worker,
            args=(rt, preset_spec, preset_bpy_code, hold_hook),
            name=f"f3d-{job_id}",
            daemon=True,
        )
        t.start()
        return rec

    def _run_job_worker(
        self,
        rt: _JobRuntime,
        preset_spec: AssetSpec | None,
        preset_bpy_code: str,
        hold_hook: threading.Event | None,
    ) -> None:
        rec = rt.record
        req = rt.req
        if hold_hook is not None:
            hold_hook.wait(timeout=10.0)
        with rt.cond:
            if rt.cancel_requested:
                rec.status = "cancelled"
                rec.current_stage = "cancelled"
                rec.updated_at = time.time()
                rt.cond.notify_all()
                return
            rec.status = "running"
            rec.current_stage = "stage_1_spec"
            rec.updated_at = time.time()
        rt.push_event("running", {"job_id": rec.job_id})

        def _hook(stage: str, payload: dict[str, Any]) -> None:
            with rt.cond:
                rec.current_stage = stage
                rec.updated_at = time.time()
                if "repair_count" in payload:
                    rec.repair_count = int(payload["repair_count"])
            rt.push_event(stage, payload)

        preset_fn = None
        if preset_bpy_code:
            def preset_fn(_spec: AssetSpec, _r: int, _fb: str) -> str:
                return preset_bpy_code

        try:
            final_state = self.runner.run_job(
                job_id=rec.job_id,
                tenant_id=rec.tenant_id,
                prompt=req.prompt,
                archetype=req.archetype,
                dimensions_m=req.dimensions_m,
                poly_budget_lod0=req.poly_budget_lod0,
                target_engine=req.target_engine,
                mode=req.mode,
                enable_vlm_critic=req.enable_vlm_critic,
                bake_pbr=req.bake_pbr,
                max_repairs=req.max_repairs,
                preset_spec=preset_spec,
                preset_bpy_fn=preset_fn,
                event_hook=_hook,
            )
            artifacts = [StageArtifact(**a) for a in (final_state.get("artifacts") or [])]
            if not artifacts:
                artifacts = self.store.list_artifacts(rec.tenant_id, rec.job_id)
            with rt.cond:
                rec.status = str(final_state.get("status", "completed"))  # type: ignore[assignment]
                rec.current_stage = rec.status
                rec.repair_count = int(final_state.get("repair_count", 0))
                rec.artifacts = artifacts
                rec.updated_at = time.time()
            rt.push_event("done", {"status": rec.status, "job_id": rec.job_id})
        except LowMemoryQueuedError as exc:
            with rt.cond:
                rec.status = "queued_low_mem"
                rec.current_stage = "queued_low_mem"
                rec.error = str(exc)
                rec.updated_at = time.time()
            rt.push_event("queued_low_mem", {"error": str(exc)})
        except Exception as exc:
            with rt.cond:
                rec.status = "failed"
                rec.current_stage = "failed"
                rec.error = f"{type(exc).__name__}: {exc}"
                rec.updated_at = time.time()
            rt.push_event("failed", {"error": rec.error})

    def get_job(self, tenant_id: str, job_id: str) -> _JobRuntime | None:
        with self._lock:
            rt = self._jobs.get(job_id)
            if rt is None or rt.record.tenant_id != tenant_id:
                return None
            return rt

    def list_jobs(self, tenant_id: str) -> list[JobRecord]:
        with self._lock:
            out = [jr.record for jr in self._jobs.values() if jr.record.tenant_id == tenant_id]
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def delete_job(self, tenant_id: str, job_id: str) -> bool:
        with self._lock:
            rt = self._jobs.get(job_id)
            if rt is None or rt.record.tenant_id != tenant_id:
                return False
            with rt.cond:
                rt.cancel_requested = True
                if rt.record.status in ACTIVE_STATUSES:
                    rt.record.status = "cancelled"
                    rt.record.current_stage = "cancelled"
                    rt.record.updated_at = time.time()
                rt.cond.notify_all()
            del self._jobs[job_id]
        self.store.cleanup_intermediate(tenant_id, job_id, ())
        return True

    def status_summary(self, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            tenant_active = sum(
                1 for jr in self._jobs.values()
                if jr.record.tenant_id == tenant_id and jr.record.status in ACTIVE_STATUSES
            )
            total_active = sum(
                1 for jr in self._jobs.values() if jr.record.status in ACTIVE_STATUSES
            )
            total_jobs = len(self._jobs)
        return {
            "service": "foundry3d",
            "status": "ok",
            "mode": "M1",
            "tenant_id": tenant_id,
            "tenant_active_jobs": tenant_active,
            "total_active_jobs": total_active,
            "total_jobs": total_jobs,
            "max_active_per_tenant": self.max_active_per_tenant,
            "artifacts_bytes": self.store.total_size_bytes(),
        }


def make_handler(state: FoundryServiceState) -> type[BaseHTTPRequestHandler]:
    class FoundryHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _require_proxy_and_tenant(self) -> str | None:
            secret = self.headers.get("X-Aicf-Proxy-Secret", "")
            if not state.verify_secret(secret):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid or missing X-Aicf-Proxy-Secret"})
                return None
            tenant = (self.headers.get("X-Aicf-Tenant") or "").strip()
            if not tenant or not SAFE_ID_RE.match(tenant) or ".." in tenant:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing or invalid X-Aicf-Tenant"})
                return None
            return tenant

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                raw_len = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
                return None
            if raw_len <= 0 or raw_len > MAX_REQUEST_BODY_BYTES:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body empty or exceeds 1 MiB"})
                return None
            raw = self.rfile.read(raw_len)
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return None
            if not isinstance(obj, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
                return None
            return obj

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok", "service": "foundry3d"})
                return

            tenant = self._require_proxy_and_tenant()
            if tenant is None:
                return

            if path == "/v1/status":
                self._send_json(HTTPStatus.OK, state.status_summary(tenant))
                return

            if path == "/v1/jobs":
                recs = [r.model_dump() for r in state.list_jobs(tenant)]
                self._send_json(HTTPStatus.OK, {"jobs": recs})
                return

            if path.startswith("/v1/jobs/"):
                rest = path[len("/v1/jobs/"):]
                parts = rest.split("/")
                job_id = parts[0]
                if not job_id or not SAFE_ID_RE.match(job_id):
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return

                rt = state.get_job(tenant, job_id)
                if rt is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return

                if len(parts) == 1:
                    with rt.cond:
                        rec_dict = rt.record.model_dump()
                    self._send_json(HTTPStatus.OK, rec_dict)
                    return

                if len(parts) == 2 and parts[1] == "events":
                    self._serve_sse(rt)
                    return

                if len(parts) == 3 and parts[1] == "artifacts":
                    fname = parts[2]
                    try:
                        blob_p, art = state.store.get_artifact_path(tenant, job_id, fname)
                    except ArtifactSecurityError as exc:
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                        return
                    except ArtifactNotFoundError:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "artifact not found"})
                        return
                    data = blob_p.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", art.mime_type or "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _serve_sse(self, rt: _JobRuntime) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            sent = 0
            deadline = time.monotonic() + 300.0
            try:
                while time.monotonic() < deadline:
                    with rt.cond:
                        while sent >= len(rt.events) and rt.record.status not in TERMINAL_STATUSES:
                            rt.cond.wait(timeout=1.0)
                            break
                        batch = rt.events[sent:]
                        done = rt.record.status in TERMINAL_STATUSES and sent + len(batch) >= len(rt.events)
                    for ev in batch:
                        payload = json.dumps(ev, ensure_ascii=False)
                        msg = f"event: {ev['event']}\ndata: {payload}\n\n".encode("utf-8")
                        self.wfile.write(msg)
                        sent += 1
                    self.wfile.flush()
                    if done:
                        break
                    if not batch:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            tenant = self._require_proxy_and_tenant()
            if tenant is None:
                return

            if path == "/v1/jobs":
                body = self._read_json_body()
                if body is None:
                    return
                try:
                    req = JobCreateRequest(**body)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                try:
                    rec = state.create_job(tenant, req)
                except PermissionError as exc:
                    self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
                    return
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.CREATED, rec.model_dump())
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            tenant = self._require_proxy_and_tenant()
            if tenant is None:
                return
            if path.startswith("/v1/jobs/"):
                job_id = path[len("/v1/jobs/"):].strip("/")
                if "/" not in job_id and SAFE_ID_RE.match(job_id):
                    if state.delete_job(tenant, job_id):
                        self._send_json(HTTPStatus.OK, {"deleted": job_id})
                        return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "job not found"})

    return FoundryHandler


def read_secret_file(path: str) -> str:
    txt = Path(path).read_text(encoding="utf-8").strip()
    if len(txt) < 16:
        raise ValueError(f"Secret tại {path} phải dài >= 16 ký tự")
    return txt


def build_server(host: str, port: int, state: FoundryServiceState) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))


def main() -> None:
    ap = argparse.ArgumentParser(description="AICF 3D Asset Foundry HTTP Service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18120)
    ap.add_argument("--secret-file", default=os.environ.get("AICF_FOUNDRY3D_SECRET_FILE", ""))
    ap.add_argument("--gateway-url", default=os.environ.get("AICF_GATEWAY_URL", "http://127.0.0.1:18080"))
    ap.add_argument("--max-active-per-tenant", type=int, default=2)
    args = ap.parse_args()

    if args.secret_file:
        secret = read_secret_file(args.secret_file)
    else:
        secret = os.environ.get("AICF_FOUNDRY3D_SECRET", "").strip()
    if len(secret) < 16:
        raise SystemExit("Bắt buộc truyền --secret-file hoặc AICF_FOUNDRY3D_SECRET (>= 16 ký tự)")

    state = FoundryServiceState(
        proxy_secret=secret,
        llm_client=GatewayLlmVlmClient(gateway_url=args.gateway_url),
        max_active_per_tenant=args.max_active_per_tenant,
    )
    srv = build_server(args.host, args.port, state)
    print(f"foundry3d listening on http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        state.pool.shutdown()


if __name__ == "__main__":
    main()
