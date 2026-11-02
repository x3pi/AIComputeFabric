"""UI web cuc bo (stdlib). Bind 127.0.0.1 + token. Key chi nam trong bo nho, khong ghi log/audit."""
import json, mimetypes, os, secrets, threading, time, uuid
from . import lessons as lessons_mod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PAGE = r"""<!doctype html><meta charset=utf-8><title>wagent</title>
<style>body{font:14px system-ui;margin:20px;max-width:900px}input,textarea{width:100%;padding:6px;margin:3px 0}
button{padding:6px 14px;margin:4px}pre{background:#111;color:#ddd;padding:10px;height:260px;overflow:auto}
.ap{border:2px solid #c60;padding:8px;margin:6px 0}</style>
<h2>wagent - dieu khien Windows bang model local</h2>
<input id=url placeholder="URL model, vd http://10.42.0.1:18090/v1">
<input id=model placeholder="model (tuy chon)"><input id=key type=password placeholder="API key (tuy chon)">
<textarea id=task rows=3 placeholder="Nhiem vu..."></textarea>
<button onclick=run()>Chay</button><button onclick=stop() style="background:#c33;color:#fff">DUNG</button>
<div id=st></div><div id=ap></div><pre id=log></pre>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
const H={'X-Token':T,'Content-Type':'application/json'};
const $=i=>document.getElementById(i);
async function run(){await fetch('/api/run',{method:'POST',headers:H,body:JSON.stringify({url:$('url').value,model:$('model').value,key:$('key').value,task:$('task').value})});}
async function stop(){await fetch('/api/stop',{method:'POST',headers:H,body:'{}'});}
async function appr(id,a){await fetch('/api/approve',{method:'POST',headers:H,body:JSON.stringify({id,allow:a})});}
async function tick(){try{const s=await (await fetch('/api/state',{headers:H})).json();
$('st').textContent='Trang thai: '+s.status+(s.summary?' - '+s.summary:'');
$('log').textContent=s.log.join('\n');
$('ap').innerHTML=s.pending.map(p=>`<div class=ap><b>Can duyet:</b> ${p.reason}<br><code>${JSON.stringify(p.action)}</code><br>
<button onclick="appr('${p.id}',true)">Cho phep</button><button onclick="appr('${p.id}',false)">Tu choi</button></div>`).join('');}catch(e){}}
setInterval(tick,1000);tick();
</script>"""

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status, self.summary, self.log, self.pending = "idle", "", [], {}
        self.stop = threading.Event()
        self.last = None     # {"task","status","actions","lesson"} cua lan chay gan nhat (de luu thanh bai hoc)

class WebApprover:
    def __init__(self, state, timeout=120):
        self.s, self.timeout = state, timeout

    def __call__(self, action, reason):
        pid, ev = uuid.uuid4().hex[:8], threading.Event()
        with self.s.lock:
            self.s.pending[pid] = {"id": pid, "action": action, "reason": reason, "ev": ev, "ok": False}
        ev.wait(self.timeout)
        with self.s.lock:
            return self.s.pending.pop(pid)["ok"] if ev.is_set() else (self.s.pending.pop(pid), False)[1]

