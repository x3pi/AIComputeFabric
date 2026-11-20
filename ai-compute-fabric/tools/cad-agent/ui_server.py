#!/usr/bin/env python3
"""Browser chat for the CAD agent: a local web page (opened in Chrome) with the model's streamed reply, every SolidWorks
tool step as a card, screenshots, and Allow/Deny buttons for the risky tools.

    python ui_server.py [--port 8765] [--no-open]

Security model: the server listens on 127.0.0.1 only; every request needs a random per-run token (set as an HttpOnly,
SameSite=Strict cookie by the first URL the launcher opens); the Host header must be this machine (blocks DNS-rebinding);
POSTs also need a custom header and a same-origin Origin. Design data never leaves the PC: the page talks only to this process.
One conversation at a time (SolidWorks is a single shared session)."""
import argparse, http.server, json, os, secrets, shutil, socketserver, subprocess, sys, threading, time, urllib.parse, webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import aicfconf
import sw_agent as A                     # starts solidworks-mcp and defines the tool loop

TOKEN = secrets.token_urlsafe(24)
PAGE = os.path.join(ROOT, "ui_page.html")
CONFIRM_TIMEOUT = 600                    # a risky call nobody answers within 10 minutes is denied

lock = threading.Lock()
events = []                              # [{"id","t","kind",...}] full history of this run, in memory
images = []                              # data URIs of screenshots, served at /img/<n>
state = {"busy": False, "stop": False, "pending": None, "port": 0}
msgs = [{"role": "system", "content": A.SYSTEM}]
chat_log = None


def push(kind, **kw):
    global chat_log
    with lock:
        ev = {"id": len(events) + 1, "t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        events.append(ev)
    try:
        if chat_log is None:
            d = os.path.join(aicfconf.data_dir(A.CFG), "chats")
            os.makedirs(d, exist_ok=True)
            chat_log = open(os.path.join(d, "chat-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")), "a", encoding="utf-8")
        if kind != "text" and kind != "think":           # token deltas are not logged one by one; text_end carries the full text
            chat_log.write(json.dumps(ev, ensure_ascii=False) + "\n")
            chat_log.flush()
    except OSError:
        pass
    return ev


_buf = {"text": "", "think": ""}


def emit(kind, **kw):
    if kind in ("text", "think"):
        _buf[kind] += kw["text"]
        push(kind, text=kw["text"])
    elif kind == "text_end":
        full, _buf["text"], _buf["think"] = _buf["text"], "", ""
        push("text_end", full=full)
    elif kind == "tool_result":
        urls = []
        for u in kw.get("images", []):
            with lock:
                images.append(u)
                urls.append("/img/%d" % (len(images) - 1))
        push("tool_result", name=kw["name"], result=kw["result"][:6000], images=urls)
    else:
        push(kind, **kw)


def confirm(name, args):
    cid = secrets.token_hex(6)
    ev = threading.Event()
    state["pending"] = {"id": cid, "event": ev, "allow": False}
    push("confirm", cid=cid, name=name, args=args)
    end = time.time() + CONFIRM_TIMEOUT
    while time.time() < end and not state["stop"]:
        if ev.wait(0.5):
            break
    allow = state["pending"]["allow"] if state["pending"] else False
    state["pending"] = None
    push("confirm_done", cid=cid, allow=allow)
    return allow


A.EMIT, A.CONFIRM_FN, A.SHOULD_STOP = emit, confirm, lambda: state["stop"]


def run_turn(text):
    state["stop"] = False
    try:
        msgs.append({"role": "user", "content": text})
        A.audit("user", text=text)
        A.turn(msgs)
    except Exception as e:
        push("error", text="%s: %s" % (type(e).__name__, e))
    finally:
        _buf["text"] = _buf["think"] = ""
        state["busy"] = False
        push("done")


class H(http.server.BaseHTTPRequestHandler):
    server_version = "aicf-cad-ui"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % state["port"], "localhost:%d" % state["port"])

    def _cookie_ok(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "aicf" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def _guard(self, post):
        if not self._host_ok():
            self._send(403, {"error": "bad host"})
            return False
        if not self._cookie_ok():
            self._send(401, {"error": "open the link printed by the launcher (it carries the access token)"})
            return False
        if post:
            origin = self.headers.get("Origin")
            if self.headers.get("X-Requested-With") != "aicf" or (origin and origin not in ("http://127.0.0.1:%d" % state["port"], "http://localhost:%d" % state["port"])):
                self._send(403, {"error": "bad origin"})
                return False
        return True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/" and "t" in q:                           # first visit: swap the URL token for a cookie, then hide it
            if not self._host_ok() or not secrets.compare_digest(q["t"][0], TOKEN):
                return self._send(403, {"error": "bad token"})
            return self._send(302, b"", "text/plain", {"Location": "/", "Set-Cookie": "aicf=%s; HttpOnly; SameSite=Strict; Path=/" % TOKEN})
        if not self._guard(False):
            return
        if u.path == "/":
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html")
        if u.path == "/api/state":
            return self._send(200, {"busy": state["busy"], "model": A.MODEL, "tools": len(A.TOOLS), "think": A.think, "last": len(events)})
        if u.path == "/api/events":
            since = int(q.get("since", ["0"])[0])
            with lock:
                out = events[since:since + 400]
            return self._send(200, {"events": out, "busy": state["busy"]})
        if u.path.startswith("/img/"):
            try:
                uri = images[int(u.path[5:])]
                head, _, b64 = uri.partition(",")
                import base64
                return self._send(200, base64.b64decode(b64), head[5:].split(";")[0] or "image/png")
            except (ValueError, IndexError):
                return self._send(404, {"error": "no image"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard(True):
            return
        n = min(int(self.headers.get("Content-Length") or 0), 200000)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        p = self.path
        if p == "/api/send":
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "empty"})
            with lock:
                if state["busy"]:
                    return self._send(409, {"error": "the agent is still working; press Stop or wait"})
                state["busy"] = True
            push("user", text=text)
            threading.Thread(target=run_turn, args=(text,), daemon=True).start()
            return self._send(200, {"ok": True})
        if p == "/api/confirm":
            pend = state["pending"]
            if not pend or pend["id"] != body.get("cid"):
                return self._send(409, {"error": "nothing to confirm"})
            pend["allow"] = bool(body.get("allow"))
            pend["event"].set()
            return self._send(200, {"ok": True})
        if p == "/api/stop":
            state["stop"] = True
            return self._send(200, {"ok": True})
        if p == "/api/think":
            A.think = bool(body.get("on"))
            return self._send(200, {"think": A.think})
        if p == "/api/reset":
            if state["busy"]:
                return self._send(409, {"error": "stop the running task first"})
            msgs[:] = [{"role": "system", "content": A.SYSTEM}]
            push("reset")
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def find_chrome():
    names = ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = Server(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    if not srv:
        sys.exit("no free port in %d-%d" % (a.port, a.port + 19))
    state["port"] = port
    url = "http://127.0.0.1:%d/?t=%s" % (port, TOKEN)
    print("AICF CAD Agent chat is running (model %s, %d tools)." % (A.MODEL, len(A.TOOLS)))
    print("Open this link in Chrome if it does not open by itself (it is private to this PC):\n  " + url)
    print("Close this window or press Ctrl+C to stop.")
    if not a.no_open:
        chrome = find_chrome()
        try:
            if chrome:
                subprocess.Popen([chrome, "--new-window", url])
            else:
                webbrowser.open(url)
        except Exception as e:
            print("could not launch the browser automatically:", e)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state["stop"] = True
        A.srv.terminate()


if __name__ == "__main__":
    main()
