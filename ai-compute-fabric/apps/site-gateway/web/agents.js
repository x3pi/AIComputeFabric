const NS = "http://www.w3.org/2000/svg";
const STATUS_VI = { pending: "chờ", running: "đang chạy", retrying: "thử lại", ok: "xong", rejected: "kết quả không đạt kiểm tra", failed: "lỗi (mất worker mọi lần thử)" };
const STATE_VI = { running: "đang chạy", done: "hoàn tất", failed: "có bước lỗi", idle: "chưa chạy" };
let current = new URLSearchParams(location.search).get("wf") || "";
let selected = "";
let zoom = 1;
const msg = t => { document.getElementById("msg").textContent = t; };
const busStatus = t => { document.getElementById("bus-action-status").textContent = t; };

function applyZoom() {
  const g = document.getElementById("dag");
  g.style.transform = zoom === 1 ? "" : `scale(${zoom})`;
  document.getElementById("zoom-reset").textContent = Math.round(zoom * 100) + "%";
}
document.getElementById("zoom-out").onclick = () => { zoom = Math.max(0.6, +(zoom - 0.15).toFixed(2)); applyZoom(); };
document.getElementById("zoom-in").onclick = () => { zoom = Math.min(1.6, +(zoom + 0.15).toFixed(2)); applyZoom(); };
document.getElementById("zoom-reset").onclick = () => { zoom = 1; applyZoom(); };

async function runBusDemo(query, label) {
  busStatus("Đang thực hiện: " + label + "…");
  const r = await api("/api/agents/bus/demo?" + query);
  if (!r || !r.ok) {
    busStatus("Không thực hiện được thao tác (kiểm tra --nats-url).");
    return null;
  }
  const data = await r.json();
  if (data.workflow) {
    current = data.workflow;
    selected = "";
    busStatus("Đã khởi chạy workflow " + data.workflow + " qua NATS Bus!");
  } else if (data.agents) {
    busStatus(`${label}: ${data.agents.join(", ") || "xong"}`);
  } else if (data.sent) {
    busStatus(`Đã bơm ${data.sent} tác vụ ${data.task_type} vào stream AICF_TASKS!`);
  } else {
    busStatus("Hoàn tất: " + label);
  }
  await loadBusOverview();
  await loadList();
  await loadOne();
  return data;
}

document.getElementById("btn-spawn-sandbox").onclick = () => runBusDemo("action=spawn&preset=sandbox&count=2", "Thêm +2 Agent Sandbox");
document.getElementById("btn-spawn-coder").onclick = () => runBusDemo("action=spawn&preset=coder&count=1", "Thêm +1 Agent Code");
document.getElementById("btn-spawn-rag").onclick = () => runBusDemo("action=spawn&preset=rag&count=1", "Thêm +1 Agent RAG");
document.getElementById("btn-drain-one").onclick = () => runBusDemo("action=drain&count=1", "Rút lui êm 1 Agent");
document.getElementById("btn-kill-one").onclick = () => runBusDemo("action=kill&count=1", "Kill đột ngột 1 Agent");
document.getElementById("btn-burst-tasks").onclick = () => runBusDemo("action=burst&task_type=TOOL_EXECUTE&count=16&delay_s=1.8", "Bơm 16 tác vụ TOOL_EXECUTE");
document.getElementById("btn-run-bon-wf").onclick = () => runBusDemo("action=workflow", "Chạy Workflow Best-of-N mẫu");
document.getElementById("btn-reset-fleet").onclick = () => runBusDemo("action=reset", "Khôi phục 4 Agent mặc định");

// Re-render only when the data really changed (heartbeat timestamps ignored; ages refresh every 10 s), otherwise the 2 s poll flickers.
const lastSig = { ov: "", list: "", one: "" };
const sigOf = (v, bucket) => JSON.stringify(v, (k, x) => (k === "updated_at" || k === "last_heartbeat" ? undefined : x)) + (bucket ? "|" + Math.floor(Date.now() / 10000) : "");

