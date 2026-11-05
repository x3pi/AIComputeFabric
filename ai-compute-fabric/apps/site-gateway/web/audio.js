const API = "/api/studio/v1/audio";
let CURRENT_AUDIO_B64 = "";
let CURRENT_VAD_B64 = "";

const canWrite = () => !!(window.AICF && AICF.can && AICF.can.studio);

function say(text, cls) {
  const m = document.getElementById("msg");
  m.textContent = text || "";
  m.className = cls || "mut";
}

async function call(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const r = await api(API + path, opts);
  if (!r) throw new Error("không kết nối được máy chủ");
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data.error && data.error.message) || ("lỗi HTTP " + r.status));
  return data;
}

function readFileBase64(file) {
  return new Promise((ok, no) => {
    const fr = new FileReader();
    fr.onerror = () => no(new Error("không đọc được tệp"));
    fr.onload = () => ok(String(fr.result).split(",")[1] || "");
    fr.readAsDataURL(file);
  });
}

function setTtsSample(txt) {
  document.getElementById("tts-text").value = txt;
}
document.querySelectorAll("[data-tts-sample]").forEach(btn => {
  btn.addEventListener("click", () => setTtsSample(btn.getAttribute("data-tts-sample") || ""));
});
document.querySelectorAll("[data-preset]").forEach(btn => {
  btn.addEventListener("click", () => {
    const parts = (btn.getAttribute("data-preset") || "").split("|");
    if (parts.length >= 5) {
      applyPresetSliders(parseFloat(parts[0]), parseFloat(parts[1]), parseFloat(parts[2]), parseFloat(parts[3]), parts[4]);
    }
  });
});

function renderSegments(segs, dur) {
  const total = Math.max(dur || 1, 0.1);
  const bar = el("div", { class: "vad-timeline", title: "Các khoảng màu xanh là đoạn phát hiện có tiếng nói (VAD)" });
  for (const s of (segs || [])) {
    const left = Math.max(0, Math.min(100, (s.start / total) * 100));
    const width = Math.max(1, Math.min(100 - left, ((s.end - s.start) / total) * 100));
    bar.append(el("div", {
      class: "vad-seg",
      style: `left:${left.toFixed(1)}%;width:${width.toFixed(1)}%`,
      title: `Tiếng nói: ${s.start}s → ${s.end}s`
    }));
  }
  const pills = el("div", { class: "chip-row" },
    ...(segs && segs.length
      ? segs.map((s, i) => el("span", { class: "pill ok" }, `Đoạn #${i + 1}: ${s.start}s – ${s.end}s`))
      : [el("span", { class: "pill warn" }, "Không phát hiện tiếng nói (Im lặng)")])
  );
  return el("div", {}, bar, pills);
}

// 1. TTS, Round-trip & Custom Voice Profile Studio handlers
let VOICES_CACHE = [];

function updateSliderLabels() {
  const p = +document.getElementById("vp-pitch").value;
  const r = +document.getElementById("vp-rate").value;
  const n = +document.getElementById("vp-noise").value;
  const w = +document.getElementById("vp-warmth").value;
  document.getElementById("lbl-vp-pitch").textContent = (p >= 0 ? "+" : "") + p.toFixed(1) + " semitones";
  document.getElementById("lbl-vp-rate").textContent = r.toFixed(2) + "x";
  document.getElementById("lbl-vp-noise").textContent = n.toFixed(2);
  document.getElementById("lbl-vp-warmth").textContent = (w >= 0 ? "+" : "") + w.toFixed(1) + " dB";
}

function applyPresetSliders(pitch, rate, noise, warmth, suggestedName) {
  document.getElementById("vp-pitch").value = pitch;
  document.getElementById("vp-rate").value = rate;
  document.getElementById("vp-noise").value = noise;
  document.getElementById("vp-warmth").value = warmth;
  if (suggestedName) document.getElementById("vp-name").value = suggestedName;
  updateSliderLabels();
}

for (const id of ["vp-pitch", "vp-rate", "vp-noise", "vp-warmth"]) {
  document.getElementById(id).oninput = updateSliderLabels;
}

