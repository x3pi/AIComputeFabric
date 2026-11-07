const SL_KEY_STORE = "aicf_sl_api_key";
const SL_TENANT_STORE = "aicf_sl_tenant_id";
const SL_EMAIL_STORE = "aicf_sl_account_email";
const SL_MODE_STORE = "aicf_sl_ui_mode";

const CODE_TEMPLATES = {
  gpu_matmul: {
    fn: "classify",
    hw: "gpu-1024",
    code: `import aicf_gpu

def classify(payload: dict) -> dict:
    """Thực thi nhân ma trận 2D (SGEMM) & SAXPY trực tiếp trên GPU CUDA (PTX Kernel)."""
    p = payload or {}
    A = p.get("A") or [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
    B = p.get("B") or [[1.0, 0.0, 2.0], [0.0, 1.0, 1.0], [2.0, 1.0, 0.0]]
    mm = aicf_gpu.matmul(A, B)
    sx = aicf_gpu.saxpy(float(p.get("alpha", 2.5)), [1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0])
    dev = aicf_gpu.device_info()
    print(f"[gpu_matmul] backend={mm['backend']} device={dev.get('device_name')} kernel_ms={mm['kernel_ms']}")
    return {
        "release": "gpu-matmul-v1",
        "gpu": dev,
        "matmul": mm,
        "saxpy": sx,
    }
`
  },
  gpu_vector_search: {
    fn: "classify",
    hw: "gpu-1024",
    code: `import aicf_gpu

def classify(payload: dict) -> dict:
    """Tìm kiếm tương đồng Vector Cosine (RAG / Embedding Search) bằng nhân ma trận GPU."""
    p = payload or {}
    query = p.get("query_vec") or [0.9, 0.2, 0.1, 0.8]
    docs = p.get("doc_vecs") or [
        [0.88, 0.25, 0.12, 0.79],
        [0.10, 0.95, 0.80, 0.05],
        [0.75, 0.15, 0.20, 0.85],
        [0.05, 0.10, 0.92, 0.12],
    ]
    labels = p.get("labels") or [
        "doc-gpu-serverless-cuda",
        "doc-network-routing",
        "doc-vram-lease-reclaim",
        "doc-storage-volume-sha256",
    ]
    res = aicf_gpu.cosine_search(query, docs, labels)
    dev = aicf_gpu.device_info()
    print(f"[gpu_vector_search] backend={res['backend']} top={res['ranked'][0]['label']}")
    return {
        "release": "gpu-rag-v1",
        "gpu": dev,
        "search": res,
    }
`
  },
  gpu_bench: {
    fn: "classify",
    hw: "gpu-1024",
    code: `import os
import aicf_gpu

def classify(payload: dict) -> dict:
    """Cấp phát bộ nhớ VRAM thực tế trên GPU (cuMemAlloc_v2) & chạy PTX Kernel Benchmark."""
    p = payload or {}
    alloc_mib = int(p.get("alloc_mib", 64))
    n_floats = int(p.get("size", 262144))
    stress = aicf_gpu.vram_stress(alloc_mib=alloc_mib, n_floats=n_floats)
    dev = aicf_gpu.device_info()
    print(f"[gpu_bench] backend={stress['backend']} alloc={stress['allocated_mib']}MiB kernel_ms={stress['kernel_ms']}")
    return {
        "release": "gpu-bench-v2",
        "gpu": dev,
        "benchmark": stress,
        "vram_cap_mb": int(os.environ.get("AICF_VRAM_MB", "0") or 0),
        "sandbox_pid": os.getpid(),
    }
`
  },
  volume_io: {
    fn: "classify",
    hw: "gpu-1024",
    code: `import json
import os
import time
import aicf_gpu

def classify(payload: dict) -> dict:
    """Thực thi trên GPU & lưu kết quả bền vững vào Volume của Tenant (/mnt/volumes)."""
    p = payload or {}
    note = str(p.get("text", "Hello from AICF GPU Volume Container"))
    vol_dir = "/mnt/volumes/shared-artifacts"
    if not os.path.isdir("/mnt/volumes"):
        vol_dir = "/tmp/shared-artifacts"
    os.makedirs(vol_dir, exist_ok=True)
    path = os.path.join(vol_dir, "gpu_run_artifact.json")
    sx = aicf_gpu.saxpy(2.0, [10.0, 20.0, 30.0, 40.0], [1.0, 2.0, 3.0, 4.0])
    record = {
        "saved_at": time.time(),
        "note": note,
        "gpu_saxpy": sx,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    print(f"[volume_io] persisted GPU artifact to {path}")
    return {
        "release": "storage-gpu-v2",
        "file": path,
        "persisted_in_volume": path.startswith("/mnt/volumes/"),
        "size_bytes": os.path.getsize(path),
        "data": loaded,
    }
`
  },
  beta9_style: {
    fn: "classify",
    hw: "gpu-1024",
    code: `from aicf import endpoint, Image, Volume
import aicf_gpu

@endpoint(
    name="text-analyzer",
    gpu="any",
    vram_mb=1024,
    memory=512,
    keep_warm_seconds=120,
    image=Image.debian_slim(python_version="3.12"),
    volumes=[Volume("shared-artifacts", mount_path="/mnt/volumes/shared-artifacts")],
)
def classify(**inputs) -> dict:
    """Phong cách Beam/Beta9 (@endpoint + **inputs) kết hợp thực thi GPU CUDA."""
    text = str(inputs.get("text", ""))
    words = text.split()
    sx = aicf_gpu.saxpy(float(inputs.get("alpha", 3.0)), [1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0])
    dev = aicf_gpu.device_info()
    print(f"[beta9_endpoint] words={len(words)} gpu={dev.get('device_name')} backend={sx['backend']}")
    return {
        "sdk_style": "beam-beta9-compatible",
        "word_count": len(words),
        "top_words": words[:5],
        "gpu_device": dev.get("device_name"),
        "gpu_saxpy": sx,
        "preview": text[:80],
    }
`
  },
  demo_v1: {
    fn: "classify",
    hw: "cpu",
    code: `def classify(payload: dict) -> dict:
    """Phân tích văn bản cơ bản (v1) — chạy trong container cô lập."""
    text = str((payload or {}).get("text", ""))
    words = text.split()
    print(f"[demo_v1] classified {len(words)} words")
    return {
        "release": "v1",
        "word_count": len(words),
        "char_count": len(text),
        "preview": text[:80],
    }
`
  },
  demo_v2: {
    fn: "classify",
    hw: "cpu",
    code: `def classify(payload: dict) -> dict:
    """Phân tích văn bản nâng cao & trích xuất top token (v2)."""
    text = str((payload or {}).get("text", ""))
    words = text.split()
    upper_words = [w.upper() for w in words[:5]]
    print(f"[demo_v2] classified {len(words)} words -> top={upper_words}")
    return {
        "release": "v2",
        "word_count": len(words),
        "char_count": len(text),
        "top_tokens": upper_words,
        "preview": text[:80],
    }
`
  }
};

const keyInput = document.getElementById("sl-api-key");
const tenantInput = document.getElementById("sl-tenant-id");
const emailBadge = document.getElementById("sl-account-email-badge");
const loginGate = document.getElementById("sl-login-gate");
const workspaceContent = document.getElementById("sl-workspace-content");
const loginKeyInput = document.getElementById("sl-login-key-input");
const loginIdInput = document.getElementById("sl-login-id-input");
const loginPwInput = document.getElementById("sl-login-pw-input");
const signupNameInput = document.getElementById("sl-signup-name-input");
const signupEmailInput = document.getElementById("sl-signup-email-input");
const signupPwInput = document.getElementById("sl-signup-pw-input");
const codeEditor = document.getElementById("sl-code-editor");

let uiMode = "user"; // "user" (Developer Workspace) or "admin" (Cluster Operations)
try {
  const savedKey = sessionStorage.getItem(SL_KEY_STORE);
  const savedTenant = sessionStorage.getItem(SL_TENANT_STORE);
  const savedEmail = sessionStorage.getItem(SL_EMAIL_STORE);
  const savedMode = sessionStorage.getItem(SL_MODE_STORE);
  if (savedKey) {
    keyInput.value = savedKey;
    loginKeyInput.value = savedKey;
  }
  if (savedTenant) {
    tenantInput.value = savedTenant;
    signupNameInput.value = savedTenant;
  }
  if (savedEmail && emailBadge) {
    emailBadge.textContent = savedEmail;
  }
  if (savedMode === "admin") uiMode = "admin";
} catch {}

function updateLoginGateVisibility() {
  const hasTenantKey = !!keyInput.value.trim();
  if (!hasTenantKey && uiMode === "user") {
    loginGate.style.display = "block";
    if (workspaceContent) workspaceContent.style.display = "none";
  } else {
    loginGate.style.display = "none";
    if (workspaceContent) workspaceContent.style.display = "block";
  }
}

function isAdminRole() {
  const r = (window.AICF && window.AICF.role) || "";
  return r === "admin" || r === "operator";
}

function activateTab(tabBtnId) {
  const btn = document.getElementById(tabBtnId);
  if (!btn) return;
  for (const b of document.querySelectorAll(".sl-tab")) b.classList.remove("active");
  for (const p of document.querySelectorAll(".sl-panel")) p.classList.remove("active");
  btn.classList.add("active");
  const panel = document.getElementById(btn.dataset.tab);
  if (panel) panel.classList.add("active");
  if (btn.dataset.tab === "panel-volumes") listVolumeFiles();
}

