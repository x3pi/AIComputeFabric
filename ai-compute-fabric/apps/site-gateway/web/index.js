const MIB = 1048576, PAGE = 50;
const NAMES = ["offline", "registered", "stopped", "paused", "yielded", "reclaiming", "running"];
const COL = ["--off", "--stp", "--stp", "--stp", "--yld", "--rec", "--run"];
let offset = 0, total = 0, lastWorkers = [];

function td(t, c) {
  const d = document.createElement("td");
  if (typeof t === "string" || typeof t === "number") d.textContent = t;
  else if (t) d.append(t);
  if (c) d.className = c;
  return d;
}

// Heatmap: ONE compact frame per second shared by all viewers; rows are [id,stateCode,vramUsedPermille,tempC].
function renderGrid(s) {
  const run = s.counts.running || 0,
        yld = s.counts.yielded || 0,
        rec = s.counts.reclaiming || 0,
        on = s.total - (s.counts.offline || 0);
  // E2E invariant: #sum must match /1\/1 worker/ when 1 worker is online
  document.getElementById("sum").textContent =
    `${on}/${s.total} worker trực tuyến · ${run} đang chạy AI` +
    (yld ? ` · ${yld} nhường người dùng` : "") +
    (rec ? ` · ${rec} đang thu hồi` : "");

  document.getElementById("kpi-nodes").textContent = `${on} / ${s.total}`;
  document.getElementById("kpi-nodes-sub").textContent =
    `${run} running · ${yld} yielded · ${(s.counts.paused || 0) + (s.counts.stopped || 0)} paused`;

  const g = document.getElementById("grid"), f = document.createDocumentFragment();
  for (const [id, st, used, t] of s.cells) {
    const pct = Math.max(0, Math.min(100, Math.round(used / 10)));
    const c = document.createElement("div");
    c.className = "cell";
    c.role = "listitem";
    c.style.background = `var(${COL[st]})`;
    c.title = `${id}: ${NAMES[st]} · VRAM dùng ${used / 10}% · ${t} °C (Bấm để xem 6-Bucket)`;
    c.onclick = () => inspectWorker(id);

    const top = el("div", { class: "cell-top" },
      el("span", {}, `${pct}%`),
      el("span", {}, t ? `${t}°` : "")
    );
    const label = el("div", { class: "cell-id" }, id.slice(-6));
    const meter = el("div", { class: "cell-meter" }, el("span", { style: `width:${pct}%` }));
    c.append(top, label, meter);
    f.append(c);
  }
  g.replaceChildren(f);
}

async function loadStats() {
  const r = await api("/api/stats");
  if (!r || !r.ok) return;
  const st = await r.json();
  const vp = st.vram_pool || {};
  document.getElementById("kpi-vram").textContent = fmtMiB(vp.allocatable_bytes || vp.free_bytes || 0);
  document.getElementById("kpi-vram-sub").textContent =
    `Tổng cụm: ${fmtMiB(vp.total_bytes || 0)} · Đã dùng: ${fmtMiB(vp.used_bytes || 0)}`;

  const rt = st.router || {};
  document.getElementById("kpi-router").textContent = `${rt.inflight || 0} đang chạy`;
  document.getElementById("kpi-router-sub").textContent =
    `${rt.queued || 0} đợi · ${rt.served_total || 0} hoàn tất · chờ TB ${rt.avg_queue_wait_ms || 0} ms`;

  document.getElementById("kpi-sla").textContent = st.temp_max_c ? `${st.temp_avg_c} °C (max ${st.temp_max_c} °C)` : "-- °C";
  document.getElementById("kpi-sla-sub").textContent =
    st.reclaim_p95_ms ? `Thu hồi p95: ${st.reclaim_p95_ms} ms (max ${st.reclaim_max_ms} ms)` : "Thu hồi p95: < 300 ms (Sẵn sàng)";

  if (vp.total_bytes > 0) {
    renderSixBuckets(document.getElementById("cluster-vram-bar"), document.getElementById("cluster-vram-leg"), vp);
  }
}

function buildMiniBucketBar(w) {
  const wrap = el("div", {});
  const bar = el("div", { class: "vram-bar sm" });
  if (w.buckets && w.buckets.Total > 0) {
    renderSixBuckets(bar, null, w.buckets);
    const usedMiB = Math.round((w.buckets.Total - w.buckets.Allocatable) / MIB);
    const totMiB = Math.round(w.buckets.Total / MIB);
    wrap.append(
      el("div", { class: "num", style: "font-size:11.5px;color:var(--mut)" }, `${usedMiB} / ${totMiB} MiB`),
      bar
    );
  } else if (w.vram_total_bytes > 0) {
    const used = Math.max(0, w.vram_total_bytes - w.vram_free_bytes);
    const pct = Math.min(100, (100 * used) / w.vram_total_bytes);
    bar.append(el("div", { style: `width:${pct.toFixed(1)}%;background:var(--b-weights)` }));
    wrap.append(
      el("div", { class: "num", style: "font-size:11.5px;color:var(--mut)" },
        `${Math.round(used / MIB)} / ${Math.round(w.vram_total_bytes / MIB)} MiB`),
      bar
    );
  } else {
    wrap.textContent = "-";
  }
  return wrap;
}

// a model/adapter change was requested and the engine has not started with it yet
const switchingNow = w => !!w.switching && w.state !== "running";
const modelLabel = (w, none = "-") => (w.model || none) + (w.lora ? " + " + w.lora : "");

