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
      const modeTag = item.thinking_mode === "hybrid" ? " [💭 Bật/Tắt Thinking]" :
                      item.thinking_mode === "always" ? " [🧠 Reasoning]" : " [⚡ Direct]";
      modelSel.append(el("option", { value: item.id }, item.id + modeTag));
    }
    if (prev && modelMetaMap[prev]) modelSel.value = prev;
    updateThinkCapBadge();
  } catch {}
}
if ($("model")) $("model").addEventListener("change", updateThinkCapBadge);
if ($("think-mode")) {
  $("think-mode").addEventListener("change", () => store.set("aicf_think_mode", $("think-mode").value));
}
loadModels();

function clearChat() {
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

async function ask(question) {
  store.set("aicf_key", $("key").value);
  addMsg("user", question);
  const bot = addMsg("bot", "");
  const think = el("details", { class: "think" }, el("summary", {}, "💭 Suy nghĩ của mô hình (Reasoning)")),
        thinkText = el("div"),
        answer = el("div");
  think.append(thinkText);
  think.hidden = true;
  bot.append(think, answer);
  const stats = el("div", { class: "stats" });
  bot.append(stats);

  const headers = { "Content-Type": "application/json" };
  if ($("key").value) headers["Authorization"] = "Bearer " + $("key").value;
  else headers["X-Tenant-ID"] = $("tenant").value || "demo";
  if ($("prio").value) headers["X-Priority"] = $("prio").value;
  if (+$("ddl").value > 0) headers["X-Deadline-Ms"] = $("ddl").value;
  const thinkMode = $("think-mode") ? $("think-mode").value : "auto";
  if (thinkMode === "on" || thinkMode === "off") {
    headers["X-AICF-Thinking"] = thinkMode;
  }

  const reqBody = {
    stream: true,
    max_tokens: 400,
    messages: [{ role: "user", content: question }],
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
  let first = 0, n = 0;
  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST",
      headers,
      signal: ctl.signal,
      body: JSON.stringify(reqBody),
    });
    const wk = r.headers.get("X-AICF-Worker");
    if (!r.ok) {
      let m = "";
      try { m = (await r.json()).error.message; } catch {}
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
        try { d = JSON.parse(ev.slice(6)).choices[0].delta; } catch { continue; }
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
  if ($("burst").checked) {
    await Promise.all(Array.from({ length: 8 }, (_, i) => ask(q + " (#" + (i + 1) + ")")));
  } else {
    await ask(q);
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
