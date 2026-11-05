"""HTTP wrapper (stdlib, same pattern as bestofn/service.py). Reached only through site-gateway: every request must carry
X-Aicf-Proxy-Secret (ADR-003 D2); X-Aicf-Tenant is trusted only with it. Bind to 127.0.0.1."""
from __future__ import annotations

import hmac
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from serverless.service import ServerlessService, ServiceError

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

        def do_GET(self):
            if self.path == "/v1/healthz":
                return self._send(200, {"ok": True})
            if not self._trusted():
                return self._send(401, {"error": {"code": "unauthorized", "message": "gateway secret required"}})
            if self.path == "/v1/metrics":
                return self._send(200, svc.metrics.render(), "text/plain; version=0.0.4")
            self._send(404, {"error": {"code": "not_found", "message": self.path}})

        def do_POST(self):
            if not self._trusted():
                return self._send(401, {"error": {"code": "unauthorized", "message": "gateway secret required"}})
            if self.path != "/v1/invoke":
                return self._send(404, {"error": {"code": "not_found", "message": self.path}})
            tenant = self.headers.get("X-Aicf-Tenant", "")
            if self.headers.get("X-Aicf-Role") != "tenant" or not tenant:
                return self._send(403, {"error": {"code": "tenant_key_required", "message": "calls must use a tenant API key"}})
            try:
                n = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._send(413 if n > MAX_BODY else 411, {"error": {"code": "bad_length", "message": "Content-Length required, max 16 MiB"}})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": {"code": "bad_request", "message": "body is not JSON"}})
            try:
                self._send(200, svc.invoke(tenant, body))
            except ServiceError as e:
                self._fail(e)
            except Exception as e:  # never leak internals to the tenant
                svc.metrics.inc("sl_internal_errors_total", "Unexpected errors", type=type(e).__name__)
                self._send(500, {"error": {"code": "internal", "message": "internal error"}})

    return H


def serve(svc: ServerlessService, secret: str, host: str = "127.0.0.1", port: int = 18110) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(svc, secret))
    srv.daemon_threads = True
    return srv
