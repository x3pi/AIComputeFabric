"""HTTP wrapper (stdlib, same pattern as bestofn/service.py). Reached only through site-gateway: every request must carry
X-Aicf-Proxy-Secret (ADR-003 D2); X-Aicf-Tenant is trusted only with it. Bind to 127.0.0.1."""
from __future__ import annotations

import base64
import hmac
import json
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from serverless.service import ServerlessService, ServiceError
from serverless.volumes import VolumeError

MAX_BODY = 16 * 1024 * 1024


def make_handler(svc: ServerlessService, secret: str):
    secret_b = secret.encode()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "aicf-serverless"
        wbufsize = -1          # headers + body leave in ONE segment (see setup/_send)

        def setup(self):
            super().setup()
            # Without TCP_NODELAY a keep-alive response (headers, then body) hits Nagle + the peer's delayed ACK: ~40 ms per call [ĐO]
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *a):       # no access log with tenant data on stderr
            pass

        def _send(self, code: int, obj, ctype="application/json", extra=None):
            data = obj if isinstance(obj, bytes) else (obj.encode() if isinstance(obj, str) else json.dumps(obj).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if code >= 400 and int(self.headers.get("Content-Length", "0") or 0) > 0 and not getattr(self, "_body_drained", False):
                self.close_connection = True
                self.send_header("Connection", "close")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _trusted(self) -> bool:
            got = self.headers.get("X-Aicf-Proxy-Secret", "").encode()
            return hmac.compare_digest(got, secret_b)

        def _fail(self, e: ServiceError):
            extra = {"Retry-After": str(e.retry_after)} if e.retry_after else None
            self._send(e.status, e.body(), extra=extra)

        def _vol_fail(self, e: VolumeError):
            self._send(e.status, {"error": {"code": e.code, "message": str(e)}})

        def _read_raw(self) -> bytes | None:
            try:
                n = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self._send(413 if n > MAX_BODY else 411, {"error": {"code": "bad_length", "message": "Content-Length required, max 16 MiB"}})
                return None
            raw = self.rfile.read(n) if n > 0 else b""
            self._body_drained = True
            return raw

        def _read_json(self, allow_empty: bool = False):
            raw = self._read_raw()
            if raw is None:
                return None
            if not raw and allow_empty:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                self._send(400, {"error": {"code": "bad_request", "message": "body is not JSON"}})
                return None

        def _split_path(self) -> tuple[str, dict[str, str]]:
            parsed = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(parsed.query))
            return parsed.path, q

        def _require_tenant(self) -> str | None:
            tenant = self.headers.get("X-Aicf-Tenant", "")
            if self.headers.get("X-Aicf-Role") != "tenant" or not tenant:
                self._send(403, {"error": {"code": "tenant_key_required", "message": "calls must use a tenant API key"}})
                return None
            return tenant

        def do_GET(self):
            path, q = self._split_path()
            if path == "/v1/healthz":
                return self._send(200, {"ok": True})
            if not self._trusted():
                return self._send(401, {"error": {"code": "unauthorized", "message": "gateway secret required"}})
            role = self.headers.get("X-Aicf-Role", "")
            tenant = self.headers.get("X-Aicf-Tenant", "")
            user = self.headers.get("X-Aicf-User", "")
            try:
                if path == "/v1/metrics":
                    return self._send(200, svc.render_metrics(), "text/plain; version=0.0.4")
                if path in ("/v1/status", "/v1/overview"):
                    return self._send(200, svc.overview(tenant=tenant if role == "tenant" else q.get("tenant"), role=role, user=user))
                if path == "/v1/usage":
                    totals = svc.ledger.totals_by_tenant()
                    if role == "tenant":
                        totals = {tenant: totals.get(tenant, {"tenant": tenant, "calls": 0, "idle_records": 0, "gpu_ms": 0, "gpu_seconds": 0.0, "vram_mib_s": 0})}
                    return self._send(200, {"usage": totals})
                if path == "/v1/containers":
                    return self._send(200, {"containers": svc.list_containers(tenant=tenant if role == "tenant" else q.get("tenant"))})
                if path == "/v1/apps":
                    return self._send(200, {"apps": svc.deployments.list_apps(tenant=tenant if role == "tenant" else q.get("tenant"))})
                if path == "/v1/secrets":
                    t = tenant if role == "tenant" else (q.get("tenant") or tenant)
                    if not t:
                        return self._send(400, {"error": {"code": "bad_request", "message": "tenant required"}})
                    return self._send(200, {"tenant": t, "secrets": svc.quotas.list_secrets_metadata(t)})
                if path.startswith("/v1/calls/"):
                    rest = path[len("/v1/calls/"):]
                    if rest.endswith("/logs"):
                        call_id = rest[:-len("/logs")].strip("/")
                        call = svc.get_call(tenant, role, call_id, wait_s=float(q.get("wait", "2.0")))
                        chunks = []
                        if call.get("stdout"):
                            for ln in call["stdout"].splitlines():
                                chunks.append("data: " + json.dumps({"stream": "stdout", "text": ln}) + "\n\n")
                        if call.get("stderr"):
                            for ln in call["stderr"].splitlines():
                                chunks.append("data: " + json.dumps({"stream": "stderr", "text": ln}) + "\n\n")
                        chunks.append("event: done\ndata: " + json.dumps({"call_id": call_id, "status": call["status"]}) + "\n\n")
                        return self._send(200, "".join(chunks), "text/event-stream; charset=utf-8", extra={"Cache-Control": "no-cache"})
                    call_id = rest.strip("/")
                    wait_s = float(q.get("wait") or q.get("wait_s") or "0")
                    return self._send(200, svc.get_call(tenant, role, call_id, wait_s=wait_s))
                if path.startswith("/v1/volumes/"):
                    t = self._require_tenant()
                    if not t:
                        return
                    rest = urllib.parse.unquote(path[len("/v1/volumes/"):])
                    vol, sep, rel = rest.partition("/")
                    if not sep or not rel:
                        return self._send(200, {"volume": vol, "files": svc.volumes.ls(t, vol)})
                    data, digest = svc.volumes.get(t, vol, rel)
                    if q.get("format") == "json":
                        return self._send(200, {"volume": vol, "path": rel, "size": len(data), "sha256": digest, "data_b64": base64.b64encode(data).decode()})
                    return self._send(200, data, "application/octet-stream", extra={"X-Aicf-Sha256": digest})
                if path == "/v1/model-cache" or path.startswith("/v1/model-cache/"):
                    rest = urllib.parse.unquote(path[len("/v1/model-cache"):].lstrip("/"))
                    if not rest:
                        return self._send(200, {"models": svc.model_cache.ls()})
                    model, sep, rel = rest.partition("/")
                    if not sep or not rel:
                        return self._send(200, {"model": model, "files": svc.model_cache.ls(model)})
                    data, digest = svc.model_cache.get(model, rel)
                    return self._send(200, data, "application/octet-stream", extra={"X-Aicf-Sha256": digest})
                self._send(404, {"error": {"code": "not_found", "message": self.path}})
            except ServiceError as e:
                self._fail(e)
            except VolumeError as e:
                self._vol_fail(e)
            except Exception as e:
                svc.metrics.inc("sl_internal_errors_total", "Unexpected errors", type=type(e).__name__)
                self._send(500, {"error": {"code": "internal", "message": "internal error"}})

        def do_POST(self):
            self._body_drained = False
            if not self._trusted():
                return self._send(401, {"error": {"code": "unauthorized", "message": "gateway secret required"}})
            path, _ = self._split_path()
            role = self.headers.get("X-Aicf-Role", "")
            tenant = self.headers.get("X-Aicf-Tenant", "")
            user = self.headers.get("X-Aicf-User", role)
            try:
                if path == "/v1/invoke":
                    if self.headers.get("X-Aicf-Role") != "tenant" or not tenant:
                        return self._send(403, {"error": {"code": "tenant_key_required", "message": "calls must use a tenant API key"}})
                    body = self._read_json()
                    if body is None:
                        return
                    return self._send(200, svc.invoke(tenant, body))
                if path == "/v1/spawn":
                    t = self._require_tenant()
                    if not t:
                        return
                    body = self._read_json()
                    if body is None:
                        return
                    return self._send(202, svc.spawn(t, body))
                if path == "/v1/deploy":
                    t = self._require_tenant()
                    if not t:
                        return
                    body = self._read_json()
                    if body is None:
                        return
                    return self._send(200, svc.deploy_app(t, body))
                if path == "/v1/lint":
                    if role not in ("tenant", "admin", "operator", "viewer"):
                        return self._send(403, {"error": {"code": "forbidden", "message": "authentication required"}})
                    body = self._read_json()
                    if body is None or not isinstance(body, dict):
                        return
                    from serverless.deployments import lint_python_source
                    res = lint_python_source(
                        source_code=str(body.get("source_code") or ""),
                        filename=str(body.get("filename") or "user_app.py"),
                        extra_files=body.get("files") if isinstance(body.get("files"), dict) else None,
                        timeout_s=2.0,
                        entrypoint=str(body.get("entrypoint") or ""),
                    )
                    return self._send(200, res)
                if path.startswith("/v1/apps/") and path.endswith("/rollback"):
                    t = self._require_tenant()
                    if not t:
                        return
                    app_name = path[len("/v1/apps/"):-len("/rollback")].strip("/")
                    body = self._read_json(allow_empty=True)
                    if body is None:
                        return
                    return self._send(200, svc.rollback_app(t, app_name, body.get("version") if isinstance(body, dict) else None))
                if path.startswith("/v1/web/"):
                    parts = [p for p in path[len("/v1/web/"):].split("/") if p]
                    if len(parts) != 3:
                        return self._send(404, {"error": {"code": "not_found", "message": self.path}})
                    ctype = self.headers.get("Content-Type", "")
                    if "application/json" not in ctype.lower():
                        return self._send(415, {"error": {"code": "unsupported_media_type", "message": "web endpoints accept application/json only"}})
                    body = self._read_json(allow_empty=True)
                    if body is None:
                        return
                    return self._send(200, svc.invoke_web(tenant, role, parts[0], parts[1], parts[2], body))
                if path == "/v1/admin/state":
                    if role not in ("admin", "operator"):
                        return self._send(403, {"error": {"code": "forbidden", "message": "admin or operator role required"}})
                    body = self._read_json()
                    if body is None or not isinstance(body, dict):
                        return
                    return self._send(200, svc.set_admin_state(enabled=body.get("enabled"), draining=body.get("draining"), actor=user))
                if path == "/v1/internal/reclaim-metric":
                    body = self._read_json(allow_empty=True)
                    if body is None or not isinstance(body, dict):
                        return
                    return self._send(200, svc.record_reclaim(
                        float(body.get("latency_s", 0.0)),
                        evicted=int(body.get("evicted", 0)),
                        orphans=int(body.get("orphans", 0)),
                        reason=str(body.get("reason", "reclaim")),
                    ))
                self._send(404, {"error": {"code": "not_found", "message": self.path}})
            except ServiceError as e:
                self._fail(e)
            except VolumeError as e:
                self._vol_fail(e)
            except Exception as e:  # never leak internals to the tenant
                svc.metrics.inc("sl_internal_errors_total", "Unexpected errors", type=type(e).__name__)
                self._send(500, {"error": {"code": "internal", "message": "internal error"}})

        def do_PUT(self):
            self._body_drained = False
            if not self._trusted():
                return self._send(401, {"error": {"code": "unauthorized", "message": "gateway secret required"}})
            path, _ = self._split_path()
            role = self.headers.get("X-Aicf-Role", "")
            tenant = self.headers.get("X-Aicf-Tenant", "")
            user = self.headers.get("X-Aicf-User", role)
            try:
                if path.startswith("/v1/volumes/"):
                    t = self._require_tenant()
                    if not t:
                        return
                    rest = urllib.parse.unquote(path[len("/v1/volumes/"):])
                    vol, sep, rel = rest.partition("/")
                    if not sep or not rel:
                        return self._send(400, {"error": {"code": "bad_request", "message": "volume file path required"}})
                    raw = self._read_raw()
                    if raw is None:
                        return
                    expected_sha = self.headers.get("X-Aicf-Sha256")
                    if self.headers.get("Content-Type", "").startswith("application/json"):
                        try:
                            doc = json.loads(raw)
                            if isinstance(doc, dict) and "data_b64" in doc:
                                raw = base64.b64decode(doc["data_b64"], validate=True)
                                expected_sha = doc.get("sha256") or expected_sha
                        except Exception:
                            return self._send(400, {"error": {"code": "bad_request", "message": "invalid volume JSON payload"}})
                    max_b = svc.quotas.policy(t).max_volume_bytes
                    return self._send(200, svc.volumes.put(t, vol, rel, raw, max_volume_bytes=max_b, expected_sha256=expected_sha))
                if path.startswith("/v1/model-cache/"):
                    rest = urllib.parse.unquote(path[len("/v1/model-cache/"):])
                    model, sep, rel = rest.partition("/")
                    if not sep or not rel:
                        return self._send(400, {"error": {"code": "bad_request", "message": "model cache file path required"}})
                    raw = self._read_raw()
                    if raw is None:
                        return
                    return self._send(200, svc.model_cache.put_admin(role, model, rel, raw))
                if path.startswith("/v1/policies/"):
                    if role != "admin":
                        return self._send(403, {"error": {"code": "forbidden", "message": "admin role required"}})
                    target_tenant = path[len("/v1/policies/"):].strip("/")
                    body = self._read_json()
                    if body is None or not isinstance(body, dict):
                        return
                    try:
                        from dataclasses import asdict
                        pol = svc.quotas.set_policy(target_tenant, body)
                        svc.audit.record("policy_update", tenant=target_tenant, actor=user, detail=body)
                        return self._send(200, {"tenant": target_tenant, "policy": asdict(pol)})
                    except ValueError as e:
                        return self._send(400, {"error": {"code": "bad_request", "message": str(e)}})
                if path.startswith("/v1/secrets/"):
                    name = path[len("/v1/secrets/"):].strip("/")
                    t = tenant if role == "tenant" else self.headers.get("X-Aicf-Target-Tenant", tenant)
                    if not t or role not in ("tenant", "admin"):
                        return self._send(403, {"error": {"code": "forbidden", "message": "tenant or admin role required"}})
                    body = self._read_json()
                    if body is None or not isinstance(body, dict):
                        return
                    svc.quotas.set_secret(t, name, body)
                    svc.audit.record("secret_set", tenant=t, actor=user or t, detail={"secret_name": name, "keys": sorted(body.keys())})
                    return self._send(200, {"secret": name, "keys": sorted(body.keys())})
                self._send(404, {"error": {"code": "not_found", "message": self.path}})
            except ServiceError as e:
                self._fail(e)
            except VolumeError as e:
                self._vol_fail(e)
            except Exception as e:
                svc.metrics.inc("sl_internal_errors_total", "Unexpected errors", type=type(e).__name__)
                self._send(500, {"error": {"code": "internal", "message": "internal error"}})

        def do_DELETE(self):
            if not self._trusted():
                return self._send(401, {"error": {"code": "unauthorized", "message": "gateway secret required"}})
            path, q = self._split_path()
            role = self.headers.get("X-Aicf-Role", "")
            tenant = self.headers.get("X-Aicf-Tenant", "")
            user = self.headers.get("X-Aicf-User", role)
            try:
                if path.startswith("/v1/containers/"):
                    if role not in ("tenant", "admin", "operator"):
                        return self._send(403, {"error": {"code": "forbidden", "message": "insufficient role to evict container"}})
                    cid = urllib.parse.unquote(path[len("/v1/containers/"):].strip("/"))
                    reason = q.get("reason") or ("admin" if role in ("admin", "operator") else "idle")
                    return self._send(200, svc.evict_container(cid, actor=user or tenant or role, role=role,
                                                               tenant=tenant if role == "tenant" else None, reason=reason))
                if path.startswith("/v1/calls/"):
                    call_id = path[len("/v1/calls/"):].strip("/")
                    return self._send(200, svc.cancel_call(tenant, role, call_id))
                if path.startswith("/v1/apps/"):
                    if role not in ("tenant", "admin", "operator"):
                        return self._send(403, {"error": {"code": "forbidden", "message": "insufficient role to stop app"}})
                    app_name = path[len("/v1/apps/"):].strip("/")
                    return self._send(200, svc.stop_app(tenant, app_name, role=role))
                if path.startswith("/v1/volumes/"):
                    t = self._require_tenant()
                    if not t:
                        return
                    rest = urllib.parse.unquote(path[len("/v1/volumes/"):])
                    vol, sep, rel = rest.partition("/")
                    if not sep or not rel:
                        return self._send(400, {"error": {"code": "bad_request", "message": "volume file path required"}})
                    deleted = svc.volumes.delete(t, vol, rel)
                    if not deleted:
                        return self._send(404, {"error": {"code": "not_found", "message": f"{vol}/{rel} not found"}})
                    return self._send(200, {"deleted": True, "volume": vol, "path": rel})
                self._send(404, {"error": {"code": "not_found", "message": self.path}})
            except ServiceError as e:
                self._fail(e)
            except VolumeError as e:
                self._vol_fail(e)
            except Exception as e:
                svc.metrics.inc("sl_internal_errors_total", "Unexpected errors", type=type(e).__name__)
                self._send(500, {"error": {"code": "internal", "message": "internal error"}})

    return H


