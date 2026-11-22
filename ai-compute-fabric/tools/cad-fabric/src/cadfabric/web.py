"""Web chat for cadfabric: `cadfabric serve`. Stdlib only, one job at a time, one part per session.
Security: binds 127.0.0.1 unless told otherwise; every request needs the access token (URL ?t=... once, then a cookie); POSTs need the X-Requested-With header;
the Host header must be one we serve (blocks DNS rebinding); Origin, when present, must match; request bodies are capped."""
import hmac, http.server, json, os, re, secrets, socketserver, subprocess, sys, threading, time, urllib.parse
from http import cookies
from . import advisors, agent, config, history as history_mod, llm as llm_mod, viewers, workspace as ws_mod
from . import cq_backend
from .cq_backend import CadQueryBackend

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_page.html")
MAX_BODY = 40_000


def spawn_window(project, session=None):
    """Start another CadFabric window for `project` (optionally with a saved conversation open). Detached: it outlives this one. -> {ok, detail}."""
    exe = sys.executable
    if os.name == "nt":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.isfile(w) else exe
    argv = [exe, "-m", "cadfabric", "app", "--project", project] + (["--session", session] if session else [])
    try:
        viewers._spawn(argv)
    except OSError as e:
        return {"ok": False, "detail": "could not open a new window: %s" % str(e)[:100]}
    return {"ok": True, "detail": "opened a new window"}