async function loadPage() {
  const st = document.getElementById("flt").value;
  const q = (document.getElementById("q-worker").value || "").trim().toLowerCase();
  const r = await api(`/api/workers?offset=${offset}&limit=${PAGE}&state=${encodeURIComponent(st)}`);
  if (!r || !r.ok) return;
  total = +r.headers.get("X-Total-Count") || 0;
  const ws = await r.json();
  lastWorkers = ws;
  svRender();
  document.getElementById("pg").textContent = `${total ? offset + 1 : 0}–${offset + ws.length} / ${total}`;
  const b = document.getElementById("rows");
  b.replaceChildren();

  const filtered = q
    ? ws.filter(w =>
        (w.worker_id || "").toLowerCase().includes(q) ||
        (w.hostname || "").toLowerCase().includes(q) ||
        (w.model || "").toLowerCase().includes(q) ||
        (w.lora || "").toLowerCase().includes(q) ||
        (w.gpu_name || "").toLowerCase().includes(q))
    : ws;

  for (const w of filtered) {
    const tr = document.createElement("tr");
    // IMPORTANT E2E invariant: The first <button> inside #rows MUST be the pause/resume button
    const btn = document.createElement("button");
    btn.className = "sm";
    const paused = w.pause_requested;
    btn.textContent = paused ? "Cho chạy lại" : "Tạm dừng AI";
    const may = window.AICF.can.pause && (!window.AICF.site || window.AICF.site === w.site_id);
    if (!may) { btn.disabled = true; btn.title = "Vai trò hiện tại không được tạm dừng/cho chạy lại worker này"; }
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      await api(`/api/workers/${encodeURIComponent(w.worker_id)}/${paused ? "resume" : "pause"}`, { method: "POST" });
      loadPage();
      loadStats();
    };
    const act = document.createElement("td");
    act.append(btn);

    const idCell = document.createElement("td");
    const idLink = el("a", {
      class: "worker-id-link mono",
      href: "#inspect-" + w.worker_id,
      onclick: (ev) => { ev.preventDefault(); inspectWorker(w.worker_id); }
    }, w.worker_id);
    idCell.append(idLink);

    const stateCell = document.createElement("td");
    stateCell.append(el("span", { class: `state-badge state-${w.state}` }, switchingNow(w) ? "đang đổi model" : w.state));

    const gpuHost = w.gpu_name
      ? `${w.gpu_name}${w.hostname ? " · " + w.hostname : ""}`
      : (w.hostname || "-");

    tr.append(
      idCell,
      stateCell,
      td(gpuHost),
      td(modelLabel(w), "mono"),
      td(buildMiniBucketBar(w)),
      td(w.online ? Math.round(w.vram_free_bytes / MIB) + " MiB" : "-", "num"),
      td(w.online ? w.gpu_temp_c + " °C" : "-", "num"),
      td(w.last_reclaim_ms ? Math.round(w.last_reclaim_ms) + " ms" : "-", "num"),
      act
    );
    b.append(tr);
  }
}

async function inspectWorker(id) {
  const r = await api(`/api/workers/${encodeURIComponent(id)}`);
  if (!r || !r.ok) return;
  const w = await r.json();
  document.getElementById("dw-title").textContent = `Worker ${w.worker_id}`;
  document.getElementById("dw-sub").textContent =
    `${w.gpu_name || "GPU"} · Máy trạm: ${w.hostname || "-"} · Trạng thái: ${w.state}`;

  const body = document.getElementById("dw-body");
  body.replaceChildren();

  const bar = el("div", { class: "vram-bar" });
  const leg = el("div", { class: "vram-legend" });
  const buckets = w.buckets && w.buckets.Total > 0 ? w.buckets : {
    Total: w.vram_total_bytes || 1,
    OSUser: 0,
    Weights: Math.max(0, (w.vram_total_bytes || 0) - (w.vram_free_bytes || 0)),
    KVState: 0,
    Compute: 0,
    Headroom: 0,
    Allocatable: w.vram_free_bytes || 0,
  };
  renderSixBuckets(bar, leg, buckets);

  const meta = el("div", { class: "card", style: "margin:0;padding:12px 14px;background:var(--bg-elevated)" },
    el("div", { class: "row", style: "justify-content:space-between;margin-bottom:6px" },
      el("span", { class: "mut" }, "Model (+ adapter):"),
      el("strong", { class: "mono" }, modelLabel(w, "Chưa nạp"))
    ),
    el("div", { class: "row", style: "justify-content:space-between;margin-bottom:6px" },
      el("span", { class: "mut" }, "Engine Endpoint:"),
      el("span", { class: "mono" }, w.engine_url || "-")
    ),
    el("div", { class: "row", style: "justify-content:space-between;margin-bottom:6px" },
      el("span", { class: "mut" }, "Số Slot song song:"),
      el("strong", { class: "num" }, String(w.parallel_slots || 1))
    ),
    el("div", { class: "row", style: "justify-content:space-between;margin-bottom:6px" },
      el("span", { class: "mut" }, "Nhiệt độ GPU:"),
      el("strong", { class: "num" }, w.online ? `${w.gpu_temp_c} °C` : "Offline")
    ),
    el("div", { class: "row", style: "justify-content:space-between" },
      el("span", { class: "mut" }, "Thời gian thu hồi gần nhất:"),
      el("strong", { class: "num" }, w.last_reclaim_ms ? `${Math.round(w.last_reclaim_ms)} ms` : "Chưa phát sinh")
    )
  );

  body.append(
    el("div", { style: "font-weight:600;font-size:13px" }, "Kế toán 6-Bucket VRAM trên nút này:"),
    bar,
    leg,
    meta
  );
  document.getElementById("worker-drawer").hidden = false;
}

document.getElementById("dw-close").onclick = () => {
  document.getElementById("worker-drawer").hidden = true;
};
document.getElementById("worker-drawer").onclick = (e) => {
  if (e.target.id === "worker-drawer") document.getElementById("worker-drawer").hidden = true;
};

// VRAM Fit & Layer Split Estimator
async function initEstimator() {
  const r = await api("/api/models/catalog");
  if (!r || !r.ok) return;
  const models = await r.json();
  const sel = document.getElementById("est-model");
  sel.replaceChildren();
  for (const m of models) {
    const opt = el("option", { value: m.id }, `${m.name} — ${m.architecture}`);
    if (m.id === "Qwen/Qwen3-8B-GGUF") opt.selected = true;
    sel.append(opt);
  }
  runEstimate();
}

