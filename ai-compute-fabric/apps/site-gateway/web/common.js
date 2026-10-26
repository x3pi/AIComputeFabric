// Shared by every page: admin token (kept in sessionStorage only), authenticated fetch,
// SSE over fetch (EventSource cannot send an Authorization header), theme toggle, contextual user guide, and 6-bucket VRAM renderer.
const NAV = [
  ["index.html", "Cụm GPU & VRAM"],
  ["tenants.html", "Tenants & Khóa API"],
  ["chat.html", "Playground & Tải"],
  ["agents.html", "AI Agent (DAG)"],
  ["studio.html", "AI Studio"],
  ["serving.html", "Model đang chạy"],
  ["guide.html", "📖 Hướng dẫn"],
];

const BUCKET_GROUPS = [
  ["OSUser", "OS & người dùng", "--b-os"],
  ["Weights", "Trọng số Model", "--b-weights"],
  ["KVState", "KV / State Cache", "--b-kv"],
  ["Compute", "Compute Buffer", "--b-compute"],
  ["Headroom", "Headroom bảo vệ", "--b-headroom"],
  ["Allocatable", "Còn cấp phát được", "--b-free"],
];

const PAGE_GUIDES = {
  "index.html": {
    title: "Hướng dẫn nhanh — Quản trị Cụm GPU & Dự báo 6-Bucket VRAM",
    steps: [
      ["Quan sát Heatmap & 6-Bucket", "Mỗi ô đại diện 1 máy trạm GPU cập nhật 1 Hz. Nhấp vào bất kỳ ô GPU nào (hoặc mã worker dưới bảng) để mở ngăn kéo soi chi tiết 6 ngăn bộ nhớ VRAM của máy đó."],
      ["Dự báo VRAM Fit & Chia Layer", "Chọn mô hình GGUF, độ dài Context, số Slot song song và số GPU để kiểm tra trạng thái GREEN / YELLOW / RED cùng chế độ LOCAL_FULL, LOCAL_HYBRID hoặc DISTRIBUTED_MODEL."],
      ["Điều khiển & Bảo vệ Máy trạm", "Khi chủ máy mở ứng dụng đồ họa nặng, Worker Guard tự động nhường GPU trong < 2 giây (yielded). Vận hành viên có thể bấm 'Tạm dừng AI' để chủ động giải phóng GPU."]
    ],
    tips: [
      "Công thức 6-Bucket bão hòa: Allocatable = Total − OSUser − Weights − KVState − Compute − Headroom.",
      "Chế độ LOCAL_HYBRID tự động tính toán số layer tối đa nạp lên VRAM (MaxGPULayers) và phần tràn sang Host RAM.",
      "Endpoint Prometheus /metrics xuất toàn bộ số liệu cụm theo thời gian thực cho Grafana."
    ]
  },
  "tenants.html": {
    title: "Hướng dẫn nhanh — Quản trị Multi-Tenant, Khóa API & Trọng số WFQ",
    steps: [
      ["Đăng ký Phòng ban / Ứng dụng", "Nhập tên định danh (VD: engineering, sales-bot), đặt trọng số WFQ (Weight) và giới hạn request đồng thời tối đa (0 = không giới hạn cứng)."],
      ["Sao chép Khóa API Một lần", "Sau khi khởi tạo, khóa bí mật có tiền tố aicf_... chỉ hiển thị duy nhất một lần (gateway chỉ lưu băm SHA-256 trên đĩa với quyền 0600)."],
      ["Sử dụng & Xoay vòng Khóa", "Dán khóa aicf_... sang trang Playground hoặc gửi qua header 'Authorization: Bearer aicf_...' từ OpenAI SDK. Bấm 'Đổi khóa' khi cần vô hiệu hóa khóa cũ ngay lập tức."]
    ],
    tips: [
      "Weighted Fair Queuing (WFQ) phân bổ GPU theo tỷ lệ trọng số khi có tranh chấp tải giữa nhiều phòng ban.",
      "Chỉ tài khoản vai trò 'admin' mới được phép khởi tạo, chỉnh sửa hạn mức hoặc xoay vòng khóa API."
    ]
  },
  "chat.html": {
    title: "Hướng dẫn nhanh — AI Playground & Kiểm thử Tải Đồng thời (OpenAI-Compatible)",
    steps: [
      ["Xác thực Phiên Suy luận", "Dán khóa API (aicf_...) vừa cấp từ trang Người dùng vào ô 'Khóa API Tenant' ở cột trái (hoặc dùng mã Tenant ở chế độ mở loopback)."],
      ["Thiết lập QoS & Chọn Câu hỏi", "Chọn lớp ưu tiên Interactive (ưu tiên cao) hoặc Batch (chạy nền), đặt Deadline SLA (ms), hoặc bấm các nút gợi ý câu hỏi nhanh."],
      ["Streaming & Kiểm thử Burst 8×", "Bấm 'Gửi yêu cầu' để xem phản hồi SSE theo thời gian thực kèm thông số TTFT và tok/s, hoặc tích 'Gửi 8 yêu cầu cùng lúc' để kiểm tra bộ lập lịch WFQ."]
    ],
    tips: [
      "Tương thích 100% chuẩn OpenAI: chỉ cần trỏ base_url tới http://<gateway>:18080/v1 và truyền api_key='aicf_...'.",
      "Header X-Session-ID giữ kết nối về cùng worker (KV Prefix Affinity), giúp giảm thời gian chờ token đầu (TTFT) tới 36,6 lần."
    ]
  },
  "agents.html": {
    title: "Hướng dẫn nhanh — Giám sát Đồ thị Tác vụ AI Agent (DAG)",
    steps: [
      ["Chọn Workflow cần theo dõi", "Danh sách Workflow tự động làm mới mỗi 2,5 giây từ Orchestrator (PostgreSQL + NATS JetStream). Nhấp vào tên workflow để nạp đồ thị DAG."],
      ["Đọc Trạng thái & Phụ thuộc", "Mỗi hộp biểu diễn một bước tác vụ xếp theo tầng phụ thuộc từ trái sang phải: Chờ (nét đứt), Đang chạy (xanh dương), Thử lại (vàng), Xong (xanh lá), Từ chối/Lỗi (đỏ)."],
      ["Soi Chi tiết Không lộ Dữ liệu", "Nhấp vào từng ô bước trên đồ thị để xem loại tác vụ (TaskType), số lần thử và thời gian chạy. Nội dung văn bản gốc được bảo vệ tuyệt đối (Zero-Payload Privacy)."]
    ],
    tips: [
      "Sử dụng các nút − / 100% / + ở góc phải đồ thị để thu phóng các workflow phức tạp nhiều tầng.",
      "Khi một bước không đạt kiểm định (Validator) hoặc mất kết nối worker, Orchestrator tự động thử lại theo cấu hình max_attempts."
    ]
  },
  "studio.html": {
    title: "Hướng dẫn nhanh — AI Studio: Tùy biến Mô hình Đa phương pháp theo Tenant",
    steps: [
      ["Chọn Tenant & Phương pháp", "Mọi dữ liệu trong AI Studio được cô lập nghiêm ngặt theo Tenant bằng PostgreSQL Row-Level Security (RLS). Chọn 1 trong 4 tab phương pháp tùy biến."],
      ["Tri thức RAG & Prompt Few-shot", "Tab 'Kho tri thức (RAG)' nhận tệp .txt/.md/.html/.pdf/.docx và trả lời kèm trích dẫn nguồn [1], [2]. Tab 'Prompt & ví dụ mẫu' lưu phiên bản bất biến và chấm điểm đối sánh Zero-shot vs Few-shot."],
      ["Huấn luyện QLoRA & Xuất GGUF", "Tab 'Huấn luyện LoRA' kiểm tra tập dữ liệu JSONL/CSV, huấn luyện QLoRA thực tế trên GPU, vẽ đường cong Loss/Perplexity và cho phép tải về cả .zip (Safetensors) lẫn .gguf cho llama-server."]
    ],
    tips: [
      "Vai trò 'viewer' chỉ được xem và hỏi thử; 'operator' được phép tải tài liệu, lưu phiên bản prompt và chạy job LoRA; chỉ 'admin' mới được xóa dữ liệu.",
      "Bạn có thể bấm nút '✨ Nạp mẫu thử nhanh' trong từng tab để điền sẵn dữ liệu mẫu chuẩn và chạy thử ngay lập tức."
    ]
  }
};

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

