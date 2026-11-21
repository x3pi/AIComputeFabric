"""Web chat for cadfabric: `cadfabric serve`. Stdlib only, one job at a time, one part per session.
Security: binds 127.0.0.1 unless told otherwise; every request needs the access token (URL ?t=... once, then a cookie); POSTs need the X-Requested-With header;
the Host header must be one we serve (blocks DNS rebinding); Origin, when present, must match; request bodies are capped."""
import hmac, http.server, json, os, re, secrets, socketserver, threading, time, urllib.parse
from http import cookies
from . import agent, config, llm as llm_mod
from .cq_backend import CadQueryBackend

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_page.html")
MAX_BODY = 40_000
EXTERNAL = ("codex", "codex-relay")


class Session:
    """The conversation, the part and the event log shared by all requests."""

    def __init__(self, llm, cfg, backend=None, local=True):
        self.llm, self.cfg, self.local, self.quit = llm, cfg, local, False
        self.backend = backend or CadQueryBackend()
        self.lock = threading.Lock()
        self.events, self.busy, self.stop, self.history, self.measure = [], False, False, None, None
        self.thread = None

    def emit(self, kind, **kw):
        with self.lock:
            self.events.append(dict(kw, kind=kind, t=round(time.time(), 1)))

    def state(self, since):
        with self.lock:
            return {"busy": self.busy, "events": self.events[since:], "next": len(self.events), "measure": self.measure, "has_part": self.measure is not None,
                    "follow_up": self.history is not None}

    def send(self, text, planner):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.stop = True, False
        self.emit("user", text=text, planner=planner if self.history is None else "(follow-up: no plan)")
        self.thread = threading.Thread(target=self._work, args=(text, planner), daemon=True)
        self.thread.start()
        return True

    def _work(self, text, planner):
        try:
            r = agent.run(self.llm, self.backend, text, plan_mode=planner, log=lambda s: None, cfg=self.cfg,
                          history=self.history, should_stop=lambda: self.stop, on_event=lambda kind, **kw: self.emit(kind, **kw))
            self.history = r["history"]
            m = self.backend.measure()
            with self.lock:
                self.measure = m.as_dict() if m else None
            self.emit("final", text=r["final"] or ("(stopped)" if r.get("stopped") else "(step limit reached without a final answer)"), warnings=r["warnings"], gates=r["gates"],
                      steps=r["steps"], sec=r["sec"])
        except Exception as e:                                                   # noqa: BLE001 - show the failure in the chat
            self.emit("error", text="%s: %s" % (type(e).__name__, str(e)[:300]))
        finally:
            with self.lock:
                self.busy = False

    def apply_settings(self, clean, key):
        """Save and hot-reload. Refused while a job runs."""
        with self.lock:
            if self.busy:
                return None
        path = config.save_settings(clean, key)
        cfg = config.load()
        self.cfg, self.llm = cfg, llm_mod.LLM(cfg)
        return path

    def reset(self):
        with self.lock:
            if self.busy:
                return False
            self.events, self.history, self.measure = [], None, None
        self.backend.reset()
        return True

    def close(self):
        self.backend.close()


def public_settings(cfg):
    """What the settings dialog may see. The key is never sent back, only whether one is set."""
    try:
        has_key = bool(config.read_key(cfg))
    except SystemExit:
        has_key = False
    return {"url": cfg["url"], "model": cfg["model"], "planner": cfg["planner"], "relay": cfg.get("relay") or {}, "has_key": has_key,
            "configured": bool(cfg.get("_path")), "config_path": cfg.get("_path") or config.user_config_path()}


