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
import http.client
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
STATIC = {"/chat.html", "/common.css", "/common.js", "/chat.js", "/favicon.ico", "/serverless.html", "/serverless.js"}
MAX_BODY = 4 * 1024 * 1024
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


def allowed(method: str, path: str) -> bool:
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
        if self.command in ("GET", "HEAD") and path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/chat.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not allowed(self.command, path):
            return self._deny(403, "Đường dẫn này không được mở trên mạng LAN. Chỉ có trang chat và API /v1.")
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._deny(411, "Content-Length required")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._deny(413, "Body too large")
        body = self.rfile.read(n) if n else b""
        blob = (str(self.headers) + body.decode("utf-8", "ignore")).lower()
        if any(s and s.lower() in blob for s in self.secrets):
            return self._deny(403, "Thông tin quản trị không được dùng qua cổng LAN này.")
        if self.command == "POST" and path.startswith("/api/") and body.strip():
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
        headers["Host"] = self.headers.get("Host") or f"{self.upstream[0]}:{self.upstream[1]}"
        headers["X-Forwarded-For"] = self.client_address[0]
        conn = http.client.HTTPConnection(*self.upstream, timeout=600)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True, help="ip:port to bind (use the LAN IP, not 0.0.0.0)")
    ap.add_argument("--upstream", default="127.0.0.1:18080")
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
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    print(f"listening on {host}:{port} -> {a.upstream}; blocking {len(Handler.secrets)} token strings and admin users {Handler.admin_users}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
