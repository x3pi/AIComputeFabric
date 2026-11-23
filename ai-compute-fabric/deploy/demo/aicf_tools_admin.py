#!/usr/bin/env python3
"""Admin settings page for the AICF tools router: switch the tools (incl. the Langflow news lookup) on/off.

Listens on 127.0.0.1 ONLY (reach it from this machine, or through an SSH tunnel). Login = the gateway's own admin login (/api/auth/login); every
settings call re-checks the Bearer token against the gateway (/api/whoami must say role=admin). It is deliberately NOT exposed on the LAN/ngrok
port: the chat proxy refuses admin logins there. Settings are stored by aicf_tools_config.py and apply to the next chat request.
Python standard library only."""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aicf_tools_config as C

GATEWAY = ("127.0.0.1", 18080)
LANGFLOW = "http://127.0.0.1:7860"
ALLOWED_HOSTS = set()
_FAILS: dict[str, list[float]] = {}
_ADMIN_OK: dict[str, tuple[float, str]] = {}

PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Công cụ chat: cài đặt</title><style>
:root{--bg:#f6f7f9;--fg:#1c2230;--mut:#667085;--card:#fff;--line:#e3e6ec;--ok:#16a34a;--bad:#dc2626;--acc:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#14171f;--fg:#e8eaf0;--mut:#9aa3b5;--card:#1c212c;--line:#2b3242;--acc:#6ea0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 60px}h1{font-size:22px;margin:0 0 4px}.mut{color:var(--mut);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}
.row{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;padding:12px 0;border-top:1px solid var(--line)}.row:first-child{border-top:0}
.row b{display:block}label.sw{position:relative;flex:none;width:46px;height:26px}label.sw input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#98a2b3;border-radius:26px;transition:.15s;cursor:pointer}.sl:before{content:"";position:absolute;width:20px;height:20px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
input:checked+.sl{background:var(--ok)}input:checked+.sl:before{transform:translateX(20px)}input:focus-visible+.sl{outline:2px solid var(--acc);outline-offset:2px}
input[type=text],input[type=password]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit;margin:4px 0 10px}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 18px;font:inherit;font-weight:600;cursor:pointer}button:disabled{opacity:.5}
.pill{display:inline-block;font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);margin-left:6px;vertical-align:middle}
.ok{color:var(--ok)}.bad{color:var(--bad)}#msg{min-height:22px;margin-top:10px}[hidden]{display:none!important}
</style></head><body><main>
<h1>Công cụ chat (AICF /v1)</h1><div class="mut">Bật/tắt các công cụ mà cổng chat tự gọi. Thay đổi có hiệu lực ở câu hỏi kế tiếp, không cần khởi động lại.</div>
<section class="card" id="login"><b>Đăng nhập quản trị</b><div class="mut">Dùng tài khoản admin của gateway.</div>
<label for="u" class="mut">Tài khoản</label><input type="text" id="u" autocomplete="username"><label for="p" class="mut">Mật khẩu</label><input type="password" id="p" autocomplete="current-password">
<button id="go">Đăng nhập</button></section>
<section id="panel" hidden>
<div class="card"><div class="row"><div><b>Bật công cụ cho chat</b><span class="mut">Công tắc tổng. Tắt thì chat chạy như bình thường, không công cụ nào được gọi.</span></div>
<label class="sw"><input type="checkbox" id="enabled" aria-label="Bật công cụ"><span class="sl"></span></label></div>
<div class="row"><div><b>Ghi nguồn cuối câu trả lời</b><span class="mut">Thêm dòng "Nguồn dữ liệu: ... (lấy lúc ...)" khi có công cụ chạy.</span></div>
<label class="sw"><input type="checkbox" id="footer" aria-label="Ghi nguồn"><span class="sl"></span></label></div>
<div class="row"><div><b>Áp dụng cho yêu cầu của Agent (có danh sách công cụ)</b><span class="mut">Langflow Agent và các client gọi hàm gửi kèm danh sách công cụ nên mặc định bị bỏ qua. Bật thì thời tiết, tin tức... vẫn được chèn khi tin nhắn cuối của người dùng khớp; không bao giờ chèn trong vòng gọi công cụ của Agent.</span></div>
<label class="sw"><input type="checkbox" id="agent" aria-label="Áp dụng cho Agent"><span class="sl"></span></label></div></div>
<div class="card" id="tools"></div>
<button id="save">Lưu cài đặt</button> <button id="out" style="background:transparent;color:var(--mut);border:1px solid var(--line)">Đăng xuất</button>
<div id="msg" role="status"></div><div class="mut" id="meta"></div></section></main><script src="/app.js"></script></body></html>"""

JS = r"""
const $=id=>document.getElementById(id);let tok=null,info={};
try{tok=sessionStorage.getItem("tok")}catch(e){}
const msg=(t,c)=>{$("msg").textContent=t;$("msg").className=c||""};
async function api(path,opt={}){const r=await fetch(path,{...opt,headers:{"Content-Type":"application/json",...(tok?{Authorization:"Bearer "+tok}:{})}});let d={};try{d=await r.json()}catch(e){}return{ok:r.ok,status:r.status,d}}
function draw(c){$("enabled").checked=c.enabled;$("footer").checked=c.footer;$("agent").checked=!!c.agent_requests;const box=$("tools");box.textContent="";
 for(const [n,[label,desc,lf]] of Object.entries(info)){const row=document.createElement("div");row.className="row";const l=document.createElement("div");
  const b=document.createElement("b");b.textContent=label;if(lf){const p=document.createElement("span");p.className="pill";p.id="lfpill";p.textContent="Langflow: đang kiểm tra";b.appendChild(p)}
  const s=document.createElement("span");s.className="mut";s.textContent=desc;l.append(b,s);const w=document.createElement("label");w.className="sw";
  const i=document.createElement("input");i.type="checkbox";i.dataset.tool=n;i.checked=c.tools[n]!==false;i.setAttribute("aria-label",label);const sl=document.createElement("span");sl.className="sl";w.append(i,sl);row.append(l,w);box.appendChild(row)}
 $("meta").textContent=c.updated_at?("Lưu lần cuối: "+c.updated_at+" bởi "+c.updated_by):"Chưa từng lưu: đang dùng mặc định (bật hết).";}
