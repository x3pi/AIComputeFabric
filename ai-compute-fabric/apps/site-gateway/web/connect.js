// Standalone one-click activation page (no dependency on common.js / serverless.js).
const $ = (id) => document.getElementById(id);
let token = "", os = "windows", polling = null;

(function detectOS() {
  const ua = (navigator.userAgent || "") + " " + (navigator.platform || "");
  os = /Win/i.test(ua) ? "windows" : /Mac/i.test(ua) && !/iPhone|iPad/i.test(ua) ? "macos" : "linux";
})();

function setMsg(el, text, cls) { el.textContent = text; el.className = "msg " + (cls || ""); }

function selectTab(container, attr, val, cb) {
  container.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset[attr] === val));
  if (cb) cb(val);
}
$("auth-tabs").onclick = (e) => {
  const t = e.target.dataset.t; if (!t) return;
  selectTab($("auth-tabs"), "t", t);
  ["login", "signup", "key"].forEach((n) => ($("f-" + n).hidden = n !== t));
  setMsg($("auth-msg"), "");
};

const HOW = {
  windows: `<ol class="how"><li>Mở thư mục <b>Downloads</b>, bấm đúp <b>aicf-ollama-bridge.bat</b>.</li>
    <li>Nếu Windows hiện "Windows protected your PC": bấm <b>More info</b> → <b>Run anyway</b>.</li>
    <li>Chờ dòng <b>[XONG]</b> rồi đóng cửa sổ. Cầu nối chạy nền và tự bật mỗi lần bạn đăng nhập Windows.</li></ol>
    <small>Gỡ: chạy <code>aicf-ollama-bridge.bat uninstall</code> trong terminal.</small>`,
  macos: `<ol class="how"><li>Mở <b>Terminal</b> và dán lệnh:</li></ol><pre>bash ~/Downloads/aicf-ollama-bridge.command</pre>
    <small>Cầu nối chạy nền, tự bật khi đăng nhập. Gỡ: thêm <code>uninstall</code> vào cuối lệnh. Cần python3 (macOS sẽ gợi ý cài nếu thiếu).</small>`,
  linux: `<ol class="how"><li>Mở Terminal và dán lệnh:</li></ol><pre>bash ~/Downloads/aicf-ollama-bridge.sh</pre>
    <small>Cầu nối chạy nền (systemd --user), tự bật khi đăng nhập. Gỡ: thêm <code>uninstall</code> vào cuối lệnh. Nếu báo cổng 11434 đang bị Ollama chiếm: <code>sudo systemctl stop ollama</code> rồi chạy lại.</small>`,
};

function setOS(v) {
  os = v;
  selectTab($("os-tabs"), "os", v);
  $("how").innerHTML = "";
}
$("os-tabs").onclick = (e) => { if (e.target.dataset.os) setOS(e.target.dataset.os); };
setOS(os);

const KEY_STORE = "aicf_connect_key";
function loggedIn(tok, label) {
  token = tok;
  try { localStorage.setItem(KEY_STORE, tok); } catch {}
  $("s1").classList.add("done");
  $("auth-forms").hidden = true;
  setMsg($("auth-msg"), "");
  $("logged").hidden = false;
  $("logged-as").textContent = "✓ Đã đăng nhập: " + label;
  $("s2").classList.remove("off");
  $("s3").classList.remove("off");
  startPolling();
}

async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const txt = await r.text();
  let d = {}; try { d = JSON.parse(txt); } catch {}
  return { ok: r.ok, status: r.status, d, txt };
}