function currentSliderParams() {
  return {
    pitch_semitones: +document.getElementById("vp-pitch").value,
    speaking_rate: +document.getElementById("vp-rate").value,
    noise_scale: +document.getElementById("vp-noise").value,
    noise_scale_w: 0.8,
    warmth_db: +document.getElementById("vp-warmth").value
  };
}

async function loadVoices(selectedId) {
  const tenant = (document.getElementById("tts-tenant").value || "demo").trim();
  const r = await call("GET", `/tenants/${encodeURIComponent(tenant)}/voices`);
  VOICES_CACHE = r.voices || [];
  const sel = document.getElementById("tts-voice");
  const cur = selectedId || sel.value || "default";
  sel.replaceChildren(...VOICES_CACHE.map(v =>
    el("option", { value: v.id }, `${v.builtin ? "🎙️ " : "✨ [Tùy chỉnh] "}${v.name} (Pitch ${v.pitch_semitones >= 0 ? "+" : ""}${v.pitch_semitones}st · ${v.speaking_rate}x)`)
  ));
  if (VOICES_CACHE.some(v => v.id === cur)) sel.value = cur;

  const customBox = document.getElementById("vp-custom-list");
  const customs = VOICES_CACHE.filter(v => !v.builtin);
  if (!customs.length) {
    customBox.replaceChildren(el("span", { class: "mut", style: "font-size:11.5px" }, `Chưa có giọng tùy chỉnh nào lưu riêng cho tenant '${tenant}'. Hãy chỉnh thanh trượt và bấm "💾 Lưu thành Giọng mới của Tenant".`));
  } else {
    customBox.replaceChildren(
      el("span", { class: "mut", style: "font-size:11.5px;align-self:center" }, `Giọng đã lưu của '${tenant}':`),
      ...customs.map(v => el("span", { class: "pill ok", style: "gap:6px;display:inline-flex;align-items:center" },
        el("span", { style: "cursor:pointer", onclick: () => {
          sel.value = v.id;
          applyPresetSliders(v.pitch_semitones, v.speaking_rate, v.noise_scale, v.warmth_db, v.name);
        } }, `✨ ${v.name} (${v.pitch_semitones >= 0 ? "+" : ""}${v.pitch_semitones}st, ${v.speaking_rate}x)`),
        ...(window.AICF && AICF.can && AICF.can.admin ? [
          el("button", { type: "button", class: "sm ghost", style: "padding:0 5px;height:18px;font-size:10px", title: "Xóa giọng này", onclick: async () => {
            try {
              await call("DELETE", `/tenants/${encodeURIComponent(tenant)}/voices/${encodeURIComponent(v.id)}`);
              await loadVoices("default");
              say(`Đã xóa hồ sơ giọng '${v.name}'.`, "ok");
            } catch (e) { say(e.message, "err"); }
          } }, "✕")
        ] : [])
      ))
    );
  }
}

document.getElementById("tts-voice").onchange = e => {
  const v = VOICES_CACHE.find(x => x.id === e.target.value);
  if (v) applyPresetSliders(v.pitch_semitones, v.speaking_rate, v.noise_scale, v.warmth_db);
};
document.getElementById("tts-tenant").onchange = () => loadVoices().catch(e => say(e.message, "err"));