function currentPage() {
  const p = location.pathname.split("/").pop() || "index.html";
  return p === "" ? "index.html" : p;
}

function openHelpDrawer() {
  let bd = document.getElementById("help-drawer");
  if (!bd) {
    bd = buildHelpDrawer();
    document.body.append(bd);
  }
  bd.hidden = false;
}

function buildHelpDrawer() {
  const here = currentPage();
  const g = PAGE_GUIDES[here] || PAGE_GUIDES["index.html"];
  const close = () => { bd.hidden = true; };
  const bd = el("div", {
    class: "drawer-backdrop",
    id: "help-drawer",
    hidden: true,
    onclick: e => { if (e.target.id === "help-drawer") close(); }
  });

  const stepItems = g.steps.map(([stTitle, stDesc], idx) =>
    el("div", { class: "guide-step" },
      el("span", { class: "guide-num" }, String(idx + 1)),
      el("div", {}, el("strong", {}, stTitle), el("span", {}, stDesc))
    )
  );

  const tipItems = (g.tips || []).map(t => el("li", {}, t));

  const drawer = el("aside", { class: "drawer", role: "dialog", "aria-label": "Hướng dẫn sử dụng" },
    el("div", { class: "row", style: "justify-content:space-between;border-bottom:1px solid var(--bd);padding-bottom:12px" },
      el("div", {},
        el("h2", {}, "📖 " + g.title),
        el("div", { class: "mut", style: "font-size:12.5px" }, "Cẩm nang thao tác nhanh theo ngữ cảnh trang hiện tại")
      ),
      el("button", { type: "button", class: "sm", onclick: close }, "Đóng ✕")
    ),
    el("div", { style: "display:flex;flex-direction:column;gap:10px" }, ...stepItems),
    el("div", { class: "help-section" },
      el("h3", {}, "💡 Lưu ý vận hành & Mẹo hữu ích"),
      el("ul", {}, ...tipItems)
    ),
    el("div", { class: "help-section" },
      el("h3", {}, "🔐 Thông tin Kết nối Mặc định (Demo)"),
      el("div", { style: "font-size:12.5px;color:var(--fg-secondary);line-height:1.6" },
        el("div", {}, "• Admin Token: ", el("code", { class: "mono" }, "demo-admin-token")),
        el("div", {}, "• OpenAI Endpoint: ", el("code", { class: "mono" }, location.origin + "/v1/chat/completions")),
        el("div", {}, "• Giao diện Worker Guard cục bộ: ", el("code", { class: "mono" }, "http://127.0.0.1:8099/"))
      )
    ),
    el("div", { class: "row", style: "justify-content:space-between;padding-top:8px;border-top:1px solid var(--bd)" },
      el("a", { href: "guide.html", style: "text-decoration:none" },
        el("button", { type: "button", class: "primary sm" }, "📚 Mở trang Tài liệu & Hướng dẫn đầy đủ →")
      ),
      el("button", { type: "button", class: "sm ghost", onclick: close }, "Đóng")
    )
  );
  bd.append(drawer);
  return bd;
}

