// Shared by every page: admin token (kept in sessionStorage only), authenticated fetch,
// SSE over fetch (EventSource cannot send an Authorization header), theme toggle, and 6-bucket VRAM renderer.
const NAV = [
  ["connect.html", "Kích hoạt máy bạn", "🔌", "user"],
  ["serverless.html", "Serverless GPU", "⚡", "user"],
  ["chat.html", "Playground & Tải", "💬", "user"],
  ["studio.html", "AI Studio", "🧪", "user"],
  ["audio.html", "Audio AI", "🎙️", "user"],
  ["index.html", "Cụm GPU & VRAM", "🖥️", "admin"],
  ["tenants.html", "Tenants & Khóa API", "🔑", "admin"],
  ["agents.html", "AI Agent (DAG)", "🕸️", "admin"],
];
const ADMIN_PAGES = ["index.html", "tenants.html", "agents.html"];

const BUCKET_GROUPS = [
  ["OSUser", "OS & người dùng", "--b-os"],
  ["Weights", "Trọng số Model", "--b-weights"],
  ["KVState", "KV / State Cache", "--b-kv"],
  ["Compute", "Compute Buffer", "--b-compute"],
  ["Headroom", "Headroom bảo vệ", "--b-headroom"],
  ["Allocatable", "Còn cấp phát được", "--b-free"],
];

const store = {
  get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
  del(k) { try { sessionStorage.removeItem(k); } catch {} },
};

function applyTheme(t) {
  if (t === "light" || t === "dark") {
    document.documentElement.setAttribute("data-theme", t);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

try {
  const savedTheme = localStorage.getItem("aicf_theme");
  if (savedTheme) applyTheme(savedTheme);
} catch {}

function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("aicf_theme", next); } catch {}
}

// Many requests fire at page load and all get 401 together. `usedTok` is the token the failed request sent: if another
// request has stored a different token meanwhile, just retry with it instead of asking again; if the user cancelled
// a moment ago, do not re-prompt for every other pending request.
let lastTokenCancel = 0;
function askAdminToken(msg, usedTok) {
  if (usedTok !== undefined) {
    const cur = store.get("aicf_admin");
    if (cur && cur !== usedTok) return true;
    if (Date.now() - lastTokenCancel < 5000) return false;
  }
  // Preserve legacy Playwright dialog listener compatibility unless __AICF_FORCE_LOGIN_MODAL is enabled.
  if (typeof navigator !== "undefined" && navigator.webdriver && !window.__AICF_FORCE_LOGIN_MODAL) {
    const t = prompt(msg || "Nhập admin token của gateway (--admin-token):", "");
    if (t === null || !t.trim()) { lastTokenCancel = Date.now(); return false; }
    store.set("aicf_admin", t.trim());
    return true;
  }
  lastTokenCancel = Date.now();
  if (currentPage() !== "serverless.html") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => openAuthModal({ message: msg }), { once: true });
    } else {
      openAuthModal({ message: msg });
    }
  }
  return false;
}

function openAuthModal(opts = {}) {
  let bd = document.getElementById("aicf-auth-modal");
  if (!bd) {
    bd = buildAuthModal();
    document.body.append(bd);
  }
  const msgEl = document.getElementById("aicf-auth-msg");
  if (msgEl) {
    msgEl.textContent = opts.message || "";
    msgEl.style.color = opts.message ? "#dc2626" : "var(--fg-secondary)";
  }
  bd.hidden = false;
  setTimeout(() => {
    const u = document.getElementById("aicf-auth-user");
    if (u) u.focus();
  }, 40);
}

function buildAuthModal() {
  const close = () => { bd.hidden = true; };
  const bd = el("div", {
    class: "drawer-backdrop",
    id: "aicf-auth-modal",
    hidden: true,
    style: "display:flex;align-items:center;justify-content:center;z-index:9999",
    onclick: e => { if (e.target.id === "aicf-auth-modal") close(); }
  });

  const userInp = el("input", {
    id: "aicf-auth-user",
    class: "mono",
    placeholder: "admin hoặc email@congty.vn",
    autocomplete: "username",
    required: true,
    style: "width:100%;padding:9px 12px;font-size:13.5px"
  });

  const passInp = el("input", {
    id: "aicf-auth-pass",
    type: "password",
    placeholder: "Nhập mật khẩu tài khoản…",
    autocomplete: "current-password",
    required: true,
    style: "flex:1;padding:9px 12px;font-size:13.5px"
  });

  const togglePwBtn = el("button", {
    type: "button",
    id: "aicf-auth-pass-toggle",
    class: "sm ghost",
    title: "Hiện / Ẩn mật khẩu",
    onclick: () => {
      const isPw = passInp.type === "password";
      passInp.type = isPw ? "text" : "password";
      togglePwBtn.textContent = isPw ? "🙈 Ẩn" : "👁️ Hiện";
    }
  }, "👁️ Hiện");

  const msgEl = el("div", {
    id: "aicf-auth-msg",
    role: "status",
    style: "font-size:12.5px;min-height:18px;font-weight:500"
  });

  const submitBtn = el("button", {
    type: "submit",
    id: "aicf-auth-submit",
    class: "primary",
    style: "width:100%;padding:10px 14px;font-weight:700;font-size:13.5px"
  }, "🔐 Đăng nhập bằng Tài khoản & Mật khẩu");

  const doLogin = async (payload) => {
    msgEl.style.color = "var(--fg-secondary)";
    msgEl.textContent = "Đang xác thực thông tin đăng nhập (Argon2id)…";
    submitBtn.disabled = true;
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        msgEl.style.color = "#dc2626";
        msgEl.textContent = "❌ " + (d?.error?.message || `Đăng nhập thất bại (HTTP ${r.status})`);
        submitBtn.disabled = false;
        return;
      }
      const tok = d.session_token || d.token || d.api_key || "";
      const isStaffLogin = d.role === "admin" || d.role === "operator" || d.role === "viewer";
      if (isStaffLogin) {
        store.set("aicf_admin", tok);
      } else {
        store.del("aicf_admin");
        store.set("aicf_sl_api_key", tok);
        store.set("aicf_sl_ui_mode", "user");
        if (d.tenant?.id) store.set("aicf_sl_tenant_id", d.tenant.id);
        store.set("aicf_sl_account_email", `👤 ${d.email || d.name || d.tenant?.id}`);
      }
      if (d.name || d.email) {
        store.set("aicf_user_label", d.email ? `${d.name} (${d.email})` : d.name);
      }
      msgEl.style.color = "#16a34a";
      msgEl.textContent = `✓ Đăng nhập thành công: ${d.name || d.email} (${d.role})!`;
      passInp.value = "";
      close();
      if (!isStaffLogin && ADMIN_PAGES.includes(currentPage())) {
        location.href = "serverless.html";
      } else {
        location.reload();
      }
    } catch (err) {
      msgEl.style.color = "#dc2626";
      msgEl.textContent = "❌ Lỗi kết nối máy chủ: " + err;
      submitBtn.disabled = false;
    }
  };

  const form = el("form", {
    id: "aicf-auth-form",
    style: "display:flex;flex-direction:column;gap:12px",
    onsubmit: e => {
      e.preventDefault();
      const identifier = userInp.value.trim();
      const password = passInp.value;
      if (!identifier || !password) {
        msgEl.style.color = "#dc2626";
        msgEl.textContent = "⚠️ Vui lòng nhập đầy đủ Tên đăng nhập/Email và Mật khẩu.";
        return;
      }
      doLogin({ identifier, password });
    }
  },
    el("div", {},
      el("label", { for: "aicf-auth-user", style: "display:block;font-size:12px;font-weight:600;margin-bottom:4px" }, "Tên đăng nhập (Username) hoặc Email"),
      userInp
    ),
    el("div", {},
      el("label", { for: "aicf-auth-pass", style: "display:block;font-size:12px;font-weight:600;margin-bottom:4px" }, "Mật khẩu (Password)"),
      el("div", { class: "row", style: "gap:6px" }, passInp, togglePwBtn)
    ),
    submitBtn
  );

  const tokInp = el("input", {
    id: "aicf-auth-token-input",
    type: "password",
    class: "mono",
    placeholder: "Dán khóa aicf_... hoặc Admin Bearer Token",
    autocomplete: "off",
    style: "flex:1;padding:7px 10px;font-size:12.5px"
  });

  const tokBtn = el("button", {
    type: "button",
    id: "aicf-auth-token-submit",
    class: "sm",
    onclick: () => {
      const k = tokInp.value.trim();
      if (!k) {
        msgEl.style.color = "#dc2626";
        msgEl.textContent = "⚠️ Vui lòng nhập Khóa API hoặc Bearer Token.";
        return;
      }
      doLogin({ token: k });
    }
  }, "Xác thực Khóa");

  const tokenAccordion = el("details", { style: "margin-top:8px;border-top:1px solid var(--bd);padding-top:10px" },
    el("summary", { style: "cursor:pointer;font-size:12px;color:var(--fg-secondary);font-weight:600" }, "🔑 Tùy chọn nâng cao: Đăng nhập bằng Khóa API / CLI Token"),
    el("div", { class: "row", style: "gap:6px;margin-top:8px" }, tokInp, tokBtn)
  );

  const card = el("div", {
    class: "card",
    role: "dialog",
    "aria-label": "Đăng nhập Hệ thống AI Compute Fabric",
    style: "width:min(440px, 94vw);padding:22px;display:flex;flex-direction:column;gap:12px;box-shadow:0 20px 50px rgba(0,0,0,0.45)"
  },
    el("div", { class: "row", style: "justify-content:space-between;align-items:flex-start" },
      el("div", {},
        el("h2", { style: "margin:0;font-size:17px" }, "🔐 Đăng nhập AI Compute Fabric"),
        el("div", { class: "mut", style: "font-size:12px;margin-top:3px" }, "Xác thực tập trung Cloud IAM · Argon2id & Phiên Revocable Session")
      ),
      el("button", { type: "button", class: "sm ghost", onclick: close }, "✕")
    ),
    form,
    msgEl,
    tokenAccordion
  );

  bd.append(card);
  return bd;
}