async function runEstimate() {
  const faVal = document.getElementById("est-fa").value;
  const flashAttn = faVal !== "0";
  const kvBytes = faVal === "q8" ? 1 : 2;
  const body = {
    model_id: document.getElementById("est-model").value || "Qwen/Qwen3-8B-GGUF",
    ctx_per_slot: +document.getElementById("est-ctx").value || 8192,
    parallel: +document.getElementById("est-np").value || 4,
    flash_attn: flashAttn,
    kv_bytes_per_elem: kvBytes,
    gpu_total_mib: +document.getElementById("est-gpu-mib").value || 12288,
    os_user_mib: 512,
    headroom_mib: +document.getElementById("est-headroom").value || 1536,
    gpu_count: +document.getElementById("est-gpus").value || 1,
  };
  const r = await api("/api/vram/estimate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r || !r.ok) return;
  const d = await r.json();

  const fitBadge = document.getElementById("est-fit-badge");
  const fitText = {
    GREEN: "GREEN · Đủ VRAM an toàn",
    YELLOW: "YELLOW · Sát ngưỡng / Hybrid RAM",
    RED: "RED · Vượt quá VRAM khả dụng",
  }[d.fit_status] || d.fit_status;
  fitBadge.textContent = fitText;
  fitBadge.className = `pill fit-${d.fit_status}`;

  const modeBadge = document.getElementById("est-mode-badge");
  modeBadge.textContent = d.execution_mode;

  const confBadge = document.getElementById("est-conf-badge");
  confBadge.textContent = d.confidence === "CALIBRATED" ? "CALIBRATED (RTX 3060)" : "ESTIMATED";
  confBadge.className = "pill " + (d.confidence === "CALIBRATED" ? "ok" : "warn");

  document.getElementById("est-compat-workers").textContent =
    `Máy trực tuyến đáp ứng cấu hình này: ${d.compatible_workers_online} worker`;

  renderSixBuckets(document.getElementById("est-bar"), document.getElementById("est-leg"), d.buckets_bytes);

  const summary = document.getElementById("est-summary");
  let msg = `Cần mỗi GPU: ${fmtMiB(d.required_per_gpu_bytes)} / Khả dụng: ${fmtMiB(d.allocatable_per_gpu_bytes)} · ` +
            `Nạp GPU: ${d.max_gpu_layers}/${d.model.layers} layers`;
  if (d.offload_ram_bytes > 0) {
    msg += ` · Offload sang Host RAM (LOCAL_HYBRID): ~${fmtMiB(d.offload_ram_bytes)}`;
  }
  summary.textContent = msg;
  document.getElementById("est-note").textContent = d.model.calibration_note || "";

  const sp = document.getElementById("est-splits");
  sp.replaceChildren();
  for (const s of d.layer_splits || []) {
    sp.append(
      el("div", { class: "split-box" },
        el("div", { class: "split-box-hdr" },
          el("span", {}, `GPU #${s.gpu_index}`),
          el("span", { class: "num" }, fmtMiB(s.vram_bytes))
        ),
        el("div", { class: "split-box-layers" }, `Layers ${s.range_label}`),
        el("div", { class: "mut", style: "font-size:11.5px" },
          `${s.layer_count} layers` + (s.gpu_index === 0 ? " + Embeddings/Norm" : ""))
      )
    );
  }
}

for (const id of ["est-model", "est-ctx", "est-np", "est-gpu-mib", "est-headroom", "est-gpus", "est-fa"]) {
  document.getElementById(id).onchange = runEstimate;
}

document.getElementById("prev").onclick = () => { offset = Math.max(0, offset - PAGE); loadPage(); };
document.getElementById("next").onclick = () => { if (offset + PAGE < total) { offset += PAGE; loadPage(); } };
document.getElementById("flt").onchange = () => { offset = 0; loadPage(); };
document.getElementById("q-worker").oninput = () => loadPage();


// ---- Model & adapter of this PC (Studio -> worker-guard), shown live from the same worker rows as the table ----
const SV = "/api/studio/v1";
let svInfo = null, svSeen = "", svAt = 0;
const svEl = id => document.getElementById(id);
const svOpt = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; return o; };
const svSay = (t, bad) => { const m = svEl("sv-msg"); m.textContent = t; m.className = bad ? "pill err" : "mut"; };

async function svCall(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await api(url, opts);
  if (!r) throw new Error("không kết nối được");
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((d.error && d.error.message) || ("lỗi " + r.status));
  return d;
}

async function svLoadOptions() {
  svAt = Date.now();
  try { svInfo = await svCall("GET", SV + "/serving"); } catch (e) { return; }   // Studio or worker-guard not configured: the card stays hidden
  const src = [svOpt("", "(không gắn adapter)")];
  for (const a of svInfo.adapters || []) src.push(svOpt("file:" + a, "Tệp có sẵn: " + a));
  try {
    const d = await svCall("GET", `${SV}/tenants/${encodeURIComponent(svEl("sv-tenant").value.trim() || "demo")}/lora/jobs`);
    for (const j of d.jobs.filter(j => j.status === "succeeded"))
      src.push(svOpt("job:" + j.id, `Job ${j.id.slice(0, 8)} (${(j.params && j.params.base_model) || "?"}, ${j.created_at.slice(0, 16).replace("T", " ")})`));
  } catch (e) { /* tenant without jobs */ }
  // read what the user has chosen only now: a choice made while the requests above were in flight must survive the refresh
  const keepModel = svEl("sv-model").value, keepSrc = svEl("sv-src").value;
  svEl("sv-model").replaceChildren(...svInfo.models.map(m => svOpt(m, m)));
  svEl("sv-src").replaceChildren(...src);
  const has = (sel, v) => [...svEl(sel).options].some(o => o.value === v);
  svEl("sv-model").value = has("sv-model", keepModel) ? keepModel : svInfo.model;
  if (has("sv-src", keepSrc)) svEl("sv-src").value = keepSrc;
}