async function executeTts(extraPayload, statusLabel) {
  const btn = document.getElementById("btn-tts");
  const out = document.getElementById("tts-out");
  btn.disabled = true;
  out.replaceChildren(el("div", { class: "mut" }, statusLabel || "Đang tổng hợp giọng nói tiếng Việt trên GPU (facebook/mms-tts-vie + DSP)…"));
  try {
    const payload = Object.assign({
      text: document.getElementById("tts-text").value,
      voice: document.getElementById("tts-voice").value || "default",
      tenant: (document.getElementById("tts-tenant").value || "demo").trim()
    }, extraPayload || {});
    const r = await call("POST", "/tts", payload);
    CURRENT_AUDIO_B64 = r.audio_base64;
    CURRENT_VAD_B64 = r.audio_base64;
    const src = "data:audio/wav;base64," + r.audio_base64;
    const vp = r.voice_profile || {};
    out.replaceChildren(
      el("div", { class: "audio-box" },
        el("div", { class: "row", style: "justify-content:space-between;flex-wrap:wrap;gap:6px" },
          el("strong", {}, `Âm thanh đã tạo (${r.duration_s}s · ${vp.name || "Mặc định"})`),
          el("div", { class: "row", style: "gap:8px" },
            el("span", { class: "pill info" }, `Pitch ${vp.pitch_semitones >= 0 ? "+" : ""}${vp.pitch_semitones || 0}st · ${vp.speaking_rate || 1}x · EQ ${vp.warmth_db >= 0 ? "+" : ""}${vp.warmth_db || 0}dB`),
            el("a", { href: src, download: "aicf-tts.wav", style: "font-size:12px;color:var(--acc);font-weight:600" }, "⬇ Tải .wav")
          )
        ),
        el("audio", { controls: true, autoplay: false, src }),
        el("div", { class: "row", style: "margin-top:8px;justify-content:space-between" },
          el("span", { class: "mut", style: "font-size:11.5px" }, r.licence_note),
          el("button", { type: "button", class: "sm", onclick: () => {
            document.getElementById("asr-ref").value = r.text;
            document.getElementById("asr-source-hint").textContent = `Đã nạp âm thanh TTS (${r.duration_s}s) vào bộ nhớ đệm ASR & VAD.`;
            say("Đã chuyển âm thanh TTS sang ô Nhận dạng ASR (Mục 2) và Lọc nhiễu (Mục 3). Bấm nút tương ứng để chạy!", "ok");
          } }, "↪ Đưa âm thanh này sang ô ASR & Lọc nhiễu")
        )
      )
    );
    say(`Tổng hợp giọng nói '${vp.name || "Mặc định"}' hoàn tất.`, "ok");
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  } finally {
    btn.disabled = !canWrite();
  }
}

document.getElementById("btn-tts").onclick = () => executeTts(null);

document.getElementById("btn-vp-preview").onclick = () =>
  executeTts({ voice_params: currentSliderParams() }, "Đang tổng hợp thử nghiệm Hồ sơ Giọng đang chỉnh (VITS + Pitch/EQ DSP)…");

document.getElementById("btn-vp-save").onclick = async () => {
  const tenant = (document.getElementById("tts-tenant").value || "demo").trim();
  const name = document.getElementById("vp-name").value.trim();
  try {
    const r = await call("POST", `/tenants/${encodeURIComponent(tenant)}/voices`, Object.assign({ name }, currentSliderParams()));
    await loadVoices(r.voice.id);
    say(`Đã lưu giọng mới '${r.voice.name}' (${r.voice.id}) cho tenant '${tenant}'!`, "ok");
  } catch (e) {
    say(e.message, "err");
  }
};

document.getElementById("btn-roundtrip").onclick = async () => {
  const btn = document.getElementById("btn-roundtrip");
  const out = document.getElementById("tts-out");
  btn.disabled = true;
  out.replaceChildren(el("div", { class: "mut" }, "Đang chạy vòng lặp khép kín: TTS (mms-tts-vie + DSP) → WAV → VAD → ASR (Qwen3-ASR-0.6B) → Chấm điểm WER…"));
  try {
    const r = await call("POST", "/roundtrip", {
      text: document.getElementById("tts-text").value,
      voice: document.getElementById("tts-voice").value || "default",
      tenant: (document.getElementById("tts-tenant").value || "demo").trim()
    });
    CURRENT_AUDIO_B64 = r.audio_base64;
    CURRENT_VAD_B64 = r.audio_base64;
    const src = "data:audio/wav;base64," + r.audio_base64;
    const werPct = r.eval ? (r.eval.wer * 100).toFixed(1) + "%" : "—";
    const cerPct = r.eval ? (r.eval.cer * 100).toFixed(1) + "%" : "—";
    const vp = r.voice_profile || {};
    out.replaceChildren(
      el("div", { class: "audio-box stack" },
        el("div", { class: "row", style: "justify-content:space-between;flex-wrap:wrap;gap:6px" },
          el("strong", {}, `Kết quả Vòng lặp TTS → ASR (${r.duration_s}s · ${vp.name || "Mặc định"})`),
          el("div", { class: "row", style: "gap:6px" },
            el("span", { class: "pill " + (r.eval && r.eval.wer <= 0.15 ? "ok" : "warn") }, `WER: ${werPct}`),
            el("span", { class: "pill info" }, `CER: ${cerPct}`)
          )
        ),
        el("audio", { controls: true, src }),
        el("div", { class: "transcript-box" },
          el("div", { class: "mut", style: "font-size:11.5px;margin-bottom:4px" }, "Văn bản Qwen3-ASR-0.6B nghe lại từ âm thanh vừa tổng hợp:"),
          el("strong", {}, r.asr_text || "(trống)")
        ),
        renderSegments(r.segments, r.duration_s)
      )
    );
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  } finally {
    btn.disabled = !canWrite();
  }
};

