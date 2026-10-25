// Shared by every page: admin token (kept in sessionStorage only), authenticated fetch,
// SSE over fetch (EventSource cannot send an Authorization header).
const NAV = [["index.html", "Cụm máy"], ["tenants.html", "Người dùng & khóa API"], ["chat.html", "Chat thử"]];
function renderNav() {
  const nav = document.createElement("nav");
  const here = location.pathname.split("/").pop() || "index.html";
  for (const [href, label] of NAV) {
    const a = document.createElement("a");
    a.href = href; a.textContent = label;
    if (href === here) a.setAttribute("aria-current", "page");
    nav.append(a);
  }
  document.body.prepend(nav);
}
const store = {
  get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
};
function askAdminToken(msg) {
  const t = prompt(msg || "Nhập admin token của gateway (--admin-token):", "");
  if (t !== null) store.set("aicf_admin", t.trim());
  return !!t;
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
    if (k === "class") e.className = v; else if (k.startsWith("on")) e[k] = v; else e.setAttribute(k, v);
  }
  e.append(...kids.filter(k => k != null));
  return e;
}
document.addEventListener("DOMContentLoaded", renderNav);