function syncRoleUi() {
  const adminOk = isAdminRole();
  const modeAdminBtn = document.getElementById("sl-mode-admin");
  const modeSwitchWrap = modeAdminBtn ? modeAdminBtn.closest(".mode-switch") : null;
  if (modeAdminBtn) modeAdminBtn.style.display = adminOk ? "inline-flex" : "none";
  if (modeSwitchWrap) modeSwitchWrap.style.display = adminOk ? "inline-flex" : "none";
  if (!adminOk && uiMode === "admin") {
    uiMode = "user";
    try { sessionStorage.setItem(SL_MODE_STORE, "user"); } catch {}
  }

  const wsBar = document.getElementById("sl-workspace-bar");
  const tabApps = document.getElementById("tab-btn-apps");
  const tabCont = document.getElementById("tab-btn-containers");
  const tabVols = document.getElementById("tab-btn-volumes");
  const tabAudit = document.getElementById("tab-btn-audit");
  const panelApps = document.getElementById("panel-apps");
  const panelCont = document.getElementById("panel-containers");
  const panelVols = document.getElementById("panel-volumes");
  const panelAudit = document.getElementById("panel-audit");
  const adminPanel = document.getElementById("sl-admin-cluster-panel");

  if (tenantInput) {
    tenantInput.readOnly = !adminOk;
    tenantInput.title = adminOk
      ? "Admin có thể chuyển đổi Tenant để kiểm tra"
      : "Tenant ID được gắn cố định với tài khoản đăng nhập của bạn";
  }

  if (adminOk && uiMode === "admin") {
    // Admin Mode: Hide Developer Workspace IDE & Volumes, show ONLY Admin Cluster Console + Containers + Audit
    if (wsBar) wsBar.style.display = "none";
    if (adminPanel) adminPanel.style.display = "block";
    if (tabApps) tabApps.style.display = "none";
    if (tabVols) tabVols.style.display = "none";
    if (tabCont) tabCont.style.display = "inline-flex";
    if (tabAudit) tabAudit.style.display = "inline-flex";
    if (panelApps) panelApps.classList.remove("active");
    if (panelVols) panelVols.classList.remove("active");
    if (!panelCont?.classList.contains("active") && !panelAudit?.classList.contains("active")) {
      activateTab("tab-btn-containers");
    }
  } else {
    // User / Developer Mode: Hide Admin Cluster Console & Audit tab, show Developer Workspace + 3 User tabs
    if (wsBar) wsBar.style.display = "";
    if (adminPanel) adminPanel.style.display = "none";
    if (tabApps) tabApps.style.display = "inline-flex";
    if (tabCont) tabCont.style.display = "inline-flex";
    if (tabVols) tabVols.style.display = "inline-flex";
    if (tabAudit) tabAudit.style.display = "none";
    if (panelAudit) panelAudit.classList.remove("active");
    if (
      !panelApps?.classList.contains("active") &&
      !panelCont?.classList.contains("active") &&
      !panelVols?.classList.contains("active")
    ) {
      activateTab("tab-btn-apps");
    }
  }
}
window.syncServerlessRoleUi = syncRoleUi;

function applyAuthenticatedSession({ token, tenantId, email, name, role, preserveAdminToken }) {
  const effectiveRole = role || "tenant";
  keyInput.value = token;
  loginKeyInput.value = token;
  if (tenantId) tenantInput.value = tenantId;
  const label = email || name || tenantId || "active-session";
  if (emailBadge) emailBadge.textContent = `👤 ${label}`;
  try {
    sessionStorage.setItem(SL_KEY_STORE, token);
    if (tenantId) sessionStorage.setItem(SL_TENANT_STORE, tenantId);
    sessionStorage.setItem(SL_EMAIL_STORE, `👤 ${label}`);
    if (effectiveRole === "admin" || effectiveRole === "operator" || effectiveRole === "viewer") {
      if (!preserveAdminToken) sessionStorage.setItem("aicf_admin", token);
    } else {
      sessionStorage.removeItem("aicf_admin");
    }
  } catch {}
  if (window.AICF) {
    window.AICF.role = effectiveRole;
    window.AICF.name = name || email || tenantId;
    window.AICF.email = email || "";
    window.AICF.tenant_id = tenantId || "";
    window.AICF.open = false;
    if (typeof applyRole === "function") applyRole();
  }
  syncRoleUi();
  if (effectiveRole !== "admin" && effectiveRole !== "operator") {
    setUiMode("user");
  } else {
    updateLoginGateVisibility();
    loadOverview();
  }
}

function setUiMode(mode) {
  const wantAdmin = mode === "admin" && isAdminRole();
  uiMode = wantAdmin ? "admin" : "user";
  try { sessionStorage.setItem(SL_MODE_STORE, uiMode); } catch {}
  document.body.classList.toggle("sl-user-mode", uiMode === "user");
  document.getElementById("sl-mode-user").classList.toggle("active", uiMode === "user");
  document.getElementById("sl-mode-admin").classList.toggle("active", uiMode === "admin");
  const wsBar = document.getElementById("sl-workspace-bar");
  wsBar.classList.toggle("admin-mode", uiMode === "admin");
  const badge = document.getElementById("sl-ws-role-badge");
  const heading = document.getElementById("sl-page-heading");
  if (uiMode === "admin") {
    badge.textContent = "🛡️ ADMIN CLUSTER VIEW";
    badge.className = "pill warn";
    heading.textContent = "🛡️ AICF Serverless Cloud — Quản trị Hạ tầng Cụm (Admin / SRE)";
  } else {
    badge.textContent = "👤 DEVELOPER WORKSPACE";
    badge.className = "pill ok";
    heading.textContent = "⚡ AICF Serverless Cloud — Cổng Developer (GPU/Python)";
  }
  syncRoleUi();
  updateLoginGateVisibility();
  loadOverview();
}

document.getElementById("sl-mode-user").onclick = () => setUiMode("user");
document.getElementById("sl-mode-admin").onclick = () => setUiMode("admin");

document.getElementById("sl-btn-switch-account").onclick = () => {
  loginGate.style.display = loginGate.style.display === "none" ? "block" : "none";
};
const slSecBtn = document.getElementById("sl-btn-security");
if (slSecBtn) {
  slSecBtn.onclick = () => {
    if (typeof openAccountSecurityModal === "function") openAccountSecurityModal();
  };
}
const slLoginPwToggle = document.getElementById("sl-btn-login-pw-toggle");
if (slLoginPwToggle && loginPwInput) {
  slLoginPwToggle.onclick = () => {
    const isPw = loginPwInput.type === "password";
    loginPwInput.type = isPw ? "text" : "password";
    slLoginPwToggle.textContent = isPw ? "Ẩn" : "Hiện";
  };
}
const slSignupPwToggle = document.getElementById("sl-btn-signup-pw-toggle");
if (slSignupPwToggle && signupPwInput) {
  slSignupPwToggle.onclick = () => {
    const isPw = signupPwInput.type === "password";
    signupPwInput.type = isPw ? "text" : "password";
    slSignupPwToggle.textContent = isPw ? "Ẩn" : "Hiện";
  };
}
const slPwStrengthEl = document.getElementById("sl-signup-pw-strength");
if (signupPwInput && slPwStrengthEl) {
  signupPwInput.addEventListener("input", () => {
    const len = signupPwInput.value.length;
    if (len === 0) {
      slPwStrengthEl.textContent = "Chuẩn OWASP ≥ 12 ký tự";
      slPwStrengthEl.style.color = "var(--fg-secondary)";
    } else if (len < 12) {
      slPwStrengthEl.textContent = `Cần thêm ${12 - len} ký tự (${len}/12)`;
      slPwStrengthEl.style.color = "#dc2626";
    } else {
      slPwStrengthEl.textContent = `✓ Đạt chuẩn Argon2id (${len} ký tự)`;
      slPwStrengthEl.style.color = "#16a34a";
    }
  });
}
[loginIdInput, loginPwInput].forEach(inp => {
  if (inp) {
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        document.getElementById("sl-btn-login-account")?.click();
      }
    });
  }
});
[signupNameInput, signupEmailInput, signupPwInput].forEach(inp => {
  if (inp) {
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        document.getElementById("sl-btn-register-account")?.click();
      }
    });
  }
});

// Cloud Logout: revokes session token on server and locks Developer Workspace
document.getElementById("sl-btn-logout").onclick = async () => {
  const curTok = keyInput.value.trim();
  if (curTok) {
    try {
      await fetch("/api/serverless/v1/auth/logout", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + curTok,
        },
        body: JSON.stringify({ session_token: curTok }),
      });
    } catch {}
  }
  keyInput.value = "";
  loginKeyInput.value = "";
  if (loginPwInput) loginPwInput.value = "";
  if (emailBadge) emailBadge.textContent = "Đã đăng xuất";
  try {
    sessionStorage.removeItem(SL_KEY_STORE);
    sessionStorage.removeItem(SL_EMAIL_STORE);
    sessionStorage.removeItem("aicf_admin");
  } catch {}
  if (window.AICF) {
    window.AICF = { role: "", site: "", name: "", open: false, can: {} };
    if (typeof applyRole === "function") applyRole();
  }
  document.getElementById("sl-auth-msg").textContent =
    "✓ Đã đăng xuất an toàn và thu hồi phiên làm việc trên máy chủ. Hãy đăng nhập lại để tiếp tục.";
  setUiMode("user");
  updateLoginGateVisibility();
};

