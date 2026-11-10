const $ = id => document.getElementById(id);
$("key").value = store.get("aicf_key");
if ($("think-mode") && store.get("aicf_think_mode")) {
  $("think-mode").value = store.get("aicf_think_mode");
}
let ctl = null;
let modelMetaMap = {};

function updateThinkCapBadge() {
  const badge = $("think-cap-badge");
  const modelSel = $("model");
  if (!badge || !modelSel) return;
  const m = modelSel.value || Object.keys(modelMetaMap)[0] || "";
  const info = modelMetaMap[m];
  if (!info) {
    badge.textContent = "Tự động";
    badge.className = "pill sm";
    return;
  }
  if (info.thinking_mode === "hybrid") {
    badge.textContent = "💭 Hybrid (Bật/Tắt 0s)";
    badge.className = "pill sm ok";
  } else if (info.thinking_mode === "always") {
    badge.textContent = "🧠 Luôn suy luận";
    badge.className = "pill sm";
  } else {
    badge.textContent = "⚡ Trả lời thẳng";
    badge.className = "pill sm";
  }
}

async function loadModels() {
  const modelSel = $("model");
  if (!modelSel) return;
  const headers = {};
  if ($("key").value) headers["Authorization"] = "Bearer " + $("key").value;
  else headers["X-Tenant-ID"] = $("tenant").value || "demo";
  try {
    const r = await fetch("/v1/models", { headers });
    if (!r.ok) return;
    const j = await r.json();
    const list = (j && j.data) || [];
    const prev = modelSel.value;
    modelMetaMap = {};
    modelSel.replaceChildren(el("option", { value: "" }, "Tự động chọn model trên cụm"));
    for (const item of list) {
      if (item.is_alias) continue;
      modelMetaMap[item.id] = item;
      if (item.context_length && !item.is_alias) ctxLimit = item.context_length;
      const modeTag = item.thinking_mode === "hybrid" ? " [💭 Bật/Tắt Thinking]" :
                      item.thinking_mode === "always" ? " [🧠 Reasoning]" : " [⚡ Direct]";
      modelSel.append(el("option", { value: item.id }, item.id + modeTag));
    }
    if (prev && modelMetaMap[prev]) modelSel.value = prev;
    updateThinkCapBadge();
    updateVisionUI();
    updateMeter();
  } catch {}
}
if ($("model")) $("model").addEventListener("change", () => { updateThinkCapBadge(); updateVisionUI(); });
if ($("think-mode")) {
  $("think-mode").addEventListener("change", () => store.set("aicf_think_mode", $("think-mode").value));
}
loadModels();

// ---- Image attachments (shown only when the selected model reports capabilities.vision) ----
const MAX_IMAGES = 4, MAX_EDGE = 1568;
let attachments = [];   // data URLs (JPEG/PNG, longest edge <= MAX_EDGE)