function openAccountSecurityModal() {
  let bd = document.getElementById("aicf-security-modal");
  if (!bd) {
    bd = buildAccountSecurityModal();
    document.body.append(bd);
  }
  bd.hidden = false;
  if (typeof bd.loadSessions === "function") bd.loadSessions();
}

function buildAccountSecurityModal() {
  const close = () => { bd.hidden = true; };
  const bd = el("div", {
    class: "drawer-backdrop",
    id: "aicf-security-modal",
    hidden: true,
    style: "display:flex;align-items:center;justify-content:center;z-index:9999",
    onclick: e => { if (e.target.id === "aicf-security-modal") close(); }
  });

  const curPwInp = el("input", {
    id: "aicf-sec-cur-pw",
    type: "password",
    placeholder: "Nhập mật khẩu hiện tại…",
    autocomplete: "current-password",
    style: "width:100%;padding:8px 10px"
  });
  const newPwInp = el("input", {
    id: "aicf-sec-new-pw",
    type: "password",
    placeholder: "Mật khẩu mới (tối thiểu 12 ký tự, chuẩn Argon2id)",
    autocomplete: "new-password",
    style: "width:100%;padding:8px 10px"
  });
  const cfmPwInp = el("input", {
    id: "aicf-sec-cfm-pw",
    type: "password",
    placeholder: "Nhập lại mật khẩu mới",
    autocomplete: "new-password",
    style: "width:100%;padding:8px 10px"
  });
  const pwMsg = el("div", { id: "aicf-sec-pw-msg", style: "font-size:12.5px;min-height:18px;font-weight:500" });

  const getActiveBearer = () => store.get("aicf_admin") || store.get("aicf_sl_api_key");

  const pwForm = el("form", {
    id: "aicf-sec-pw-form",
    style: "display:flex;flex-direction:column;gap:10px",
    onsubmit: async e => {
      e.preventDefault();
      const curPw = curPwInp.value;
      const newPw = newPwInp.value;
      const cfmPw = cfmPwInp.value;
      if (newPw.length < 12) {
        pwMsg.style.color = "#dc2626";
        pwMsg.textContent = "⚠️ Mật khẩu mới phải có ít nhất 12 ký tự.";
        return;
      }
      if (newPw !== cfmPw) {
        pwMsg.style.color = "#dc2626";
        pwMsg.textContent = "⚠️ Mật khẩu xác nhận không khớp.";
        return;
      }
      pwMsg.style.color = "var(--fg-secondary)";
      pwMsg.textContent = "Đang băm Argon2id & thu hồi các phiên cũ…";
      const r = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + getActiveBearer() },
        body: JSON.stringify({ current_password: curPw, new_password: newPw }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        pwMsg.style.color = "#dc2626";
        pwMsg.textContent = "❌ " + (d?.error?.message || `Đổi mật khẩu thất bại (${r.status})`);
        return;
      }
      if (d.session_token) {
        if (store.get("aicf_admin")) store.set("aicf_admin", d.session_token);
        if (store.get("aicf_sl_api_key")) store.set("aicf_sl_api_key", d.session_token);
      }
      curPwInp.value = "";
      newPwInp.value = "";
      cfmPwInp.value = "";
      pwMsg.style.color = "#16a34a";
      pwMsg.textContent = "✓ Đã đổi mật khẩu thành công (Argon2id) và thu hồi toàn bộ phiên trên thiết bị khác!";
      loadSessions();
    }
  },
    el("div", {},
      el("label", { for: "aicf-sec-cur-pw", style: "font-size:12px;display:block;margin-bottom:3px" }, "Mật khẩu hiện tại"),
      curPwInp
    ),
    el("div", {},
      el("label", { for: "aicf-sec-new-pw", style: "font-size:12px;display:block;margin-bottom:3px" }, "Mật khẩu mới (≥ 12 ký tự)"),
      newPwInp
    ),
    el("div", {},
      el("label", { for: "aicf-sec-cfm-pw", style: "font-size:12px;display:block;margin-bottom:3px" }, "Xác nhận mật khẩu mới"),
      cfmPwInp
    ),
    el("button", { type: "submit", id: "aicf-sec-pw-submit", class: "primary sm" }, "🔒 Cập nhật Mật khẩu & Thu hồi Phiên khác"),
    pwMsg
  );

  const sessListEl = el("div", { id: "aicf-sec-sessions-list", style: "display:flex;flex-direction:column;gap:6px;max-height:180px;overflow-y:auto;font-size:12px" },
    el("div", { class: "mut" }, "Đang tải danh sách phiên hoạt động…")
  );

  async function loadSessions() {
    sessListEl.replaceChildren(el("div", { class: "mut" }, "Đang tải danh sách phiên…"));
    try {
      const r = await fetch("/api/auth/sessions", {
        headers: { Authorization: "Bearer " + getActiveBearer() }
      });
      if (!r.ok) {
        sessListEl.replaceChildren(el("div", { class: "mut" }, "Không có phiên động hoặc đang dùng khóa tĩnh."));
        return;
      }
      const d = await r.json();
      const list = d.sessions || [];
      sessListEl.replaceChildren();
      if (!list.length) {
        sessListEl.append(el("div", { class: "mut" }, "Đang xác thực bằng Khóa API tĩnh (không có phiên web tạm thời)."));
        return;
      }
      for (const s of list) {
        const row = el("div", {
          class: "row",
          style: "justify-content:space-between;padding:6px 8px;border:1px solid var(--bd);border-radius:6px"
        },
          el("div", {},
            el("strong", { class: "mono" }, s.id),
            el("span", { class: "mut", style: "margin-left:8px" }, `IP: ${s.remote_ip || "local"} · Hết hạn: ${(s.expires_at || "").slice(11, 19)}`)
          ),
          el("button", {
            type: "button",
            class: "sm ghost err",
            onclick: async () => {
              await fetch(`/api/auth/sessions/${encodeURIComponent(s.id)}`, {
                method: "DELETE",
                headers: { Authorization: "Bearer " + getActiveBearer() }
              });
              loadSessions();
            }
          }, "Thu hồi")
        );
        sessListEl.append(row);
      }
    } catch {
      sessListEl.replaceChildren(el("div", { class: "mut" }, "Không thể tải danh sách phiên."));
    }
  }

  bd.loadSessions = loadSessions;

  const card = el("div", {
    class: "card",
    role: "dialog",
    "aria-label": "Bảo mật Tài khoản & Quản lý Phiên",
    style: "width:min(480px, 95vw);padding:22px;display:flex;flex-direction:column;gap:14px;box-shadow:0 20px 50px rgba(0,0,0,0.45)"
  },
    el("div", { class: "row", style: "justify-content:space-between" },
      el("h2", { style: "margin:0;font-size:16.5px" }, "🔒 Bảo mật Tài khoản & Quản lý Phiên"),
      el("button", { type: "button", class: "sm ghost", onclick: close }, "✕")
    ),
    pwForm,
    el("div", { style: "border-top:1px solid var(--bd);padding-top:10px" },
      el("div", { class: "row", style: "justify-content:space-between;margin-bottom:6px" },
        el("strong", { style: "font-size:13px" }, "📱 Các Phiên Đăng nhập Đang Hoạt động"),
        el("button", { type: "button", class: "sm ghost", onclick: loadSessions }, "↻ Làm mới")
      ),
      sessListEl
    )
  );
  bd.append(card);
  return bd;
}