// Rotate API Key for current logged-in Workspace
document.getElementById("sl-btn-rotate-key").onclick = async () => {
  const curTok = keyInput.value.trim();
  if (!curTok) return;
  const r = await fetch("/api/serverless/v1/auth/rotate-key", {
    method: "POST",
    headers: { Authorization: "Bearer " + curTok },
  });
  if (!r || !r.ok) {
    document.getElementById("sl-deploy-msg").textContent = "❌ Không thể cấp lại Khóa API (HTTP " + (r ? r.status : "err") + ").";
    return;
  }
  const d = await r.json();
  if (d.api_key) {
    keyInput.value = d.api_key;
    loginKeyInput.value = d.api_key;
    try { sessionStorage.setItem(SL_KEY_STORE, d.api_key); } catch {}
    document.getElementById("sl-deploy-msg").textContent =
      `🔑 Đã cấp lại Khóa API mới cho Workspace '${d.tenant?.id || tenantInput.value}': ${d.api_key.slice(0, 14)}… (khóa cũ đã bị vô hiệu hóa).`;
  }
};

keyInput.addEventListener("input", () => {
  try { sessionStorage.setItem(SL_KEY_STORE, keyInput.value.trim()); } catch {}
});
tenantInput.addEventListener("input", () => {
  try { sessionStorage.setItem(SL_TENANT_STORE, tenantInput.value.trim()); } catch {}
});

document.getElementById("sl-btn-toggle-key").onclick = (ev) => {
  const isPw = keyInput.type === "password";
  keyInput.type = isPw ? "text" : "password";
  ev.currentTarget.textContent = isPw ? "Ẩn" : "Hiện";
};
document.getElementById("sl-btn-login-show").onclick = (ev) => {
  const isPw = loginKeyInput.type === "password";
  loginKeyInput.type = isPw ? "text" : "password";
  ev.currentTarget.textContent = isPw ? "Ẩn khóa" : "Hiện khóa";
};
document.getElementById("sl-btn-copy-key").onclick = async (ev) => {
  const k = keyInput.value.trim();
  if (!k) return;
  try {
    await navigator.clipboard.writeText(k);
    ev.currentTarget.textContent = "✓ Đã chép!";
    setTimeout(() => { ev.currentTarget.textContent = "📋 Copy Key"; }, 1500);
  } catch {}
};

// Main Tabs switching
for (const btn of document.querySelectorAll(".sl-tab")) {
  btn.onclick = () => {
    for (const b of document.querySelectorAll(".sl-tab")) b.classList.remove("active");
    for (const p of document.querySelectorAll(".sl-panel")) p.classList.remove("active");
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "panel-volumes") listVolumeFiles();
  };
}

// Helper for calling /api/serverless/* using the Tenant API key or Session token
async function slTenantFetch(path, init = {}) {
  const k = keyInput.value.trim();
  if (!k) return api(path, init);
  const headers = { ...(init.headers || {}), Authorization: "Bearer " + k };
  return fetch(path, { ...init, headers });
}

// 1. Cloud Account Login (Email / Username + Password)
document.getElementById("sl-btn-login-account").onclick = async () => {
  const authMsg = document.getElementById("sl-auth-msg");
  const identifier = (loginIdInput?.value || "").trim();
  const password = loginPwInput?.value || "";
  if (!identifier || !password) {
    authMsg.textContent = "⚠️ Vui lòng nhập cả Email/Tên tài khoản và Mật khẩu.";
    return;
  }
  authMsg.textContent = "Đang xác thực tài khoản Cloud…";
  const r = await fetch("/api/serverless/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier, password }),
  });
  if (!r.ok) {
    const errTxt = await r.text();
    authMsg.textContent = `❌ Đăng nhập thất bại (${r.status}): sai tài khoản hoặc mật khẩu (${errTxt})`;
    return;
  }
  const d = await r.json();
  const tok = d.session_token || d.api_key;
  const tid = d.tenant?.id || identifier;
  authMsg.textContent = `✓ Đăng nhập thành công vào tài khoản '${d.tenant?.email || d.tenant?.name || tid}'!`;
  applyAuthenticatedSession({ token: tok, tenantId: tid, email: d.tenant?.email, name: d.tenant?.name, role: d.role || "tenant" });
  if (d.role === "admin") setUiMode("admin");
};

// 2. Cloud Account Registration (Workspace Name + Email + Password)
document.getElementById("sl-btn-register-account").onclick = async () => {
  const authMsg = document.getElementById("sl-auth-msg");
  const name = (signupNameInput?.value || "dev-workspace").trim().replace(/[^A-Za-z0-9_.-]/g, "-");
  const email = (signupEmailInput?.value || "").trim();
  const password = signupPwInput?.value || "";
  if (!name || !email || password.length < 12) {
    authMsg.textContent = "⚠️ Vui lòng nhập Tên Workspace, Email hợp lệ và Mật khẩu tối thiểu 12 ký tự.";
    return;
  }
  authMsg.textContent = "Đang khởi tạo Tài khoản Cloud & cấp phát hạn mức GPU…";
  const r = await fetch("/api/serverless/v1/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, email, password }),
  });
  if (!r.ok) {
    const errTxt = await r.text();
    authMsg.textContent = `❌ Đăng ký thất bại (${r.status}): ${errTxt}`;
    return;
  }
  const d = await r.json();
  const tid = d.tenant?.id || name;
  // Prefer permanent api_key in the bar right after registration so user can copy it for CLI, or session_token
  const tok = d.api_key || d.session_token;
  if (loginIdInput) loginIdInput.value = d.tenant?.email || name;
  authMsg.textContent = `✓ Đã tạo tài khoản '${d.tenant?.email}' (Tenant '${tid}') và cấp Khóa API ${tok.slice(0, 12)}…!`;
  applyAuthenticatedSession({ token: tok, tenantId: tid, email: d.tenant?.email, name: d.tenant?.name });
};

// 3. Login with existing Tenant API Key
document.getElementById("sl-btn-login-key").onclick = async () => {
  const authMsg = document.getElementById("sl-auth-msg");
  const k = loginKeyInput.value.trim();
  if (!k) {
    authMsg.textContent = "⚠️ Vui lòng nhập Khóa API Tenant (aicf_...).";
    return;
  }
  authMsg.textContent = "Đang xác thực Khóa API với cổng Serverless…";
  const r = await fetch("/api/serverless/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: k }),
  });
  if (!r.ok) {
    authMsg.textContent = "❌ Khóa API không hợp lệ hoặc đã bị thu hồi (HTTP " + r.status + ").";
    return;
  }
  const d = await r.json();
  const resolvedTid = d.tenant?.id || tenantInput.value.trim() || "demo";
  authMsg.textContent = `✓ Đăng nhập thành công vào Workspace '${resolvedTid}'!`;
  applyAuthenticatedSession({ token: k, tenantId: resolvedTid, email: d.tenant?.email, name: d.tenant?.name, role: d.role || "tenant" });
  if (d.role === "admin") setUiMode("admin");
};