function svRender() {
  if (!svInfo) return;
  const ws = lastWorkers || [];
  const w = ws.find(x => x.worker_id === svInfo.worker_id) || (ws.length === 1 ? ws[0] : null);
  svEl("sv-card").style.display = "";
  svEl("sv-who").textContent = w ? `Worker ${w.worker_id} (máy này)` : "Worker máy này chưa đăng ký với gateway";
  const badge = svEl("sv-state");
  const now = svEl("sv-now");
  if (!w) { badge.textContent = "không rõ"; badge.className = "state-badge state-offline"; now.textContent = ""; return; }
  const sw = switchingNow(w);
  badge.textContent = sw ? "đang đổi model" : (w.online ? w.state : "offline");
  badge.className = "state-badge state-" + (sw ? "reclaiming" : (w.online ? w.state : "offline"));
  now.replaceChildren(
    el("strong", { class: "mono" }, w.model || "(chưa chọn)"),
    document.createTextNode(w.lora ? "  +  adapter " : "  (không adapter)"),
    ...(w.lora ? [el("strong", { class: "mono" }, w.lora)] : []),
    ...(sw ? [el("div", { class: "mut", style: "font-size:12.5px;margin-top:4px" }, "Đã dừng engine cũ; worker nạp lại khi GPU rảnh ổn định (không giành VRAM của người dùng).")] : [])
  );
  const admin = window.AICF && window.AICF.role === "admin";
  svEl("sv-go").disabled = !admin || sw;
  svEl("sv-go").title = !admin ? "Chỉ admin được đổi model" : (sw ? "Đang đổi, chờ hoàn tất" : "");
  const sig = [w.model, w.lora, w.state, w.switching].join("|");
  if (sig !== svSeen || Date.now() - svAt > 15000) { svSeen = sig; svLoadOptions(); }
}

svEl("sv-tenant").addEventListener("change", svLoadOptions);
svEl("sv-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const v = svEl("sv-src").value, i = v.indexOf(":");
  const body = { model: svEl("sv-model").value };
  if (v.startsWith("file:")) body.adapter_file = v.slice(i + 1);
  if (v.startsWith("job:")) { body.job = v.slice(i + 1); body.tenant = svEl("sv-tenant").value.trim(); }
  svEl("sv-go").disabled = true; svSay("Đang gửi…");
  try {
    await svCall("POST", SV + "/serving", body);
    svSay("Đã nhận. Trạng thái bên trên cập nhật theo thời gian thực.");
  } catch (e) { svSay(e.message, true); }
  loadPage();
});

// ---- Cluster topology: 3 modes, step by step ----
// Sizes are the GGUF file sizes on the demo host; kvKiB = f16 KV cache per token. The VRAM estimate constants below were
// calibrated on the measured collapse points of the RTX 5070 (8k: fast at 60%, collapsed at 62%; 32k: fast at 52%, collapsed at 54%).
const TOPO_MODELS = {
  "Qwen3-4B-Q4_K_M.gguf":      { label: "Qwen3-4B (2,5 GB): nhỏ, vừa 1 card",                          mib: 2381,  kvKiB: 144, fits1: true },
  "Qwen3-8B-Q4_K_M.gguf":      { label: "Qwen3-8B (5,0 GB): nhỏ, vừa 1 card",                          mib: 4794,  kvKiB: 144, fits1: true },
  "Qwen3-30B-A3B-Q4_K_M.gguf": { label: "Qwen3-30B-A3B (18,56 GB): MoE lớn, KHÔNG vừa 1 card 12 GB",   mib: 17697, kvKiB: 96,  fits1: false },
  // same qwen3moe shape as the 30B-A3B (48 layers, 4 KV heads x 128): file 18 556 689 568 B, KV 96 KiB/token. Measured in P3_CODER_30B_GOP_2_CARD.md (8k 58:42 -> 132.4 tok/s; 32k 52:48 -> 126.7 tok/s).
  "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf": { label: "Qwen3-Coder-30B-A3B (18,56 GB): chuyên viết code, MoE lớn, KHÔNG vừa 1 card 12 GB", mib: 17697, kvKiB: 96, fits1: false }
};
const TOPO_DEFAULT_MODEL = { split: "Qwen3-4B-Q4_K_M.gguf", smart_1gpu: "Qwen3-30B-A3B-Q4_K_M.gguf", merged: "Qwen3-30B-A3B-Q4_K_M.gguf" };
const TOPO_OS_BASE_MIB = 1000;      // Windows/driver already hold ~1 GB of the card (measured: RTX 5070 idle 11,0 of 12,2 GB free)
let selectedTopoMode = "split";
let currentTopo = null;
let topoWorkers = {};
let topoSelected = null;             // Set of worker ids ticked by the user (null until first render)
let topoMachineKey = "";
let topoModeSynced = false;

(function initTopoModelSelect() {
  const sel = document.getElementById("topo-model-select");
  sel.replaceChildren(...Object.entries(TOPO_MODELS).map(([k, m]) => el("option", { value: k }, m.label)));
  sel.value = TOPO_DEFAULT_MODEL.split;
  sel.addEventListener("change", topoRefresh);
})();

function selectTopoMode(mode, keepModel) {
  selectedTopoMode = mode;
  ["split", "smart", "merged"].forEach(k => document.getElementById("mode-card-" + k)
    .classList.toggle("active", (k === "smart" ? "smart_1gpu" : k) === mode));
  if (!keepModel) document.getElementById("topo-model-select").value = TOPO_DEFAULT_MODEL[mode];
  document.getElementById("topo-opt-smart").style.display = mode === "smart_1gpu" ? "block" : "none";
  document.getElementById("topo-opt-merged").style.display = mode === "merged" ? "block" : "none";
  document.getElementById("topo-primary-wrap").style.display = mode === "merged" ? "block" : "none";
  topoRefresh();
}

function topoChosen() {   // {primary, members[]}: for split/smart the first ticked machine is only the "first of the list"
  const ids = [...(topoSelected || [])].filter(id => topoWorkers[id] && topoWorkers[id].online && !topoWorkers[id].group_id).sort();
  let primary = ids[0] || "";
  if (selectedTopoMode === "merged") {
    const p = document.getElementById("topo-primary").value;
    if (ids.includes(p)) primary = p;
  }
  return { primary, members: ids.filter(id => id !== primary) };
}
const gpuLabel = id => id + (topoWorkers[id] && topoWorkers[id].gpu_name ? " (" + topoWorkers[id].gpu_name.replace("NVIDIA GeForce ", "") + ")" : "");