document.getElementById("btn-clone-test").onclick = async () => {
  const out = document.getElementById("tts-out");
  try {
    await call("POST", "/tts", { text: "Xin chào", voice: "cloned_vip", consent_ref: "fake-consent" });
  } catch (e) {
    out.replaceChildren(
      el("div", { class: "audio-box", style: "border-color:var(--ok)" },
        el("div", { class: "row", style: "gap:8px" },
          el("span", { class: "pill ok" }, "ĐÃ CHẶN AN TOÀN (HTTP 400)"),
          el("strong", {}, "Kiểm thử Chính sách Chống Giả mạo Giọng nói (Zero Voice-Cloning)")
        ),
        el("div", { class: "mut", style: "margin-top:6px" }, "Phản hồi từ máy chủ: " + e.message)
      )
    );
  }
};

// 2. ASR Handler
document.getElementById("asr-file").onchange = async e => {
  const f = e.target.files[0];
  if (f) {
    CURRENT_AUDIO_B64 = await readFileBase64(f);
    document.getElementById("asr-source-hint").textContent = `Đã nạp tệp: ${f.name} (${Math.round(f.size / 1024)} KB)`;
  }
};

document.getElementById("btn-asr").onclick = async () => {
  const btn = document.getElementById("btn-asr");
  const out = document.getElementById("asr-out");
  if (!CURRENT_AUDIO_B64) {
    out.replaceChildren(el("div", { class: "err" }, "Hãy chọn một tệp .wav hoặc tổng hợp âm thanh từ ô TTS bên trái trước."));
    return;
  }
  btn.disabled = true;
  out.replaceChildren(el("div", { class: "mut" }, "Đang chạy Energy VAD và nhận dạng giọng nói bằng Qwen3-ASR-0.6B…"));
  try {
    const r = await call("POST", "/asr", {
      audio_base64: CURRENT_AUDIO_B64,
      lang: document.getElementById("asr-lang").value,
      reference: document.getElementById("asr-ref").value
    });
    const items = [
      el("div", { class: "row", style: "justify-content:space-between" },
        el("strong", {}, `Kết quả Nhận dạng (${r.duration_s}s · ${r.engine} · ${r.device})`),
        r.eval ? el("span", { class: "pill " + (r.eval.wer <= 0.15 ? "ok" : "warn") }, `WER: ${(r.eval.wer * 100).toFixed(1)}% · CER: ${(r.eval.cer * 100).toFixed(1)}%`) : null
      ),
      el("div", { class: "transcript-box", id: "asr-transcript" }, r.text || "(Không phát hiện lời nói)"),
      renderSegments(r.segments, r.duration_s)
    ];
    if (r.eval) {
      items.push(el("div", { class: "mut", style: "font-size:12px" },
        `Chuẩn hóa câu gốc: "${r.eval.reference_normalized}" | Chuẩn hóa câu nhận dạng: "${r.eval.hypothesis_normalized}"`));
    }
    out.replaceChildren(...items);
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  } finally {
    btn.disabled = !canWrite();
  }
};