async function status(){const r=await api("/api/status");const p=$("lfpill");if(!p||!r.ok)return;p.textContent=r.d.langflow?"Langflow: đang chạy":"Langflow: không phản hồi";p.className="pill "+(r.d.langflow?"ok":"bad")}
async function load(){const r=await api("/api/config");if(!r.ok){tok=null;try{sessionStorage.removeItem("tok")}catch(e){}$("login").hidden=false;$("panel").hidden=true;if(r.status!==401)msg("Lỗi "+r.status,"bad");return}
 info=r.d.info;$("login").hidden=true;$("panel").hidden=false;draw(r.d.config);status()}
$("go").onclick=async()=>{msg("Đang đăng nhập…");const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:$("u").value.trim(),password:$("p").value})});
 const d=await r.json().catch(()=>({}));$("p").value="";if(!r.ok||!d.token){msg((d.error&&d.error.message)||d.error||("Đăng nhập thất bại ("+r.status+")"),"bad");return}tok=d.token;try{sessionStorage.setItem("tok",tok)}catch(e){}msg("");load()};
$("save").onclick=async()=>{const tools={};document.querySelectorAll("input[data-tool]").forEach(i=>tools[i.dataset.tool]=i.checked);
 $("save").disabled=true;const r=await api("/api/config",{method:"PUT",body:JSON.stringify({enabled:$("enabled").checked,footer:$("footer").checked,agent_requests:$("agent").checked,tools})});$("save").disabled=false;
 if(r.ok){draw(r.d.config);status();msg("Đã lưu. Có hiệu lực từ câu hỏi kế tiếp.","ok")}else msg(r.status===401?"Phiên hết hạn, hãy đăng nhập lại.":("Lưu thất bại ("+r.status+")"),"bad");if(r.status===401)load()};