function renderTopoMachines(workers) {
  const ids = Object.keys(workers).filter(id => workers[id].online).sort();
  const key = ids.map(id => id + ":" + (workers[id].group_id || "")).join("|");
  if (key === topoMachineKey) return;
  topoMachineKey = key;
  const free = ids.filter(id => !workers[id].group_id);   // grouped machines must be split first
  if (topoSelected === null) topoSelected = new Set(free);
  topoSelected = new Set([...topoSelected].filter(id => free.includes(id)));
  const box = document.getElementById("topo-machines");
  if (!ids.length) { box.textContent = "Chưa có worker online"; return; }
  box.replaceChildren(...ids.map(id => {
    const cb = el("input", { type: "checkbox", value: id });
    cb.checked = topoSelected.has(id);
    if (workers[id].group_id) cb.disabled = true;
    cb.addEventListener("change", () => { cb.checked ? topoSelected.add(id) : topoSelected.delete(id); topoRefresh(); });
    const g = workers[id].group_id;
    return el("label", { style: "display:inline-flex;gap:6px;margin-right:16px;align-items:center" }, cb, gpuLabel(id), g ? el("span", { class: "mut", style: "font-size:11.5px" }, "(đang trong nhóm " + g + " — bấm \"Tách nhóm\" trước mới chọn được)") : "");
  }));
  const prim = document.getElementById("topo-primary");
  const prev = prim.value;
  prim.replaceChildren(...free.map(id => el("option", { value: id }, gpuLabel(id))));
  prim.value = free.includes(prev) ? prev : free[0];
  prim.onchange = topoRefresh;
}

function setNcmoe(n) { document.getElementById("topo-ncmoe").value = n; topoRefresh(); }
function setRatioEven() { document.getElementById("topo-slider").value = 50; topoRefresh(); }
function setRatioRec(pct5070) {  // the measured recommendation names the RTX 5070's share
  const { primary, members } = topoChosen();
  const w = [primary, ...members];
  if (w.length !== 2) return;
  const i = w.findIndex(id => (topoWorkers[id].gpu_name || "").includes("5070"));
  const slider = document.getElementById("topo-slider");
  slider.value = i === 0 ? pct5070 : i === 1 ? 100 - pct5070 : 50;
  topoRefresh();
}

function topoEstimate(id, share) {
  const m = TOPO_MODELS[document.getElementById("topo-model-select").value];
  const ctx = parseInt(document.getElementById("topo-ctx").value, 10) || 8192;
  const kvMiB = ctx * m.kvKiB / 1024;
  const computeMiB = ctx > 8192 ? 264 : 168;
  const used = share * (m.mib + kvMiB) + computeMiB + TOPO_OS_BASE_MIB;
  const total = topoWorkers[id].vram_total_bytes ? topoWorkers[id].vram_total_bytes / 1048576 : 0;
  let level = "ok";
  if (total && used >= total + 250) level = "bad";
  else if (total && used >= total - 500) level = "warn";
  return { used, total, level };
}
const LEVEL_TXT = { ok: "an toàn", warn: "sát giới hạn (nhanh nhưng rủi ro)", bad: "vượt: tốc độ sẽ sụp hoặc hết VRAM" };

function topoRefresh() {
  const modelKey = document.getElementById("topo-model-select").value;
  const m = TOPO_MODELS[modelKey];
  const hint = document.getElementById("topo-model-hint");
  hint.textContent = (m.fits1 ? "Vừa trong một card 12 GB." : "Lớn hơn một card 12 GB: chạy riêng cần đẩy chuyên gia sang RAM (chế độ B), hoặc gộp hai card (chế độ C).")
    + (selectedTopoMode === "split" && !m.fits1 ? " ⚠ Chế độ A với model này sẽ không nạp được." : "")
    + (selectedTopoMode !== "split" && m.fits1 ? " (Chế độ B/C thường dành cho model lớn hơn.)" : "");
  const { primary, members } = topoChosen();
  const all = primary ? [primary, ...members] : [];
  const sumEl = document.getElementById("topo-sumline");

  // merged: ratio widgets
  const two = selectedTopoMode === "merged" && all.length === 2;
  document.getElementById("topo-ratio-two").style.display = two ? "block" : "none";
  document.getElementById("topo-ratio-many").style.display = selectedTopoMode === "merged" && all.length > 2 ? "block" : "none";
  const bars = document.getElementById("topo-bars");
  bars.replaceChildren();
  let ratioText = "";
  if (two) {
    const p = parseInt(document.getElementById("topo-slider").value, 10);
    document.getElementById("topo-nameA").textContent = gpuLabel(all[0]) + " (chủ)";
    document.getElementById("topo-nameB").textContent = gpuLabel(all[1]);
    document.getElementById("topo-pctA").textContent = p + "%";
    document.getElementById("topo-pctB").textContent = (100 - p) + "%";
    [[all[0], p / 100], [all[1], (100 - p) / 100]].forEach(([id, share]) => {
      const e = topoEstimate(id, share);
      const pct = e.total ? Math.min(100, e.used / e.total * 100) : 0;
      bars.append(el("div", {},
        el("div", { style: "font-size:12.5px" }, el("b", {}, gpuLabel(id)), " giữ " + Math.round(share * 100) + "%"),
        el("div", { class: "topo-bar-track" }, el("div", { class: "topo-bar-fill " + e.level, style: "width:" + pct + "%" })),
        el("div", { class: "topo-lvl-" + e.level, style: "font-size:12px" },
          "ước tính ~" + (e.used / 1024).toFixed(1) + " / " + (e.total / 1024).toFixed(1) + " GB: " + LEVEL_TXT[e.level])));
    });
    ratioText = " " + gpuLabel(all[0]) + " giữ " + p + "%, " + gpuLabel(all[1]) + " giữ " + (100 - p) + "%.";
    const has5070 = all.some(id => (topoWorkers[id].gpu_name || "").includes("5070"));
    document.getElementById("topo-rec8").style.display = has5070 ? "" : "none";
    document.getElementById("topo-rec32").style.display = has5070 ? "" : "none";
  }

  let line;
  if (!all.length) line = "Chọn ít nhất một máy ở bước 3 (máy đang trong nhóm gộp phải được tách trước).";
  else if (selectedTopoMode === "split") line = "Sẽ làm: " + all.map(gpuLabel).join(" và ") + " mỗi máy chạy riêng " + modelKey + ", phục vụ song song.";
  else if (selectedTopoMode === "smart_1gpu") line = "Sẽ làm: " + all.map(gpuLabel).join(" và ") + " mỗi máy chạy riêng " + modelKey + ", đẩy chuyên gia của N tầng đầu sang RAM (N = " + (document.getElementById("topo-ncmoe").value || "chưa nhập") + ").";
  else if (all.length < 2) line = "Chế độ gộp cần chọn ít nhất 2 máy ở bước 3.";
  else line = "Sẽ làm: gộp " + all.map(gpuLabel).join(" + ") + " thành một máy chạy " + modelKey + "." + ratioText;
  sumEl.textContent = line;
}
document.getElementById("topo-slider").addEventListener("input", topoRefresh);
document.getElementById("topo-ctx").addEventListener("input", topoRefresh);
document.getElementById("topo-ncmoe").addEventListener("input", topoRefresh);
document.querySelectorAll("[data-topo-mode]").forEach(el => {
  el.addEventListener("click", () => selectTopoMode(el.getAttribute("data-topo-mode")));
});
document.querySelectorAll("[data-ncmoe]").forEach(btn => {
  btn.addEventListener("click", () => setNcmoe(parseInt(btn.getAttribute("data-ncmoe"), 10)));
});
if (document.getElementById("btn-ratio-even")) {
  document.getElementById("btn-ratio-even").addEventListener("click", setRatioEven);
}
document.querySelectorAll("[data-ratio-rec]").forEach(btn => {
  btn.addEventListener("click", () => setRatioRec(parseInt(btn.getAttribute("data-ratio-rec"), 10)));
});

