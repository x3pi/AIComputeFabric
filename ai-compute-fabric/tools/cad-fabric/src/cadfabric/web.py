"""Web chat for cadfabric: `cadfabric serve`. Stdlib only, one job at a time, one part per session.
Security: binds 127.0.0.1 unless told otherwise; every request needs the access token (URL ?t=... once, then a cookie); POSTs need the X-Requested-With header;
the Host header must be one we serve (blocks DNS rebinding); Origin, when present, must match; request bodies are capped."""
import hmac, http.server, json, os, re, secrets, socketserver, threading, time, urllib.parse
from http import cookies
from . import advisors, agent, config, history as history_mod, llm as llm_mod, viewers, workspace as ws_mod
from .cq_backend import CadQueryBackend

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_page.html")
MAX_BODY = 40_000


class Session:
    """The conversation, the part and the event log shared by all requests."""

    def __init__(self, llm, cfg, backend=None, local=True):
        self.llm, self.cfg, self.local, self.quit = llm, cfg, local, False
        self.backend = backend or CadQueryBackend()
        self.lock = threading.Lock()
        self.events, self.busy, self.stop, self.history, self.measure = [], False, False, None, None
        self.thread = None
        self.sid, self.created, self.title = None, "", ""          # the saved-session record this conversation is written to

    def emit(self, kind, **kw):
        with self.lock:
            self.events.append(dict(kw, kind=kind, t=round(time.time(), 1)))

    def state(self, since):
        with self.lock:
            return {"busy": self.busy, "events": self.events[since:], "next": len(self.events), "measure": self.measure, "has_part": self.measure is not None,
                    "follow_up": self.history is not None}

    def send(self, text, planner, files=()):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.stop = True, False
        if not self.sid:
            self.sid, self.created, self.title = history_mod.new_id(), time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(text.split())[:60]
        self.emit("user", files=list(files), text=text, planner=planner if self.history is None else "(follow-up: no plan)")
        self.thread = threading.Thread(target=self._work, args=(text, planner, tuple(files)), daemon=True)
        self.thread.start()
        return True

    def _work(self, text, planner, files=()):
        try:
            root = self.cfg.get("workspace") or ""
            block, _used, err = ws_mod.attachments(root, files) if files else ("", [], "")
            if err:
                raise ValueError("attached file: " + err)
            r = agent.run(self.llm, self.backend, text, plan_mode=planner, log=lambda s: None, cfg=self.cfg, workspace=root or None, attachments=block,
                          history=self.history, should_stop=lambda: self.stop, on_event=lambda kind, **kw: self.emit(kind, **kw),
                          opener=(lambda: self.open_in_viewer()[1]) if self.local else None)
            self.history = r["history"]
            m = self.backend.measure()
            with self.lock:
                self.measure = m.as_dict() if m else None
            self.emit("final", text=r["final"] or ("(stopped)" if r.get("stopped") else "(step limit reached without a final answer)"), warnings=r["warnings"], gates=r["gates"],
                      steps=r["steps"], sec=r["sec"])
        except Exception as e:                                                   # noqa: BLE001 - show the failure in the chat
            self.emit("error", text="%s: %s" % (type(e).__name__, str(e)[:300]))
        finally:
            self.save_history()
            with self.lock:
                self.busy = False

    def save_history(self):
        """Write the conversation to disk (after every turn, even a failed one). A disk problem must never break the chat."""
        if not self.sid or not self.local:
            return
        try:
            with self.lock:
                evs = list(self.events)
            history_mod.save(config.data_dir(self.cfg), {"id": self.sid, "title": self.title, "created": self.created, "events": evs, "messages": self.history,
                                                          "cells": list(getattr(self.backend, "history", [])), "measure": self.measure})
        except (OSError, TypeError, ValueError):
            pass

    def load_history(self, sid):
        """Reopen a saved conversation: rebuild the part by replaying its cells, restore the chat and the model's context. -> (status, body)."""
        rec = history_mod.load(config.data_dir(self.cfg), sid) if history_mod.valid(sid) else None
        if not rec:
            return 404, {"error": "no such saved session"}
        with self.lock:
            if self.busy:
                return 409, {"error": "busy: wait for the current job"}
            self.busy = True
        try:
            self.backend.reset()
            bad = 0
            for code in rec.get("cells") or []:
                if isinstance(code, str) and not self.backend.execute(code).ok:
                    bad += 1
            m = self.backend.measure() if rec.get("cells") else None
            with self.lock:
                self.events = list(rec["events"])
                self.history = rec.get("messages") if isinstance(rec.get("messages"), list) else None
                self.measure = m.as_dict() if m else None
                self.sid, self.created, self.title = rec["id"], rec.get("created", ""), rec.get("title", "")
            return 200, {"ok": True, "id": sid, "replay_failed": bad, "has_part": m is not None}
        finally:
            with self.lock:
                self.busy = False

    def apply_settings(self, clean, key, advisor_keys=None):
        """Save and hot-reload. Refused while a job runs."""
        with self.lock:
            if self.busy:
                return None
        path = config.save_settings(clean, key, advisor_keys)
        cfg = config.load()
        self.cfg, self.llm = cfg, llm_mod.LLM(cfg)
        return path

    def open_in_viewer(self):
        """Export the part to <data dir>/exports and open it in the chosen CAD program. -> (http status, body)."""
        if not self.measure:
            return 404, {"error": "no part yet"}
        d = os.path.join(config.data_dir(self.cfg), "exports"); os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "part-%s.step" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            self.backend.export_step(p)
        except Exception as e:                                                   # noqa: BLE001
            return 500, {"error": "export failed: %s" % str(e)[:160]}
        for old in sorted(os.listdir(d))[:-20]:                                   # keep the last 20 exports
            try: os.unlink(os.path.join(d, old))
            except OSError: pass
        r = viewers.open_file(p, self.cfg); r["path"] = p
        return 200, r

    def reset(self):
        with self.lock:
            if self.busy:
                return False
            self.events, self.history, self.measure = [], None, None
            self.sid, self.created, self.title = None, "", ""
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
    return {"url": cfg["url"], "model": cfg["model"], "planner": cfg["planner"], "relay": cfg.get("relay") or {}, "has_key": has_key, "advisors": advisors.meta(cfg),
            "viewer": cfg.get("viewer") or {"kind": "auto", "command": ""}, "viewer_label": viewers.resolve(cfg)[1], "viewers_detected": viewers.detected(), "workspace": cfg.get("workspace") or "",
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
                self._send(200, {"planners": advisors.names(sess.cfg), "external": [n for n in advisors.names(sess.cfg) if advisors.is_external(sess.cfg, n)], "default": sess.cfg["planner"],
                                 "advisors": advisors.meta(sess.cfg), "cli_detected": advisors.detected(sess.cfg), "cli_presets": advisors.presets_info(), "viewer_label": viewers.resolve(sess.cfg)[1], "version": __import__("cadfabric").__version__,
                                 "local": sess.local, "settings": public_settings(sess.cfg)})
            elif u.path == "/api/history":
                self._send(200, {"sessions": history_mod.list_sessions(data_dir) if sess.local else [], "current": sess.sid}, extra=cookie)
            elif u.path == "/api/files":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                r, err = ws_mod.listing(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0])
                self._send(200 if r else 400, dict(r, root=os.path.basename((sess.cfg.get("workspace") or "").rstrip("/\\"))) if r else {"error": err})
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
                if planner not in advisors.names(sess.cfg):
                    return self._send(400, {"error": "unknown planner"})
                if advisors.is_external(sess.cfg, planner) and not body.get("confirm_external"):
                    return self._send(409, {"error": "this planner sends your request to an external service; confirm_external is required"})
                files = body.get("files") or []
                if files and (not sess.local or not isinstance(files, list) or len(files) > 8 or not all(isinstance(x, str) and len(x) < 300 for x in files)):
                    return self._send(400, {"error": "attached files are only available locally (at most 8)"})
                _b, _u, ferr = ws_mod.attachments(sess.cfg.get("workspace") or "", files) if files else ("", [], "")
                if ferr:
                    return self._send(400, {"error": ferr})
                ok = sess.send(text, planner, files)
                self._send(200 if ok else 409, {"ok": True} if ok else {"error": "busy"})
            elif u.path in ("/api/settings", "/api/test", "/api/quit", "/api/advisor_test", "/api/open"):
                if not sess.local:
                    return self._send(403, {"error": "settings and quit are only available when the chat is bound to this computer (no --allow-remote)"})
                if u.path == "/api/quit":
                    sess.quit = True; self._send(200, {"ok": True}); return
                cur = sess.cfg
                saved = {a["id"]: a for a in cur.get("advisors") or []}
                if u.path == "/api/open":
                    return self._send(*sess.open_in_viewer())
                if u.path == "/api/advisor_test":
                    a, err = advisors.validate(dict(body.get("advisor") or {}))
                    if err:
                        return self._send(400, {"error": err})
                    if a["type"] == "api":
                        a["key_file"] = (saved.get(a["id"]) or {}).get("key_file")         # never a path chosen by the browser
                    k = str(body.get("key") or "").strip()
                    return self._send(200, advisors.self_test(a, cur, key=k if k else None))
                for a in body.get("advisors") or []:
                    if isinstance(a, dict):
                        a.pop("key_file", None)                                               # the browser never chooses which file is read as a key
                adv_in = body.get("advisors", [{k: v for k, v in a.items() if k != "key_file"} for a in saved.values()])
                clean, err = config.validate_settings({"url": body.get("url", cur["url"]), "model": body.get("model", cur["model"]), "advisors": adv_in, "viewer": body.get("viewer", cur.get("viewer") or {}), "workspace": body.get("workspace", cur.get("workspace") or ""),
                                                       "planner": body.get("planner", cur["planner"]), "relay": body.get("relay", cur.get("relay") or {})})
                if err:
                    return self._send(400, {"error": err})
                key = str(body.get("key") or "").strip()
                if len(key) > 500 or re.search(r"[\r\n]", key):
                    return self._send(400, {"error": "key is not valid"})
                if u.path == "/api/test":
                    return self._send(200, llm_mod.probe(clean["url"], clean["model"], key or config.read_key(cur)))
                akeys = {str(k): str(v).strip() for k, v in (body.get("advisor_keys") or {}).items() if isinstance(v, str) and v.strip() and len(v) < 500 and not re.search(r"[\r\n]", v)}
                path = sess.apply_settings(clean, key, akeys)
                self._send(200 if path else 409, {"ok": True, "path": path, "settings": public_settings(sess.cfg)} if path else {"error": "busy: wait for the current job"})
            elif u.path in ("/api/history/load", "/api/history/delete", "/api/history/clear"):
                if not sess.local:
                    return self._send(403, {"error": "saved sessions are only available when the chat is bound to this computer"})
                sid = body.get("id")
                if u.path == "/api/history/clear":
                    if sess.busy:
                        return self._send(409, {"error": "busy"})
                    sess.sid = None
                    return self._send(200, {"ok": True, "deleted": history_mod.clear(data_dir)})
                if not history_mod.valid(sid):
                    return self._send(400, {"error": "bad id"})
                if u.path == "/api/history/load":
                    return self._send(*sess.load_history(sid))
                if sid == sess.sid:
                    sess.sid = None                                                         # deleting the open conversation: the next message starts a fresh record
                self._send(200, {"ok": history_mod.delete(data_dir, sid)})
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