$("out").onclick=()=>{tok=null;try{sessionStorage.removeItem("tok")}catch(e){}$("panel").hidden=true;$("login").hidden=false};
$("p").addEventListener("keydown",e=>{if(e.key==="Enter")$("go").click()});
if(tok)load();
"""


def gateway(method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    c = http.client.HTTPConnection(*GATEWAY, timeout=8)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(200_000)
    finally:
        c.close()


def admin_name(auth: str) -> str | None:
    """Name of the admin behind this Bearer token (checked against the gateway, cached 20 s), or None."""
    if not auth.startswith("Bearer ") or len(auth) < 20:
        return None
    hit = _ADMIN_OK.get(auth)
    if hit and time.monotonic() - hit[0] < 20:
        return hit[1]
    try:
        st, raw = gateway("GET", "/api/whoami", headers={"Authorization": auth})
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if st != 200 or d.get("role") != "admin":
        return None
    who = str(d.get("name") or d.get("email") or "admin")
    if len(_ADMIN_OK) > 50:
        _ADMIN_OK.clear()
    _ADMIN_OK[auth] = (time.monotonic(), who)
    return who


def langflow_up() -> bool:
    try:
        with urllib.request.urlopen(LANGFLOW + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "aicf-tools-admin"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20_000:
            raise ValueError("too big")
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError("object expected")
        return d

    def _guard(self) -> bool:
        if (self.headers.get("Host") or "") not in ALLOWED_HOSTS:  # DNS-rebinding protection
            self._json(403, {"error": "bad host"})
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html")
        if p == "/app.js":
            return self._send(200, JS.encode(), "text/javascript")
        who = admin_name(self.headers.get("Authorization") or "")
        if not who:
            return self._json(401, {"error": "admin token required"})
        if p == "/api/config":
            return self._json(200, {"config": C.load(), "info": {k: list(v) for k, v in C.TOOL_INFO.items()}})
        if p == "/api/status":
            return self._json(200, {"langflow": langflow_up()})
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        if self.path.split("?")[0] != "/api/login":
            return self._json(404, {"error": "not found"})
        ip = self.client_address[0]
        recent = [t for t in _FAILS.get(ip, []) if time.monotonic() - t < 60]
        if len(recent) >= 5:
            return self._json(429, {"error": {"message": "Quá nhiều lần thử, đợi 1 phút."}})
        try:
            d = self._body()
            st, raw = gateway("POST", "/api/auth/login", json.dumps({"username": str(d.get("username", ""))[:100], "password": str(d.get("password", ""))[:200]}).encode(), {"Content-Type": "application/json"})
            r = json.loads(raw)
        except Exception:  # noqa: BLE001
            return self._json(502, {"error": {"message": "Không nói chuyện được với gateway."}})
        tok = r.get("session_token") or r.get("token") or ""
        if st != 200 or r.get("role") != "admin" or not tok:
            _FAILS[ip] = recent + [time.monotonic()]
            return self._json(401, {"error": {"message": "Sai tài khoản/mật khẩu, hoặc không phải admin."}})
        _FAILS.pop(ip, None)
        self._json(200, {"token": tok})

    def do_PUT(self):  # noqa: N802
        if not self._guard():
            return
        who = admin_name(self.headers.get("Authorization") or "")
        if not who:
            return self._json(401, {"error": "admin token required"})
        if self.path.split("?")[0] != "/api/config":
            return self._json(404, {"error": "not found"})
        try:
            new = self._body()
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        cfg = C.save(new, who)
        print(f"settings saved by {who}: enabled={cfg['enabled']} footer={cfg['footer']} agent_requests={cfg['agent_requests']} tools={cfg['tools']}", file=sys.stderr, flush=True)
        self._json(200, {"config": cfg})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18120)
    ap.add_argument("--gateway", default="127.0.0.1:18080")
    ap.add_argument("--langflow", default="http://127.0.0.1:7860")
    a = ap.parse_args()
    global GATEWAY, LANGFLOW
    h, p = a.gateway.rsplit(":", 1)
    GATEWAY, LANGFLOW = (h, int(p)), a.langflow.rstrip("/")
    ALLOWED_HOSTS.update({f"127.0.0.1:{a.port}", f"localhost:{a.port}"})
    print(f"tools admin on http://127.0.0.1:{a.port}/ (gateway {a.gateway}), settings file {C.PATH}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