$("b-login").onclick = async () => {
  const identifier = $("l-id").value.trim(), password = $("l-pw").value;
  if (!identifier || !password) return setMsg($("auth-msg"), "Nhập tài khoản và mật khẩu.", "warn");
  setMsg($("auth-msg"), "Đang đăng nhập…");
  const r = await postJSON("/api/serverless/v1/auth/login", { identifier, password });
  if (!r.ok) return setMsg($("auth-msg"), `Đăng nhập thất bại (${r.status}). Kiểm tra lại tài khoản/mật khẩu.`, "bad");
  loggedIn(r.d.api_key || r.d.session_token, r.d.tenant?.email || r.d.tenant?.id || identifier);
};
$("b-signup").onclick = async () => {
  const name = $("s-name").value.trim().replace(/[^A-Za-z0-9_.-]/g, "-"), email = $("s-email").value.trim(), password = $("s-pw").value;
  if (!name || !email || password.length < 12) return setMsg($("auth-msg"), "Nhập tên workspace, email và mật khẩu tối thiểu 12 ký tự.", "warn");
  setMsg($("auth-msg"), "Đang tạo tài khoản…");
  const r = await postJSON("/api/serverless/v1/auth/register", { name, email, password });
  if (!r.ok) return setMsg($("auth-msg"), `Tạo tài khoản thất bại (${r.status}): ${r.txt.slice(0, 160)}`, "bad");
  loggedIn(r.d.api_key || r.d.session_token, r.d.tenant?.email || name);
};
$("b-key").onclick = async () => {
  const k = $("k-key").value.trim();
  if (!k) return setMsg($("auth-msg"), "Nhập khóa API (aicf_...).", "warn");
  const r = await postJSON("/api/serverless/v1/auth/login", { api_key: k });
  if (!r.ok) return setMsg($("auth-msg"), `Khóa không hợp lệ (${r.status}).`, "bad");
  loggedIn(k, r.d.tenant?.id || "khóa API");
};

$("b-activate").onclick = async () => {
  const r = await fetch("/api/ollama-emulator/bridge-script?os=" + os, { headers: { Authorization: "Bearer " + token } });
  if (!r.ok) return ($("how").textContent = "Không tải được file kích hoạt (" + r.status + ").");
  const blob = await r.blob();
  const name = os === "windows" ? "aicf-ollama-bridge.bat" : os === "macos" ? "aicf-ollama-bridge.command" : "aicf-ollama-bridge.sh";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove();
  $("how").innerHTML = HOW[os];
};

// Is the bridge on this computer answering on 127.0.0.1:11434 ? (the bridge sends CORS + Private-Network headers)
async function probe() {
  try {
    const r = await fetch("http://127.0.0.1:11434/aicf/status", { signal: AbortSignal.timeout(2500) });
    const d = r.ok ? await r.json() : { emulator: false };
    const emu = d.emulator === true;
    $("status").classList.add("up");
    $("status-txt").textContent = emu ? "✓ Đã kích hoạt: máy bạn đã nối với cụm GPU" : "Cổng 11434 đang chạy Ollama khác (chưa phải cầu nối AICF)";
    $("models").textContent = emu ? `Đã đi qua cầu nối: ${d.requests} request (từ ${String(d.since).replace("T", " ")})` : "";
    $("next").hidden = !emu;
    if (emu) $("s3").classList.add("done");
  } catch {
    $("status").classList.remove("up");
    $("status-txt").textContent = "Đang chờ bạn chạy file ở bước 2…";
    $("next").hidden = true;
  }
}
function startPolling() { if (!polling) { probe(); polling = setInterval(probe, 3000); } }

$("b-switch").onclick = (e) => {
  e.preventDefault();
  token = ""; try { localStorage.removeItem(KEY_STORE); } catch {}
  if (polling) { clearInterval(polling); polling = null; }
  ["l-pw", "s-pw", "k-key"].forEach((id) => ($(id).value = ""));
  $("s1").classList.remove("done"); $("logged").hidden = true; $("auth-forms").hidden = false;
  $("s2").classList.add("off"); $("s3").classList.add("off");
};

// Already signed in (this tab on serverless.html, or a previous visit here)? Then skip step 1.
(async function resume() {
  let cands = [];
  try { cands.push(sessionStorage.getItem("aicf_sl_api_key")); } catch {}
  try { cands.push(localStorage.getItem(KEY_STORE)); } catch {}
  for (const k of cands.filter(Boolean)) {
    try {
      const r = await fetch("/api/whoami", { headers: { Authorization: "Bearer " + k } });
      if (!r.ok) continue;
      const w = await r.json();
      if (w.open || w.role !== "tenant") continue;
      return loggedIn(k, w.email || w.name || w.tenant_id);
    } catch {}
  }
})();