function detectClientOS() {
  const ua = (navigator.userAgent || "").toLowerCase();
  if (ua.includes("win")) return "windows";
  if (ua.includes("mac")) return "macos";
  return "linux";
}

function openOllamaEmulatorModal() {
  let bd = document.getElementById("aicf-ollama-modal");
  if (!bd) {
    bd = buildOllamaEmulatorModal();
    document.body.append(bd);
  }
  bd.hidden = false;
  if (typeof bd.refreshStatus === "function") bd.refreshStatus();
}

function buildOllamaEmulatorModal() {
  const close = () => { bd.hidden = true; };
  const bd = el("div", {
    class: "drawer-backdrop",
    id: "aicf-ollama-modal",
    hidden: true,
    style: "display:flex;align-items:center;justify-content:center;z-index:9999",
    onclick: e => { if (e.target.id === "aicf-ollama-modal") close(); }
  });

  const getActiveBearer = () => store.get("aicf_sl_api_key") || store.get("aicf_admin");
  let selectedOS = detectClientOS();

  const localProbeBox = el("div", {
    id: "aicf-ollama-local-probe",
    style: "padding:12px 14px;border-radius:8px;border:1px solid var(--bd);background:var(--bg-2, rgba(255,255,255,0.03));font-size:13px"
  }, "⏳ Đang kiểm tra cổng mặc định http://127.0.0.1:11434 trên máy của bạn…");

  const gwStatusBox = el("div", {
    id: "aicf-ollama-gw-status",
    style: "font-size:12.5px;color:var(--fg-secondary)"
  }, "Đang tải trạng thái từ Gateway…");

  const modelsListEl = el("div", {
    id: "aicf-ollama-models-list",
    style: "display:flex;flex-wrap:wrap;gap:6px;margin-top:6px"
  });

  const clientsListEl = el("div", {
    id: "aicf-ollama-clients-list",
    style: "display:flex;flex-direction:column;gap:4px;max-height:110px;overflow-y:auto;font-size:12px;margin-top:6px"
  });

  const oneLinerCode = el("pre", {
    id: "aicf-ollama-oneliner",
    class: "mono code-box",
    style: "margin:6px 0 0;padding:10px 12px;font-size:12px;overflow-x:auto;white-space:pre;border:1px solid var(--bd);border-radius:6px"
  });

  const actionMsg = el("div", {
    id: "aicf-ollama-action-msg",
    style: "font-size:12.5px;font-weight:600;min-height:18px"
  });

  function updateOSButtons() {
    for (const [osId, btn] of Object.entries(osBtns)) {
      if (osId === selectedOS) {
        btn.classList.add("primary");
        btn.classList.remove("ghost");
      } else {
        btn.classList.remove("primary");
        btn.classList.add("ghost");
      }
    }
    const tok = getActiveBearer();
    const isLocalhost = location.hostname === "127.0.0.1" || location.hostname === "localhost";
    const gw = (selectedOS === "windows" && isLocalhost) ? "http://10.42.0.1:18080" : location.origin;
    const scriptUrl = `${gw}/api/ollama-emulator/bridge-script?os=${encodeURIComponent(selectedOS)}&gateway=${encodeURIComponent(gw)}${tok ? `&token=${encodeURIComponent(tok)}` : ""}`;
    if (selectedOS === "windows") {
      oneLinerCode.textContent = `curl.exe -fsSL "${scriptUrl}" -o "%TEMP%\\aicf-ollama-bridge.bat" && "%TEMP%\\aicf-ollama-bridge.bat"`;
    } else {
      oneLinerCode.textContent = `curl -fsSL "${scriptUrl}" | bash`;
    }
    dlBtn.onclick = () => {
      const a = document.createElement("a");
      a.href = scriptUrl;
      a.download = selectedOS === "windows" ? "aicf-ollama-bridge.bat" : (selectedOS === "macos" ? "aicf-ollama-bridge.command" : "aicf-ollama-bridge.sh");
      document.body.appendChild(a);
      a.click();
      a.remove();
      actionMsg.style.color = "#16a34a";
      actionMsg.textContent = "✓ Đã tải tệp kích hoạt 1-Click (đã nhúng sẵn URL & API Key của bạn). Hãy mở tệp vừa tải để đồng bộ mọi ứng dụng!";
    };
  }

  const osBtns = {
    linux: el("button", { type: "button", class: "sm ghost", onclick: () => { selectedOS = "linux"; updateOSButtons(); } }, "🐧 Linux (.sh)"),
    macos: el("button", { type: "button", class: "sm ghost", onclick: () => { selectedOS = "macos"; updateOSButtons(); } }, "🍎 macOS (.command)"),
    windows: el("button", { type: "button", class: "sm ghost", onclick: () => { selectedOS = "windows"; updateOSButtons(); } }, "🪟 Windows (.bat)"),
  };

  const dlBtn = el("button", {
    type: "button",
    id: "aicf-ollama-download-btn",
    class: "primary sm",
    style: "font-weight:700;padding:8px 14px"
  }, "⚡ Tải tệp Kích hoạt 1-Click (Zero-Config)");

  const copyCmdBtn = el("button", {
    type: "button",
    id: "aicf-ollama-copy-cmd-btn",
    class: "sm",
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(oneLinerCode.textContent);
        actionMsg.style.color = "#16a34a";
        actionMsg.textContent = "✓ Đã sao chép lệnh kích hoạt 1 dòng vào bộ nhớ tạm!";
      } catch {
        actionMsg.style.color = "#d97706";
        actionMsg.textContent = "⚠️ Hãy bôi đen và sao chép lệnh bên trên.";
      }
    }
  }, "📋 Sao chép lệnh 1 dòng");

  const startServerPortBtn = el("button", {
    type: "button",
    id: "aicf-ollama-server-start-btn",
    class: "sm ghost",
    onclick: async () => {
      actionMsg.style.color = "var(--fg-secondary)";
      actionMsg.textContent = "Đang kích hoạt cổng 11434 trực tiếp trên máy chủ Gateway…";
      try {
        const r = await fetch("/api/ollama-emulator/config", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + getActiveBearer() },
          body: JSON.stringify({ enabled: true, zero_config_auth: true, listener_action: "start" })
        });
        const d = await r.json().catch(() => ({}));
        if (d.action_error) {
          actionMsg.style.color = "#d97706";
          actionMsg.textContent = `⚠️ Cổng 11434 trên Server đang bận (${d.action_error}). Hãy chạy lệnh 1 dòng bên trên để tự động dừng ollama.service gốc và chiếm cổng 11434!`;
        } else {
          actionMsg.style.color = "#16a34a";
          actionMsg.textContent = "✓ Đã mở cổng 11434 trực tiếp trên Server Gateway!";
        }
        refreshStatus();
      } catch (e) {
        actionMsg.style.color = "#dc2626";
        actionMsg.textContent = "❌ Không thể gọi API: " + e;
      }
    }
  }, "🖥️ Mở cổng :11434 trên Server");

  async function probeBrowserLocal11434() {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 1200);
      const r = await fetch("http://127.0.0.1:11434/api/tags", { signal: ctrl.signal });
      clearTimeout(t);
      const isAICF = r.headers.get("X-AICF-Ollama-Emulator") === "true";
      const data = await r.json().catch(() => ({}));
      const count = (data.models || []).length;
      if (isAICF) {
        localProbeBox.style.borderColor = "#16a34a";
        localProbeBox.innerHTML = `<strong style="color:#16a34a">🟢 ĐÃ KẾT NỐI TỰ ĐỘNG (127.0.0.1:11434)</strong><div style="margin-top:4px;font-size:12px">Máy của bạn đang đồng bộ trực tiếp <b>${count} model</b> từ Cụm GPU AI Compute Fabric. Mở ngay <b>Cursor, Continue, Chatbox, Open WebUI, AnythingLLM</b> là thấy danh sách model mà không cần cấu hình!</div>`;
      } else {
        localProbeBox.style.borderColor = "#d97706";
        localProbeBox.innerHTML = `<strong style="color:#d97706">🟡 CỔNG 11434 ĐANG BỊ DỊCH VỤ OLLAMA GỐC CHIẾM GIỮ</strong><div style="margin-top:4px;font-size:12px">Cổng <code>127.0.0.1:11434</code> trên máy này đang trả về từ Ollama mặc định (${count} model cục bộ). Hãy bấm <b>Tải tệp Kích hoạt 1-Click</b> hoặc chạy lệnh 1 dòng bên dưới để chuyển cổng 11434 sang Cụm GPU 24GB VRAM!</div>`;
      }
    } catch {
      localProbeBox.style.borderColor = "var(--bd)";
      localProbeBox.innerHTML = `<strong style="color:var(--fg-secondary)">⚪ CỔNG 127.0.0.1:11434 CHƯA BẬT TRÊN MÁY NÀY</strong><div style="margin-top:4px;font-size:12px">Hãy bấm <b>⚡ Tải tệp Kích hoạt 1-Click</b> hoặc dán <b>Lệnh 1 dòng</b> bên dưới vào Terminal/PowerShell để các ứng dụng bên thứ 3 tự động nhận diện model.</div>`;
    }
  }

  async function refreshStatus() {
    updateOSButtons();
    probeBrowserLocal11434();
    try {
      const tok = getActiveBearer();
      const headers = tok ? { Authorization: "Bearer " + tok } : {};
      const r = await fetch("/api/ollama-emulator/status", { headers });
      if (!r.ok) {
        gwStatusBox.textContent = "ℹ️ Đăng nhập tài khoản để đính kèm API Key tự động vào cầu nối.";
        return;
      }
      const d = await r.json();
      gwStatusBox.textContent = `Trạng thái Gateway: ${d.enabled ? "🟢 Đang bật bộ dịch giao thức Ollama (/api/tags, /api/chat, /api/show)" : "🔴 Đang tắt"} · ${d.port_11434_detail || ""} · Đã phục vụ: ${d.served_requests || 0} yêu cầu`;

      modelsListEl.replaceChildren();
      const models = d.ollama_tags || [];
      if (!models.length) {
        modelsListEl.append(el("span", { class: "mut", style: "font-size:12px" }, "Chưa có worker model nào đang chạy."));
      } else {
        for (const m of models) {
          modelsListEl.append(el("span", {
            class: "mono",
            style: "padding:4px 8px;border-radius:6px;border:1px solid var(--bd);font-size:11.5px;background:rgba(66,133,244,0.12)"
          }, `📦 ${m.name} (${m.details?.parameter_size || ""} · ${m.details?.quantization_level || ""})`));
        }
      }

      clientsListEl.replaceChildren();
      const clients = d.recent_clients || [];
      if (!clients.length) {
        clientsListEl.append(el("div", { class: "mut" }, "Chưa có ứng dụng bên thứ 3 nào truy cập trong phiên này."));
      } else {
        for (const c of clients) {
          clientsListEl.append(el("div", {
            class: "row",
            style: "justify-content:space-between;padding:4px 8px;border:1px solid var(--bd);border-radius:5px"
          },
            el("span", {}, el("strong", {}, c.client_app), el("span", { class: "mut", style: "margin-left:6px" }, `${c.method} ${c.path}`)),
            el("span", { class: "mono mut" }, (c.time || "").slice(11, 19))
          ));
        }
      }
    } catch {}
  }

  bd.refreshStatus = refreshStatus;

  const card = el("div", {
    class: "card",
    role: "dialog",
    "aria-label": "Giả lập Ollama qua Cổng Mặc định 11434",
    style: "width:min(640px, 96vw);max-height:90vh;overflow-y:auto;padding:22px;display:flex;flex-direction:column;gap:14px;box-shadow:0 20px 50px rgba(0,0,0,0.45)"
  },
    el("div", { class: "row", style: "justify-content:space-between;align-items:flex-start" },
      el("div", {},
        el("h2", { style: "margin:0;font-size:17px" }, "🔌 Giả lập Ollama qua Cổng Mặc định (127.0.0.1:11434)"),
        el("div", { class: "mut", style: "font-size:12px;margin-top:3px" }, "Tự động đồng bộ danh sách Model sang Cursor, Continue, Chatbox, Open WebUI, AnythingLLM mà không cần cấu hình URL")
      ),
      el("button", { type: "button", class: "sm ghost", onclick: close }, "✕")
    ),
    localProbeBox,
    gwStatusBox,
    el("div", { style: "border-top:1px solid var(--bd);padding-top:10px" },
      el("div", { class: "row", style: "justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px" },
        el("strong", { style: "font-size:13px" }, "1️⃣ Chọn Hệ điều hành & Kích hoạt 1-Click:"),
        el("div", { class: "row", style: "gap:6px" }, osBtns.linux, osBtns.macos, osBtns.windows)
      ),
      el("div", { class: "row", style: "gap:8px;margin-top:10px;flex-wrap:wrap" },
        dlBtn,
        copyCmdBtn,
        startServerPortBtn,
        el("button", { type: "button", class: "sm ghost", onclick: refreshStatus }, "↻ Kiểm tra lại cổng 11434")
      ),
      oneLinerCode,
      actionMsg
    ),
    el("div", { style: "border-top:1px solid var(--bd);padding-top:10px" },
      el("strong", { style: "font-size:13px" }, "2️⃣ Danh sách Model đang đồng bộ tự động qua /api/tags:"),
      modelsListEl
    ),
    el("div", { style: "border-top:1px solid var(--bd);padding-top:10px" },
      el("strong", { style: "font-size:13px" }, "3️⃣ Ứng dụng bên thứ 3 vừa tự động nhận diện:"),
      clientsListEl
    )
  );

  bd.append(card);
  return bd;
}