async function loadTopology() {
  try {
    const r = await api("/api/cluster/topology");
    if (!r || !r.ok) return;
    const d = await r.json();
    currentTopo = d;

    const badge = document.getElementById("topo-current-badge");
    const summary = document.getElementById("topo-summary");
    badge.style.background = ""; badge.style.color = "";
    if (d.mode === "split") {
      badge.textContent = "ĐANG CHẠY: A. MỖI CARD CHẠY RIÊNG"; badge.className = "pill ok";
      summary.textContent = "Model " + (d.model || "—");
    } else if (d.mode === "smart_1gpu") {
      badge.textContent = "ĐANG CHẠY: B. MỖI CARD + RAM"; badge.className = "pill warn";
      summary.textContent = "Model " + (d.model || "—") + ", -ncmoe " + (d.n_cpu_moe || "?");
    } else if (d.mode === "merged") {
      badge.textContent = "ĐANG CHẠY: C. GỘP " + (1 + (d.rpc_worker_ids || []).length) + " CARD"; badge.className = "pill";
      badge.style.background = "var(--purple)"; badge.style.color = "#fff";
      summary.textContent = "Máy chính " + d.primary_worker_id + " + " + (d.rpc_worker_ids || []).join(", ") + (d.tensor_split ? ", tỷ lệ " + d.tensor_split : "");
    }
    const groups = d.groups || [];
    if (groups.length > 1) {
      badge.textContent = "ĐANG CHẠY: " + groups.length + " NHÓM GỘP"; badge.className = "pill";
      badge.style.background = "var(--purple)"; badge.style.color = "#fff";
      summary.textContent = groups.map(g => g.id + "+" + g.rpc_worker_ids.join("+")).join(" · ");
    }
    renderTopoGroups(groups, d.busy_worker_ids || []);
    if (d.status === "switching") { badge.textContent += " (đang chuyển…)"; }
    if (d.status === "error" && d.last_error) { summary.textContent = "Lỗi lần chuyển gần nhất: " + d.last_error; }

    const container = document.getElementById("topo-nodes-container");
    container.replaceChildren();
    topoWorkers = d.workers || {};
    if (!topoModeSynced && d.mode) {
      topoModeSynced = true;
      const hasModel = Boolean(d.model && TOPO_MODELS[d.model]);
      if (hasModel) document.getElementById("topo-model-select").value = d.model;
      if (d.n_cpu_moe > 0) document.getElementById("topo-ncmoe").value = d.n_cpu_moe;
      if (d.kv_type) document.getElementById("topo-kv").value = d.kv_type;
      if (d.tensor_split) {
        const parts = d.tensor_split.split(",").map(Number);
        if (parts.length === 2 && parts[0] + parts[1] > 0) {
          document.getElementById("topo-slider").value = Math.round(parts[0] / (parts[0] + parts[1]) * 100);
        } else if (parts.length > 2) {
          document.getElementById("topo-split").value = d.tensor_split;
        }
      }
      selectTopoMode(d.mode, hasModel);
    }
    renderTopoMachines(topoWorkers);
    topoRefresh();
    const wids = Object.keys(topoWorkers).sort();
    if (wids.length === 0) {
      container.innerHTML = '<div class="mut" style="padding:10px">Chưa có worker nào kết nối vào gateway...</div>';
      return;
    }
    for (const id of wids) {
      const w = topoWorkers[id];
      const card = el("div", { class: "topo-node-card" });
      const hdr = el("div", { style: "display:flex;justify-content:space-between;align-items:center" });
      hdr.append(el("strong", { class: "mono", style: "font-size:13px" }, id),
                 el("span", { class: "topo-badge " + getBadgeClass(w.badge) }, w.badge || "UNKNOWN"));
      const info = el("div", { style: "font-size:12px;color:var(--fg-secondary)" });
      info.textContent = `${w.gpu_name || "GPU"} · Model: ${w.model || "none"}`;
      const stat = el("div", { style: "font-size:11.5px;display:flex;justify-content:space-between" });
      const vramFree = w.vram_free_bytes ? Math.round(w.vram_free_bytes / (1024*1024)) : 0;
      const vramTotal = w.vram_total_bytes ? Math.round(w.vram_total_bytes / (1024*1024)) : 0;
      stat.append(el("span", { class: "mut" }, `Trạng thái: ${w.online ? w.state : "offline"}`),
                  el("span", { class: "num" }, `${vramFree} / ${vramTotal} MiB trống`));
      card.append(hdr, info, stat);
      container.append(card);
    }
  } catch (e) {
    console.error("loadTopology error:", e);
  }
}

function renderTopoGroups(groups, busy) {
  document.getElementById("topo-groups").style.display = groups.length ? "block" : "none";
  document.getElementById("topo-groups-list").replaceChildren(...groups.map(g => {
    const members = [g.primary_worker_id, ...g.rpc_worker_ids];
    const isBusy = members.some(id => busy.includes(id));
    const btn = el("button", { type: "button", class: "sm", disabled: isBusy ? "disabled" : null }, "Tách nhóm");
    btn.addEventListener("click", () => splitTopoGroup(g, members));
    return el("div", { class: "topo-opt-box", style: "margin:0;padding:8px 12px;display:flex;gap:10px;align-items:center" },
      el("span", {}, el("b", { class: "mono" }, members.map(gpuLabel).join(" + ")), " · " + g.model +
        (g.tensor_split ? " · tỷ lệ " + g.tensor_split : "") + " · " + (g.vram_total_bytes / 1073741824).toFixed(0) + " GB"), btn);
  }));
}

