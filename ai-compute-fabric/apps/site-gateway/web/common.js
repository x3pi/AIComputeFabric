// Shared by every page: admin token (kept in sessionStorage only), authenticated fetch,
// SSE over fetch (EventSource cannot send an Authorization header), theme toggle, contextual user guide, and 6-bucket VRAM renderer.
const NAV = [
  ["index.html", "Cụm GPU & VRAM", "🖥️"],
  ["tenants.html", "Tenants & Khóa API", "🔑"],
  ["chat.html", "Playground & Tải", "💬"],
  ["agents.html", "AI Agent (DAG)", "🕸️"],
  ["studio.html", "AI Studio", "🧪"],
  ["audio.html", "Audio AI", "🎙️"],
  ["guide.html", "Hướng dẫn", "📖"],
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
  },
  "audio.html": {
    title: "Hướng dẫn nhanh — Audio AI Factory (ASR · TTS Tiếng Việt · VAD · Khử nhiễu · WER/CER)",
    steps: [
      ["Tổng hợp Tiếng Việt (TTS) & Vòng lặp", "Nhập câu tiếng Việt ở mục 1 rồi bấm '🔊 Tổng hợp Giọng nói (TTS)' hoặc '🔄 Chạy Vòng lặp TTS → ASR + Chấm WER' để tạo tệp WAV bằng facebook/mms-tts-vie và kiểm chứng độc lập bằng Qwen3-ASR-0.6B."],
      ["Nhận dạng ASR & Lọc nhiễu Spectral Gate", "Tải tệp .wav (PCM 16-bit) hoặc bấm '↪ Đưa âm thanh này sang ô ASR & Lọc nhiễu' để nhận dạng văn bản, xem thanh phân đoạn giọng nói (Energy VAD) và nghe đối chiếu trước/sau lọc nhiễu Wiener Spectral Gate."],
      ["Chuẩn hóa Tiếng Việt & Chấm điểm WER/CER", "Ở mục 4, nhập câu chuẩn và câu máy nhận dạng (hoặc bấm nút điền ví dụ mẫu) để tự động chuyển số/ngày tháng thành chữ, chuẩn hóa dấu thanh (hoà → hòa) và tính sai số từ/ký tự."]
    ],
    tips: [
      "Energy VAD dùng ngưỡng tương đối -30 dB so với đỉnh tín hiệu thay vì ngưỡng cứng -40 dB, giúp không bỏ sót các bản thu âm nhỏ tiếng.",
      "Hệ thống tuân thủ chính sách Đạo đức AI: mọi yêu cầu nhân bản giọng nói (Voice Cloning) hoặc giọng tùy chỉnh không có Consent đều bị từ chối ở tầng thực thi."
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
  for (const [href, label, icon] of NAV) {
    const a = el("a", {
      href,
      class: "nav-tab",
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

  const helpBtn = el("button", {
    type: "button",
    class: "sm ghost",
    title: "Mở hướng dẫn nhanh (phím tắt ?)",
    onclick: openHelpDrawer
  }, "📖 Trợ giúp", el("span", { class: "kbd-hint" }, "?"));

  const tokBtn = el("button", {
    type: "button",
    class: "sm ghost",
    title: "Cập nhật Admin Bearer Token",
    onclick: () => { if (askAdminToken("Nhập Admin Token mới:")) location.reload(); }
  }, "🔑 Token");

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

  const roleBadge = el("span", { class: "nav-role", id: "nav-role", title: "Vai trò của token hiện tại" }, "…");

  const topbar = el("header", { class: "app-topbar" },
    el("div", { class: "topbar-left" }, topbarHamburger),
    el("div", { class: "topbar-actions" }, livePill, roleBadge, helpBtn, tokBtn, themeBtn)
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
    const r = await api("/api/whoami");
    if (r && r.ok) {
      const w = await r.json();
      window.AICF = { role: w.role, site: w.site || "", name: w.name, open: !!w.open, can: w.can };
    }
  } catch {}
  applyRole();
})();

function applyRole() {
  const { role, site, open } = window.AICF;
  const badge = document.getElementById("nav-role");
  if (badge) {
    badge.textContent = role ? (open ? "không đăng nhập (loopback)" : role + (site ? " · " + site : "")) : "chưa xác thực";
    badge.dataset.role = role || "none";
  }
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
  if (e.key === "Escape") {
    toggleSidebar(false);
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b" && !["INPUT", "TEXTAREA", "SELECT"].includes((e.target && e.target.tagName) || "")) {
    e.preventDefault();
    toggleSidebar();
  }
});