async function logoutGlobalSession() {
  const tok = store.get("aicf_admin") || store.get("aicf_sl_api_key");
  if (tok) {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + tok },
        body: JSON.stringify({ session_token: tok }),
      });
    } catch {}
  }
  store.del("aicf_admin");
  store.del("aicf_user_label");
  store.del("aicf_sl_api_key");
  store.del("aicf_sl_account_email");
  window.AICF = { role: "", site: "", name: "", open: false, can: { view: false, pause: false, tenants: false } };
  applyRole();
  if (currentPage() === "serverless.html") {
    location.reload();
  } else {
    openAuthModal({ message: "Đã đăng xuất và thu hồi phiên làm việc." });
  }
}

function currentPage() {
  const p = location.pathname.split("/").pop() || "index.html";
  return p === "" ? "index.html" : p;
}

function createGeminiSparkleSvg(size = 22) {
  const span = document.createElement("span");
  span.className = "nav-brand-sparkle-wrap";
  span.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" style="display:block;filter:drop-shadow(0 2px 6px rgba(66,133,244,0.35))">
    <defs>
      <linearGradient id="gemini-nav-grad" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#4285f4"/>
        <stop offset="50%" stop-color="#9b72cb"/>
        <stop offset="100%" stop-color="#d96570"/>
      </linearGradient>
    </defs>
    <path d="M12 0C12 6.627 6.627 12 0 12C6.627 12 12 17.373 12 24C12 17.373 17.373 12 24 12C17.373 12 12 6.627 12 0Z" fill="url(#gemini-nav-grad)"/>
  </svg>`;
  return span;
}

function createHamburgerSvg(size = 18) {
  const span = document.createElement("span");
  span.className = "icon-hamburger-svg";
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" style="display:block">
    <line x1="4" y1="6" x2="20" y2="6"></line>
    <line x1="4" y1="12" x2="20" y2="12"></line>
    <line x1="4" y1="18" x2="20" y2="18"></line>
  </svg>`;
  return span;
}

