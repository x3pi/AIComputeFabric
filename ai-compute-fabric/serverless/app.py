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
            try:
                if path == "/v1/metrics":
                    return self._send(200, svc.metrics.render(), "text/plain; version=0.0.4")
                if path in ("/v1/status", "/v1/overview"):
                    return self._send(200, svc.overview(tenant=tenant if role == "tenant" else q.get("tenant")))
                if path == "/v1/usage":
                    totals = svc.ledger.totals_by_tenant()
                    if role == "tenant":
                        totals = {tenant: totals.get(tenant, {"tenant": tenant, "calls": 0, "idle_records": 0, "gpu_ms": 0, "gpu_seconds": 0.0, "vram_mib_s": 0})}
                    return self._send(200, {"usage": totals})
                if path == "/v1/containers":
                    return self._send(200, {"containers": svc.list_containers(tenant=tenant if role == "tenant" else q.get("tenant"))})
                if path == "/v1/apps":
                    return self._send(200, {"apps": svc.deployments.list_apps(tenant=tenant if role == "tenant" else q.get("tenant"))})
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
                    wait_s = float(q.get("wait", "0"))
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