// 4. 1-Click Self-Serve Workspace Signup / Quick Demo Setup
document.getElementById("sl-btn-quick-demo").onclick = async () => {
  const authMsg = document.getElementById("sl-auth-msg");
  const rawName = (signupNameInput.value.trim() || tenantInput.value.trim() || "demo").replace(/[^A-Za-z0-9_.-]/g, "-");
  authMsg.textContent = "Đang khởi tạo Workspace và cấp khóa API Tenant…";
  try {
    let tid = rawName;
    let apiKey = "";
    let email = "";

    const adminTok = sessionStorage.getItem("aicf_admin") || "";
    const signupHeaders = { "Content-Type": "application/json" };
    if (adminTok) signupHeaders["Authorization"] = "Bearer " + adminTok;
    const sr = await fetch("/api/serverless/v1/signup", {
      method: "POST",
      headers: signupHeaders,
      body: JSON.stringify({ name: rawName }),
    });
    if (sr && (sr.status === 201 || sr.ok)) {
      const d = await sr.json();
      tid = d.tenant.id;
      apiKey = d.api_key;
      email = d.tenant.email || `${rawName}@aicf.local`;
    }

    if (!apiKey) {
      const listRes = await api("/api/tenants");
      if (listRes && listRes.ok) {
        const list = await listRes.json();
        const found = list.find(t => t.name === rawName || t.id === rawName);
        if (found) {
          tid = found.id;
          const rot = await api(`/api/tenants/${encodeURIComponent(tid)}/rotate-key`, { method: "POST" });
          if (rot && rot.ok) apiKey = (await rot.json()).api_key;
        }
      }
    }

    if (!apiKey) {
      authMsg.textContent = "Không thể khởi tạo Workspace. Hãy kiểm tra kết nối tới site-gateway.";
      return;
    }

    if (adminTok) {
      await fetch(`/api/serverless/v1/policies/${encodeURIComponent(tid)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + adminTok },
        body: JSON.stringify({ serverless_enabled: true, gpu_allowed: true, max_vram_mb: 8192, max_concurrency: 8 }),
      }).catch(() => {});
    }

    authMsg.textContent = `✓ Đã cấp khóa cho tenant '${tid}' (${apiKey.slice(0, 12)}…) và kích hoạt Developer Workspace!`;
    const keepAdmin = isAdminRole() && !!adminTok;
    applyAuthenticatedSession({
      token: apiKey,
      tenantId: tid,
      email,
      name: rawName,
      role: keepAdmin ? window.AICF.role : "tenant",
      preserveAdminToken: keepAdmin,
    });
  } catch (e) {
    authMsg.textContent = "Lỗi khởi tạo: " + e;
  }
};

// IDE Features: AicfEditor (CodeMirror 6 architecture, ADR-004), Multi-file tabs, Live Lint, Diff, Drafts
let aicfEditor = null;
let lintTimer = null;

function getDraftKey() {
  const tid = (tenantInput?.value || "demo").trim();
  const app = (document.getElementById("sl-ide-app")?.value || "text-analyzer").trim();
  return `aicf_editor_draft_${tid}_${app}`;
}

function renderFileTabs() {
  const bar = document.getElementById("sl-file-tabs");
  if (!bar || !aicfEditor) return;
  bar.replaceChildren();
  const filesMap = aicfEditor.getAllFiles();
  for (const fname of Object.keys(filesMap)) {
    const isMain = fname === "user_app.py";
    const btn = el("button", {
      type: "button",
      class: "ide-file-tab" + (aicfEditor.activeFile === fname ? " active" : ""),
      "data-file": fname,
      onclick: () => {
        aicfEditor.switchFile(fname);
        renderFileTabs();
      }
    }, `📄 ${fname}`);
    if (!isMain) {
      const delSpan = el("span", {
        style: "margin-left:4px;opacity:0.75;font-weight:700",
        title: `Xóa tệp ${fname}`,
        onclick: (ev) => {
          ev.stopPropagation();
          aicfEditor.deleteFile(fname, "user_app.py");
          renderFileTabs();
          scheduleLint();
        }
      }, "×");
      btn.append(delSpan);
    }
    bar.append(btn);
  }
}

async function runLintCheck() {
  const panel = document.getElementById("sl-lint-panel");
  if (!panel) return null;
  const filesMap = aicfEditor ? aicfEditor.getAllFiles() : { "user_app.py": codeEditor.value };
  const mainSrc = filesMap["user_app.py"] !== undefined ? filesMap["user_app.py"] : codeEditor.value;
  const extraFiles = {};
  for (const [k, v] of Object.entries(filesMap)) {
    if (k !== "user_app.py") extraFiles[k] = v;
  }
  let r = await slTenantFetch("/api/serverless/v1/lint", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_code: mainSrc,
      filename: "user_app.py",
      files: extraFiles,
      entrypoint: (document.getElementById("sl-ide-fn")?.value || "").trim(),
    }),
  });
  if ((!r || !r.ok) && !keyInput.value.trim()) {
    r = await api("/api/serverless/v1/lint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_code: mainSrc,
        filename: "user_app.py",
        files: extraFiles,
        entrypoint: (document.getElementById("sl-ide-fn")?.value || "").trim(),
      }),
    });
  }
  if (!r || !r.ok) return null;
  const res = await r.json();
  const diags = res.diagnostics || [];
  if (aicfEditor) {
    aicfEditor.setDiagnostics(diags);
  }
  panel.style.display = "block";
  panel.replaceChildren();
  if (!diags.length) {
    panel.append(
      el("span", { class: "pill ok" }, "✓ Cú pháp & AST hợp lệ"),
      el("span", { class: "mut mono", style: "margin-left:8px" },
        `Hàm phát hiện: ${(res.functions || []).join(", ") || "(không có)"}`)
    );
  } else {
    for (const d of diags) {
      const cls = d.severity === "error" ? "err" : "warn";
      const row = el("div", {
        class: "lint-diag-row",
        style: "cursor:pointer;padding:3px 0;display:flex;align-items:center;gap:8px",
        onclick: () => {
          if (aicfEditor) aicfEditor.goToLine(d.line, d.file || "user_app.py");
          renderFileTabs();
        }
      },
        el("span", { class: "pill " + cls }, `${d.file || "user_app.py"}:${d.line}:${d.col || 1}`),
        el("span", { class: "mono", style: "font-weight:600" }, `[${d.code}]`),
        el("span", {}, d.message)
      );
      panel.append(row);
    }
  }
  return res;
}

function scheduleLint() {
  if (lintTimer) clearTimeout(lintTimer);
  lintTimer = setTimeout(() => { runLintCheck().catch(() => {}); }, 380);
}

async function runQuickTest() {
  const filesMap = aicfEditor ? aicfEditor.getAllFiles() : { "user_app.py": codeEditor.value };
  const mainSrc = filesMap["user_app.py"] !== undefined ? filesMap["user_app.py"] : codeEditor.value;
  await executeDeploy({ sourceCode: mainSrc, extraFiles: filesMap, label: "quick-test" });
  document.getElementById("sl-btn-call-web").click();
}

if (window.AicfEditor) {
  aicfEditor = new window.AicfEditor(codeEditor, {
    mainFile: "user_app.py",
    onChange: (_val, _activeFile, allFiles) => {
      try {
        localStorage.setItem(getDraftKey(), JSON.stringify({ files: allFiles, ts: Date.now() }));
        const badge = document.getElementById("sl-draft-badge");
        const discardBtn = document.getElementById("sl-btn-discard-draft");
        if (badge) {
          badge.textContent = "● Đã lưu nháp (localStorage)";
          badge.className = "pill warn";
        }
        if (discardBtn) discardBtn.style.display = "inline-flex";
      } catch {}
      scheduleLint();
    },
    onTestRun: () => {
      runQuickTest();
    }
  });
  renderFileTabs();
} else {
  codeEditor.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const start = codeEditor.selectionStart;
      const end = codeEditor.selectionEnd;
      codeEditor.value = codeEditor.value.substring(0, start) + "    " + codeEditor.value.substring(end);
      codeEditor.selectionStart = codeEditor.selectionEnd = start + 4;
    }
  });
}

if (document.getElementById("sl-btn-lint-code")) {
  document.getElementById("sl-btn-lint-code").onclick = () => runLintCheck();
}
if (document.getElementById("sl-btn-autocomplete")) {
  document.getElementById("sl-btn-autocomplete").onclick = () => {
    if (aicfEditor) aicfEditor.showAutocomplete();
  };
}
if (document.getElementById("sl-btn-test-run")) {
  document.getElementById("sl-btn-test-run").onclick = () => runQuickTest();
}
if (document.getElementById("sl-btn-add-file")) {
  document.getElementById("sl-btn-add-file").onclick = () => {
    const inp = document.getElementById("sl-new-file-input");
    let raw = (inp?.value || "helpers.py").trim().replace(/[^A-Za-z0-9_.-]/g, "_");
    if (!raw.endsWith(".py")) raw += ".py";
    if (aicfEditor) {
      aicfEditor.switchFile(raw);
      renderFileTabs();
      if (inp) inp.value = "";
    }
  };
}
if (document.getElementById("sl-btn-discard-draft")) {
  document.getElementById("sl-btn-discard-draft").onclick = () => {
    try { localStorage.removeItem(getDraftKey()); } catch {}
    const tplKey = document.getElementById("sl-template-select")?.value || "gpu_matmul";
    const tpl = CODE_TEMPLATES[tplKey] || CODE_TEMPLATES.gpu_matmul;
    if (aicfEditor) {
      aicfEditor.setAllFiles({ "user_app.py": tpl.code }, "user_app.py");
      renderFileTabs();
    } else {
      codeEditor.value = tpl.code;
    }
    const badge = document.getElementById("sl-draft-badge");
    if (badge) {
      badge.textContent = "✓ Đã khôi phục gốc";
      badge.className = "pill ok";
    }
    document.getElementById("sl-btn-discard-draft").style.display = "none";
    scheduleLint();
  };
}

function renderDiffPanel() {
  const diffPanel = document.getElementById("sl-diff-panel");
  const verSel = document.getElementById("sl-diff-version-select");
  const outPre = document.getElementById("sl-diff-output");
  const sumEl = document.getElementById("sl-diff-summary");
  if (!diffPanel || !verSel || !outPre) return;

  const appName = document.getElementById("sl-ide-app").value.trim() || "text-analyzer";
  const fnName = document.getElementById("sl-ide-fn").value.trim() || "classify";
  const apps = currentOverview.apps || [];
  const appRec = apps.find(a => a.app_name === appName) || apps[0];
  const vSources = appRec?.version_sources || {};
  const versions = Object.keys(vSources);

  const prevSelected = verSel.value;
  verSel.replaceChildren();
  for (const v of versions) {
    verSel.append(el("option", { value: v }, `${v}${v === appRec?.active_version ? " (Active)" : ""}`));
  }
  if (prevSelected && versions.includes(prevSelected)) {
    verSel.value = prevSelected;
  } else if (appRec?.active_version && versions.includes(appRec.active_version)) {
    verSel.value = appRec.active_version;
  }

  const chosenVer = verSel.value;
  const deployedSrc = (vSources[chosenVer]?.functions?.[fnName]?.source_code) ||
                      (appRec?.functions_detail?.[fnName]?.source_code) || "";
  const editorSrc = aicfEditor ? (aicfEditor.getAllFiles()["user_app.py"] ?? codeEditor.value) : codeEditor.value;

  const diff = window.computeUnifiedDiff
    ? window.computeUnifiedDiff(deployedSrc, editorSrc, `${appName}@${chosenVer || "empty"}`, "Editor (hiện tại)")
    : { lines: [{ type: "ctx", text: editorSrc }], added: 0, removed: 0, identical: deployedSrc === editorSrc };

  if (sumEl) {
    sumEl.textContent = diff.identical ? "Không có thay đổi (0 dòng khác)" : `+${diff.added} / -${diff.removed} dòng`;
    sumEl.className = "pill " + (diff.identical ? "ok" : "warn");
  }
  outPre.replaceChildren();
  for (const ln of diff.lines) {
    const div = document.createElement("div");
    if (ln.type === "add") div.className = "diff-line-add";
    else if (ln.type === "del") div.className = "diff-line-del";
    else if (ln.type === "hdr") div.className = "diff-line-hdr";
    div.textContent = ln.text;
    outPre.appendChild(div);
  }
}

if (document.getElementById("sl-btn-diff-versions")) {
  document.getElementById("sl-btn-diff-versions").onclick = () => {
    const p = document.getElementById("sl-diff-panel");
    if (!p) return;
    p.style.display = "block";
    renderDiffPanel();
  };
}
if (document.getElementById("sl-diff-version-select")) {
  document.getElementById("sl-diff-version-select").onchange = () => renderDiffPanel();
}
if (document.getElementById("sl-btn-close-diff")) {
  document.getElementById("sl-btn-close-diff").onclick = () => {
    document.getElementById("sl-diff-panel").style.display = "none";
  };
}

function detectPythonEntrypoint(src) {
  const m = src.match(/^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/m);
  return m ? m[1] : "";
}

document.getElementById("sl-template-select").onchange = (ev) => {
  const tpl = CODE_TEMPLATES[ev.target.value];
  if (!tpl) return;
  codeEditor.value = tpl.code;
  document.getElementById("sl-ide-fn").value = tpl.fn;
  document.getElementById("sl-ide-hw").value = tpl.hw;
};

document.getElementById("sl-btn-upload-py").onclick = () => {
  document.getElementById("sl-py-file-input").click();
};

document.getElementById("sl-py-file-input").onchange = async (ev) => {
  const file = ev.target.files?.[0];
  if (!file) return;
  if (file.size > 512 * 1024) {
    document.getElementById("sl-deploy-msg").textContent =
      `❌ Tệp '${file.name}' (${file.size} bytes) vượt quá giới hạn 512 KiB (524.288 bytes).`;
    ev.target.value = "";
    return;
  }
  const rawText = await file.text();
  if (rawText.includes("\0")) {
    document.getElementById("sl-deploy-msg").textContent =
      `❌ Tệp '${file.name}' không hợp lệ: phát hiện ký tự NUL (tệp nhị phân).`;
    ev.target.value = "";
    return;
  }
  const text = rawText.replace(/^\uFEFF/, "");
  codeEditor.value = text;
  const baseName = file.name.replace(/\.py$/i, "").replace(/[^A-Za-z0-9_.-]/g, "-");
  if (baseName) {
    document.getElementById("sl-ide-app").value = baseName;
    document.getElementById("sl-web-app").value = baseName;
  }
  const ep = detectPythonEntrypoint(text);
  if (ep) {
    document.getElementById("sl-ide-fn").value = ep;
    document.getElementById("sl-web-fn").value = ep;
  }
  document.getElementById("sl-deploy-msg").textContent =
    `📂 Đã nạp tệp '${file.name}' (${file.size} bytes)${ep ? ` · phát hiện hàm entrypoint '${ep}()'` : ""}. Bấm '🚀 Biên dịch & Triển khai Code' để đẩy lên Cloud!`;
};

document.getElementById("sl-btn-download-py").onclick = () => {
  const appName = document.getElementById("sl-ide-app").value.trim() || "aicf_function";
  const blob = new Blob([codeEditor.value], { type: "text/x-python;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${appName}.py`;
  a.click();
  URL.revokeObjectURL(a.href);
};

let currentOverview = { enabled: true, draining: false };

function handleUnauthorizedSession(reason) {
  const savedLabel = (emailBadge?.textContent || "").replace(/^👤\s*/, "").trim();
  if (loginIdInput && !loginIdInput.value.trim() && savedLabel && savedLabel !== "Chưa đăng nhập" && savedLabel !== "Đã đăng xuất") {
    loginIdInput.value = savedLabel;
  }
  keyInput.value = "";
  loginKeyInput.value = "";
  try { sessionStorage.removeItem(SL_KEY_STORE); } catch {}
  const authMsg = document.getElementById("sl-auth-msg");
  if (authMsg) {
    authMsg.textContent = reason || "⚠️ Phiên đăng nhập trước đã hết hạn hoặc máy chủ vừa khởi động lại. Vui lòng nhập mật khẩu để đăng nhập lại hoặc bấm '⚡ Dùng thử 1-Click (Demo)'.";
  }
  updateLoginGateVisibility();
}

async function loadOverview() {
  let r;
  if (uiMode === "user" && keyInput.value.trim()) {
    r = await slTenantFetch("/api/serverless/v1/status");
    if (r && r.status === 401) {
      handleUnauthorizedSession();
      return;
    }
  } else {
    r = await api("/api/serverless/v1/status");
    if ((!r || !r.ok) && keyInput.value.trim()) {
      r = await slTenantFetch("/api/serverless/v1/status");
      if (r && r.status === 401 && uiMode === "user") {
        handleUnauthorizedSession();
        return;
      }
    }
  }
  if (!r || !r.ok) return;
  const st = await r.json();
  currentOverview = st;

  if (st.whoami?.tenant_id && st.whoami.role === "tenant") {
    tenantInput.value = st.whoami.tenant_id;
    try { sessionStorage.setItem(SL_TENANT_STORE, st.whoami.tenant_id); } catch {}
  }

  // Update Quota Pill in Workspace Bar
  const activeTid = tenantInput.value.trim() || "demo";
  const myPol = (st.policies || {})[activeTid] || Object.values(st.policies || {})[0];
  const quotaPill = document.getElementById("sl-ws-quota-pill");
  if (myPol) {
    if (!myPol.serverless_enabled) {
      quotaPill.textContent = "⚠️ Serverless: Chưa bật";
      quotaPill.className = "pill err";
    } else if (myPol.gpu_allowed) {
      quotaPill.textContent = `⚡ GPU: Bật · Tối đa ${myPol.max_vram_mb} MiB VRAM · ${myPol.max_concurrency} luồng`;
      quotaPill.className = "pill ok";
    } else {
      quotaPill.textContent = `CPU-only · ${myPol.max_memory_mb} MiB RAM · ${myPol.max_concurrency} luồng`;
      quotaPill.className = "pill";
    }
  }

  const statePill = document.getElementById("sl-console-state-pill");
  const kpiStatus = document.getElementById("sl-kpi-status");
  if (!st.enabled) {
    statePill.textContent = "ĐÃ TẮT TOÀN CỤC";
    statePill.className = "pill err";
    kpiStatus.textContent = "DISABLED";
  } else if (st.draining) {
    statePill.textContent = "ĐANG DRAIN";
    statePill.className = "pill warn";
    kpiStatus.textContent = "DRAINING";
  } else {
    statePill.textContent = "SẴN SÀNG";
    statePill.className = "pill ok";
    kpiStatus.textContent = "ONLINE";
  }

  document.getElementById("sl-console-btn-drain").textContent = st.draining ? "Tắt Drain Toàn Cụm" : "Bật Drain Toàn Cụm";
  document.getElementById("sl-console-btn-enable").textContent = st.enabled ? "Tắt Serverless Toàn Cụm" : "Bật Serverless Toàn Cụm";

  const workers = st.workers || [];
  document.getElementById("sl-kpi-workers").textContent = `${workers.length} worker Serverless trực tuyến`;

  const containers = st.containers || [];
  const vramSum = containers.reduce((acc, c) => acc + (+c.vram_mb || 0), 0);
  document.getElementById("sl-kpi-warm").textContent = `${containers.length} container`;
  document.getElementById("sl-kpi-vram").textContent = `${vramSum} MiB VRAM đang giữ`;

  const apps = st.apps || [];
  const webCount = apps.reduce((acc, a) => acc + ((a.web_endpoints || []).length), 0);
  document.getElementById("sl-kpi-apps").textContent = `${apps.length} app`;
  document.getElementById("sl-kpi-endpoints").textContent = `${webCount} web endpoint đang hoạt động`;
  document.getElementById("sl-apps-badge").textContent = `${apps.length} app`;

  const usageMap = st.usage || {};
  let totalGpuSec = 0, totalMibS = 0;
  for (const u of Object.values(usageMap)) {
    totalGpuSec += (+u.gpu_seconds || 0);
    totalMibS += (+u.vram_mib_s || 0);
  }
  document.getElementById("sl-kpi-gpusec").textContent = `${totalGpuSec.toFixed(3)} s`;
  document.getElementById("sl-kpi-mibs").textContent = `${totalMibS} MiB·s (thực thi + giữ ấm)`;

  renderAppsTable(apps);
  renderContainersTable(containers);
  renderCallsTable(st.calls || []);
  renderAuditTable(st.audit || []);
  if (uiMode === "admin") {
    renderAdminPoliciesTable(st.policies || {}, st.usage || {});
  }
}

function renderAdminPoliciesTable(policies, usageMap) {
  const tb = document.getElementById("sl-admin-policies-rows");
  tb.replaceChildren();
  const entries = Object.entries(policies);
  if (!entries.length) {
    tb.append(el("tr", {}, el("td", { colspan: 7, class: "mut" }, "Chưa có chính sách Tenant nào.")));
    return;
  }
  for (const [tid, pol] of entries) {
    const u = usageMap[tid] || { gpu_seconds: 0, vram_mib_s: 0 };
    const chkSl = el("input", { type: "checkbox", checked: !!pol.serverless_enabled });
    const chkGpu = el("input", { type: "checkbox", checked: !!pol.gpu_allowed });
    const inpVram = el("input", { type: "number", class: "mono", value: pol.max_vram_mb ?? 4096, style: "width:90px" });
    const inpConc = el("input", { type: "number", class: "mono", value: pol.max_concurrency ?? 4, style: "width:70px" });
    const saveBtn = el("button", {
      type: "button",
      class: "sm pri",
      onclick: async () => {
        const r = await api(`/api/serverless/v1/policies/${encodeURIComponent(tid)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            serverless_enabled: chkSl.checked,
            gpu_allowed: chkGpu.checked,
            max_vram_mb: parseInt(inpVram.value, 10) || 4096,
            max_concurrency: parseInt(inpConc.value, 10) || 4,
          }),
        });
        document.getElementById("sl-admin-policy-msg").textContent =
          r && r.ok ? `✓ Đã cập nhật hạn mức cho Tenant '${tid}'.` : `Lỗi cập nhật hạn mức (HTTP ${r ? r.status : "err"}).`;
        loadOverview();
      }
    }, "Lưu Hạn mức");

    tb.append(
      el("tr", {},
        el("td", { class: "mono", style: "font-weight:700" }, tid),
        el("td", {}, chkSl),
        el("td", {}, chkGpu),
        el("td", { class: "num" }, inpVram),
        el("td", { class: "num" }, inpConc),
        el("td", { class: "num mono" }, `${Number(u.gpu_seconds || 0).toFixed(3)} s · ${u.vram_mib_s || 0} MiB·s`),
        el("td", {}, saveBtn)
      )
    );
  }
}

function renderAppsTable(apps) {
  const tb = document.getElementById("sl-apps-rows");
  tb.replaceChildren();
  if (!apps.length) {
    tb.append(el("tr", {}, el("td", { colspan: 5, class: "mut" }, "Chưa có ứng dụng nào. Hãy viết/upload code và bấm '🚀 Biên dịch & Triển khai Code'.")));
    return;
  }
  for (const a of apps) {
    const vers = (a.versions || []).join(" → ");
    const fns = (a.functions || []).map(fn => {
      const isWeb = (a.web_endpoints || []).includes(fn);
      return isWeb ? `${fn} 🌐` : fn;
    }).join(", ");

    const editBtn = el("button", {
      type: "button",
      class: "sm",
      style: "margin-right:6px",
      title: "Nạp mã nguồn của ứng dụng này vào Trình soạn thảo",
      onclick: () => {
        document.getElementById("sl-ide-app").value = a.app_name;
        document.getElementById("sl-web-app").value = a.app_name;
        const firstFn = (a.functions || [])[0] || "classify";
        document.getElementById("sl-ide-fn").value = firstFn;
        document.getElementById("sl-web-fn").value = firstFn;
        const det = (a.functions_detail || {})[firstFn];
        if (det && det.source_code) {
          if (aicfEditor) {
            const filesMap = { "user_app.py": det.source_code, ...(det.files || {}) };
            aicfEditor.setAllFiles(filesMap, "user_app.py");
            renderFileTabs();
          } else {
            codeEditor.value = det.source_code;
          }
        }
        document.getElementById("sl-deploy-msg").textContent = `📝 Đã nạp mã nguồn của '${a.app_name}@${a.active_version}' vào Trình soạn thảo.`;
      }
    }, "📝 Sửa Code");

    const rbBtn = el("button", {
      type: "button",
      class: "sm sl-btn-rollback",
      disabled: (a.versions || []).length < 2,
      title: (a.versions || []).length < 2 ? "Cần ít nhất 2 phiên bản để Rollback" : "Quay về phiên bản liền trước",
      onclick: async () => {
        const r = await slTenantFetch(`/api/serverless/v1/apps/${encodeURIComponent(a.app_name)}/rollback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const msgEl = document.getElementById("sl-deploy-msg");
        if (r && r.ok) {
          const d = await r.json();
          msgEl.textContent = `✓ Đã rollback '${a.app_name}' từ ${d.previous_version} về ${d.active_version}.`;
        } else {
          msgEl.textContent = `Lỗi rollback: HTTP ${r ? r.status : "err"}`;
        }
        loadOverview();
      }
    }, "⏪ Rollback");

    const actCell = document.createElement("td");
    actCell.append(editBtn, rbBtn);

    tb.append(
      el("tr", { "data-app": a.app_name },
        el("td", {}, el("strong", {}, a.app_name), el("div", { class: "mut mono", style: "font-size:11.5px" }, a.tenant)),
        el("td", {}, el("span", { class: "pill ok mono sl-active-ver" }, a.active_version)),
        el("td", { class: "mono mut", style: "font-size:12px" }, vers),
        el("td", { class: "mono" }, fns),
        actCell
      )
    );
  }
}

function renderContainersTable(containers) {
  const tb = document.getElementById("sl-console-container-rows");
  tb.replaceChildren();
  if (!containers.length) {
    tb.append(el("tr", {}, el("td", { colspan: 8, class: "mut" }, "Không có container ấm nào đang giữ tài nguyên.")));
    return;
  }
  for (const c of containers) {
    const actTd = document.createElement("td");
    const btn = el("button", {
      type: "button",
      class: "sm sl-console-evict-btn",
      onclick: async () => {
        if (!confirm(`Giải phóng container ${c.container_id}?`)) return;
        if (uiMode === "admin") {
          await api(`/api/serverless/v1/containers/${encodeURIComponent(c.container_id)}?reason=admin`, { method: "DELETE" });
        } else {
          await slTenantFetch(`/api/serverless/v1/containers/${encodeURIComponent(c.container_id)}?reason=idle`, { method: "DELETE" });
        }
        loadOverview();
      }
    }, uiMode === "admin" ? "Force Evict" : "Giải phóng");
    actTd.append(btn);
    tb.append(
      el("tr", {},
        el("td", { class: "mono" }, c.container_id || "-"),
        el("td", {}, c.tenant_id || "-"),
        el("td", { class: "mono" }, `${c.app_name || "app"} / ${c.fn_id || "fn"}`),
        el("td", { class: "mono" }, c.worker_id || "-"),
        el("td", { class: "num mono" }, `${c.vram_mb || 0} MiB`),
        el("td", { class: "num mono" }, `${Math.round(c.ttl_remaining_s || 0)} s`),
        el("td", { class: "mono mut" }, c.lease_id || "-"),
        actTd
      )
    );
  }
}

function renderCallsTable(calls) {
  const tb = document.getElementById("sl-calls-rows");
  tb.replaceChildren();
  if (!calls.length) {
    tb.append(el("tr", {}, el("td", { colspan: 7, class: "mut" }, "Chưa có lượt gọi nào được ghi nhận.")));
    return;
  }
  for (const c of calls) {
    const stClass = c.status === "ok" ? "ok" : (c.status === "pending" || c.status === "running") ? "warn" : "err";
    const tr = el("tr", { style: "cursor:pointer", onclick: () => inspectCall(c.call_id) },
      el("td", { class: "mono", style: "color:var(--acc);font-weight:600" }, (c.call_id || "").slice(0, 14)),
      el("td", {}, c.tenant_id || "-"),
      el("td", { class: "mono" }, `${c.app_name || "-"} / ${c.fn_id || "-"}`),
      el("td", {}, el("span", { class: "pill " + stClass }, c.status || "-")),
      el("td", {}, c.cold_start ? "❄️ Cold" : "🔥 Warm"),
      el("td", { class: "num mono" }, `${Number(c.exec_ms || c.service_ms || 0).toFixed(1)} ms`),
      el("td", { class: "mono" }, c.worker_id || "-")
    );
    tb.append(tr);
  }
}

async function inspectCall(callId) {
  if (!callId) return;
  document.getElementById("sl-call-lookup-id").value = callId;
  const box = document.getElementById("sl-call-detail-box");
  box.textContent = `Đang tải thông tin lượt gọi ${callId}…`;
  const r = await slTenantFetch(`/api/serverless/v1/calls/${encodeURIComponent(callId)}`);
  if (!r || !r.ok) {
    box.textContent = `Không tìm thấy hoặc không có quyền xem lượt gọi ${callId} (HTTP ${r ? r.status : "err"}).`;
    return;
  }
  const d = await r.json();
  box.textContent = JSON.stringify(d, null, 2);
}

document.getElementById("sl-btn-lookup-call").onclick = () => {
  inspectCall(document.getElementById("sl-call-lookup-id").value.trim());
};

function renderAuditTable(events) {
  const tb = document.getElementById("sl-audit-rows");
  tb.replaceChildren();
  if (!events.length) {
    tb.append(el("tr", {}, el("td", { colspan: 5, class: "mut" }, "Chưa có bản ghi kiểm toán.")));
    return;
  }
  for (const ev of events.slice().reverse().slice(0, 25)) {
    const ts = ev.ts ? new Date(ev.ts * 1000).toLocaleTimeString("vi-VN") : "-";
    tb.append(
      el("tr", {},
        el("td", { class: "mono mut" }, ts),
        el("td", {}, el("span", { class: "pill" }, ev.action || "-")),
        el("td", { class: "mono" }, `${ev.tenant || "-"} (${ev.actor || "-"})`),
        el("td", { class: "mono", style: "font-size:11.5px" }, JSON.stringify(ev.detail || {})),
        el("td", { class: "mono mut", style: "font-size:11px" }, (ev.hash || "").slice(0, 12) + "…")
      )
    );
  }
}

// Deploy Function (supports custom source_code + multi-file from IDE OR builtin_template for v1/v2 buttons)
async function executeDeploy({ sourceCode = "", extraFiles = null, templateName = "", label = "custom" }) {
  const msgEl = document.getElementById("sl-deploy-msg");
  if (!keyInput.value.trim()) {
    msgEl.textContent = "⚠️ Bạn chưa đăng nhập Workspace. Hãy bấm nút '⚡ Khởi tạo nhanh Tenant Demo (1-Click)' hoặc nhập Khóa API phía trên!";
    loginGate.style.display = "block";
    return;
  }
  const appName = document.getElementById("sl-ide-app").value.trim() || document.getElementById("sl-web-app").value.trim() || "text-analyzer";
  const fnName = document.getElementById("sl-ide-fn").value.trim() || document.getElementById("sl-web-fn").value.trim() || "classify";
  const hwVal = document.getElementById("sl-ide-hw").value;
  let gpuSpec = null;
  let vramMb = 0;
  if (hwVal.startsWith("gpu-")) {
    gpuSpec = "any";
    vramMb = parseInt(hwVal.split("-")[1], 10) || 1024;
  }

  const fnObj = {
    fn_id: fnName,
    kind: "function",
    gpu: gpuSpec,
    vram_mb: vramMb,
    memory_mb: 512,
    cpus: 1.0,
    timeout_s: 30,
    keep_warm_s: 120,
  };

  if (templateName) {
    fnObj.builtin_template = templateName;
  } else {
    const allFiles = extraFiles || (aicfEditor ? aicfEditor.getAllFiles() : {});
    const mainCode = (allFiles && allFiles["user_app.py"] !== undefined && aicfEditor && aicfEditor.activeFile !== "user_app.py")
      ? allFiles["user_app.py"]
      : sourceCode;
    const detectedEp = detectPythonEntrypoint(mainCode) || fnName;
    fnObj.source_code = mainCode;
    fnObj.entrypoint = detectedEp;
    const secondary = {};
    for (const [k, v] of Object.entries(allFiles || {})) {
      if (k !== "user_app.py") secondary[k] = v;
    }
    if (Object.keys(secondary).length > 0) {
      fnObj.files = secondary;
    }
  }

  msgEl.textContent = `Đang biên dịch & triển khai ${appName}@${label}…`;
  const payload = {
    app_name: appName,
    functions: [fnObj],
    web_endpoints: {
      [fnName]: { requires_auth: true }
    }
  };

  let r = await slTenantFetch("/api/serverless/v1/deploy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (r && r.status === 401) {
    msgEl.textContent = "⚠️ Phiên làm việc đã hết hạn (401). Vui lòng đăng nhập lại ở khung phía trên để tiếp tục.";
    handleUnauthorizedSession("⚠️ Phiên làm việc đã hết hạn (401). Vui lòng đăng nhập lại bằng Tài khoản / Mật khẩu hoặc bấm '⚡ Dùng thử 1-Click (Demo)' để tiếp tục triển khai code.");
    return;
  }
  if (r && r.status === 403) {
    const errTxt = await r.text();
    const m = errTxt.match(/tenant\s+([A-Za-z0-9_.-]+)/);
    const detectedTenant = m ? m[1] : (tenantInput.value.trim() || "demo");
    const polRes = await api(`/api/serverless/v1/policies/${encodeURIComponent(detectedTenant)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ serverless_enabled: true, gpu_allowed: true, max_vram_mb: 8192, max_concurrency: 8 }),
    });
    if (polRes && polRes.ok) {
      r = await slTenantFetch("/api/serverless/v1/deploy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      msgEl.textContent = `Lỗi triển khai (403): Tenant '${detectedTenant}' chưa được bật quyền Serverless.`;
      return;
    }
  }
  if (r && r.ok) {
    const d = await r.json();
    if (d.tenant) {
      tenantInput.value = d.tenant;
      try { sessionStorage.setItem(SL_TENANT_STORE, d.tenant); } catch {}
    }
    document.getElementById("sl-web-app").value = d.app_name;
    document.getElementById("sl-web-fn").value = fnName;
    msgEl.textContent = `✓ Đã biên dịch & triển khai thành công '${d.app_name}' phiên bản ${d.active_version} (${label}) cho Workspace '${d.tenant}'!`;
    loadOverview();
  } else {
    const txt = r ? await r.text() : "error";
    let errMsg = txt;
    try {
      const ed = JSON.parse(txt);
      errMsg = ed.error?.message || txt;
    } catch {}
    msgEl.textContent = `❌ Lỗi biên dịch/triển khai (${r ? r.status : "err"}): ${errMsg}`;
  }
}

document.getElementById("sl-btn-deploy-code").onclick = () => {
  executeDeploy({ sourceCode: codeEditor.value, label: "custom-code" });
};

document.getElementById("sl-btn-deploy-v1").onclick = () => {
  codeEditor.value = CODE_TEMPLATES.demo_v1.code;
  document.getElementById("sl-template-select").value = "demo_v1";
  executeDeploy({ templateName: "demo_v1", label: "v1" });
};

document.getElementById("sl-btn-deploy-v2").onclick = () => {
  codeEditor.value = CODE_TEMPLATES.demo_v2.code;
  document.getElementById("sl-template-select").value = "demo_v2";
  executeDeploy({ templateName: "demo_v2", label: "v2" });
};

document.getElementById("sl-btn-fill-sample-web").onclick = () => {
  document.getElementById("sl-web-app").value = document.getElementById("sl-ide-app").value.trim() || "text-analyzer";
  document.getElementById("sl-web-fn").value = document.getElementById("sl-ide-fn").value.trim() || "classify";
  document.getElementById("sl-web-payload").value = JSON.stringify({
    text: "AI Compute Fabric Serverless GPU thực thi hàm Python tốc độ cao với bảo vệ G-RECLAIM dưới 2 giây",
    alpha: 2.5,
    alloc_mib: 64,
    size: 262144,
    A: [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]],
    B: [[1.0, 0.0, 2.0], [0.0, 1.0, 1.0], [2.0, 1.0, 0.0]]
  }, null, 2);
};

function renderGpuTelemetryBar(doc) {
  const bar = document.getElementById("sl-gpu-telemetry-bar");
  if (!bar) return;
  bar.replaceChildren();
  if (!doc || typeof doc !== "object") {
    bar.style.display = "none";
    return;
  }
  const pills = [];
  if (doc.worker_id) {
    pills.push(el("span", { class: "pill mono" }, `🖥️ Worker: ${doc.worker_id}`));
  }
  if (typeof doc.cold_start === "boolean") {
    pills.push(el("span", { class: doc.cold_start ? "pill warn" : "pill ok" }, doc.cold_start ? "❄️ Cold Start" : "🔥 Warm Container"));
  }
  if (doc.exec_ms !== undefined && doc.exec_ms !== null) {
    pills.push(el("span", { class: "pill mono" }, `⏱️ Kernel/Exec: ${Number(doc.exec_ms).toFixed(2)} ms`));
  }
  const gpuInfo = doc.result?.gpu;
  if (gpuInfo && gpuInfo.device_name) {
    pills.push(el("span", { class: "pill ok mono" }, `⚡ ${gpuInfo.device_name} (${gpuInfo.vram_free_mb || 0}/${gpuInfo.vram_total_mb || 0} MiB free)`));
  }
  if (pills.length) {
    bar.append(...pills);
    bar.style.display = "flex";
  } else {
    bar.style.display = "none";
  }
}

document.getElementById("sl-btn-call-web").onclick = async () => {
  const outBox = document.getElementById("sl-web-output");
  const latEl = document.getElementById("sl-web-latency");
  const appName = document.getElementById("sl-web-app").value.trim() || "text-analyzer";
  const fnName = document.getElementById("sl-web-fn").value.trim() || "classify";
  let tid = tenantInput.value.trim() || "demo";
  const apps = currentOverview.apps || [];
  if (!apps.some(a => a.app_name === appName && a.tenant === tid)) {
    const matched = apps.find(a => a.app_name === appName);
    if (matched && matched.tenant) {
      tid = matched.tenant;
      tenantInput.value = tid;
    }
  }
  const rawPayload = document.getElementById("sl-web-payload").value;
  let parsed;
  try {
    parsed = JSON.parse(rawPayload);
  } catch (e) {
    outBox.textContent = "Lỗi JSON payload không hợp lệ: " + e.message;
    return;
  }
  const urlPath = `/api/serverless/v1/web/${encodeURIComponent(tid)}/${encodeURIComponent(appName)}/${encodeURIComponent(fnName)}`;
  outBox.textContent = `Đang gọi đồng bộ POST ${urlPath} trên GPU…`;
  const t0 = performance.now();
  const r = await slTenantFetch(urlPath, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(parsed),
  });
  const elapsed = (performance.now() - t0).toFixed(1);
  latEl.textContent = `HTTP ${r ? r.status : "err"} · ${elapsed} ms`;
  const bodyTxt = r ? await r.text() : "";
  let pretty = bodyTxt;
  let stdoutTxt = "";
  try {
    const parsedOut = JSON.parse(bodyTxt);
    pretty = JSON.stringify(parsedOut, null, 2);
    if (parsedOut.stdout) stdoutTxt = `\n// Log stdout từ GPU Container:\n${parsedOut.stdout.trim()}\n`;
    renderGpuTelemetryBar(parsedOut);
  } catch {}
  const curlCmd = `curl -s -X POST "${location.origin}${urlPath}" \\\n  -H "Authorization: Bearer ${keyInput.value.trim() || "<AICF_API_KEY>"}" \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify(parsed)}'`;
  outBox.textContent = `// Phản hồi Đồng bộ (${elapsed} ms):\n${pretty}\n${stdoutTxt}\n// Lệnh cURL tương đương:\n${curlCmd}`;
  loadOverview();
};

// Async Task Queue Spawn (/api/serverless/v1/spawn) + Polling (/api/serverless/v1/calls/{id})
document.getElementById("sl-btn-spawn-web").onclick = async () => {
  const outBox = document.getElementById("sl-web-output");
  const latEl = document.getElementById("sl-web-latency");
  const appName = document.getElementById("sl-web-app").value.trim() || "text-analyzer";
  const fnName = document.getElementById("sl-web-fn").value.trim() || "classify";
  const rawPayload = document.getElementById("sl-web-payload").value;
  let parsed;
  try {
    parsed = JSON.parse(rawPayload);
  } catch (e) {
    outBox.textContent = "Lỗi JSON payload không hợp lệ: " + e.message;
    return;
  }
  outBox.textContent = `Đang đẩy Job bất đồng bộ vào Hàng đợi GPU (POST /api/serverless/v1/spawn → ${appName}/${fnName})…`;
  const t0 = performance.now();
  const sr = await slTenantFetch("/api/serverless/v1/spawn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app_name: appName, fn_id: fnName, payload: parsed }),
  });
  if (!sr || (!sr.ok && sr.status !== 202)) {
    const errTxt = sr ? await sr.text() : "error";
    outBox.textContent = `❌ Lỗi đẩy Task Queue (${sr ? sr.status : "err"}): ${errTxt}`;
    return;
  }
  const sd = await sr.json();
  const callId = sd.call_id;
  latEl.textContent = `QUEUED · ${callId.slice(0, 10)}…`;
  outBox.textContent = `✓ Đã xếp hàng Job '${callId}' (status=${sd.status}). Đang chờ GPU thực thi…`;

  let finalDoc = sd;
  for (let i = 0; i < 15; i++) {
    const cr = await slTenantFetch(`/api/serverless/v1/calls/${encodeURIComponent(callId)}?wait_s=2`);
    if (cr && cr.ok) {
      finalDoc = await cr.json();
      if (finalDoc.status !== "pending" && finalDoc.status !== "running") break;
    }
  }
  const elapsed = (performance.now() - t0).toFixed(1);
  latEl.textContent = `ASYNC ${finalDoc.status?.toUpperCase() || "DONE"} · ${elapsed} ms`;
  renderGpuTelemetryBar(finalDoc);
  const stdoutTxt = finalDoc.stdout ? `\n// Log stdout từ GPU Container:\n${finalDoc.stdout.trim()}\n` : "";
  const curlCmd = `curl -s -X POST "${location.origin}/api/serverless/v1/spawn" \\\n  -H "Authorization: Bearer ${keyInput.value.trim() || "<AICF_API_KEY>"}" \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify({ app_name: appName, fn_id: fnName, payload: parsed })}'`;
  outBox.textContent = `// Kết quả Task Queue Bất đồng bộ (call_id=${callId}, tổng ${elapsed} ms):\n${JSON.stringify(finalDoc, null, 2)}\n${stdoutTxt}\n// Lệnh cURL đẩy Task Queue:\n${curlCmd}`;
  loadOverview();
};

// Volume management + File Dropzone
const dz = createFileDropzone({
  id: "sl-vol-file-input",
  label: "Kéo thả tệp vào đây để nạp lên Volume",
  hint: "Mọi tệp được kiểm tra SHA-256 và cô lập theo thư mục riêng của Workspace",
  compact: true,
  onchange: async (file) => {
    if (!file) return;
    document.getElementById("sl-vol-path").value = "uploads/" + file.name.replace(/[^A-Za-z0-9_.-]/g, "_");
    try {
      const text = await file.text();
      document.getElementById("sl-vol-content").value = text.slice(0, 65536);
    } catch {}
  }
});
document.getElementById("sl-vol-dropzone-mount").append(dz);

async function listVolumeFiles() {
  const vol = document.getElementById("sl-vol-name").value.trim() || "shared-artifacts";
  const r = await slTenantFetch(`/api/serverless/v1/volumes/${encodeURIComponent(vol)}`);
  const tb = document.getElementById("sl-vol-rows");
  tb.replaceChildren();
  if (!r || !r.ok) {
    tb.append(el("tr", {}, el("td", { colspan: 4, class: "mut" }, `Hãy đăng nhập Khóa API Tenant (aicf_...) để quản lý Volume '${vol}'.`)));
    return;
  }
  const d = await r.json();
  const files = d.files || [];
  document.getElementById("sl-vol-count-badge").textContent = `${files.length} tệp`;
  if (!files.length) {
    tb.append(el("tr", {}, el("td", { colspan: 4, class: "mut" }, `Volume '${vol}' đang trống.`)));
    return;
  }
  for (const f of files) {
    const viewBtn = el("button", {
      type: "button",
      class: "sm",
      onclick: async () => {
        const res = await slTenantFetch(`/api/serverless/v1/volumes/${encodeURIComponent(vol)}/${f.path}?format=json`);
        if (res && res.ok) {
          const doc = await res.json();
          let decoded = "";
          try { decoded = atob(doc.data_b64 || ""); } catch {}
          document.getElementById("sl-vol-preview").textContent =
            `// Đường dẫn: ${doc.volume}/${doc.path} (${doc.size} B, sha256=${doc.sha256})\n` + decoded;
        }
      }
    }, "Xem");
    const delBtn = el("button", {
      type: "button",
      class: "sm",
      style: "margin-left:6px",
      onclick: async () => {
        await slTenantFetch(`/api/serverless/v1/volumes/${encodeURIComponent(vol)}/${f.path}`, { method: "DELETE" });
        listVolumeFiles();
      }
    }, "Xóa");
    const actCell = document.createElement("td");
    actCell.append(viewBtn, delBtn);
    tb.append(
      el("tr", {},
        el("td", { class: "mono" }, f.path),
        el("td", { class: "num mono" }, `${f.size} B`),
        el("td", { class: "mono mut", style: "font-size:11.5px" }, (f.sha256 || "").slice(0, 16) + "…"),
        actCell
      )
    );
  }
}

async function saveVolumeFile() {
  const vol = document.getElementById("sl-vol-name").value.trim() || "shared-artifacts";
  const rel = document.getElementById("sl-vol-path").value.trim() || "configs/prompt_template.json";
  const content = document.getElementById("sl-vol-content").value;
  const msgEl = document.getElementById("sl-vol-msg");
  const r = await slTenantFetch(`/api/serverless/v1/volumes/${encodeURIComponent(vol)}/${rel}`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: new TextEncoder().encode(content),
  });
  if (r && r.ok) {
    const d = await r.json();
    msgEl.textContent = `✓ Đã lưu '${vol}/${d.path}' (${d.size} B, sha256=${(d.sha256 || "").slice(0, 12)}…)`;
    listVolumeFiles();
  } else {
    msgEl.textContent = `Lỗi lưu Volume (HTTP ${r ? r.status : "err"}): hãy kiểm tra Khóa API Tenant.`;
  }
}

document.getElementById("sl-btn-save-vol").onclick = saveVolumeFile;
document.getElementById("sl-btn-sample-vol").onclick = async () => {
  document.getElementById("sl-vol-name").value = "shared-artifacts";
  document.getElementById("sl-vol-path").value = "configs/prompt_template.json";
  await saveVolumeFile();
};
document.getElementById("sl-btn-list-vol").onclick = listVolumeFiles;

// Admin cluster controls
document.getElementById("sl-console-btn-drain").onclick = async () => {
  await api("/api/serverless/v1/admin/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ draining: !currentOverview.draining }),
  });
  loadOverview();
};
document.getElementById("sl-console-btn-enable").onclick = async () => {
  await api("/api/serverless/v1/admin/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !currentOverview.enabled }),
  });
  loadOverview();
};
document.getElementById("sl-btn-refresh").onclick = loadOverview;

const ollamaCodexBtn = document.getElementById("sl-btn-ollama-codex");
if (ollamaCodexBtn) {
  ollamaCodexBtn.onclick = () => {
    if (typeof openOllamaEmulatorModal === "function") openOllamaEmulatorModal();
  };
}

// Render Cloud Developer Docs Snippets & Downloads
document.getElementById("sl-btn-download-env").onclick = () => {
  const gw = (location.hostname === "127.0.0.1" || location.hostname === "localhost")
    ? "http://10.42.0.1:18080"
    : location.origin;
  const key = keyInput.value.trim() || "<AICF_API_KEY>";
  const tid = tenantInput.value.trim() || "demo";
  const envContent = `AICF_GATEWAY_URL=${gw}\nAICF_TENANT_ID=${tid}\nAICF_API_KEY=${key}\nOPENAI_BASE_URL=${gw}/v1\nOPENAI_API_KEY=${key}\nOLLAMA_HOST=http://127.0.0.1:11434\n`;
  const blob = new Blob([envContent], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "aicf.env";
  a.click();
  URL.revokeObjectURL(a.href);
  if (typeof openOllamaEmulatorModal === "function") openOllamaEmulatorModal();
};
syncRoleUi();
setUiMode(uiMode);
if (window.AICF_READY && typeof window.AICF_READY.then === "function") {
  window.AICF_READY.then(() => {
    syncRoleUi();
    setUiMode(uiMode);
  });
}
setInterval(loadOverview, 5000);