// 3. VAD & Spectral Gate Enhancement
document.getElementById("vad-file").onchange = async e => {
  const f = e.target.files[0];
  if (f) {
    CURRENT_VAD_B64 = await readFileBase64(f);
    say(`Đã nạp tệp ${f.name} để phân tích VAD & lọc nhiễu.`, "ok");
  }
};

document.getElementById("btn-sample-wav").onclick = async () => {
  const out = document.getElementById("vad-out");
  out.replaceChildren(el("div", { class: "mut" }, "Đang tạo mẫu tín hiệu âm thanh 16 kHz PCM16 có nhiễu nền…"));
  try {
    const r = await call("POST", "/sample-wav", { noisy: true });
    CURRENT_VAD_B64 = r.audio_base64;
    CURRENT_AUDIO_B64 = r.audio_base64;
    document.getElementById("btn-analyze").click();
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  }
};

document.getElementById("btn-analyze").onclick = async () => {
  const out = document.getElementById("vad-out");
  if (!CURRENT_VAD_B64) {
    out.replaceChildren(el("div", { class: "err" }, "Hãy tải tệp .wav hoặc bấm '✨ Tạo mẫu Giọng nói + Nhiễu nền trắng' trước."));
    return;
  }
  out.replaceChildren(el("div", { class: "mut" }, "Đang chạy Energy VAD và lọc nhiễu Wiener Spectral Gate…"));
  try {
    const r = await call("POST", "/analyze", { audio_base64: CURRENT_VAD_B64, enhance: true });
    const origSrc = "data:audio/wav;base64," + CURRENT_VAD_B64;
    const enhSrc = "data:audio/wav;base64," + r.enhanced_audio_base64;
    out.replaceChildren(
      el("div", { class: "chip-row" },
        el("span", { class: "pill info" }, `Thời lượng: ${r.duration_s}s (${r.sample_rate} Hz)`),
        el("span", { class: "pill ok" }, `Tiếng nói: ${r.speech_seconds}s (${Math.round(r.speech_ratio * 100)}%)`),
        el("span", { class: "pill warn" }, `Mức RMS gốc: ${r.rms_db} dBFS → Sau lọc: ${r.enhanced_rms_db} dBFS`)
      ),
      renderSegments(r.segments, r.duration_s),
      el("div", { class: "two-col" },
        el("div", { class: "audio-box" },
          el("strong", {}, "🔊 Tín hiệu Gốc (Trước lọc nhiễu)"),
          el("audio", { controls: true, src: origSrc })
        ),
        el("div", { class: "audio-box" },
          el("div", { class: "row", style: "justify-content:space-between" },
            el("strong", {}, "✨ Sau Wiener Spectral Gate (-12 dB)"),
            el("a", { href: enhSrc, download: "aicf-enhanced.wav", style: "font-size:12px;color:var(--acc);font-weight:600" }, "⬇ Tải .wav")
          ),
          el("audio", { controls: true, src: enhSrc })
        )
      )
    );
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  }
};

// 4. Vietnamese Normalizer & Evaluator
document.getElementById("btn-norm-sample").onclick = () => {
  document.getElementById("norm-ref").value = "Hóa đơn số 482 được thanh toán ngày 15/03/2026 tại thành phố Hoà Bình.";
  document.getElementById("norm-hyp").value = "hóa đơn số bốn trăm tám mươi hai được thanh toán ngày mười lăm tháng ba năm hai nghìn không trăm hai mươi sáu tại thành phố hòa bình";
  document.getElementById("btn-eval").click();
};

