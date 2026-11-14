"""Cổng LAN/Internet (lan_chat_proxy.py) không được lộ trang Kích hoạt, trang Trạng thái hay /api/usage (chỉ admin nội bộ)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "deploy" / "demo" / "lan_chat_proxy.py"


def _load():
    spec = importlib.util.spec_from_file_location("lan_chat_proxy", SRC)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_withdrawn_and_admin_only_paths_are_blocked_on_public_port():
    m = _load()
    for path in ("/connect.html", "/connect.js", "/status.html", "/status.js"):
        assert not m.allowed("GET", path), path
    assert not m.allowed("GET", "/api/usage")


def test_chat_pages_still_served_and_root_does_not_point_at_withdrawn_page():
    m = _load()
    assert m.allowed("GET", "/chat.html") and m.allowed("POST", "/v1/chat/completions")
    assert 'Location", "/connect.html"' not in SRC.read_text(encoding="utf-8")


def test_admin_remote_mode_allows_everything_but_is_off_by_default():
    m = _load()
    assert not m.allowed("GET", "/index.html") and not m.allowed("POST", "/api/cluster/topology")
    assert m.allowed("GET", "/index.html", admin_remote=True)
    assert m.allowed("POST", "/api/cluster/topology", admin_remote=True)
    assert not m.ADMIN_FLAG.exists() or m.admin_remote_enabled()  # the flag file alone decides


def test_auth_fail_limiter_blocks_after_repeated_401_and_expires():
    m = _load()
    now = [0.0]
    lim = m.AuthFailLimiter(max_fail=3, window_s=60, block_s=100, clock=lambda: now[0])
    for _ in range(2):
        lim.record_401("1.2.3.4")
    assert not lim.blocked("1.2.3.4")
    lim.record_401("1.2.3.4")
    assert lim.blocked("1.2.3.4") and not lim.blocked("9.9.9.9")
    now[0] = 101.0
    assert not lim.blocked("1.2.3.4")
    now[0] = 500.0
    lim.record_401("5.5.5.5")
    now[0] = 700.0  # failures older than the window do not accumulate
    lim.record_401("5.5.5.5")
    lim.record_401("5.5.5.5")
    assert not lim.blocked("5.5.5.5")


def test_proxy_end_to_end_flag_toggles_admin_relay_and_brute_force_block(tmp_path):
    import http.client
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    m = _load()
    m.ADMIN_FLAG = tmp_path / "admin_remote.enabled"
    m.LIMITER = m.AuthFailLimiter(max_fail=3, window_s=60, block_s=900)

    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            ok = b"good-admin-password" in body or "demo-admin-token" in (self.headers.get("Authorization") or "")
            data = b"{}"
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST

    up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    m.Handler.upstream = ("127.0.0.1", up.server_address[1])
    m.Handler.secrets = ["demo-admin-token"]
    m.Handler.admin_users = ["admin"]
    px = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=px.serve_forever, daemon=True).start()

    def post(path, obj, token=""):
        c = http.client.HTTPConnection("127.0.0.1", px.server_address[1], timeout=5)
        hdr = {"Content-Type": "application/json"}
        if token:
            hdr["Authorization"] = "Bearer " + token
        c.request("POST", path, body=json.dumps(obj), headers=hdr)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    login = {"identifier": "admin", "password": "good-admin-password"}
    assert post("/api/auth/login", login) == 403  # default: admin name refused
    m.ADMIN_FLAG.write_text("on")
    assert post("/api/auth/login", login) == 200  # flag on: relayed to the gateway
    assert post("/api/cluster/topology", {}, token="demo-admin-token") == 200  # admin bearer token is relayed too
    for _ in range(3):
        assert post("/api/auth/login", {"identifier": "admin", "password": "wrong"}) == 401
    assert post("/api/auth/login", login) == 429  # 3 failures -> this IP is blocked even with the right password
    m.ADMIN_FLAG.unlink()
    m.LIMITER = m.AuthFailLimiter()
    assert post("/api/auth/login", login) == 403  # flag removed: closed again immediately
    assert post("/api/cluster/topology", {}, token="demo-admin-token") == 403
    px.shutdown()
    up.shutdown()


def test_tls_listener_serves_same_allowlist_over_https_and_enforces_tls12_minimum(tmp_path):
    import http.client
    import shutil
    import ssl
    import subprocess
    import threading

    if not shutil.which("openssl"):
        import pytest

        pytest.skip("openssl missing")
    crt, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    m = _load()
    m.Handler.upstream = ("127.0.0.1", 9)  # nothing listens: only the proxy's own decisions are tested
    m.Handler.secrets, m.Handler.admin_users = [], []
    m.ADMIN_FLAG = tmp_path / "absent.flag"
    srv = m.make_tls_server("127.0.0.1", 0, str(crt), str(key))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c = http.client.HTTPSConnection("127.0.0.1", srv.server_address[1], timeout=5, context=ctx)
        c.request("GET", "/connect.html")
        assert c.getresponse().status in (403, 404)  # blocked path is still blocked over TLS
        assert srv.socket.context.minimum_version == ssl.TLSVersion.TLSv1_2  # no TLS 1.0/1.1
        c3 = http.client.HTTPSConnection("127.0.0.1", srv.server_address[1], timeout=5, context=ctx)
        c3.connect()
        assert c3.sock.version() in ("TLSv1.2", "TLSv1.3")
        c3.request("GET", "/chat.html")
        assert c3.getresponse().status != 403  # allowed page is reached (upstream is down -> 502 is fine)
    finally:
        srv.shutdown()


def test_gallery_3d_needs_admin_flag_and_admin_token(tmp_path):
    import http.client
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Gw(BaseHTTPRequestHandler):  # fake gateway: only /api/whoami
        def do_GET(self):
            tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            role = {"good-admin": "admin", "a-viewer": "viewer"}.get(tok, "")
            body = json.dumps({"role": role}).encode()
            self.send_response(200 if role else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class Gal(BaseHTTPRequestHandler):  # fake gallery_server
        def do_GET(self):
            body = b"GALLERY"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *a):
            pass

    gw, gal = ThreadingHTTPServer(("127.0.0.1", 0), Gw), ThreadingHTTPServer(("127.0.0.1", 0), Gal)
    for x in (gw, gal):
        threading.Thread(target=x.serve_forever, daemon=True).start()
    m = _load()
    m.Handler.upstream = ("127.0.0.1", gw.server_address[1])
    m.GALLERY_UPSTREAM = ("127.0.0.1", gal.server_address[1])
    m.Handler.secrets, m.Handler.admin_users = [], []
    m.ADMIN_FLAG = tmp_path / "flag"
    m.LIMITER = m.AuthFailLimiter()
    m.ADMIN_CACHE = m.AdminTokenCache()
    px = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=px.serve_forever, daemon=True).start()

    def call(method, path, token=""):
        c = http.client.HTTPConnection("127.0.0.1", px.server_address[1], timeout=5)
        h = {"Authorization": "Bearer " + token} if token else {}
        c.request(method, path, body=b"{}" if method == "POST" else None, headers=h)
        r = c.getresponse()
        return r.status, r.read()

    try:
        assert call("GET", "/foundry3d.html")[0] == 403  # flag off: closed
        m.ADMIN_FLAG.write_text("")
        assert call("GET", "/foundry3d.html") == (200, b"GALLERY")  # page has no data: no token needed
        assert call("GET", "/api/foundry3d/v1/jobs")[0] == 401  # no token
        assert call("GET", "/api/foundry3d/v1/jobs", "wrong")[0] == 401
        assert call("POST", "/api/foundry3d/v1/jobs", "wrong")[0] == 401
        assert call("POST", "/api/foundry3d/v1/jobs", "a-viewer")[0] == 401  # valid login but not admin
        assert call("POST", "/api/foundry3d/v1/jobs", "good-admin") == (200, b"GALLERY")
    finally:
        for x in (px, gw, gal):
            x.shutdown()


def test_one_port_serves_both_http_and_https_and_survives_a_silent_client(tmp_path):
    import http.client
    import shutil
    import socket
    import ssl
    import subprocess
    import threading

    if not shutil.which("openssl"):
        import pytest

        pytest.skip("openssl missing")
    crt, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    m = _load()
    m.Handler.upstream = ("127.0.0.1", 9)
    m.Handler.secrets, m.Handler.admin_users = [], []
    m.ADMIN_FLAG = tmp_path / "absent.flag"
    srv = m.SniffTLSServer(("127.0.0.1", 0), m.Handler)
    srv.daemon_threads = True
    srv.ssl_ctx = m.make_tls_context(str(crt), str(key))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    silent = socket.create_connection(("127.0.0.1", port))  # connects and says nothing
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        s_ = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=ctx)
        s_.request("GET", "/connect.html")
        assert s_.getresponse().status == 403  # TLS on the same port, same rules
        h = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h.request("GET", "/connect.html")
        assert h.getresponse().status == 403  # plain HTTP still works there
    finally:
        silent.close()
        srv.shutdown()


def test_index_html_redirects_to_chat_unless_remote_admin_is_on(tmp_path):
    import http.client
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Gw(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/index.html":  # what Go's http.FileServer does
                self.send_response(301)
                self.send_header("Location", "./")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"DASHBOARD" if self.path == "/" else b"?"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    gw = ThreadingHTTPServer(("127.0.0.1", 0), Gw)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    m = _load()
    m.Handler.upstream = ("127.0.0.1", gw.server_address[1])
    m.Handler.secrets, m.Handler.admin_users = [], []
    m.ADMIN_FLAG = tmp_path / "flag"
    px = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=px.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", px.server_address[1], timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.getheader("Location"), r.read()

    try:
        assert get("/index.html")[:2] == (302, "/chat.html")  # flag off
        assert get("/")[:2] == (302, "/chat.html")
        m.ADMIN_FLAG.write_text("")
        assert get("/index.html") == (200, None, b"DASHBOARD")  # flag on: the GPU/VRAM page opens
        assert get("/")[:2] == (302, "/chat.html")  # root still goes to chat
    finally:
        px.shutdown()
        gw.shutdown()


def test_anonymous_401s_do_not_lock_a_visitor_out_but_bad_credentials_do(tmp_path):
    import http.client
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Gw(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)  # /api/whoami for everybody who is not signed in
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    gw = ThreadingHTTPServer(("127.0.0.1", 0), Gw)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    m = _load()
    m.Handler.upstream = ("127.0.0.1", gw.server_address[1])
    m.Handler.secrets, m.Handler.admin_users = [], []
    m.ADMIN_FLAG = tmp_path / "flag"
    m.ADMIN_FLAG.write_text("")
    m.LIMITER = m.AuthFailLimiter(max_fail=3, window_s=60, block_s=900)
    px = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=px.serve_forever, daemon=True).start()

    def get(token=""):
        c = http.client.HTTPConnection("127.0.0.1", px.server_address[1], timeout=5)
        c.request("GET", "/api/whoami", headers={"Authorization": "Bearer " + token} if token else {})
        return c.getresponse().status

    try:
        assert [get() for _ in range(10)] == [401] * 10  # ten anonymous page loads: still not blocked
        assert [get("bad") for _ in range(3)] == [401] * 3  # three wrong tokens ...
        assert get("bad") == 429  # ... and the IP is blocked
    finally:
        px.shutdown()
        gw.shutdown()