function createMenuOpenSvg(size = 20) {
  const span = document.createElement("span");
  span.className = "icon-menu-open-svg";
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="display:block">
    <line x1="3" y1="6" x2="16" y2="6"></line>
    <line x1="3" y1="12" x2="12" y2="12"></line>
    <line x1="3" y1="18" x2="16" y2="18"></line>
    <polyline points="15 9 12 12 15 15" stroke="var(--acc, currentColor)"></polyline>
  </svg>`;
  return span;
}

function isNavPinned() {
  try {
    const val = localStorage.getItem("aicf_nav_state");
    if (val !== null) return val === "pinned";
  } catch {}
  return window.innerWidth > 1024;
}

function setNavPinned(pinned) {
  if (window.innerWidth <= 768) {
    if (typeof pinned === "boolean") {
      document.body.classList.toggle("mobile-drawer-open", pinned);
    } else {
      document.body.classList.toggle("mobile-drawer-open");
    }
    return;
  }
  const isPinned = typeof pinned === "boolean" ? pinned : document.body.classList.contains("nav-hidden");
  if (isPinned) {
    document.body.classList.remove("nav-hidden");
    try { localStorage.setItem("aicf_nav_state", "pinned"); } catch {}
  } else {
    document.body.classList.add("nav-hidden");
    try { localStorage.setItem("aicf_nav_state", "hidden"); } catch {}
  }
}

function toggleSidebar() {
  setNavPinned();
}

function renderNav() {
  const here = currentPage();
  const pinned = isNavPinned();
  if (!pinned && window.innerWidth > 768) {
    document.body.classList.add("nav-hidden");
  }

  // 1. Mobile Backdrop
  const backdrop = el("div", {
    class: "sidebar-backdrop",
    id: "sidebar-backdrop",
    onclick: () => setNavPinned(false)
  });

  // 2. Sidebar Navigation (Ghim / Pinned Mode)
  const sidebar = document.createElement("nav");
  sidebar.className = "app-sidebar app-nav";
  sidebar.id = "app-sidebar";

  const brand = el("div", { class: "sidebar-brand-group" },
    el("a", { href: "index.html", class: "sidebar-brand", title: "AI Compute Fabric" },
      createGeminiSparkleSvg(22),
      el("span", { class: "sidebar-brand-title" }, "AI Compute Fabric")
    ),
    el("button", {
      type: "button",
      class: "btn-collapse-pinned",
      id: "btn-collapse-pinned",
      title: "Ẩn menu điều hướng (Ctrl+B)",
      onclick: () => setNavPinned(false)
    }, createMenuOpenSvg(19))
  );

  const navList = el("div", { class: "sidebar-nav-list" });
  const userGroupHdr = el("div", {
    id: "nav-group-user",
    class: "mut",
    style: "font-size:10.5px;font-weight:700;letter-spacing:0.06em;padding:8px 12px 4px;text-transform:uppercase"
  }, "👤 Không gian Người dùng");
  navList.append(userGroupHdr);

  for (const [href, label, icon, scope] of NAV.filter(x => x[3] === "user")) {
    const a = el("a", {
      href,
      class: "nav-tab",
      "data-nav-scope": scope,
      title: label,
      onclick: () => {
        if (window.innerWidth <= 768) setNavPinned(false);
      }
    },
      el("span", { class: "nav-tab-icon", "aria-hidden": "true" }, icon),
      el("span", { class: "nav-tab-label" }, label)
    );
    if (href === here) a.setAttribute("aria-current", "page");
    navList.append(a);
  }

  const adminGroupHdr = el("div", {
    id: "nav-group-admin",
    class: "mut",
    style: "font-size:10.5px;font-weight:700;letter-spacing:0.06em;padding:12px 12px 4px;margin-top:4px;border-top:1px solid var(--bd);text-transform:uppercase"
  }, "🛡️ Quản trị Cụm (Admin)");
  navList.append(adminGroupHdr);

  for (const [href, label, icon, scope] of NAV.filter(x => x[3] === "admin")) {
    const a = el("a", {
      href,
      class: "nav-tab",
      "data-nav-scope": scope,
      title: label,
      onclick: () => {
        if (window.innerWidth <= 768) setNavPinned(false);
      }
    },
      el("span", { class: "nav-tab-icon", "aria-hidden": "true" }, icon),
      el("span", { class: "nav-tab-label" }, label)
    );
    if (href === here) a.setAttribute("aria-current", "page");
    navList.append(a);
  }

  sidebar.append(brand, navList);

  // 3. Top Header Bar
  const topbarHamburger = el("button", {
    type: "button",
    class: "btn-topbar-hamburger",
    id: "btn-topbar-hamburger",
    title: "Ghim / Mở menu điều hướng (Ctrl+B)",
    onclick: () => setNavPinned(true)
  }, createHamburgerSvg(18));

  const authBtn = el("button", {
    type: "button",
    class: "sm ghost",
    id: "nav-auth-btn",
    title: "Đăng nhập hoặc chuyển đổi tài khoản (Username / Password)",
    onclick: () => openAuthModal()
  }, "🔐 Đăng nhập");

  const secBtn = el("button", {
    type: "button",
    class: "sm ghost",
    id: "nav-security-btn",
    hidden: true,
    title: "Đổi mật khẩu (Argon2id) & Quản lý Phiên đăng nhập",
    onclick: () => openAccountSecurityModal()
  }, "🔒 Mật khẩu & Phiên");

  const logoutBtn = el("button", {
    type: "button",
    class: "sm ghost",
    id: "nav-logout-btn",
    hidden: true,
    title: "Đăng xuất và thu hồi phiên làm việc",
    onclick: () => logoutGlobalSession()
  }, "🚪 Đăng xuất");

  const curTheme = document.documentElement.getAttribute("data-theme") || "dark";
  const themeBtn = el("button", {
    type: "button",
    class: "sm ghost",
    id: "nav-theme-btn",
    title: "Chuyển đổi giao diện Sáng / Tối",
    onclick: () => {
      toggleTheme();
      const next = document.documentElement.getAttribute("data-theme") || "dark";
      themeBtn.textContent = next === "light" ? "☀️ Sáng" : "🌙 Tối";
    }
  }, curTheme === "light" ? "☀️ Sáng" : "🌙 Tối");

  const livePill = el("span", { class: "live-pill", id: "nav-live" },
    el("span", { class: "live-dot" }),
    el("span", { id: "nav-live-txt" }, "SSE 1 Hz")
  );

  const roleBadge = el("span", { class: "nav-role", id: "nav-role", title: "Vai trò của phiên hiện tại" }, "…");

  const ollamaBtn = el("button", {
    type: "button",
    class: "sm ghost",
    id: "nav-ollama-btn",
    title: "Giả lập Ollama qua Cổng Mặc định (:11434) — Tự động đồng bộ danh sách Model sang ứng dụng bên thứ 3",
    onclick: () => openOllamaEmulatorModal()
  }, "🔌 Ollama :11434");

  const topbar = el("header", { class: "app-topbar" },
    el("div", { class: "topbar-left" }, topbarHamburger),
    el("div", { class: "topbar-actions" }, livePill, ollamaBtn, roleBadge, authBtn, secBtn, logoutBtn, themeBtn)
  );

  // 4. Wrap <main> inside .app-main-wrap
  const main = document.querySelector("main");
  const mainWrap = el("div", { class: "app-main-wrap", id: "app-main-wrap" }, topbar);
  if (main) mainWrap.append(main);

  document.body.prepend(backdrop, sidebar);
  document.body.append(mainWrap);
}

async function api(path, opts = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = Object.assign({}, opts.headers || {});
    const tok = store.get("aicf_admin") || store.get("aicf_sl_api_key");
    if (tok) headers["Authorization"] = "Bearer " + tok;
    const r = await fetch(path, Object.assign({}, opts, { headers }));
    if (r.status === 401 && attempt === 0 && askAdminToken(tok ? "Token sai. Nhập lại admin token:" : null, tok)) continue;
    return r;
  }
}

// Calls onMessage(parsedJSON) for every SSE "data:" line; reconnects after errors.
async function stream(path, onMessage) {
  for (;;) {
    try {
      const headers = {};
      const tok = store.get("aicf_admin");
      if (tok) headers["Authorization"] = "Bearer " + tok;
      const r = await fetch(path, { headers });
      if (r.status === 401) { if (!askAdminToken(null, tok)) return; continue; }
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const ev = buf.slice(0, i); buf = buf.slice(i + 2);
          if (ev.startsWith("data: ")) { try { onMessage(JSON.parse(ev.slice(6))); } catch {} }
        }
      }
    } catch {}
    await new Promise(r => setTimeout(r, 2000));
  }
}

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on") && typeof v === "function") e[k] = v;
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  e.append(...kids.filter(k => k != null));
  return e;
}

// --- Reusable File Upload / Dropzone & Data Classification Components ---

function formatFileSize(bytes) {
  if (bytes == null || isNaN(bytes) || bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let s = Number(bytes);
  while (s >= 1024 && i < units.length - 1) {
    s /= 1024;
    i++;
  }
  return `${s.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function getFileIcon(name) {
  const ext = (name || "").split(".").pop().toLowerCase();
  switch (ext) {
    case "pdf": return "📕";
    case "doc":
    case "docx": return "📘";
    case "txt": return "📄";
    case "md": return "📝";
    case "html":
    case "htm": return "🌐";
    case "json":
    case "jsonl": return "📦";
    case "csv": return "📊";
    default: return "📄";
  }
}

function createFileDropzone(opts = {}) {
  const {
    id = "",
    accept = "",
    label = "",
    hint = "Kéo thả tệp hoặc chọn từ máy tính",
    compact = false,
    onchange = null,
  } = opts;

  const container = el("div", { class: "dropzone-container" + (compact ? " compact-wrap" : "") });
  if (label) {
    container.append(el("label", { class: "dropzone-label" }, label));
  }

  const box = el("div", { class: "dropzone-box" + (compact ? " compact" : "") });

  // Real file input
  const inputAttrs = { type: "file", class: "dropzone-input" };
  if (id) inputAttrs.id = id;
  if (accept) inputAttrs.accept = accept;
  const input = el("input", inputAttrs);

  // Empty state
  const emptyWrap = el("div", { class: "dropzone-empty" },
    el("div", { class: "dropzone-icon-wrap" }, "📁"),
    el("div", { class: "dropzone-text-main" },
      "Kéo thả tệp vào đây, hoặc ",
      el("span", { class: "dropzone-browse" }, "chọn từ máy tính")
    ),
    hint ? el("div", { class: "dropzone-hint" }, hint) : null
  );

  // File preview card
  const badgeEl = el("div", { class: "dropzone-file-badge" }, "📄");
  const nameEl = el("div", { class: "dropzone-filename" }, "");
  const sizeEl = el("div", { class: "dropzone-filesize" }, "");
  const clearBtn = el("button", {
    type: "button",
    class: "dropzone-clear-btn",
    title: "Bỏ chọn tệp này",
    onclick: (e) => {
      e.stopPropagation();
      e.preventDefault();
      clear();
    }
  }, "✕ Bỏ chọn");

  const previewCard = el("div", { class: "dropzone-file-card", style: "display:none" },
    el("div", { class: "dropzone-file-info" },
      badgeEl,
      el("div", { class: "dropzone-file-meta" }, nameEl, sizeEl)
    ),
    el("div", { class: "dropzone-file-actions" },
      el("span", { class: "pill ok", style: "font-size:11px;padding:1px 7px" }, "✓ Sẵn sàng"),
      clearBtn
    )
  );

  function syncView() {
    const f = input.files && input.files[0];
    if (f) {
      badgeEl.textContent = getFileIcon(f.name);
      nameEl.textContent = f.name;
      nameEl.title = f.name;
      sizeEl.textContent = `${formatFileSize(f.size)} · ${f.type || "tệp văn bản"}`;
      emptyWrap.style.display = "none";
      previewCard.style.display = "flex";
      box.classList.add("has-file");
    } else {
      emptyWrap.style.display = "flex";
      previewCard.style.display = "none";
      box.classList.remove("has-file");
    }
  }

  function clear() {
    input.value = "";
    syncView();
    input.dispatchEvent(new Event("change", { bubbles: true }));
    if (typeof onchange === "function") onchange(null);
  }

  // Drag and drop event handlers
  box.addEventListener("dragover", (e) => {
    e.preventDefault();
    box.classList.add("drag-over");
  });
  box.addEventListener("dragleave", () => {
    box.classList.remove("drag-over");
  });
  box.addEventListener("drop", (e) => {
    e.preventDefault();
    box.classList.remove("drag-over");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      syncView();
      input.dispatchEvent(new Event("change", { bubbles: true }));
      if (typeof onchange === "function") onchange(input.files[0]);
    }
  });

  input.addEventListener("change", () => {
    syncView();
    if (typeof onchange === "function") onchange(input.files && input.files[0]);
  });

  box.append(input, emptyWrap, previewCard);
  container.append(box);

  // Helper APIs attached to container
  container.input = input;
  container.clear = clear;
  container.getFile = () => (input.files && input.files[0]) || null;
  container.sync = syncView;

  return container;
}

const DATA_CLASS_TIERS = [
  {
    id: "INTERNAL",
    name: "INTERNAL",
    label: "Nội bộ (LAN)",
    badge: "Mặc định · Mạng LAN",
    desc: "Chỉ xử lý trong mạng nội bộ, tuyệt đối không gửi dữ liệu ra mạng diện rộng (WAN).",
    icon: "🏢"
  },
  {
    id: "PUBLIC",
    name: "PUBLIC",
    label: "Công khai (WAN)",
    badge: "Toàn mạng",
    desc: "Dữ liệu công khai, an toàn cho suy luận và điều phối phân bổ tải liên vùng qua WAN.",
    icon: "🌐"
  },
  {
    id: "RESTRICTED",
    name: "RESTRICTED",
    label: "Bảo mật cao",
    badge: "Tuyệt mật · Cục bộ",
    desc: "Dữ liệu tuyệt mật, chỉ được suy luận tại worker chỉ định có nhãn riêng.",
    icon: "🛡️"
  }
];

function createDataClassSelector(opts = {}) {
  const {
    id = "rag-class",
    value = "INTERNAL",
    label = "Phân loại dữ liệu (Data Residency & Security)",
    onChange = null
  } = opts;

  let currentVal = value || "INTERNAL";

  // Underlying select for strict compatibility with standard forms and Playwright tests
  const select = el("select", { id, style: "position:absolute;width:1px;height:1px;opacity:0;pointer-events:none" },
    ...DATA_CLASS_TIERS.map(t => el("option", { value: t.id }, t.name))
  );
  select.value = currentVal;

  const cardsContainer = el("div", { class: "data-class-cards", role: "radiogroup", "aria-label": label });

  const cardEls = {};
  DATA_CLASS_TIERS.forEach(tier => {
    const isSelected = tier.id === currentVal;
    const card = el("div", {
      class: "data-class-card" + (isSelected ? " selected" : ""),
      "data-val": tier.id,
      role: "radio",
      "aria-checked": isSelected ? "true" : "false",
      tabindex: "0",
      onclick: () => selectTier(tier.id),
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectTier(tier.id);
        }
      }
    },
      el("div", { class: "data-class-card-top" },
        el("div", { class: "data-class-title-wrap" },
          el("span", { class: "dc-icon" }, tier.icon),
          el("span", {}, tier.name),
          el("span", { class: "pill " + (tier.id === "PUBLIC" ? "ok" : tier.id === "RESTRICTED" ? "err" : "acc"), style: "font-size:10px;padding:0 5px" }, tier.badge)
        ),
        el("div", { class: "data-class-radio-indicator" })
      ),
      el("div", { class: "data-class-desc" }, tier.desc)
    );
    cardEls[tier.id] = card;
    cardsContainer.append(card);
  });

  function selectTier(val) {
    if (!cardEls[val]) return;
    currentVal = val;
    select.value = val;
    DATA_CLASS_TIERS.forEach(t => {
      const card = cardEls[t.id];
      const active = t.id === val;
      if (active) {
        card.classList.add("selected");
        card.setAttribute("aria-checked", "true");
      } else {
        card.classList.remove("selected");
        card.setAttribute("aria-checked", "false");
      }
    });
    select.dispatchEvent(new Event("change", { bubbles: true }));
    if (typeof onChange === "function") onChange(val);
  }

  select.addEventListener("change", () => {
    if (select.value !== currentVal) {
      selectTier(select.value);
    }
  });

  const wrapper = el("div", { class: "data-class-wrapper" },
    el("div", { class: "data-class-header" },
      el("span", {}, label),
      el("span", { class: "mut", style: "font-size:11.5px;font-weight:400" }, "Quy tắc cách ly mạng khi suy luận")
    ),
    cardsContainer,
    select
  );

  wrapper.select = select;
  wrapper.getValue = () => currentVal;
  wrapper.setValue = selectTier;
  Object.defineProperty(wrapper, "value", {
    get: () => currentVal,
    set: (v) => selectTier(v)
  });

  return wrapper;
}