async function splitTopoGroup(g, members) {
  const fb = document.getElementById("topo-feedback");
  const model = TOPO_DEFAULT_MODEL.split;
  if (!confirm("Tách nhóm " + members.join(" + ") + ": mỗi máy sẽ chạy riêng " + model + " (khởi động lại engine, các yêu cầu đang chạy bị ngắt). Tiếp tục?")) return;
  fb.textContent = "Đang tách nhóm…"; fb.className = "pill warn";
  try {
    const r = await api("/api/cluster/topology", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "split", model, primary_worker_id: members[0], rpc_worker_ids: members.slice(1) }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("Lỗi HTTP " + r.status));
    fb.textContent = "Đã tách nhóm."; fb.className = "pill ok";
    loadTopology();
  } catch (e) { fb.textContent = "Lỗi tách nhóm: " + e.message; fb.className = "pill err"; }
}

function getBadgeClass(badge) {
  if (!badge) return "badge-split";
  if (badge.includes("MASTER")) return "badge-master";
  if (badge.includes("MEMBER")) return "badge-member";
  if (badge.includes("SMART")) return "badge-smart";
  return "badge-split";
}

document.getElementById("topo-apply-btn").addEventListener("click", async () => {
  const applyBtn = document.getElementById("topo-apply-btn");
  const fb = document.getElementById("topo-feedback");
  const fail = msg => { fb.textContent = msg; fb.className = "pill err"; };
  const { primary, members } = topoChosen();
  const all = primary ? [primary, ...members] : [];
  const modelKey = document.getElementById("topo-model-select").value;
  if (!all.length) return fail("Chọn ít nhất một máy ở bước 3.");
  const payload = { mode: selectedTopoMode, model: modelKey, primary_worker_id: primary, rpc_worker_ids: members };

  if (selectedTopoMode === "smart_1gpu") {
    const n = parseInt(document.getElementById("topo-ncmoe").value, 10);
    if (!(n > 0)) return fail("Nhập N (-ncmoe) > 0: không có giá trị mặc định, hãy dùng số từ bài đo (các nút gợi ý ở bước 3).");
    payload.n_cpu_moe = n;
  } else if (selectedTopoMode === "merged") {
    if (all.length < 2) return fail("Chế độ gộp cần chọn ít nhất 2 máy.");
    const adv = document.getElementById("topo-ts-adv").value.trim();
    if (all.length === 2) {
      const p = parseInt(document.getElementById("topo-slider").value, 10);
      payload.tensor_split = adv || (p + "," + (100 - p));
      const worst = all.map((id, i) => topoEstimate(id, (i === 0 ? p : 100 - p) / 100)).some(e => e.level === "bad");
      if (!adv && worst && !confirm("Ước tính cho thấy tỷ lệ này VƯỢT VRAM của một card (tốc độ có thể sụp hoặc không nạp được). Vẫn áp dụng?")) return;
    } else {
      const ts = adv || document.getElementById("topo-ts-many").value.trim();
      if (ts) payload.tensor_split = ts;
    }
    const kv = document.getElementById("topo-kv").value;
    if (kv) payload.kv_type = kv;
    payload.rpc_port = 50052;
  }
  if (!confirm("Đổi chế độ sẽ khởi động lại engine trên các máy (khoảng 15–90 giây), các yêu cầu đang chạy sẽ bị ngắt. Tiếp tục?")) return;

  applyBtn.disabled = true;
  fb.textContent = "Đang chuyển… chờ các máy báo trạng thái (nạp model lớn có thể mất vài phút)";
  fb.className = "pill warn";
  try {
    const r = await api("/api/cluster/topology", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || ("Lỗi HTTP " + r.status));
    }
    fb.textContent = "Đã chuyển xong.";
    fb.className = "pill ok";
    loadTopology();
    loadPage();
  } catch (e) {
    fail("Lỗi chuyển đổi: " + e.message);
  } finally {
    applyBtn.disabled = false;
  }
});

async function loadHomeBusOverview() {
  try {
    const r = await api("/api/agents/bus/overview");
    if (!r || !r.ok) return;
    const ov = await r.json();
    const pill = document.getElementById("home-bus-pill");
    const sum = document.getElementById("home-bus-summary");
    const sup = document.getElementById("home-bus-sup");
    if (!ov.connected) {
      pill.className = "pill warn";
      sum.textContent = "Chưa bật --nats-url trên dag_api";
      return;
    }
    const t = ov.totals || {};
    pill.className = "pill ok";
    sum.textContent = `${t.live_agents || 0} Agent sống trong KV (${t.compute_agents || 0} compute, ${t.executor_agents || 0} executor · ${t.total_slots || 0} slots) · Hàng đợi: ${t.queue_depth || 0} chờ / ${t.inflight_tasks || 0} đang xử lý · Đã xong: ${ov.completed_total || 0}`;
    const active = (ov.recommendations || []).filter(x => x.action !== "noop");
    sup.textContent = active.length
      ? `⚠️ Supervisor có ${active.length} đề xuất co giãn (chờ duyệt)`
      : `Supervisor: Ổn định (propose_only=True · ${t.redelivered_total || 0} giao lại)`;
  } catch {}
}

document.getElementById("home-btn-spawn").addEventListener("click", async () => {
  await api("/api/agents/bus/demo?action=spawn&preset=sandbox&count=2");
  loadHomeBusOverview();
});
document.getElementById("home-btn-burst").addEventListener("click", async () => {
  await api("/api/agents/bus/demo?action=burst&task_type=TOOL_EXECUTE&count=12&delay_s=1.6");
  loadHomeBusOverview();
});

// ---- Serverless GPU & Warm Containers (Phase 6) ----
let slState = { enabled: true, draining: false };