function modelHasVision() {
  const sel = $("model");
  const m = (sel && sel.value) || Object.keys(modelMetaMap)[0] || "";
  const info = modelMetaMap[m];
  return !!(info && (info.vision || (info.capabilities && info.capabilities.vision)));
}
function updateVisionUI() {
  const ok = modelHasVision();
  if ($("btn-attach")) $("btn-attach").hidden = !ok;
  if (!ok && attachments.length) { attachments = []; renderAttachments(); }
}
function renderAttachments() {
  const row = $("attach-row");
  row.replaceChildren(...attachments.map((u, i) => el("div", { class: "thumb" },
    el("img", { src: u, alt: "ảnh " + (i + 1) }),
    el("button", { type: "button", class: "sm", title: "Bỏ ảnh", onclick: () => { attachments.splice(i, 1); renderAttachments(); } }, "×"))));
}
function fileToDataURL(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      const k = Math.min(1, MAX_EDGE / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(img.width * k));
      c.height = Math.max(1, Math.round(img.height * k));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL("image/jpeg", 0.9));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("không đọc được ảnh")); };
    img.src = url;
  });
}
async function addImages(files) {
  if (!modelHasVision()) return;
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    if (attachments.length >= MAX_IMAGES) { $("chat-status-pill").textContent = "Tối đa " + MAX_IMAGES + " ảnh mỗi câu hỏi"; break; }
    try { attachments.push(await fileToDataURL(f)); } catch (e) { $("chat-status-pill").textContent = "Lỗi ảnh: " + e.message; }
  }
  renderAttachments();
}
if ($("btn-attach")) {
  $("btn-attach").onclick = () => $("img-file").click();
  $("img-file").onchange = async e => { await addImages([...e.target.files]); e.target.value = ""; };
  $("q").addEventListener("paste", e => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter(f => f.type.startsWith("image/"));
    if (files.length && modelHasVision()) { e.preventDefault(); addImages(files); }
  });
  const box = $("f");
  box.addEventListener("dragover", e => { if (modelHasVision()) { e.preventDefault(); box.classList.add("dragover"); } });
  box.addEventListener("dragleave", () => box.classList.remove("dragover"));
  box.addEventListener("drop", e => { box.classList.remove("dragover"); if (modelHasVision() && e.dataTransfer.files.length) { e.preventDefault(); addImages([...e.dataTransfer.files]); } });
}

// ---- Conversation memory: history, context meter and automatic compaction ----
// history entries: { role: "user"|"assistant", text, images?: [dataURL] }. Old turns keep text only; only the most
// recent turn that carried images keeps them (so "what colour was the car?" still works one question later).
let history = [];
let summaryText = "";      // running summary of everything that was compacted away
let ctxLimit = 0;          // tokens per slot, from /v1/models (context_length) or learned from a 400 overflow error
let lastTotal = 0;         // prompt + completion tokens of the last exchange = size of the context right now
const KEEP_RECENT = 4;     // messages kept verbatim when compacting
const SUMMARY_PROMPT = "Bạn nén bộ nhớ hội thoại. Viết bản ghi nhớ NGẮN GỌN nhưng ĐẦY ĐỦ từ đoạn hội thoại bên dưới: " +
  "yêu cầu và mục tiêu của người dùng, tên riêng, số liệu, quyết định đã chốt, kết luận quan trọng, việc còn dở. " +
  "Giữ NGUYÊN VĂN các đoạn mã, lệnh, đường dẫn, số hiệu quan trọng. Không thêm ý mới, không bình luận. " +
  "Viết cùng ngôn ngữ với người dùng, dưới dạng gạch đầu dòng.";