def serve(svc: ServerlessService, secret: str, host: str = "127.0.0.1", port: int = 18110) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(svc, secret))
    srv.daemon_threads = True
    return srv


def main() -> int:
    import argparse
    import sys
    import threading
    from pathlib import Path
    from types import SimpleNamespace

    from agents.env_cache import EnvCache
    from agents.sandbox import GpuDockerSandbox, WarmDockerPool
    from agents.serverless_worker import ServerlessExecutor
    from agents.sl_security import Verifier, load_keys, new_key_file
    from agents.sl_store import MemoryBlobStore, MemoryStartedLedger
    from serverless.deployments import DeploymentStore
    from serverless.ledger import ServerlessAudit, UsageLedger
    from serverless.quota import Quotas, TenantPolicy
    from serverless.service import HttpLeaseClient
    from serverless.volumes import ModelCacheStore, VolumeStore

    ap = argparse.ArgumentParser(description="AICF Serverless GPU/Python HTTP service (:18110)")
    ap.add_argument("--listen", default="127.0.0.1:18110", help="host:port to listen on (loopback)")
    ap.add_argument("--gateway", default="http://127.0.0.1:18080", help="site-gateway HTTP URL for GPU lease bridge")
    ap.add_argument("--secret-file", required=True, help="File holding X-Aicf-Proxy-Secret (>=16 chars)")
    ap.add_argument("--key-file", default="", help="HMAC signing key file (auto-created in --data-dir if omitted)")
    ap.add_argument("--data-dir", required=True, help="Persistent data directory for volumes, deployments, ledger, audit, and env_cache")
    ap.add_argument("--env-store", default="", help="EnvCache store directory (defaults to .aicf-work/sl-test/store if present, else <data-dir>/env_cache)")
    ap.add_argument("--worker-id", default="pc-001", help="Local Linux worker ID")
    ap.add_argument("--gpu-enabled", action="store_true", help="Enable GPU execution on Linux worker (ADR-003 D-WIN)")
    ap.add_argument("--gpu-index", type=int, default=0, help="GPU index on Linux worker")
    ap.add_argument("--headroom-mib", type=int, default=1536, help="Free VRAM headroom MiB for G-RECLAIM")
    ap.add_argument("--control-port", type=int, default=18111, help="ControlServer port for worker-guard --serverless-evict-url")
    ap.add_argument("--max-containers", type=int, default=8, help="Max warm Docker containers in pool")
    ap.add_argument("--canary-tenant", default="demo", help="Pre-configured canary tenant ID")
    ap.add_argument("--canary-gpu-allowed", action="store_true", help="Allow GPU for the initial canary tenant policy")
    ap.add_argument("--canary-max-vram-mb", type=int, default=4096, help="Max VRAM MiB for the canary tenant")
    ap.add_argument("--default-enabled", action="store_true", help="Enable serverless + GPU by default for all gateway-authenticated tenants")
    args = ap.parse_args()

    if args.gpu_enabled and sys.platform == "win32":
        raise SystemExit("ADR-003 D-WIN: Serverless GPU (--gpu-enabled) is Linux-only in v1.0; Windows nodes run LLM-only.")

    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = Path(args.secret_file).read_text(encoding="utf-8").strip()
    if len(secret) < 16:
        raise SystemExit("--secret-file must contain at least 16 characters")

    key_file = Path(args.key_file) if args.key_file else (data_dir / "hmac.key")
    if not key_file.exists():
        new_key_file(key_file)
    keys = load_keys(key_file)

    blob_store = MemoryBlobStore()
    started_ledger = MemoryStartedLedger()
    pool = WarmDockerPool(
        GpuDockerSandbox(volumes_root=data_dir / "volumes", model_cache_root=data_dir / "model_cache"),
        max_containers=args.max_containers,
        worker_id=args.worker_id,
    )
    control = monitor = None
    if args.gpu_enabled:
        from agents.sl_gpu import ControlServer, GpuAdmission, GpuMonitor, GpuProbe
        probe = GpuProbe()
        adm = GpuAdmission(probe, args.headroom_mib)
        pool.admission = adm
        monitor = GpuMonitor(pool, probe, adm, args.gpu_index)
        monitor.start()
        control = ControlServer(pool, monitor, secret, port=args.control_port).start()

    killed_orphans = pool.kill_orphans()
    pool.start_reaper()
    default_env_store = Path(__file__).resolve().parents[1] / ".aicf-work" / "sl-test" / "store"
    env_store_dir = Path(args.env_store).resolve() if args.env_store else (default_env_store if default_env_store.exists() else (data_dir / "env_cache"))
    executor = ServerlessExecutor(
        worker_id=args.worker_id,
        verifier=Verifier(keys),
        env_cache=EnvCache(env_store_dir),
        pool=pool,
        blobs=blob_store,
        ledger=started_ledger,
        gpu_enabled=args.gpu_enabled,
        gpu_index=args.gpu_index if args.gpu_enabled else None,
    )
    executor.orphans_killed_at_start = killed_orphans
    executor.control, executor.monitor = control, monitor

    worker_labels = ("cpu", "gpu-sl", f"w-{args.worker_id}") if args.gpu_enabled else ("cpu", f"w-{args.worker_id}")
    policies: dict[str, TenantPolicy] = {}
    if args.canary_tenant:
        policies[args.canary_tenant] = TenantPolicy(
            serverless_enabled=True,
            gpu_allowed=bool(args.canary_gpu_allowed),
            max_vram_mb=args.canary_max_vram_mb,
            max_concurrency=4,
        )
    default_policy = TenantPolicy(
        serverless_enabled=bool(args.default_enabled),
        gpu_allowed=bool(args.default_enabled and args.gpu_enabled),
        max_vram_mb=args.canary_max_vram_mb,
        max_concurrency=8,
    )

    leases = HttpLeaseClient(args.gateway, secret)
    svc = ServerlessService(
        keys=keys,
        quotas=Quotas(policies, default=default_policy),
        dispatch=lambda task, attempt: executor.execute(task, attempt),
        live_workers=lambda: [SimpleNamespace(agent_id=args.worker_id, labels=worker_labels)],
        blobs=blob_store,
        leases=leases,
        volumes=VolumeStore(data_dir / "volumes"),
        model_cache=ModelCacheStore(data_dir / "model_cache"),
        deployments=DeploymentStore(data_dir / "deployments.json"),
        ledger=UsageLedger(data_dir / "usage_ledger.jsonl"),
        audit=ServerlessAudit(data_dir / "audit.jsonl"),
        storage_dir=data_dir,
    )

    stop_ev = threading.Event()

    def _lease_renewer() -> None:
        while not stop_ev.wait(10.0):
            try:
                svc.tick_leases()
            except Exception:
                pass

    threading.Thread(target=_lease_renewer, daemon=True).start()

    host, port_s = args.listen.rsplit(":", 1)
    srv = serve(svc, secret, host, int(port_s))
    print(f"serverless service listening on http://{args.listen} (worker={args.worker_id}, gpu={args.gpu_enabled})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_ev.set()
        srv.server_close()
        if monitor:
            monitor.stop()
        if control:
            control.close()
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