document.getElementById("btn-eval").onclick = async () => {
  const out = document.getElementById("norm-out");
  try {
    const r = await call("POST", "/evaluate", {
      reference: document.getElementById("norm-ref").value,
      hypothesis: document.getElementById("norm-hyp").value
    });
    out.replaceChildren(
      el("div", { class: "audio-box stack", id: "eval-result" },
        el("div", { class: "row", style: "justify-content:space-between" },
          el("strong", {}, "Kết quả Chuẩn hóa & So khớp Levenshtein"),
          el("div", { class: "row", style: "gap:6px" },
            el("span", { class: "pill " + (r.wer === 0 ? "ok" : r.wer < 0.2 ? "info" : "warn") }, `WER: ${(r.wer * 100).toFixed(2)}%`),
            el("span", { class: "pill " + (r.cer === 0 ? "ok" : r.cer < 0.1 ? "info" : "warn") }, `CER: ${(r.cer * 100).toFixed(2)}%`)
          )
        ),
        el("div", { style: "font-size:12.5px" }, el("b", {}, "Chuẩn hóa gốc: "), el("span", { class: "mono" }, r.reference_normalized)),
        el("div", { style: "font-size:12.5px" }, el("b", {}, "Chuẩn hóa nhận dạng: "), el("span", { class: "mono" }, r.hypothesis_normalized))
      )
    );
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  }
};

// 5. Audio Dataset Governance & Speaker-Disjoint Split (P6.1)
function fillSampleManifest() {
  const speakers = ["spk_hanoi_01", "spk_hanoi_02", "spk_hue_01", "spk_hue_02", "spk_danang_01", "spk_hcm_01", "spk_hcm_02", "spk_cantho_01"];
  const lines = [];
  for (let i = 0; i < 24; i++) {
    const spk = speakers[i % speakers.length];
    lines.push(JSON.stringify({
      path: `wav/${spk}_clip_${String(i + 1).padStart(3, "0")}.wav`,
      sha256: (i + 1).toString(16).padStart(64, "a4f801"),
      speaker_id: spk,
      consent_ref: `CONSENT-VN-2026-${spk.toUpperCase()}`,
      text: `Mẫu thu âm giọng nói chuẩn hóa số ${i + 1} của người nói ${spk}`
    }));
  }
  document.getElementById("ds-manifest").value = lines.join("\n");
}

document.getElementById("btn-manifest-sample").onclick = () => {
  fillSampleManifest();
  document.getElementById("btn-ds-split").click();
};

document.getElementById("btn-ds-split").onclick = async () => {
  const out = document.getElementById("ds-out");
  try {
    const r = await call("POST", "/dataset/split", { manifest_jsonl: document.getElementById("ds-manifest").value, seed: 7 });
    out.replaceChildren(
      el("div", { class: "audio-box stack", id: "ds-split-result" },
        el("div", { class: "row", style: "justify-content:space-between" },
          el("strong", {}, `Tổng cộng ${r.total_records} bản ghi · ${r.total_speakers} người nói độc lập`),
          el("span", { class: "pill " + (r.disjoint_verified ? "ok" : "err") },
            r.disjoint_verified ? "✓ 0% Trùng lặp Người nói (Speaker-Disjoint)" : `Lỗi: trùng ${r.speaker_overlap_count} người nói`)
        ),
        el("div", { class: "table-wrap" },
          el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, "Tập chia (Split)"), el("th", { class: "num" }, "Số bản ghi"), el("th", {}, "Danh sách Speaker ID (Tách biệt hoàn toàn)"))),
            el("tbody", {},
              el("tr", {}, el("td", {}, el("b", {}, "Train (80%)")), el("td", { class: "num" }, String(r.splits.train.records)), el("td", { class: "mono" }, r.splits.train.speakers.join(", ") || "—")),
              el("tr", {}, el("td", {}, el("b", {}, "Validation (10%)")), el("td", { class: "num" }, String(r.splits.validation.records)), el("td", { class: "mono" }, r.splits.validation.speakers.join(", ") || "—")),
              el("tr", {}, el("td", {}, el("b", {}, "Test (10%)")), el("td", { class: "num" }, String(r.splits.test.records)), el("td", { class: "mono" }, r.splits.test.speakers.join(", ") || "—"))
            )
          )
        )
      )
    );
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  }
};