function renderPageGuideBanner() {
  const here = currentPage();
  const g = PAGE_GUIDES[here];
  if (!g) return;
  const main = document.querySelector("main");
  if (!main || document.getElementById("page-guide-banner")) return;

  const storageKey = "aicf_guide_hide_" + here;
  let collapsed = false;
  try { collapsed = localStorage.getItem(storageKey) === "1"; } catch {}

  const stepsBox = el("div", { class: "guide-steps" },
    ...g.steps.map(([title, desc], i) =>
      el("div", { class: "guide-step" },
        el("span", { class: "guide-num" }, String(i + 1)),
        el("div", {}, el("strong", {}, title), el("span", {}, desc))
      )
    )
  );
  stepsBox.hidden = collapsed;

  const toggleBtn = el("button", {
    type: "button",
    class: "sm ghost",
    onclick: () => {
      collapsed = !collapsed;
      stepsBox.hidden = collapsed;
      toggleBtn.textContent = collapsed ? "💡 Hiện 3 bước hướng dẫn" : "Thu gọn ▲";
      try { localStorage.setItem(storageKey, collapsed ? "1" : "0"); } catch {}
    }
  }, collapsed ? "💡 Hiện 3 bước hướng dẫn" : "Thu gọn ▲");

  const fullDocLink = el("a", {
    href: "guide.html",
    style: "font-size:12.5px;color:var(--acc);font-weight:600;text-decoration:none"
  }, "Xem tài liệu chi tiết →");

  const banner = el("section", { class: "guide-banner", id: "page-guide-banner", "aria-label": "Hướng dẫn nhanh" },
    el("div", { class: "guide-banner-hdr" },
      el("div", { class: "guide-banner-title" },
        el("span", {}, "🧭"),
        el("span", {}, g.title)
      ),
      el("div", { class: "row", style: "gap:10px" }, fullDocLink, toggleBtn)
    ),
    stepsBox
  );

  const hdr = main.querySelector(".page-header");
  if (hdr && hdr.nextSibling) {
    main.insertBefore(banner, hdr.nextSibling);
  } else {
    main.prepend(banner);
  }
}