async function loadBusOverview() {
  const r = await api("/api/agents/bus/overview");
  if (!r || !r.ok) return;
  const ov = await r.json();
  const sig = sigOf(ov, true);
  if (sig === lastSig.ov) return;
  lastSig.ov = sig;
  const connPill = document.getElementById("bus-conn-pill");
  if (!ov.connected) {
    connPill.textContent = "NATS Bus: Chưa cấu hình (--nats-url)";
    connPill.className = "pill warn";
    return;
  }
  connPill.textContent = "NATS JetStream: Trực tuyến (AICF_AGENTS · AICF_TASKS)";
  connPill.className = "pill ok";

  const tot = ov.totals || {};
  document.getElementById("kpi-bus-agents").textContent = String(tot.live_agents || 0);
  document.getElementById("kpi-bus-agents-sub").textContent =
    `${tot.compute_agents || 0} compute · ${tot.executor_agents || 0} executor · tổng ${tot.total_slots || 0} slots`;
  document.getElementById("kpi-bus-queue").textContent =
    `${tot.queue_depth || 0} chờ · ${tot.inflight_tasks || 0} chạy`;
  document.getElementById("kpi-bus-completed").textContent =
    `${ov.completed_total || 0} tác vụ đã hoàn tất qua bus`;

  const waits = (ov.queues || []).map(q => q.wait_median_s || 0).filter(v => v > 0);
  const maxWaitMs = waits.length ? Math.round(Math.max(...waits) * 1000) : 0;
  document.getElementById("kpi-bus-wait").textContent = `${maxWaitMs} ms`;
  document.getElementById("kpi-bus-redeliv").textContent =
    `${tot.redelivered_total || 0} giao lại (ack_wait) · ${tot.rejected_total || 0} từ chối`;

  const activeRecs = (ov.recommendations || []).filter(x => x.action !== "noop");
  const recVal = document.getElementById("kpi-sup-recs");
  const recSub = document.getElementById("kpi-sup-sub");
  if (activeRecs.length > 0) {
    const upCnt = activeRecs.filter(x => x.action === "scale_up").length;
    const downCnt = activeRecs.filter(x => x.action === "scale_down").length;
    recVal.textContent = `${activeRecs.length} đề xuất (${upCnt} tăng / ${downCnt} giảm)`;
    recSub.textContent = "propose_only=True · Chờ chủ dự án duyệt";
  } else {
    recVal.textContent = "Ổn định (Hysteresis)";
    recSub.textContent = "propose_only=True · Không tự động đổi cụm";
  }

  // Banner đề xuất nổi bật của Supervisor
  const bannerWrap = document.getElementById("sup-banner-wrap");
  bannerWrap.replaceChildren();
  for (const rec of activeRecs) {
    const isUp = rec.action === "scale_up";
    const preset = rec.task_type === "TEXT_GENERATE" ? "coder" : (rec.task_type === "RAG_RETRIEVE" ? "rag" : "sandbox");
    const delta = Math.max(1, Math.abs((rec.recommended_agents || 0) - (rec.current_agents || 0)));
    const box = el("div", { class: "rec-box" },
      el("div", {},
        el("b", {}, isUp ? `⚠️ Supervisor đề xuất TĂNG Agent cho ${rec.task_type}: ` : `ℹ️ Supervisor đề xuất GIẢM Agent cho ${rec.task_type}: `),
        el("span", {}, `${rec.current_agents} → ${rec.recommended_agents} agent (${rec.reason}) · `),
        el("code", { class: "mono", style: "font-size:11.5px" }, rec.suggested_command || "")
      ),
      isUp
        ? el("button", {
            type: "button",
            class: "sm primary",
            onclick: () => runBusDemo(`action=spawn&preset=${preset}&count=${delta}`, `Duyệt đề xuất tăng +${delta} Agent (${rec.task_type})`)
          }, `✅ Duyệt bật +${delta} Agent`)
        : el("button", {
            type: "button",
            class: "sm",
            onclick: () => runBusDemo("action=drain&count=1", `Duyệt đề xuất rút 1 Agent (${rec.task_type})`)
          }, "✅ Duyệt rút 1 Agent (Drain)")
    );
    bannerWrap.append(box);
  }

  // Bảng Sổ Agent sống (AICF_AGENTS KV)
  const agBody = document.getElementById("live-agents-tbody");
  agBody.replaceChildren();
  const agents = ov.live_agents || [];
  if (!agents.length) {
    agBody.append(el("tr", {}, el("td", { colspan: "6", class: "mut" }, "Không có agent nào đang sống trong KV AICF_AGENTS (bấm ➕ Thêm Agent ở trên).")));
  } else {
    const nowSec = Date.now() / 1000;
    for (const a of agents) {
      const stClass = a.state === "draining" ? "warn" : (a.state === "busy" ? "ok" : "");
      const stLabel = a.state === "draining" ? "đang rút lui" : (a.state === "busy" ? "đang bận" : "sẵn sàng");
      const ageSec = Math.max(0, Math.round(nowSec - (a.last_heartbeat || a.started_at || nowSec)));
      const caps = el("div", { class: "tag-list" });
      for (const tp of (a.task_types || [])) caps.append(el("span", { class: "tag-chip" }, tp));
      for (const lb of (a.labels || [])) caps.append(el("span", { class: "tag-chip", style: "color:var(--acc)" }, "#" + lb));

      const dcs = el("div", { class: "tag-list" });
      for (const dc of (a.data_classes || [])) dcs.append(el("span", { class: "tag-chip" }, dc));

      const slotTxt = `${a.slots || 1} slot · ${Math.round((a.ctx_per_slot || 8192) / 1024)}k ctx` +
        (a.tok_s_ref > 0 ? ` · ${a.tok_s_ref} tok/s` : "");

      agBody.append(el("tr", {},
        el("td", { class: "mono", style: "font-weight:600" },
          a.agent_id,
          el("div", { class: "mut", style: "font-size:11px;font-weight:400" }, (a.models || []).join(", ") || "worker")
        ),
        el("td", {},
          el("span", { class: "pill " + (a.kind === "compute" ? "ok" : "") }, a.kind || "executor"),
          " ",
          el("span", { class: "pill " + stClass }, stLabel),
          el("div", { class: "mut mono", style: "font-size:10.5px;margin-top:3px" }, `HB ${ageSec}s trước`)
        ),
        el("td", {}, caps),
        el("td", { class: "mono", style: "font-size:12px" }, slotTxt),
        el("td", {}, dcs),
        el("td", {},
          el("div", { class: "row", style: "gap:4px" },
            el("button", {
              type: "button",
              class: "sm ghost",
              title: "Rút lui êm (hoàn tất việc đang làm rồi xóa khóa KV)",
              onclick: () => runBusDemo("action=drain&agent_id=" + encodeURIComponent(a.agent_id), "Drain " + a.agent_id)
            }, "Drain"),
            el("button", {
              type: "button",
              class: "sm ghost",
              title: "Giả lập sập đột ngột (không xóa KV, chờ hết hạn TTL & giao lại việc)",
              onclick: () => runBusDemo("action=kill&agent_id=" + encodeURIComponent(a.agent_id), "Kill " + a.agent_id)
            }, "Kill")
          )
        )
      ));
    }
  }

  // Bảng Hàng đợi & Đề xuất theo loại tác vụ
  const qBody = document.getElementById("bus-queues-tbody");
  qBody.replaceChildren();
  const primaryTypes = new Set(["TEXT_GENERATE", "TOOL_EXECUTE", "RESULT_VALIDATE", "MODEL_EVALUATE", "RAG_RETRIEVE", "EMBEDDING", "RERANK", "DOCUMENT_PARSE"]);
  const rows = (ov.queues || []).filter(q =>
    primaryTypes.has(q.task_type) || q.queue_depth > 0 || q.inflight_tasks > 0 || q.live_agents > 0 || q.redelivered_total > 0
  );
  for (const q of rows) {
    const rec = q.recommendation || {};
    let recPill = el("span", { class: "pill" }, "Giữ nguyên (" + (rec.current_agents ?? q.live_agents) + ")");
    if (rec.action === "scale_up") {
      recPill = el("span", { class: "pill warn" }, `Đề xuất TĂNG: ${rec.current_agents} → ${rec.recommended_agents}`);
    } else if (rec.action === "scale_down") {
      recPill = el("span", { class: "pill" }, `Đề xuất GIẢM: ${rec.current_agents} → ${rec.recommended_agents}`);
    }
    const waitMs = Math.round((q.wait_median_s || 0) * 1000);
    qBody.append(el("tr", {},
      el("td", { class: "mono", style: "font-weight:600" }, q.task_type),
      el("td", { class: "mono" },
        q.queue_depth > 0
          ? el("span", { class: "pill warn" }, String(q.queue_depth) + " chờ")
          : el("span", { class: "mut" }, "0")
      ),
      el("td", { class: "mono" },
        q.inflight_tasks > 0
          ? el("span", { class: "pill ok" }, String(q.inflight_tasks) + " đang xử lý")
          : el("span", { class: "mut" }, "0")
      ),
      el("td", { class: "mono" }, String(q.live_agents)),
      el("td", { class: "mono", style: "font-size:12px" }, `${waitMs} ms · ${q.redelivered_total || 0} giao lại`),
      el("td", {}, recPill)
    ));
  }
}