function syncServerlessRoleUI() {
  const canEvict = Boolean(window.AICF && (window.AICF.can.serverless_evict || window.AICF.role === "admin" || window.AICF.role === "operator"));
  const ctrl = document.getElementById("sl-global-controls");
  if (ctrl) ctrl.style.display = canEvict ? "flex" : "none";
}

async function loadServerless() {
  syncServerlessRoleUI();
  try {
    const r = await api("/api/serverless/v1/status");
    if (!r || !r.ok) return;
    const d = await r.json();
    slState = { enabled: d.enabled !== false, draining: !!d.draining };
    const badge = document.getElementById("sl-status-badge");
    const drainBtn = document.getElementById("sl-btn-toggle-drain");
    const enBtn = document.getElementById("sl-btn-toggle-enabled");
    if (!slState.enabled) {
      badge.textContent = "ĐÃ TẮT TOÀN CỤC";
      badge.className = "pill err";
    } else if (slState.draining) {
      badge.textContent = "ĐANG RÚT LUI (DRAIN)";
      badge.className = "pill warn";
    } else {
      badge.textContent = "ĐANG BẬT · SẴN SÀNG";
      badge.className = "pill ok";
    }
    if (drainBtn) drainBtn.textContent = slState.draining ? "▶ Tắt Drain" : "⏸ Bật Drain";
    if (enBtn) enBtn.textContent = slState.enabled ? "⛔ Tắt Serverless" : "✅ Bật Serverless";

    const containers = d.containers || [];
    const totalVramMb = containers.reduce((acc, c) => acc + (+c.vram_mb || 0), 0);
    const usageObj = d.usage || {};
    const totalCalls = Object.values(usageObj).reduce((acc, u) => acc + (+u.calls || 0), 0);
    document.getElementById("sl-summary").textContent =
      `${containers.length} container ấm · ${totalVramMb} MiB VRAM giữ · ${totalCalls} lượt gọi hoàn tất`;
    const vramSumEl = document.getElementById("sl-vram-summary");
    if (vramSumEl) {
      vramSumEl.textContent =
        `⚡ Serverless GPU Leases: ${totalVramMb} MiB (${containers.length} container ấm) · Phân biệt với LLM Weights/KV & OSUser trên thanh 6-Bucket`;
    }

    const canEvict = Boolean(window.AICF && (window.AICF.can.serverless_evict || window.AICF.role === "admin" || window.AICF.role === "operator"));
    const tb = document.getElementById("sl-rows");
    tb.replaceChildren();
    if (!containers.length) {
      tb.append(el("tr", {}, el("td", { colspan: 7, class: "mut" }, "Chưa có container Serverless ấm nào đang giữ lease.")));
      return;
    }
    for (const c of containers) {
      const actTd = el("td", {});
      if (canEvict) {
        const evBtn = el("button", {
          type: "button",
          class: "sm err sl-evict-btn",
          "data-cid": c.container_id,
          onclick: async () => {
            if (!confirm(`Force Evict container ${c.container_id} (tenant ${c.tenant_id})? Lease VRAM sẽ được giải phóng ngay và ghi Audit.`)) return;
            const res = await api(`/api/serverless/v1/containers/${encodeURIComponent(c.container_id)}`, { method: "DELETE" });
            const fb = document.getElementById("sl-feedback");
            if (res && res.ok) {
              fb.textContent = `Đã Force Evict container ${c.container_id} và giải phóng lease.`;
              fb.className = "pill ok";
            } else {
              fb.textContent = `Lỗi Force Evict: HTTP ${res ? res.status : "err"}`;
              fb.className = "pill err";
            }
            loadServerless();
          }
        }, "💥 Force Evict");
        actTd.append(evBtn);
      } else {
        actTd.append(el("span", { class: "mut", style: "font-size:12px" }, "chỉ xem"));
      }
      tb.append(
        el("tr", { "data-cid": c.container_id },
          el("td", { class: "mono" }, c.container_id),
          el("td", { style: "font-weight:600" }, c.tenant_id || "-"),
          el("td", { class: "mono" }, `${c.app_name || "-"} / ${c.fn_id || "-"}`),
          el("td", { class: "mono" }, c.worker_id || "-"),
          el("td", { class: "num mono" }, `${c.vram_mb || 0} MiB` + (c.lease_id ? ` (${c.lease_id})` : "")),
          el("td", { class: "num mono" }, `${Math.round(+c.ttl_remaining_s || 0)} s`),
          actTd
        )
      );
    }
  } catch {}
}

document.getElementById("sl-btn-toggle-drain").addEventListener("click", async () => {
  const nextDrain = !slState.draining;
  const r = await api("/api/serverless/v1/admin/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ draining: nextDrain }),
  });
  const fb = document.getElementById("sl-feedback");
  if (r && r.ok) {
    fb.textContent = nextDrain ? "Đã bật chế độ Drain Serverless (chặn lượt mới)." : "Đã tắt chế độ Drain Serverless.";
    fb.className = "pill ok";
  }
  loadServerless();
});

document.getElementById("sl-btn-toggle-enabled").addEventListener("click", async () => {
  const nextEnabled = !slState.enabled;
  if (!nextEnabled && !confirm("Tắt Serverless toàn cục? Mọi lượt gọi mới sẽ bị từ chối với HTTP 503.")) return;
  const r = await api("/api/serverless/v1/admin/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: nextEnabled }),
  });
  const fb = document.getElementById("sl-feedback");
  if (r && r.ok) {
    fb.textContent = nextEnabled ? "Đã bật lại Serverless toàn cục." : "Đã tắt Serverless toàn cục.";
    fb.className = nextEnabled ? "pill ok" : "pill warn";
  }
  loadServerless();
});

let lastLoad = 0;
stream("/api/fleet/stream", s => { renderGrid(s); if (Date.now() - lastLoad > 1000) { lastLoad = Date.now(); loadPage(); loadTopology(); } });
window.AICF_READY.then(() => { syncServerlessRoleUI(); svLoadOptions().then(svRender); loadTopology(); loadHomeBusOverview(); loadServerless(); });
loadPage();
loadStats();
loadTopology();
loadHomeBusOverview();
loadServerless();
initEstimator();
setInterval(() => { loadPage(); loadStats(); loadTopology(); loadHomeBusOverview(); loadServerless(); }, 2000);