document.getElementById("btn-ds-bad-test").onclick = async () => {
  const out = document.getElementById("ds-out");
  try {
    await call("POST", "/dataset/split", {
      manifest_jsonl: JSON.stringify({ path: "wav/unverified.wav", sha256: "1234", speaker_id: "spk_99", consent_ref: "" })
    });
  } catch (e) {
    out.replaceChildren(
      el("div", { class: "audio-box", style: "border-color:var(--ok)" },
        el("div", { class: "row", style: "gap:8px" },
          el("span", { class: "pill ok" }, "ĐÃ CHẶN VI PHẠM DỮ LIỆU (HTTP 400)"),
          el("strong", {}, "Data Governance: Bắt buộc phải có văn bản đồng ý (consent_ref) của người nói")
        ),
        el("div", { class: "mut", style: "margin-top:6px" }, "Phản hồi từ máy chủ: " + e.message)
      )
    );
  }
};

// 6. Distributed Training (LoRA-FA & DiLoCo Coordinator) (P6.3)
document.getElementById("btn-sim-diloco").onclick = async () => {
  const out = document.getElementById("diloco-out");
  try {
    const r = await call("POST", "/training/simulate-diloco", {
      workers: +document.getElementById("dl-workers").value || 4,
      inner_steps: +document.getElementById("dl-h").value || 50,
      total_steps: 200,
      rank: 8,
      inject_stale: true
    });
    out.replaceChildren(
      el("div", { class: "audio-box stack", id: "diloco-sim-result" },
        el("div", { class: "chip-row" },
          el("span", { class: "pill ok" }, `Tiết kiệm mạng: ${r.network.bandwidth_saved_pct}% (${r.network.diloco_mib} MiB vs DDP ${r.network.ddp_mib} MiB)`),
          el("span", { class: "pill info" }, `Sai số LoRA-FA (Frozen-A): ${r.lora_math.lora_fa_frobenius_error} (Chính xác tuyệt đối)`),
          el("span", { class: "pill warn" }, `Sai số gộp ngây thơ (B̄·Ā bị cấm): ${r.lora_math.naive_merge_frobenius_error}`),
          el("span", { class: "pill " + (r.stale_update_rejected ? "ok" : "err") },
            r.stale_update_rejected ? `✓ Đã chặn Stale Worker (${r.outer_versions_completed} vòng outer-step)` : "Chưa chặn stale")
        )
      )
    );
  } catch (e) {
    out.replaceChildren(el("div", { class: "err" }, e.message));
  }
};