function renderNav() {
  const nav = document.createElement("nav");
  nav.className = "app-nav";
  const here = currentPage();

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

  const helpBtn = el("button", {
    type: "button",
    class: "sm ghost",
    title: "Mở hướng dẫn nhanh cho màn hình này (phím tắt ?)",
    onclick: openHelpDrawer
  }, "📖 Trợ giúp");

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

  const roleBadge = el("span", { class: "nav-role", id: "nav-role", title: "Vai trò của token hiện tại" }, "…");
  const actions = el("div", { class: "nav-actions" }, livePill, roleBadge, helpBtn, tokBtn, themeBtn);
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

// Who am I and what may I do? The server enforces it; this only decides what the page shows.
// Until /api/whoami answers, everything is treated as NOT allowed (fail closed in the UI too).
window.AICF = { role: "", site: "", can: { view: false, pause: false, tenants: false } };
window.AICF_READY = (async () => {
  try {
    const r = await api("/api/whoami");
    if (r && r.ok) {
      const w = await r.json();
      window.AICF = { role: w.role, site: w.site || "", name: w.name, open: !!w.open, can: w.can };
    }
  } catch {}
  applyRole();
})();

function applyRole() {
  const { role, site, open, can } = window.AICF;
  const badge = document.getElementById("nav-role");
  if (badge) {
    badge.textContent = role ? (open ? "không đăng nhập (loopback)" : role + (site ? " · " + site : "")) : "chưa xác thực";
    badge.dataset.role = role || "none";
  }
  const tab = document.querySelector('.nav-tab[href="tenants.html"]');
  if (tab && !can.tenants) { tab.style.opacity = "0.5"; tab.title = "Chỉ vai trò admin được quản lý người dùng & khóa"; }
}

document.addEventListener("DOMContentLoaded", () => {
  renderNav();
  renderPageGuideBanner();
  applyRole();
});

document.addEventListener("keydown", e => {
  if (e.key === "?" && !["INPUT", "TEXTAREA", "SELECT"].includes((e.target && e.target.tagName) || "")) {
    e.preventDefault();
    openHelpDrawer();
  }
});