function createDataClassBadge(cls) {
  const c = String(cls || "INTERNAL").toUpperCase();
  const meta = {
    PUBLIC: { label: "PUBLIC", desc: "Dữ liệu công khai · Cho phép phân bổ WAN", icon: "🌐", cls: "data-class-public" },
    INTERNAL: { label: "INTERNAL", desc: "Dữ liệu nội bộ · Chỉ chạy cụm LAN", icon: "🏢", cls: "data-class-internal" },
    RESTRICTED: { label: "RESTRICTED", desc: "Bảo mật cao · Cục bộ nghiêm ngặt", icon: "🛡️", cls: "data-class-restricted" }
  }[c] || { label: c, desc: c, icon: "🏷️", cls: "data-class-internal" };

  return el("span", {
    class: `data-class-badge ${meta.cls}`,
    title: `${meta.label}: ${meta.desc}`
  }, el("span", { class: "dc-icon", "aria-hidden": "true" }, meta.icon), el("span", {}, meta.label));
}


function fmtMiB(bytes) {
  const m = Math.round((bytes || 0) / 1048576);
  if (m >= 10240) return (m / 1024).toFixed(1) + " GiB";
  return m.toLocaleString("en-US") + " MiB";
}

function renderSixBuckets(barEl, legEl, b) {
  if (!barEl) return;
  barEl.replaceChildren();
  if (legEl) legEl.replaceChildren();
  const total = Math.max(b?.Total || b?.total_bytes || 1, 1);
  const mapKey = {
    OSUser: b?.OSUser ?? b?.os_user_bytes ?? 0,
    Weights: b?.Weights ?? b?.weights_bytes ?? 0,
    KVState: b?.KVState ?? b?.kv_state_bytes ?? 0,
    Compute: b?.Compute ?? b?.compute_bytes ?? 0,
    Headroom: b?.Headroom ?? b?.headroom_bytes ?? 0,
    Allocatable: b?.Allocatable ?? b?.allocatable_bytes ?? 0,
  };
  for (const [k, label, cssVar] of BUCKET_GROUPS) {
    const bytes = mapKey[k] || 0;
    const pct = Math.max(0, Math.min(100, (100 * bytes) / total));
    const seg = el("div", {
      style: `width:${pct.toFixed(2)}%;background:var(${cssVar})`,
      title: `${label}: ${fmtMiB(bytes)} (${pct.toFixed(1)}%)`
    });
    barEl.append(seg);
    if (legEl) {
      legEl.append(
        el("div", { class: "vram-item" },
          el("span", { class: "vram-item-label" },
            el("i", { class: "vram-dot", style: `background:var(${cssVar})` }),
            label
          ),
          el("strong", { class: "num" }, fmtMiB(bytes))
        )
      );
    }
  }
}