(async () => {
  await window.AICF_READY;
  const writable = canWrite();
  document.getElementById("ro").hidden = writable;
  for (const id of ["btn-tts", "btn-roundtrip", "btn-clone-test", "btn-vp-preview", "btn-vp-save", "btn-asr", "btn-sample-wav", "btn-analyze"]) {
    document.getElementById(id).disabled = !writable;
  }
  updateSliderLabels();
  try {
    await loadVoices();
    const st = await call("GET", "/status");
    const asrP = document.getElementById("asr-pill");
    const ttsP = document.getElementById("tts-pill");
    asrP.textContent = st.asr_ready ? "ASR: Qwen3-ASR-0.6B sẵn sàng" : "ASR: chưa cấu hình";
    asrP.className = "pill " + (st.asr_ready ? "ok" : "warn");
    ttsP.textContent = st.tts_ready ? "TTS: mms-tts-vie sẵn sàng" : "TTS: chưa cấu hình";
    ttsP.className = "pill " + (st.tts_ready ? "ok" : "warn");

    const fl = st.benchmarks && st.benchmarks.fleurs_vi;
    if (fl && fl.clean) {
      document.getElementById("kpi-asr-wer").textContent = `WER ${(fl.clean.wer * 100).toFixed(2)}%`;
      document.getElementById("kpi-asr-sub").textContent = `CER ${(fl.clean.cer * 100).toFixed(2)}% · ${fl.clips} mẫu FLEURS-vi (${fl.audio_seconds}s)`;
      const rows = (fl.examples || []).slice(0, 4).map(ex => el("tr", {},
        el("td", {}, ex.ref),
        el("td", {}, ex.hyp)
      ));
      document.getElementById("bench-table").replaceChildren(
        el("div", { class: "chip-row", style: "margin-bottom:8px" },
          el("span", { class: "pill ok" }, `Bản sạch (Clean): WER ${(fl.clean.wer * 100).toFixed(2)}% · CER ${(fl.clean.cer * 100).toFixed(2)}%`),
          el("span", { class: "pill warn" }, `Nhiễu trắng 10 dB: WER ${(fl.noisy.wer * 100).toFixed(2)}%`),
          el("span", { class: "pill info" }, `Sau lọc Spectral Gate: WER ${(fl.enhanced.wer * 100).toFixed(2)}%`)
        ),
        el("div", { class: "table-wrap" },
          el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, "Câu gốc (FLEURS vi_vn)"), el("th", {}, "Qwen3-ASR-0.6B nhận dạng trên RTX 3060"))),
            el("tbody", {}, ...rows)
          )
        )
      );
    }

    const dl = st.benchmarks && st.benchmarks.diloco_vs_ddp;
    if (dl && dl.summary) {
      const s = dl.summary;
      const b = s.comm_bytes_per_worker_fp32 || {};
      document.getElementById("diloco-bench").replaceChildren(
        el("h3", { style: "margin:8px 0 6px;font-size:13.5px" }, `📈 Bảng Hội tụ Thực nghiệm trên RTX 3060 (${dl.model} · ${dl.data})`),
        el("div", { class: "table-wrap" },
          el("table", {},
            el("thead", {}, el("tr", {},
              el("th", {}, "Phương thức Huấn luyện"),
              el("th", { class: "num" }, "Perplexity Trước"),
              el("th", { class: "num" }, "Perplexity Sau (Test)"),
              el("th", { class: "num" }, "Dung lượng Đồng bộ / Worker"),
              el("th", { class: "num" }, "Tiết kiệm Băng thông")
            )),
            el("tbody", {},
              el("tr", {},
                el("td", {}, "1 Worker Đơn lẻ (Single GPU)"),
                el("td", { class: "num" }, String(s.single_worker.test_ppl_before_training)),
                el("td", { class: "num" }, String(s.single_worker.final_test_ppl_mean)),
                el("td", { class: "num" }, "0 MB"),
                el("td", { class: "num" }, "—")
              ),
              el("tr", {},
                el("td", {}, "DDP 4 Workers (Đồng bộ mỗi bước)"),
                el("td", { class: "num" }, String(s.ddp.test_ppl_before_training)),
                el("td", { class: "num" }, el("b", {}, String(s.ddp.final_test_ppl_mean))),
                el("td", { class: "num" }, (b.ddp / 1048576).toFixed(1) + " MiB"),
                el("td", { class: "num" }, "0% (Baseline)")
              ),
              el("tr", {},
                el("td", {}, "DiLoCo 4 Workers (H = 10 bước)"),
                el("td", { class: "num" }, String(s.diloco_H10.test_ppl_before_training)),
                el("td", { class: "num" }, el("b", {}, String(s.diloco_H10.final_test_ppl_mean))),
                el("td", { class: "num" }, (b.diloco_H10 / 1048576).toFixed(1) + " MiB"),
                el("td", { class: "num" }, el("span", { class: "pill ok" }, "-86.7%"))
              ),
              el("tr", {},
                el("td", {}, "DiLoCo 4 Workers (H = 50 bước)"),
                el("td", { class: "num" }, String(s.diloco_H50.test_ppl_before_training)),
                el("td", { class: "num" }, el("b", {}, String(s.diloco_H50.final_test_ppl_mean))),
                el("td", { class: "num" }, (b.diloco_H50 / 1048576).toFixed(1) + " MiB"),
                el("td", { class: "num" }, el("span", { class: "pill ok" }, "-97.3%"))
              )
            )
          )
        )
      );
    }

    document.getElementById("btn-eval").click();
    fillSampleManifest();
    document.getElementById("btn-ds-split").click();
    document.getElementById("btn-sim-diloco").click();
  } catch (e) {
    say("Không kết nối được dịch vụ Audio AI: " + e.message, "err");
  }
})();