def list_models(url, key="", timeout=5, _open=None):
    """Hoi server model (OpenAI-compatible) danh sach model: GET <url>/models. Chi http/https, toi da 1 MB. Tra ve [id]."""
    import urllib.request
    u = urlparse(str(url))
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("URL phai la http(s)://host[:port]/v1")
    req = urllib.request.Request(str(url).rstrip("/") + "/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with (_open or urllib.request.urlopen)(req, timeout=timeout) as r:
        raw = r.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("phan hoi qua lon")
    data = json.loads(raw).get("data")
    if not isinstance(data, list):
        raise ValueError("phan hoi khong phai danh sach model OpenAI")
    return [str(m["id"]) for m in data if isinstance(m, dict) and "id" in m]

def serve(make_agent, host="127.0.0.1", port=8765, token=None, approve_timeout=120, ui_dir=None, thread_ctx=None, lessons_dir=None):
    """make_agent(cfg, log, approver, stop_event) -> obj co .run(task)->(status, summary)
    thread_ctx: ham tra ve context manager, vao/ra tren CHINH luong chay nhiem vu (Windows: khoi tao COM cho UI Automation)."""
    token = token or secrets.token_urlsafe(16)
    st = State()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _ok(self):
            q = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest(self.headers.get("X-Token", q.get("t", [""])[0]), token)
        def _send(self, code, body, ct="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code); self.send_header("Content-Type", ct + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def _static(self, p):
            root = os.path.realpath(ui_dir)
            rel = "index.html" if p in ("", "/") else p.lstrip("/")
            f = os.path.realpath(os.path.join(root, rel))
            if not (f == root or f.startswith(root + os.sep)) or not os.path.isfile(f):
                return self._send(404, '{"error":"not found"}')
            ct = mimetypes.guess_type(f)[0] or "application/octet-stream"
            self.send_response(200); self.send_header("Content-Type", ct)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
            data = open(f, "rb").read()
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            p = urlparse(self.path).path
            if ui_dir and not p.startswith("/api/"):
                return self._static(p)       # file tinh cua UI khong chua bi mat; /api/* van can token
            if not self._ok(): return self._send(403, '{"error":"token"}')
            if p == "/": return self._send(200, PAGE, "text/html")
            if p == "/api/state":
                with st.lock:
                    pend = [{k: v for k, v in x.items() if k not in ("ev", "ok")} for x in st.pending.values()]
                    return self._send(200, json.dumps({"status": st.status, "summary": st.summary,
                                                       "log": st.log[-200:], "pending": pend,
                                                       "can_save_lesson": bool(lessons_dir and st.last and st.last["status"] == "done" and st.last["actions"]),
                                                       "lesson_used": (st.last or {}).get("lesson")}))
            if p == "/api/lessons":
                if not lessons_dir: return self._send(404, '{"error":"bai hoc chua bat"}')
                return self._send(200, json.dumps({"lessons": [lessons_mod.summary(x) for x in lessons_mod.load_all(lessons_dir)]}))
            self._send(404, "{}")
        def do_POST(self):
            if not self._ok(): return self._send(403, '{"error":"token"}')
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            p = urlparse(self.path).path
            if p == "/api/run":
                with st.lock:
                    if st.status == "running": return self._send(409, '{"error":"dang chay"}')
                    st.status, st.summary, st.log = "running", "", []
                    st.stop.clear()
                threading.Thread(target=self._work, args=(d,), daemon=True).start()
                return self._send(200, '{"ok":true}')
            if p == "/api/models":
                try:
                    return self._send(200, json.dumps({"models": list_models(d.get("url", ""), d.get("key", ""))}))
                except Exception as e:     # khong lo noi dung phan hoi cua server dich; chi loai loi
                    code = getattr(e, "code", None)
                    msg = ("server model tra HTTP %s%s" % (code, " (can API key?)" if code in (401, 403) else "")) if code else \
                          "%s: %s" % (type(e).__name__, e)
                    return self._send(502, json.dumps({"error": msg[:200]}))
            if p in ("/api/lessons", "/api/lessons/delete"):
                if not lessons_dir: return self._send(404, '{"error":"bai hoc chua bat"}')
                try:
                    if p == "/api/lessons/delete":
                        lessons_mod.delete(lessons_dir, str(d.get("id", "")))
                        return self._send(200, '{"ok":true}')
                    with st.lock:
                        last = dict(st.last) if st.last else None
                    if not last or last["status"] != "done":
                        return self._send(409, '{"error":"chi luu duoc bai hoc tu lan chay vua hoan thanh (done)"}')
                    les = lessons_mod.from_run(last["task"], last["actions"], title=str(d.get("title", "")))
                    les = lessons_mod.unique_id(lessons_dir, les)
                    lessons_mod.save(lessons_dir, les)
                    return self._send(200, json.dumps({"lesson": lessons_mod.summary(les)}))
                except FileNotFoundError:
                    return self._send(404, '{"error":"khong co bai hoc nay"}')
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)[:200]}))
            if p == "/api/stop":
                st.stop.set()
                with st.lock:
                    for x in st.pending.values(): x["ev"].set()   # ok=False => tu choi
                return self._send(200, '{"ok":true}')
            if p == "/api/approve":
                with st.lock:
                    x = st.pending.get(d.get("id"))
                    if x: x["ok"] = bool(d.get("allow")); x["ev"].set()
                return self._send(200, '{"ok":true}')
            self._send(404, "{}")
        def _work(self, cfg):
            def log(m):
                with st.lock: st.log.append(str(m))
            actions, lesson = [], None
            try:
                import contextlib, gc
                with (thread_ctx() if thread_ctx else contextlib.nullcontext()):
                    ag = None
                    try:
                        ag = make_agent(cfg, log, WebApprover(st, approve_timeout), st.stop)
                        s, m = ag.run(cfg.get("task", ""))
                    finally:
                        actions = list(getattr(ag, "actions", []) or [])
                        lesson = getattr(ag, "lesson_used", None)
                        ag = None
                        gc.collect()      # giai phong con tro COM (UIA) TRUOC khi thoat COM; sau CoUninitialize se crash 0xc0000005
            except Exception as e:
                s, m = "failed", "%s: %s" % (type(e).__name__, e)
            with st.lock:
                st.status, st.summary = s, m
                st.last = {"task": str(cfg.get("task", "")), "status": s, "actions": actions, "lesson": lesson}

    srv = ThreadingHTTPServer((host, port), H)
    srv.token = token
    return srv