// Who am I and what may I do? The server enforces it; this only decides what the page shows.
// Until /api/whoami answers, everything is treated as NOT allowed (fail closed in the UI too).
window.AICF = { role: "", site: "", can: { view: false, pause: false, tenants: false } };
window.AICF_READY = (async () => {
  try {
    let r = null;
    const adminTok = store.get("aicf_admin");
    const tenantTok = store.get("aicf_sl_api_key");
    const tok = adminTok || tenantTok;
    if (!ADMIN_PAGES.includes(currentPage())) {
      const headers = tok ? { Authorization: "Bearer " + tok } : {};
      r = await fetch("/api/whoami", { headers });
    } else if (tenantTok && !adminTok) {
      r = await fetch("/api/whoami", { headers: { Authorization: "Bearer " + tenantTok } });
    } else {
      r = await api("/api/whoami");
    }
    if (r && r.ok) {
      const w = await r.json();
      window.AICF = {
        role: w.role,
        site: w.site || "",
        name: w.name,
        email: w.email || "",
        tenant_id: w.tenant_id || "",
        open: !!w.open,
        can: w.can || {},
      };
    }
  } catch {}
  applyRole();
})();

function applyRole() {
  const { role, site, name, tenant_id, open } = window.AICF;
  const isStaff = role === "admin" || role === "operator" || role === "viewer";
  document.body.dataset.role = role || "none";

  // Hide Admin sidebar section and all Admin nav tabs when logged in as a regular User (tenant) or unauthenticated
  const adminHdr = document.getElementById("nav-group-admin");
  if (adminHdr) adminHdr.style.display = isStaff ? "" : "none";
  for (const elTab of document.querySelectorAll('.nav-tab[data-nav-scope="admin"]')) {
    elTab.style.display = isStaff ? "" : "none";
  }
  const brandLink = document.querySelector("a.sidebar-brand");
  if (brandLink) {
    brandLink.setAttribute("href", isStaff ? "index.html" : "serverless.html");
  }
  const livePill = document.getElementById("nav-live");
  if (livePill) {
    livePill.style.display = isStaff ? "" : "none";
  }

  // If logged in as a regular User (tenant) and visiting an Admin-only page, redirect to User Workspace
  if (role === "tenant" && ADMIN_PAGES.includes(currentPage())) {
    location.replace("serverless.html");
    return;
  }

  // Bind tenant inputs on User pages (studio.html, audio.html, chat.html) when logged in as a Tenant
  if (role === "tenant") {
    const activeTid = tenant_id || name || store.get("aicf_sl_tenant_id");
    for (const id of ["tenant", "tts-tenant"]) {
      const inp = document.getElementById(id);
      if (inp && activeTid) {
        inp.value = activeTid;
        inp.readOnly = true;
      }
    }
    const chatKeyInp = document.getElementById("key");
    const slKey = store.get("aicf_sl_api_key");
    if (chatKeyInp && slKey && !chatKeyInp.value) {
      chatKeyInp.value = slKey;
    }
  }

  const badge = document.getElementById("nav-role");
  if (badge) {
    const userLbl = name && name !== "admin-token" && name !== "open" ? `${name} · ` : "";
    const roleText = role === "tenant" ? "người dùng" : role;
    badge.textContent = role ? (open ? "không đăng nhập (loopback)" : userLbl + roleText + (site ? " · " + site : "")) : "chưa xác thực";
    badge.dataset.role = role || "none";
  }
  const authBtn = document.getElementById("nav-auth-btn");
  const secBtn = document.getElementById("nav-security-btn");
  const logoutBtn = document.getElementById("nav-logout-btn");
  const isLoggedIn = Boolean(role && !open);
  if (authBtn) {
    authBtn.textContent = isLoggedIn ? `👤 ${name || role}` : "🔐 Đăng nhập";
  }
  if (secBtn) {
    secBtn.hidden = !isLoggedIn;
  }
  if (logoutBtn) {
    logoutBtn.hidden = !isLoggedIn;
  }
  if (typeof window.syncServerlessRoleUi === "function") {
    window.syncServerlessRoleUi();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  renderNav();
  applyRole();
});

document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    toggleSidebar(false);
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b" && !["INPUT", "TEXTAREA", "SELECT"].includes((e.target && e.target.tagName) || "")) {
    e.preventDefault();
    toggleSidebar();
  }
});
