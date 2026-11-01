"""UI web cuc bo (stdlib). Bind 127.0.0.1 + token. Key chi nam trong bo nho, khong ghi log/audit."""
import json, mimetypes, os, secrets, threading, time, uuid
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

def serve(make_agent, host="127.0.0.1", port=8765, token=None, approve_timeout=120, ui_dir=None):
    """make_agent(cfg, log, approver, stop_event) -> obj co .run(task)->(status, summary)"""
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
                                                       "log": st.log[-200:], "pending": pend}))
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
            try:
                ag = make_agent(cfg, log, WebApprover(st, approve_timeout), st.stop)
                s, m = ag.run(cfg.get("task", ""))
            except Exception as e:
                s, m = "failed", "%s: %s" % (type(e).__name__, e)
            with st.lock: st.status, st.summary = s, m

    srv = ThreadingHTTPServer((host, port), H)
    srv.token = token
    return srv