function fmtTok(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n); }
function updateMeter() {
  const m = $("ctx-meter");
  if (!m) return;
  if (!ctxLimit) { m.textContent = lastTotal ? "Ngữ cảnh: " + fmtTok(lastTotal) + " token" : "Ngữ cảnh: —"; m.className = "pill sm"; return; }
  const pct = Math.min(100, Math.round(lastTotal / ctxLimit * 100));
  m.textContent = "Ngữ cảnh: " + fmtTok(lastTotal) + " / " + fmtTok(ctxLimit) + " (" + pct + "%)" + (summaryText ? " · đã nén" : "");
  m.className = "pill sm" + (pct >= 85 ? " err" : pct >= 60 ? " warn" : " ok");
}
function saveHistory() {
  try {
    store.set("aicf_chat_mem", JSON.stringify({ summaryText, lastTotal, history: history.map(h => ({ role: h.role, text: h.text })) }));
  } catch {}
}
function loadHistory() {
  try {
    const d = JSON.parse(store.get("aicf_chat_mem") || "null");
    if (!d || !Array.isArray(d.history)) return;
    history = d.history.filter(h => h && (h.role === "user" || h.role === "assistant") && typeof h.text === "string");
    summaryText = d.summaryText || "";
    lastTotal = +d.lastTotal || 0;
    if (history.length || summaryText) {
      for (const h of history) addMsg(h.role === "user" ? "user" : "bot", h.text);
      noteInLog("↩ Khôi phục " + history.length + " tin nhắn của cuộc trò chuyện trước" + (summaryText ? " (kèm bản tóm tắt)" : "") + ".");
    }
  } catch {}
}
function noteInLog(text) {
  const empty = $("empty-state");
  if (empty) empty.remove();
  const d = el("div", { class: "mut", style: "text-align:center;font-size:12px;padding:4px 0" }, text);
  $("log").append(d);
  d.scrollIntoView({ block: "end", behavior: "smooth" });
}
function buildHeaders() {
  const headers = { "Content-Type": "application/json" };
  if ($("key").value) headers["Authorization"] = "Bearer " + $("key").value;
  else headers["X-Tenant-ID"] = $("tenant").value || "demo";
  return headers;
}
// messages to send: running summary (as a system message) + history + the new user message
function buildMessages(userContent) {
  const out = [];
  if (summaryText) out.push({ role: "system", content: "Ghi nhớ từ phần đầu cuộc trò chuyện (đã được nén):\n" + summaryText });
  let lastImg = -1;
  history.forEach((h, i) => { if (h.images && h.images.length) lastImg = i; });
  history.forEach((h, i) => {
    if (h.images && h.images.length && i === lastImg) {
      out.push({ role: h.role, content: [{ type: "text", text: h.text }, ...h.images.map(u => ({ type: "image_url", image_url: { url: u } }))] });
    } else {
      out.push({ role: h.role, content: h.text + (h.images && h.images.length ? " [đã đính kèm " + h.images.length + " ảnh]" : "") });
    }
  });
  out.push({ role: "user", content: userContent });
  return out;
}
const approxTokens = s => Math.ceil((s || "").length / 3);   // rough: only used to decide when to compact BEFORE sending

