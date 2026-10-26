// Shared by every page: admin token (kept in sessionStorage only), authenticated fetch,
// SSE over fetch (EventSource cannot send an Authorization header), theme toggle, and 6-bucket VRAM renderer.
const NAV = [
  ["index.html", "Cụm GPU & Dự báo VRAM"],
  ["tenants.html", "Người dùng & Khóa API"],
  ["chat.html", "Playground & Đo tải"],
  ["agents.html", "AI Agent (đồ thị DAG)"],
];

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

function askAdminToken(msg) {
  const t = prompt(msg || "Nhập admin token của gateway (--admin-token):", "");
  if (t !== null) store.set("aicf_admin", t.trim());
  return !!t;
}

function renderNav() {
  const nav = document.createElement("nav");
  nav.className = "app-nav";
  const here = location.pathname.split("/").pop() || "index.html";

  const brand = el("a", { href: "index.html", class: "nav-brand" },
    el("span", { class: "nav-brand-logo", "aria-hidden": "true" }, "⚡"),
    el("span", {}, "AI Compute Fabric"),
    el("span", { class: "nav-brand-badge" }, "Control Plane")
  );

  const links = el("div", { class: "nav-links" });
  for (const [href, label] of NAV) {
    const a = el("a", { href, class: "nav-tab" }, label);
    if (href === here) a.setAttribute("aria-current", "page");
    links.append(a);
  }

  const tokBtn = el("button", {
    type: "button",
    class: "sm ghost",
    title: "Cập nhật Admin Bearer Token cho phiên làm việc này",
    onclick: () => { if (askAdminToken("Nhập Admin Token mới:")) location.reload(); }
  }, "🔑 Token");

  const themeBtn = el("button", {
    type: "button",
    class: "sm ghost",
    title: "Chuyển đổi giao diện Sáng / Tối",
    onclick: toggleTheme
  }, "◐ Giao diện");

  const livePill = el("span", { class: "live-pill", id: "nav-live" },
    el("span", { class: "live-dot" }),
    el("span", { id: "nav-live-txt" }, "SSE 1 Hz")
  );

  const actions = el("div", { class: "nav-actions" }, livePill, tokBtn, themeBtn);
  nav.append(brand, links, actions);
  document.body.prepend(nav);
}

async function api(path, opts = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = Object.assign({}, opts.headers || {});
    const tok = store.get("aicf_admin");
    if (tok) headers["Authorization"] = "Bearer " + tok;
    const r = await fetch(path, Object.assign({}, opts, { headers }));
    if (r.status === 401 && attempt === 0 && askAdminToken(tok ? "Token sai. Nhập lại admin token:" : null)) continue;
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
      if (r.status === 401) { if (!askAdminToken()) return; continue; }
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

document.addEventListener("DOMContentLoaded", renderNav);
