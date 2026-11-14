#!/usr/bin/env python3
"""Filtering reverse proxy that exposes ONLY the chat page and the OpenAI-style /v1 API of the local site-gateway to a LAN.

Why: the demo gateway runs with the default admin token and a weak admin password, so it must not be bound to the LAN
directly. This proxy forwards a fixed allowlist to http://127.0.0.1:18080 and refuses everything else (cluster topology,
tenants, studio, agents, serverless, ollama-emulator config ...). Requests/bodies that carry the real admin token or the
admin user name are refused too, so the weak admin credentials cannot be used through this port.

Usage:  python3 lan_chat_proxy.py --listen 192.168.1.40:18080 [--upstream 127.0.0.1:18080]
Stop:   kill <pid>   (nothing is persisted)
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
STATIC = {"/chat.html", "/common.css", "/common.js", "/chat.js", "/favicon.ico", "/serverless.html", "/serverless.js"}
# Remote admin (owner decision 2026-11-14, risk accepted): while this file exists next to the script the proxy relays ANY path
# and stops refusing admin credentials. Delete the file to close it again (takes effect on the next request, no restart).
ADMIN_FLAG = Path(__file__).with_name("admin_remote.enabled")
MAX_BODY = 32 * 1024 * 1024  # base64 images in chat requests
LOGIN_PATHS = {"/api/auth/login", "/api/serverless/v1/auth/login"}  # where an admin user name is refused

# (method, exact path or compiled regex)
API = [
    ("GET", "/api/whoami"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/change-password"),
    ("GET", "/api/auth/sessions"),
    ("DELETE", re.compile(r"^/api/auth/sessions/[A-Za-z0-9_.~%-]+$")),
    ("GET", "/api/ollama-emulator/status"),
    ("GET", "/api/ollama-emulator/bridge-script"),  # download of the Windows .bat / Linux+macOS sh bridge (config POST stays blocked)
    # user space of the serverless page: sign-up / login / key only. deploy, spawn, containers, volumes, web, apps,
    # policies, calls and admin/* run tenant code on the GPU host and stay blocked on the LAN port.
    ("GET", "/api/serverless/v1/status"),
    ("POST", "/api/serverless/v1/signup"),
    ("POST", "/api/serverless/v1/auth/register"),
    ("POST", "/api/serverless/v1/auth/login"),
    ("POST", "/api/serverless/v1/auth/logout"),
    ("POST", "/api/serverless/v1/auth/rotate-key"),
    # Ollama-compatible endpoints (Codex, Cursor, Continue, Open WebUI ... pointing OLLAMA_HOST/OPENAI_BASE_URL at this port)
    ("GET", "/api/tags"), ("GET", "/api/version"), ("GET", "/api/ps"),
    ("POST", "/api/show"), ("POST", "/api/chat"), ("POST", "/api/generate"), ("POST", "/api/embed"), ("POST", "/api/embeddings"),
    ("GET", re.compile(r"^/api/ollama/v1/[A-Za-z0-9_./-]*$")),
    ("POST", re.compile(r"^/api/ollama/v1/[A-Za-z0-9_./-]*$")),
    ("POST", "/v1/responses"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("GET", "/v1/models"),
]


def admin_remote_enabled() -> bool:
    try:
        return ADMIN_FLAG.exists()
    except OSError:
        return False


class AuthFailLimiter:
    """Blocks an IP for `block_s` after `max_fail` HTTP 401 answers within `window_s` (token / password guessing)."""

    def __init__(self, max_fail: int = 60, window_s: float = 300.0, block_s: float = 300.0, clock=time.monotonic) -> None:
        self.max_fail, self.window_s, self.block_s, self.clock = max_fail, window_s, block_s, clock
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._mu = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._mu:
            until = self._blocked.get(ip, 0.0)
            if until and self.clock() < until:
                return True
            self._blocked.pop(ip, None)
            return False

    def record_401(self, ip: str) -> None:
        now = self.clock()
        with self._mu:
            xs = [t for t in self._fails.get(ip, []) if now - t < self.window_s] + [now]
            self._fails[ip] = xs
            if len(xs) >= self.max_fail:
                self._blocked[ip] = now + self.block_s
                self._fails[ip] = []


LIMITER = AuthFailLimiter()

# 3D Foundry web UI (foundry3d.gallery_server --enable-create on localhost). Remote use needs the admin flag AND a valid
# admin Bearer token on every /api/foundry3d/ call (the static page itself holds no data and no secrets).
GALLERY_UPSTREAM = ("127.0.0.1", 18130)
GALLERY_PAGES = {"/foundry3d.html", "/foundry3d.js"}
GALLERY_API_PREFIX = "/api/foundry3d/"


def is_gallery_path(path: str) -> bool:
    return path in GALLERY_PAGES or path.startswith(GALLERY_API_PREFIX)


class AdminTokenCache:
    """Asks the gateway (/api/whoami) whether a Bearer token is an admin; positive answers are cached for `ttl` seconds."""

    def __init__(self, ttl: float = 30.0, clock=time.monotonic) -> None:
        self.ttl, self.clock = ttl, clock
        self._ok: dict[str, float] = {}
        self._mu = threading.Lock()

    def is_admin(self, authorization: str, upstream: tuple[str, int]) -> bool:
        tok = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
        if not tok:
            return False
        key = hashlib.sha256(tok.encode()).hexdigest()
        now = self.clock()
        with self._mu:
            if self._ok.get(key, 0.0) > now:
                return True
        try:
            c = http.client.HTTPConnection(*upstream, timeout=5)
            c.request("GET", "/api/whoami", headers={"Authorization": authorization})
            r = c.getresponse()
            ok = r.status == 200 and json.loads(r.read().decode("utf-8")).get("role") == "admin"
            c.close()
        except (OSError, ValueError):
            return False
        if ok:
            with self._mu:
                self._ok[key] = now + self.ttl
        return ok


ADMIN_CACHE = AdminTokenCache()


def allowed(method: str, path: str, admin_remote: bool = False) -> bool:
    if admin_remote:
        return method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH")
    if method in ("GET", "HEAD") and (path in STATIC or path.startswith("/vendor/")):
        return True
    for m, p in API:
        if method == m and ((p.match(path) if hasattr(p, "match") else p == path)):
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aicf-lan-chat-proxy"
    upstream = ("127.0.0.1", 18080)
    secrets: list[str] = []  # admin token strings: never relayed anywhere
    admin_users: list[str] = []  # admin user names: refused in login bodies only

    def log_message(self, fmt, *args):  # one line per request on stderr
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _deny(self, code: int, msg: str) -> None:
        body = json.dumps({"error": {"code": "blocked_by_lan_proxy", "message": msg}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        # "/" always lands on the chat page; the admin dashboard (/index.html) is reachable only while remote admin is on.
        if self.command in ("GET", "HEAD") and (path == "/" or (path == "/index.html" and not admin_remote_enabled())):
            self.send_response(302)
            self.send_header("Location", "/chat.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        admin_remote = admin_remote_enabled()
        if admin_remote and path == "/index.html":
            self.path = "/"  # Go's file server 301-redirects /index.html to /; fetch the dashboard directly instead
        if admin_remote and LIMITER.blocked(self.client_address[0]):
            return self._deny(429, "Quá nhiều lần xác thực sai. Thử lại sau 15 phút.")
        upstream = self.upstream
        if is_gallery_path(path):
            if not admin_remote:
                return self._deny(403, "Trang 3D chưa được mở từ xa.")
            if path.startswith(GALLERY_API_PREFIX) and not ADMIN_CACHE.is_admin(self.headers.get("Authorization") or "", self.upstream):
                LIMITER.record_401(self.client_address[0])
                return self._deny(401, "Cần đăng nhập admin để dùng 3D.")
            upstream = GALLERY_UPSTREAM
        elif not allowed(self.command, path, admin_remote):
            return self._deny(403, "Đường dẫn này không được mở trên mạng LAN. Chỉ có trang chat và API /v1.")
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._deny(411, "Content-Length required")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._deny(413, "Body too large")
        body = self.rfile.read(n) if n else b""
        blob = (str(self.headers) + body.decode("utf-8", "ignore")).lower()
        if not admin_remote and any(s and s.lower() in blob for s in self.secrets):
            return self._deny(403, "Thông tin quản trị không được dùng qua cổng LAN này.")
        if not admin_remote and self.command == "POST" and path.startswith("/api/") and body.strip():
            # The gateway JSON-decodes the body (\\uXXXX escapes!), so compare DECODED string values, never the raw text.
            try:
                doc = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._deny(400, "JSON không hợp lệ")
            vals: list[str] = []

            def walk(x):
                if isinstance(x, str):
                    vals.append(x)
                elif isinstance(x, dict):
                    for k, v in x.items():
                        walk(k)
                        walk(v)
                elif isinstance(x, list):
                    for v in x:
                        walk(v)

            walk(doc)
            norm = [re.sub(r"\s+|\x00", "", v).casefold() for v in vals]
            if any(t.casefold() in n for n in norm for t in self.secrets) or (path in LOGIN_PATHS and any(n == u.casefold() for n in norm for u in self.admin_users)):
                return self._deny(403, "Tài khoản/token quản trị không dùng được qua cổng LAN này.")

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
        headers["Host"] = self.headers.get("Host") or f"{upstream[0]}:{upstream[1]}"
        headers["X-Forwarded-For"] = self.client_address[0]
        conn = http.client.HTTPConnection(*upstream, timeout=600)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            # Only a 401 for a request that CARRIED credentials (Bearer header or a login POST) counts as a failed guess;
            # an anonymous page load (/api/whoami without a token -> 401) is normal and must not lock the visitor out.
            if admin_remote and resp.status == 401 and (self.headers.get("Authorization") or (self.command == "POST" and path in LOGIN_PATHS)):
                LIMITER.record_401(self.client_address[0])
            clen = resp.getheader("Content-Length")
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            head_only = self.command == "HEAD" or resp.status in (204, 304)
            if head_only:
                if clen is not None:
                    self.send_header("Content-Length", clen)
                self.end_headers()
                return
            if clen is not None:
                self.send_header("Content-Length", clen)
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            else:  # streaming (SSE): relay as chunked, flush every piece
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # upstream down etc.
            try:
                self._deny(502, f"Gateway không phản hồi: {type(e).__name__}")
            except Exception:
                pass
        finally:
            conn.close()

    do_GET = do_POST = do_DELETE = do_HEAD = _handle

    def do_OPTIONS(self):  # no CORS preflight support: same-origin pages only
        self._deny(403, "CORS not available on this port")


class SniffTLSServer(ThreadingHTTPServer):
    """One port, two protocols: a connection whose first byte is 0x16 (TLS ClientHello) is TLS-wrapped, anything else stays plain HTTP.
    The sniff happens in the per-connection worker thread, so a silent client cannot stall the accept loop."""

    ssl_ctx: ssl.SSLContext | None = None

    def finish_request(self, request, client_address):
        wrapped = None
        try:
            request.settimeout(10)
            first = request.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first == b"\x16" and self.ssl_ctx is not None:
                wrapped = self.ssl_ctx.wrap_socket(request, server_side=True)
                request = wrapped
            request.settimeout(None)
            super().finish_request(request, client_address)
        except (OSError, ssl.SSLError):
            pass
        finally:
            if wrapped is not None:
                try:
                    wrapped.close()
                except OSError:
                    pass


def make_tls_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def make_tls_server(host: str, port: int, cert: str, key: str) -> ThreadingHTTPServer:
    """Same Handler (same allowlist, limiter and admin rules) behind TLS 1.2+; the cert is read once at start."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True, help="ip:port to bind (use the LAN IP, not 0.0.0.0)")
    ap.add_argument("--upstream", default="127.0.0.1:18080")
    ap.add_argument("--tls-listen", default="", help="optional extra HTTPS listener ip:port (same rules as --listen)")
    ap.add_argument("--tls-sniff", action="store_true", help="also accept TLS on the --listen port itself (HTTP keeps working there)")
    ap.add_argument("--tls-cert", default="", help="PEM certificate chain (leaf first) for --tls-listen")
    ap.add_argument("--tls-key", default="", help="PEM private key for --tls-listen")
    ap.add_argument("--gateway-pid", type=int, default=0, help="read AICF_ADMIN_TOKEN/USER from this process env (so they can be blocked)")
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)
    uh, up = a.upstream.rsplit(":", 1)
    Handler.upstream = (uh, int(up))
    secrets = [os.environ.get("AICF_ADMIN_TOKEN", "demo-admin-token")]
    users = [os.environ.get("AICF_ADMIN_USER", "")]
    if a.gateway_pid:
        try:
            env = dict(kv.split("=", 1) for kv in open(f"/proc/{a.gateway_pid}/environ").read().split("\0") if "=" in kv)
            secrets += [env.get("AICF_ADMIN_TOKEN", "")]
            users += [env.get("AICF_ADMIN_USER", "")]
        except OSError as e:
            sys.exit(f"cannot read gateway env: {e}")
    secrets += ["demo-admin-token"]
    Handler.secrets = [s for s in dict.fromkeys(secrets) if len(s) >= 8]
    Handler.admin_users = [u for u in dict.fromkeys(users + ["admin"]) if u]
    if a.tls_sniff:
        if not (a.tls_cert and a.tls_key):
            sys.exit("--tls-sniff needs --tls-cert and --tls-key")
        srv = SniffTLSServer((host, int(port)), Handler)
        srv.ssl_ctx = make_tls_context(a.tls_cert, a.tls_key)
    else:
        srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    if a.tls_listen:
        if not (a.tls_cert and a.tls_key):
            sys.exit("--tls-listen needs --tls-cert and --tls-key")
        th, tp = a.tls_listen.rsplit(":", 1)
        tsrv = make_tls_server(th, int(tp), a.tls_cert, a.tls_key)
        threading.Thread(target=tsrv.serve_forever, daemon=True).start()
        print(f"https listening on {th}:{tp} (TLS>=1.2)", flush=True)
    print(f"listening on {host}:{port} -> {a.upstream}; blocking {len(Handler.secrets)} token strings and admin users {Handler.admin_users}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