async function compactHistory(why) {
  if (history.length <= KEEP_RECENT) return false;
  const old = history.slice(0, history.length - KEEP_RECENT);
  const rest = history.slice(-KEEP_RECENT);
  const transcript = (summaryText ? "Bản ghi nhớ trước đó:\n" + summaryText + "\n\n" : "") +
    old.map(m => (m.role === "user" ? "Người dùng: " : "Trợ lý: ") + m.text + (m.images && m.images.length ? " [kèm " + m.images.length + " ảnh]" : "")).join("\n\n");
  noteInLog("🗜 Đang nén " + old.length + " tin nhắn cũ (" + why + ")…");
  const body = {
    stream: false, max_tokens: 1200, temperature: 0.2, enable_thinking: false,
    chat_template_kwargs: { enable_thinking: false },
    messages: [{ role: "system", content: SUMMARY_PROMPT }, { role: "user", content: transcript }],
  };
  if ($("model") && $("model").value) body.model = $("model").value;
  try {
    const r = await fetch("/v1/chat/completions", { method: "POST", headers: buildHeaders(), body: JSON.stringify(body) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = ((await r.json()).choices[0].message.content || "").trim();
    if (!s) throw new Error("bản tóm tắt rỗng");
    summaryText = s;
    history = rest;
    lastTotal = approxTokens(summaryText) + approxTokens(rest.map(m => m.text).join(" "));
    saveHistory();
    updateMeter();
    noteInLog("🗜 Đã nén " + old.length + " tin nhắn thành bản ghi nhớ " + fmtTok(approxTokens(s)) + " token. Giữ nguyên " + rest.length + " tin nhắn gần nhất.");
    return true;
  } catch (e) {
    noteInLog("⚠ Không nén được hội thoại (" + e.message + "). Giữ nguyên lịch sử.");
    return false;
  }
}

function clearChat() {
  history = [];
  summaryText = "";
  lastTotal = 0;
  saveHistory();
  updateMeter();
  const log = $("log");
  log.replaceChildren();
  const empty = el("div", { class: "chat-empty", id: "empty-state" },
    el("div", { class: "chat-empty-sparkle" }, "✨"),
    el("div", { class: "chat-empty-title" }, "Không gian Thử nghiệm Trực tiếp (Playground)"),
    el("div", { class: "chat-empty-desc" }, "Nhập prompt bên dưới hoặc chọn gợi ý nhanh để bắt đầu phiên làm việc. Phản hồi mô hình sẽ truyền về qua Server-Sent Events (SSE) kèm số liệu TTFT và tok/s.")
  );
  log.append(empty);
}

function setPrompt(txt) {
  $("q").value = txt;
  $("q").focus();
}
document.querySelectorAll("[data-prompt]").forEach(btn => {
  btn.addEventListener("click", () => setPrompt(btn.getAttribute("data-prompt") || ""));
});
if ($("btn-clear-chat")) $("btn-clear-chat").addEventListener("click", clearChat);

function addMsg(cls, text) {
  const empty = $("empty-state");
  if (empty) empty.remove();

  const d = el("div", { class: "msg " + cls }, text);
  $("log").append(d);
  d.scrollIntoView({ block: "end", behavior: "smooth" });
  return d;
}

async function ask(question, images = [], useMemory = true, retried = false) {
  store.set("aicf_key", $("key").value);
  const thresholdPct = Math.min(95, Math.max(30, +($("compact-pct") && $("compact-pct").value) || 70));
  if (useMemory && !retried && ctxLimit && history.length > KEEP_RECENT &&
      lastTotal + approxTokens(question) + 1500 * images.length + (+($("maxtok") && $("maxtok").value) || 4096) > ctxLimit * thresholdPct / 100) {
    await compactHistory("sắp đầy ngữ cảnh: " + fmtTok(lastTotal) + "/" + fmtTok(ctxLimit));
  }
  const um = addMsg("user", question);
  if (images.length) um.prepend(el("div", { class: "msg-imgs" }, ...images.map(u => el("img", { src: u, alt: "ảnh đính kèm" }))));
  const bot = addMsg("bot", "");
  const think = el("details", { class: "think" }, el("summary", {}, "💭 Suy nghĩ của mô hình (Reasoning)")),
        thinkText = el("div"),
        answer = el("div");
  think.append(thinkText);
  think.hidden = true;
  bot.append(think, answer);
  const stats = el("div", { class: "stats" });
  bot.append(stats);

  const headers = buildHeaders();
  if ($("prio").value) headers["X-Priority"] = $("prio").value;
  if (+$("ddl").value > 0) headers["X-Deadline-Ms"] = $("ddl").value;
  const thinkMode = $("think-mode") ? $("think-mode").value : "auto";
  if (thinkMode === "on" || thinkMode === "off") {
    headers["X-AICF-Thinking"] = thinkMode;
  }

  const reqBody = {
    stream: true,
    stream_options: { include_usage: true },
    max_tokens: Math.min(32768, Math.max(16, +($("maxtok") && $("maxtok").value) || 4096)),
    messages: (useMemory ? buildMessages : (c => [{ role: "user", content: c }]))(images.length
      ? [{ type: "text", text: question }, ...images.map(u => ({ type: "image_url", image_url: { url: u } }))]
      : question),
  };
  if ($("model") && $("model").value) {
    reqBody.model = $("model").value;
  }
  if (thinkMode === "on") {
    reqBody.enable_thinking = true;
    reqBody.chat_template_kwargs = { enable_thinking: true };
  } else if (thinkMode === "off") {
    reqBody.enable_thinking = false;
    reqBody.chat_template_kwargs = { enable_thinking: false };
  }

  const t0 = performance.now();
  let first = 0, n = 0, finish = "", usage = null;
  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST",
      headers,
      signal: ctl.signal,
      body: JSON.stringify(reqBody),
    });
    const wk = r.headers.get("X-AICF-Worker");
    if (!r.ok) {
      let m = "", ej = null;
      try { ej = (await r.json()).error; m = ej.message; } catch {}
      if (ej && ej.type === "exceed_context_size_error") {
        if (ej.n_ctx) { ctxLimit = ej.n_ctx; updateMeter(); }
        if (useMemory && !retried && history.length > 0) {
          bot.remove(); um.remove();
          if (await compactHistory("vượt giới hạn ngữ cảnh")) return ask(question, images, useMemory, true);
        }
      }
      const ra = r.headers.get("Retry-After");
      answer.className = "err";
      answer.textContent = `${r.status}: ${m || r.statusText}` + (ra ? ` — thử lại sau ${ra}s` : "");
      return;
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const ev = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (!ev.startsWith("data: ") || ev.startsWith("data: [DONE]")) continue;
        let d;
        let ch, parsed;
        try { parsed = JSON.parse(ev.slice(6)); } catch { continue; }
        if (parsed.usage && parsed.usage.total_tokens) usage = parsed.usage;
        try { ch = parsed.choices[0]; d = ch.delta || {}; } catch { continue; }
        if (!ch) continue;
        if (ch.finish_reason) finish = ch.finish_reason;
        if (d.reasoning_content) {
          think.hidden = false;
          thinkText.textContent += d.reasoning_content;
          n++;
        }
        if (d.content) {
          answer.textContent += d.content;
          n++;
        }
        if (n && !first) first = performance.now() - t0;
      }
    }
    if (finish === "length") {
      answer.append(el("div", { class: "err", style: "margin-top:8px" }, "⚠ Câu trả lời bị cắt vì chạm giới hạn max_tokens. Tăng \"Độ dài tối đa\" ở cột trái rồi hỏi lại."));
    }
    if (useMemory && (answer.textContent || "").length) {
      history.push({ role: "user", text: question, images: images.length ? images : undefined });
      history.push({ role: "assistant", text: answer.textContent });
      if (usage) lastTotal = usage.total_tokens;
      else lastTotal += approxTokens(question) + approxTokens(answer.textContent);
      saveHistory();
      updateMeter();
    }
    const total = (performance.now() - t0) / 1000;
    stats.append(
      el("span", {}, "worker: " + (wk || "?")),
      el("span", {}, "token đầu: " + Math.round(first) + " ms"),
      el("span", {}, n + " token"),
      el("span", {}, (n / Math.max(total - first / 1000, 0.001)).toFixed(1) + " tok/s")
    );
  } catch (e) {
    if (e.name !== "AbortError") {
      answer.className = "err";
      answer.textContent = "Lỗi kết nối: " + e.message;
    }
  }
}

$("f").onsubmit = async e => {
  e.preventDefault();
  ctl = new AbortController();
  $("send").disabled = true;
  $("stop").disabled = false;
  const q = $("q").value.trim();
  const imgs = attachments.slice();
  attachments = [];
  renderAttachments();
  if ($("burst").checked) {
    await Promise.all(Array.from({ length: 8 }, (_, i) => ask(q + " (#" + (i + 1) + ")", imgs, false)));
  } else {
    await ask(q, imgs);
  }
  $("send").disabled = false;
  $("stop").disabled = true;
};

$("stop").onclick = () => ctl && ctl.abort();

$("q").addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    if (!$("send").disabled) {
      $("f").requestSubmit();
    }
  }
});

if ($("btn-compact")) {
  $("btn-compact").onclick = async () => {
    $("btn-compact").disabled = true;
    if (!(history.length > KEEP_RECENT && await compactHistory("do bạn yêu cầu"))) {
      if (history.length <= KEEP_RECENT) noteInLog("ℹ Chưa đủ tin nhắn cũ để nén (cần hơn " + KEEP_RECENT + " tin).");
    }
    $("btn-compact").disabled = false;
  };
}
loadHistory();
updateMeter();
