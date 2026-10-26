"""A small router and HTTP server on the standard library. It exists so the service has no web-framework dependency
to patch or pin, and stays about a screen long: path templates, JSON in/out, one place that turns exceptions into
responses, size limits and identity headers set by the gateway.

The service trusts X-Aicf-User / X-Aicf-Role because it must only listen on loopback behind site-gateway, which
overwrites both headers from the caller's credential (apps/site-gateway/admin.go)."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from studio.errors import ApiError, bad, forbidden

log = logging.getLogger("studio")
ROLES = ("viewer", "operator", "admin")


@dataclass
class Request:
    method: str
    path: str
    params: dict
    query: dict
    body: dict
    user: str
    role: str
    raw: bytes = b""

    def need(self, role: str):
        """Raise 403 unless the caller's role is at least `role` (viewer < operator < admin)."""
        if ROLES.index(self.role if self.role in ROLES else "viewer") < ROLES.index(role):
            raise forbidden(f"role {self.role or 'none'} may not do this (needs {role})")

    def str_param(self, key: str, default=None, maxlen: int = 500) -> str:
        v = self.body.get(key, default)
        if not isinstance(v, str) or len(v) > maxlen:
            raise bad(f"'{key}' must be a string of at most {maxlen} characters")
        return v

    def int_param(self, key: str, default: int, lo: int, hi: int) -> int:
        v = self.body.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise bad(f"'{key}' must be an integer in [{lo}, {hi}]")
        return v

    def int_query(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int((self.query.get(key) or [default])[0])
        except ValueError:
            raise bad(f"'{key}' must be an integer") from None
        return max(lo, min(hi, v))


@dataclass
class Raw:
    """A non-JSON response (a file download)."""
    data: bytes
    content_type: str
    filename: str = ""


Handler = Callable[[Request], "tuple[int, object] | object"]


@dataclass
class Router:
    routes: list = field(default_factory=list)

    def add(self, method: str, template: str, fn: Handler):
        pattern = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "/?$")
        self.routes.append((method, pattern, fn, template))

    def match(self, method: str, path: str):
        allowed = False
        for m, pat, fn, template in self.routes:
            g = pat.match(path)
            if g:
                if m == method:
                    return fn, {k: unquote(v) for k, v in g.groupdict().items()}, template
                allowed = True
        return (None, None, "405") if allowed else (None, None, "404")


def make_handler(router: Router, max_body: int):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _send_raw(self, r: Raw):
            self.send_response(200)
            self.send_header("Content-Type", r.content_type)
            self.send_header("Content-Length", str(len(r.data)))
            if r.filename:
                self.send_header("Content-Disposition", f'attachment; filename="{r.filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(r.data)

        def _err(self, e: ApiError):
            self._send(e.status, {"error": {"code": e.code, "message": e.message}})

        def _serve(self, method: str):
            try:
                u = urlparse(self.path)
                fn, params, template = router.match(method, u.path)
                if fn is None:
                    raise ApiError(405 if template == "405" else 404, "method_not_allowed" if template == "405" else "not_found", "no such route")
                n = int(self.headers.get("Content-Length") or 0)
                if n > max_body:
                    raise ApiError(413, "too_large", f"body larger than {max_body} bytes")
                raw = self.rfile.read(n) if n else b""
                body: dict = {}
                if raw and "json" in (self.headers.get("Content-Type") or "json"):
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise bad("body is not valid JSON") from None
                    if not isinstance(body, dict):
                        raise bad("body must be a JSON object")
                req = Request(method, u.path, params, parse_qs(u.query), body, self.headers.get("X-Aicf-User") or "anonymous",
                              (self.headers.get("X-Aicf-Role") or "viewer").lower(), raw)
                out = fn(req)
                if isinstance(out, Raw):
                    return self._send_raw(out)
                code, payload = out if isinstance(out, tuple) else (200, out)
                self._send(code, payload)
            except ApiError as e:
                self._err(e)
            except Exception:       # never leak internals to the caller; the log has the traceback
                log.exception("unhandled error in %s %s", method, self.path)
                self._err(ApiError(500, "internal", "internal error"))

        def do_GET(self): self._serve("GET")
        def do_POST(self): self._serve("POST")
        def do_PUT(self): self._serve("PUT")
        def do_DELETE(self): self._serve("DELETE")

        def log_message(self, *a):
            pass

    return H


def serve(router: Router, host: str, port: int, max_body: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(router, max_body))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