def make_handler(sess, token, hosts, data_dir):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "cadfabric"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=()):
            b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(b)

        def _auth(self, query):
            host = (self.headers.get("Host") or "").lower()
            if host not in hosts:
                self._send(403, {"error": "bad host"}); return None
            c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
            given = (query.get("t") or [None])[0] or (c["cft"].value if "cft" in c else "")
            if not given or not hmac.compare_digest(given, token):
                self._send(401, {"error": "access token required: open the URL printed by `cadfabric serve`"}); return None
            return "t" in query

        def _csrf(self):
            if self.headers.get("X-Requested-With") != "cadfabric":
                self._send(403, {"error": "missing X-Requested-With"}); return False
            o = self.headers.get("Origin")
            if o and urllib.parse.urlparse(o).netloc.lower() not in hosts:
                self._send(403, {"error": "bad origin"}); return False
            return True

        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
            fresh = self._auth(q)
            if fresh is None:
                return
            cookie = [("Set-Cookie", "cft=%s; HttpOnly; SameSite=Strict; Path=/" % token)] if fresh else []
            if u.path == "/":
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8", cookie)
            elif u.path == "/api/state":
                self._send(200, sess.state(int((q.get("since") or ["0"])[0] or 0)), extra=cookie)
            elif u.path == "/api/config":
                self._send(200, {"planners": list(config.PLANNERS), "external": list(EXTERNAL), "default": sess.cfg["planner"], "version": __import__("cadfabric").__version__,
                                 "local": sess.local, "settings": public_settings(sess.cfg)})
            elif u.path == "/api/part.svg":
                try:
                    self._send(200, sess.backend.preview_svg(), "image/svg+xml")
                except Exception as e:                                           # noqa: BLE001
                    self._send(404, {"error": str(e)[:200]})
            elif u.path == "/api/part.step":
                if not sess.measure:
                    return self._send(404, {"error": "no part yet"})
                p = os.path.join(data_dir, "web-part-%d.step" % int(time.time()))
                try:
                    sess.backend.export_step(p)
                    with open(p, "rb") as f:
                        self._send(200, f.read(), "application/octet-stream", [("Content-Disposition", 'attachment; filename="part.step"')])
                finally:
                    if os.path.exists(p): os.unlink(p)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if self._auth({}) is None or not self._csrf():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if u.path == "/api/send":
                text, planner = str(body.get("text", "")).strip(), body.get("planner") or sess.cfg["planner"]
                if not text or len(text) > 8000:
                    return self._send(400, {"error": "empty or too long"})
                if planner not in config.PLANNERS:
                    return self._send(400, {"error": "unknown planner"})
                if planner in EXTERNAL and not body.get("confirm_external"):
                    return self._send(409, {"error": "this planner sends your request to an external service; confirm_external is required"})
                ok = sess.send(text, planner)
                self._send(200 if ok else 409, {"ok": True} if ok else {"error": "busy"})
            elif u.path in ("/api/settings", "/api/test", "/api/quit"):
                if not sess.local:
                    return self._send(403, {"error": "settings and quit are only available when the chat is bound to this computer (no --allow-remote)"})
                if u.path == "/api/quit":
                    sess.quit = True; self._send(200, {"ok": True}); return
                cur = sess.cfg
                clean, err = config.validate_settings({"url": body.get("url", cur["url"]), "model": body.get("model", cur["model"]),
                                                       "planner": body.get("planner", cur["planner"]), "relay": body.get("relay", cur.get("relay") or {})})
                if err:
                    return self._send(400, {"error": err})
                key = str(body.get("key") or "").strip()
                if len(key) > 500 or re.search(r"[\r\n]", key):
                    return self._send(400, {"error": "key is not valid"})
                if u.path == "/api/test":
                    return self._send(200, llm_mod.probe(clean["url"], clean["model"], key or config.read_key(cur)))
                path = sess.apply_settings(clean, key)
                self._send(200 if path else 409, {"ok": True, "path": path, "settings": public_settings(sess.cfg)} if path else {"error": "busy: wait for the current job"})
            elif u.path == "/api/stop":
                sess.stop = True; self._send(200, {"ok": True})
            elif u.path == "/api/reset":
                self._send(200 if sess.reset() else 409, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
    return H


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(llm, cfg, host="127.0.0.1", port=8765, token=None, backend=None, quiet=False):
    token = token or secrets.token_urlsafe(18)
    extra = [h.strip() for h in os.environ.get("CADFABRIC_HOSTS", "").split(",") if h.strip()]      # names/IPs remote users type, with --allow-remote
    hosts = {("%s:%d" % (h, port)).lower() for h in ("127.0.0.1", "localhost", host, *extra)}
    sess = Session(llm, cfg, backend, local=host in ("127.0.0.1", "localhost", "::1"))
    srv = Server((host, port), make_handler(sess, token, hosts, config.data_dir(cfg)))
    srv.sess, srv.token, srv.port = sess, token, srv.server_address[1]
    srv.hosts = hosts
    if not quiet:
        print("cadfabric web chat: http://%s:%d/?t=%s" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, srv.port, token), flush=True)
    return srv