function svg(tag, attrs, ...kids) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  e.append(...kids);
  return e;
}

// Layer = length of the longest dependency chain above the node; rows inside a layer keep input order.
function layout(nodes) {
  const byId = new Map(nodes.map(n => [n.id, n]));
  const depth = new Map();
  const d = id => {
    if (depth.has(id)) return depth.get(id);
    depth.set(id, 0);                      // guards against a malformed cycle
    const n = byId.get(id);
    const v = n && n.deps.length ? 1 + Math.max(...n.deps.map(x => byId.has(x) ? d(x) : -1)) : 0;
    depth.set(id, v);
    return v;
  };
  const rows = {};
  const pos = new Map();
  for (const n of nodes) {
    const l = d(n.id);
    rows[l] = (rows[l] || 0) + 1;
    pos.set(n.id, { l, r: rows[l] - 1 });
  }
  return { pos, layers: Object.keys(rows).length, maxRows: Math.max(1, ...Object.values(rows)) };
}

function draw(snap) {
  const W = 176, H = 46, GX = 58, GY = 16, PAD = 18;
  const { pos, layers, maxRows } = layout(snap.nodes);
  const g = document.getElementById("dag");
  g.replaceChildren();
  g.setAttribute("width", Math.max(320, PAD * 2 + layers * (W + GX) - GX));
  g.setAttribute("height", Math.max(120, PAD * 2 + maxRows * (H + GY) - GY));
  const xy = id => { const p = pos.get(id); return { x: PAD + p.l * (W + GX), y: PAD + p.r * (H + GY) }; };
  for (const n of snap.nodes) for (const dep of n.deps) {
    if (!pos.has(dep)) continue;
    const a = xy(dep), b = xy(n.id);
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
    g.append(svg("path", { class: "edge", d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` }));
  }
  for (const n of snap.nodes) {
    const { x, y } = xy(n.id);
    const sub = n.type + (n.attempts > 1 ? ` · lần ${n.attempts}` : "") + (n.status === "running" && n.running_for_s != null ? ` · ${n.running_for_s}s` : "");
    const node = svg("g", { class: `node s-${n.status}${n.id === selected ? " sel" : ""}`, tabindex: 0, role: "button", "aria-label": `${n.id}: ${STATUS_VI[n.status] || n.status}`, style: "cursor:pointer" },
      svg("rect", { x, y, width: W, height: H }),
      svg("text", { x: x + 10, y: y + 19 }, n.id.length > 24 ? n.id.slice(0, 23) + "…" : n.id),
      svg("text", { class: "sub", x: x + 10, y: y + 34 }, sub.length > 28 ? sub.slice(0, 27) + "…" : sub));
    const pick = () => { selected = n.id; show(n); draw(snap); };
    node.onclick = pick;
    node.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } };
    g.append(node);
  }
  const sel = snap.nodes.find(n => n.id === selected);
  if (sel) show(sel);
}

function show(n) {
  document.getElementById("detail").textContent =
    `${n.id} · ${n.type} · tenant ${n.tenant} · ${STATUS_VI[n.status] || n.status} · ${n.attempts} lần thử` +
    (n.last_duration_s != null ? ` · lần gần nhất ${n.last_duration_s}s` : "") + (n.deps.length ? ` · phụ thuộc: ${n.deps.join(", ")}` : " · không phụ thuộc");
}

function updateKPI(list, snap) {
  if (Array.isArray(list)) {
    document.getElementById("kpi-wf-count").textContent = String(list.length);
    const activeWf = list.filter(w => w.state === "running").length;
    document.getElementById("kpi-wf-sub").textContent = activeWf > 0 ? `${activeWf} workflow đang thực thi` : "Cập nhật tự động mỗi 2.0s";
  }
  if (snap && snap.counts) {
    const c = snap.counts;
    const total = snap.nodes ? snap.nodes.length : 0;
    const okCount = c.ok || 0;
    const runCount = (c.running || 0) + (c.retrying || 0);
    const pendCount = c.pending || 0;
    const failCount = (c.failed || 0) + (c.rejected || 0);
    document.getElementById("kpi-step-ok").textContent = `${okCount} / ${total}`;
    document.getElementById("kpi-step-pct").textContent = total > 0 ? `Hoàn thành ${Math.round((okCount / total) * 100)}% các bước` : "Tiến độ workflow hiện tại";
    document.getElementById("kpi-step-run").textContent = String(runCount);
    document.getElementById("kpi-step-pending").textContent = `${pendCount} bước đang chờ phụ thuộc`;
    document.getElementById("kpi-step-fail").textContent = String(failCount);
  }
}

async function loadList() {
  const r = await api("/api/agents/workflows");
  if (!r || !r.ok) { msg(r && r.status === 502 ? "Chưa kết nối được dịch vụ xem agent (gateway --agents-url)." : "Không tải được danh sách workflow."); return; }
  msg("");
  const list = await r.json();
  const sig = sigOf(list) + "|" + current;
  document.getElementById("stamp").textContent = "cập nhật " + new Date().toLocaleTimeString("vi-VN");
  if (sig === lastSig.list) return;
  lastSig.list = sig;
  updateKPI(list, null);
  const box = document.getElementById("wf-list");
  box.replaceChildren();
  if (!list.length) box.append(el("span", { class: "mut" }, "Chưa có workflow nào (bấm 🚀 Chạy Workflow Best-of-N mẫu ở trên để tạo)."));
  for (const w of list) {
    const c = w.counts, total = w.steps;
    box.append(el("button", { type: "button", class: "sm" + (w.workflow === current ? " primary" : ""), onclick: () => { current = w.workflow; selected = ""; loadList(); loadOne(); } },
      `${w.workflow} · ${c.ok || 0}/${total} · ${STATE_VI[w.state] || w.state}`));
  }
  document.getElementById("stamp").textContent = "cập nhật " + new Date().toLocaleTimeString("vi-VN");
  if (!current && list.length) { current = list[0].workflow; loadOne(); }
}

async function loadOne() {
  if (!current) return;
  const r = await api("/api/agents/workflows/" + encodeURIComponent(current));
  if (!r || !r.ok) return;
  const snap = await r.json();
  const sig = sigOf(snap) + "|" + current + "|" + selected;
  if (sig === lastSig.one) return;
  lastSig.one = sig;
  updateKPI(null, snap);
  document.getElementById("wf-title").textContent = snap.workflow;
  const st = document.getElementById("wf-state");
  st.textContent = `${STATE_VI[snap.state] || snap.state} · ` + Object.entries(snap.counts).map(([k, v]) => `${v} ${STATUS_VI[k] || k}`).join(", ");
  st.className = "pill " + (snap.state === "failed" ? "err" : snap.state === "done" ? "ok" : "");
  draw(snap);
}

loadBusOverview();
loadList();
setInterval(() => {
  if (!document.hidden) {
    loadBusOverview();
    loadList();
    loadOne();
  }
}, 2000);