class Session:
    """The conversation, the part and the event log shared by all requests."""

    def __init__(self, llm, cfg, backend=None, local=True):
        self.llm, self.cfg, self.local, self.quit = llm, cfg, local, False
        self.pinned = cfg.get("workspace") or ""   # this window's project; a different folder opens another window
        self.autofile = None                    # the STEP file this conversation keeps up to date in the project folder
        self.picker = None                      # set by the desktop window: () -> folder path or ''
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

    def send(self, text, planner, files=(), uploads=None):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.stop = True, False
        if not self.sid:
            self.sid, self.created, self.title = history_mod.new_id(), time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(text.split())[:60]
        self.emit("user", files=list(files) + [u["name"] for u in (uploads or [])], text=text, planner=planner if self.history is None else "(follow-up: no plan)")
        self.thread = threading.Thread(target=self._work, args=(text, planner, tuple(files), uploads or []), daemon=True)
        self.thread.start()
        return True

    def _work(self, text, planner, files=(), uploads=()):
        try:
            root = self.cfg.get("workspace") or ""
            block, _used, err = ws_mod.attachments(root, files) if files else ("", [], "")
            if err:
                raise ValueError("attached file: " + err)
            ublock, _n, uerr = ws_mod.uploads(list(uploads)) if uploads else ("", [], "")
            if uerr:
                raise ValueError("dropped file: " + uerr)
            block = "\n\n".join(x for x in (block, ublock) if x)
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
            self.autosave()
            self.save_history()
            with self.lock:
                self.busy = False

    def autosave(self):
        """Keep the current part in the project folder by default: <name>.step plus the CadQuery script <name>.py. One pair per conversation:
        the first turn picks a fresh name (never an existing file), later turns rewrite that pair. A disk problem must never break the chat."""
        root = self.cfg.get("workspace") or ""
        if not root or not self.local or not self.measure or not getattr(self.backend, "history", None):
            return
        try:
            real = os.path.realpath(root)
            if not (self.autofile and os.path.dirname(self.autofile) == real and os.path.isfile(self.autofile)):
                import re, unicodedata
                slug = unicodedata.normalize("NFKD", (self.title or "part").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
                slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:40] or "part"
                for i in range(100):                                       # the .py name must be free too
                    p, err = ws_mod.new_step_path(root, slug + ("" if i == 0 else "-%d" % i))
                    if err:
                        return
                    if not os.path.exists(p[:-5] + ".py"):
                        break
                self.autofile, first = p, True
            else:
                first = False
            tmp = os.path.join(os.path.dirname(self.autofile), ".cadfabric-tmp.step")
            self.backend.export_step(tmp)
            os.replace(tmp, self.autofile)
            with open(self.autofile[:-5] + ".py", "w", encoding="utf-8") as f:
                f.write("# CadQuery script that rebuilds this part (written by CadFabric)\n" + "\n\n# ---- next step ----\n".join(self.backend.history) + "\n")
            self.emit("file", op="saved", path=os.path.basename(self.autofile) + (" (+ .py)" if first else " (cập nhật)"))
        except Exception:                                                  # noqa: BLE001
            pass

    def save_history(self):
        """Write the conversation to disk (after every turn, even a failed one). A disk problem must never break the chat."""
        if not self.sid or not self.local:
            return
        try:
            with self.lock:
                evs = list(self.events)
            history_mod.save(config.data_dir(self.cfg), {"id": self.sid, "title": self.title, "created": self.created, "events": evs, "messages": self.history,
                                                          "cells": list(getattr(self.backend, "history", [])), "measure": self.measure,
                                                          "workspace": history_mod.norm(self.cfg.get("workspace") or "")})
        except (OSError, TypeError, ValueError):
            pass

    def load_history(self, sid):
        """Reopen a saved conversation: rebuild the part by replaying its cells, restore the chat and the model's context. -> (status, body)."""
        rec = history_mod.load(config.data_dir(self.cfg), sid) if history_mod.valid(sid) else None
        if not rec:
            return 404, {"error": "no such saved session"}
        rws, cur = rec.get("workspace") or "", self.cfg.get("workspace") or ""
        if rws and history_mod.norm(rws) != history_mod.norm(cur):             # it belongs to another project: open that project's window
            if not os.path.isdir(rws):
                return 404, {"error": "the project folder of this conversation no longer exists: " + rws}
            r = spawn_window(rws, sid)
            return (200, {"ok": True, "opened_window": rws}) if r["ok"] else (500, {"error": r["detail"]})
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
                self.sid, self.created, self.title = rec["id"], rec.get("created", ""), rec.get("title", ""); self.autofile = None
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
        if "workspace" in clean:
            self.pinned = clean["workspace"] or ""
        cfg["workspace"] = self.pinned                                          # another window's choice never leaks into this one
        self.cfg, self.llm = cfg, llm_mod.LLM(cfg)
        return path

    def set_project(self, path):
        """Choose the project. First project of this window: bound here. A different folder than the one this window already has: it opens in a NEW window,
        this one keeps its project (and its conversations). -> (http status, body)."""
        ws, err = ws_mod.check_folder(path)
        if err:
            return 400, {"error": err}
        cur = self.cfg.get("workspace") or ""
        if not ws:                                                      # empty = leave the project
            return (200, {"workspace": "", "recent": self.recent()}) if self.apply_settings({"workspace": "", "relay": {}}, None) is not None else (409, {"error": "busy"})
        recent = [ws] + [p for p in (self.cfg.get("recent") or []) if p != ws]
        if cur and os.path.realpath(ws) != os.path.realpath(cur):
            r = spawn_window(ws)
            if not r["ok"]:
                return 500, {"error": r["detail"]}
            self.apply_settings({"recent": recent[:8], "relay": {}}, None)
            return 200, {"workspace": cur, "recent": self.recent(), "opened_window": ws}
        if self.apply_settings({"workspace": ws, "recent": recent[:8], "relay": {}}, None) is None:
            return 409, {"error": "busy"}
        return 200, {"workspace": ws, "recent": self.recent()}

    def recent(self):
        return [p for p in (self.cfg.get("recent") or []) if isinstance(p, str) and os.path.isdir(p)]

    def open_in_viewer(self):
        """Export the part to <data dir>/exports and open it in the chosen CAD program. -> (http status, body)."""
        if not self.measure:
            return 404, {"error": "no part yet"}
        d = os.path.join(config.data_dir(self.cfg), "exports"); os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "part-%s.step" % time.strftime("%Y%m%d-%H%M%S"))
        if self.autofile and os.path.isfile(self.autofile):                       # the part is already in the project folder: open that file
            d, p = os.path.dirname(self.autofile), self.autofile
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
            self.autofile = None
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

        def _static(self, rel):
            """Bundled viewer libraries (syntax colouring, Markdown, 3D). Whitelisted names only, long cache: they never change within a version."""
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
            full = os.path.realpath(os.path.join(base, *rel.split("/"))) if re.match(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", rel) and ".." not in rel else ""
            if not full or not full.startswith(base + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".wasm": "application/wasm", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}.get(os.path.splitext(full)[1].lower())
            if not ctype:
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers(); self.wfile.write(data)

        def _send(self, code, body, ctype="application/json", extra=()):
            b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:")
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
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/":
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8", cookie)
            elif u.path == "/api/state":
                self._send(200, sess.state(int((q.get("since") or ["0"])[0] or 0)), extra=cookie)
            elif u.path == "/api/config":
                self._send(200, {"planners": advisors.names(sess.cfg), "external": [n for n in advisors.names(sess.cfg) if advisors.is_external(sess.cfg, n)], "default": sess.cfg["planner"],
                                 "advisors": advisors.meta(sess.cfg), "cli_detected": advisors.detected(sess.cfg), "cli_presets": advisors.presets_info(), "viewer_label": viewers.resolve(sess.cfg)[1], "version": __import__("cadfabric").__version__,
                                 "recent": sess.recent() if sess.local else [], "local": sess.local, "picker": bool(sess.picker and sess.local), "settings": public_settings(sess.cfg)})
            elif u.path == "/api/history":
                every = (q.get("all") or [""])[0] == "1"
                self._send(200, {"sessions": history_mod.list_sessions(data_dir, None if every else sess.cfg.get("workspace") or "") if sess.local else [], "current": sess.sid,
                                 "project": sess.cfg.get("workspace") or "", "all": every}, extra=cookie)
            elif u.path == "/api/files":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                r, err = ws_mod.listing(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0])
                self._send(200 if r else 400, dict(r, root=os.path.basename((sess.cfg.get("workspace") or "").rstrip("/\\"))) if r else {"error": err})
            elif u.path == "/api/file":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                rel = (q.get("path") or [""])[0]
                text, err = ws_mod.read_text(sess.cfg.get("workspace") or "", rel)
                self._send(200 if text is not None else 400, {"path": rel, "text": text} if text is not None else {"error": err})
            elif u.path == "/api/stepmesh":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                full, _m, err = ws_mod.binary_file(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0], "step")
                if err:
                    return self._send(400, {"error": err})
                try:
                    r = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stepmesh.py"), full], capture_output=True, timeout=90,
                                       env=cq_backend.worker_env(), **cq_backend.NOWIN)
                except subprocess.TimeoutExpired:
                    return self._send(504, {"error": "the 3D conversion took too long"})
                if r.returncode != 0 or not r.stdout.startswith(b"{"):
                    return self._send(422, {"error": "this STEP file could not be converted for 3D"})
                self._send(200, r.stdout)
            elif u.path in ("/api/raw", "/api/preview"):
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                kind = "image" if u.path == "/api/raw" else "step"
                full, mime, err = ws_mod.binary_file(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0], kind)
                if err:
                    return self._send(400, {"error": err})
                lock = [("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")]
                if kind == "image":
                    with open(full, "rb") as f:
                        return self._send(200, f.read(), mime, lock)
                try:
                    r = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stepview.py"), full], capture_output=True, timeout=90,
                                       env=cq_backend.worker_env(), **cq_backend.NOWIN)
                except subprocess.TimeoutExpired:
                    return self._send(504, {"error": "the preview took too long"})
                if r.returncode != 0 or not r.stdout.startswith(b"<?xml") and not r.stdout.startswith(b"<svg"):
                    return self._send(422, {"error": "this STEP file could not be drawn"})
                self._send(200, r.stdout, "image/svg+xml", lock)
            elif u.path == "/api/browse":
                if not sess.local:
                    return self._send(403, {"error": "folder browsing is only available when the chat is bound to this computer"})
                cur = os.path.abspath(os.path.expanduser((q.get("path") or [""])[0] or sess.cfg.get("workspace") or "~"))
                if not os.path.isdir(cur):
                    return self._send(400, {"error": "not a folder"})
                try:
                    names = sorted((n for n in os.listdir(cur) if not n.startswith(".") and os.path.isdir(os.path.join(cur, n))), key=str.lower)
                except OSError as e:
                    return self._send(400, {"error": str(e)[:120]})
                self._send(200, {"path": cur, "parent": os.path.dirname(cur) if os.path.dirname(cur) != cur else "", "dirs": names[:500]})
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
                ups = body.get("uploads") or []
                if ups:
                    if not sess.local or len(files) + len(ups) > 8:
                        return self._send(400, {"error": "dropped files are only available locally (at most 8 files in total)"})
                    _b, _u, uerr = ws_mod.uploads(ups)
                    if uerr:
                        return self._send(400, {"error": uerr})
                ok = sess.send(text, planner, files, ups)
                self._send(200 if ok else 409, {"ok": True} if ok else {"error": "busy"})
            elif u.path in ("/api/settings", "/api/test", "/api/quit", "/api/advisor_test", "/api/open", "/api/pick_folder", "/api/project", "/api/reveal", "/api/open_file", "/api/open_cad", "/api/open_blender"):
                if not sess.local:
                    return self._send(403, {"error": "settings and quit are only available when the chat is bound to this computer (no --allow-remote)"})
                if u.path == "/api/quit":
                    sess.quit = True; self._send(200, {"ok": True}); return
                if u.path in ("/api/reveal", "/api/open_file", "/api/open_cad", "/api/open_blender"):
                    rel = body.get("path") if isinstance(body, dict) else ""
                    root = sess.cfg.get("workspace") or ""
                    if rel in ("", "."):                                                    # the project folder itself
                        full, err = (os.path.realpath(root), "") if root else (None, "no working folder is set")
                    else:
                        full, err = ws_mod.resolve(root, rel)
                    if err or not full or not os.path.exists(full):
                        return self._send(400, {"error": err or "no such file"})
                    if u.path == "/api/reveal":
                        r = viewers.reveal(full)
                    elif os.path.isdir(full):
                        return self._send(400, {"error": "that is a folder"})
                    elif u.path == "/api/open_file":
                        r = viewers.open_default(full)
                    elif u.path == "/api/open_blender":
                        r = viewers.open_blender(full)
                    else:
                        if os.path.splitext(full)[1].lower() not in (".step", ".stp"):
                            return self._send(400, {"error": "only STEP files open in the CAD program"})
                        r = viewers.open_file(full, sess.cfg)
                    return self._send(200 if r.get("ok") else 500, r)
                if u.path == "/api/project":
                    return self._send(*sess.set_project(body.get("path") if isinstance(body, dict) else ""))
                if u.path == "/api/pick_folder":
                    if not sess.picker:
                        return self._send(404, {"error": "no native folder dialog in this mode"})
                    return self._send(200, {"path": sess.picker() or ""})
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
                    return self._send(200, {"ok": True, "deleted": history_mod.clear(data_dir, None if body.get("all") else sess.cfg.get("workspace") or "")})
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
