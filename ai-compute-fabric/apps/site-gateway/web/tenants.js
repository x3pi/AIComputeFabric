const msg = t => { document.getElementById("msg").textContent = t; };

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; } catch (e) {}
  const ta = document.createElement("textarea");
  ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
  ta.remove(); return ok;
}

function selectAll(el) {
  const r = document.createRange();
  r.selectNodeContents(el);
  const s = getSelection();
  s.removeAllRanges();
  s.addRange(r);
}

async function showKey(k) {
  const kt = document.getElementById("keytext"), cp = document.getElementById("copy");
  kt.textContent = k;
  document.getElementById("newkey").hidden = false;
  kt.onclick = () => selectAll(kt);
  const done = async () => {
    const ok = await copyText(k);
    cp.textContent = ok ? "Đã sao chép ✓" : "Không tự sao chép được — bấm vào khóa rồi nhấn Ctrl+C";
    if (!ok) selectAll(kt);
  };
  cp.textContent = "Sao chép khóa";
  cp.onclick = done;
  done();
  document.getElementById("dismiss").onclick = () => {
    kt.textContent = "";
    document.getElementById("newkey").hidden = true;
  };
}

async function patch(id, body) {
  const r = await api("/api/tenants/" + encodeURIComponent(id), {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  if (!r.ok) msg("Lỗi: " + (await r.text()));
  else msg("Đã cập nhật chính sách tenant.");
  load();
}

async function load() {
  const [r, stRes] = await Promise.all([api("/api/tenants"), api("/api/stats")]);
  if (!r || !r.ok) return;
  const list = await r.json();
  let servedMap = {}, routerStats = {};
  if (stRes && stRes.ok) {
    const st = await stRes.json();
    routerStats = st.router || {};
    servedMap = routerStats.served_by_tenant || {};
  }

  let activeCount = 0, weightSum = 0;
  for (const t of list) {
    if (!t.disabled) {
      activeCount++;
      weightSum += (+t.weight || 1);
    }
  }
  document.getElementById("kpi-t-active").textContent = String(activeCount);
  document.getElementById("kpi-t-total").textContent = `Tổng cộng ${list.length} tenant đã đăng ký`;
  document.getElementById("kpi-t-weight").textContent = weightSum.toFixed(1);
  document.getElementById("kpi-t-served").textContent = String(routerStats.served_total || 0);
  const rejObj = routerStats.rejected || {};
  const rejSum = Object.values(rejObj).reduce((a, b) => a + (+b || 0), 0);
  document.getElementById("kpi-t-rejected").textContent = `${rejSum} bị từ chối do quá tải / hạn mức`;

  const tb = document.getElementById("rows");
  tb.replaceChildren();
  if (!list.length) {
    tb.append(el("tr", {}, el("td", { colspan: 8, class: "mut" }, "Chưa có tenant nào. Tạo tenant đầu tiên ở biểu mẫu phía trên.")));
    return;
  }

  for (const t of list) {
    const sharePct = !t.disabled && weightSum > 0 ? Math.round((100 * (+t.weight || 1)) / weightSum) : 0;
    const shareCell = el("td", { class: "num" },
      el("span", {}, `${sharePct}%`),
      el("span", { class: "share-bar" }, el("span", { style: `width:${sharePct}%` }))
    );

    // IMPORTANT E2E invariant: First <input> in each row inside #rows MUST be the weight input
    const w = el("input", {
      type: "number", min: "0.1", step: "0.1", value: t.weight,
      style: "width:82px", "aria-label": "Trọng số " + t.name
    });
    const m = el("input", {
      type: "number", min: "0", step: "1", value: t.max_inflight,
      style: "width:82px", "aria-label": "Tối đa đồng thời " + t.name
    });
    const save = el("button", {
      class: "sm primary",
      onclick: () => patch(t.id, { weight: +w.value, max_inflight: +m.value })
    }, "Lưu");
    const tog = el("button", {
      class: "sm",
      onclick: () => patch(t.id, { disabled: !t.disabled })
    }, t.disabled ? "Bật lại" : "Vô hiệu hóa");
    const rot = el("button", {
      class: "sm",
      onclick: async () => {
        if (!confirm("Đổi khóa của " + t.name + "? Khóa cũ sẽ ngừng hoạt động ngay.")) return;
        const res = await api("/api/tenants/" + encodeURIComponent(t.id) + "/rotate-key", { method: "POST" });
        if (res.ok) showKey((await res.json()).api_key);
        else msg("Lỗi: " + (await res.text()));
      }
    }, "Đổi khóa");
    const del = el("button", {
      class: "sm ghost err",
      title: "Xóa vĩnh viễn tenant và khóa API (không hoàn tác). Dùng 'Vô hiệu hóa' nếu có thể cần lại.",
      onclick: async () => {
        const typed = prompt('Xóa VĨNH VIỄN tenant "' + t.name + '"? Khóa API ngừng hoạt động ngay, không hoàn tác.\nGõ lại tên tenant để xác nhận:');
        if (typed === null) return;
        if (typed.trim() !== t.name) { msg("Tên không khớp, chưa xóa."); return; }
        const res = await api("/api/tenants/" + encodeURIComponent(t.id), { method: "DELETE" });
        msg(res.ok ? 'Đã xóa tenant "' + t.name + '". Dữ liệu Studio của tenant (nếu có) vẫn còn đến khi xóa ở trang AI Studio.' : "Lỗi: " + (await res.text()));
        load();
      }
    }, "Xóa");

    const approveBtn = t.pending ? el("button", {
      class: "sm primary approve-tenant-btn",
      "data-tid": t.id,
      onclick: () => patch(t.id, { pending: false })
    }, "✓ Duyệt") : null;

    const servedCount = servedMap[t.id] || servedMap[t.name] || 0;
    const statusPillClass = t.disabled ? "err" : (t.pending ? "warn" : "ok");
    const statusPillText = t.disabled ? "đã vô hiệu" : (t.pending ? "⏳ chờ duyệt" : "hoạt động");
    const actionRow = approveBtn ? [approveBtn, save, tog, rot, del] : [save, tog, rot, del];
    tb.append(
      el("tr", {},
        el("td", { style: "font-weight:600" }, t.name),
        el("td", { class: "mut mono" }, t.id),
        shareCell,
        el("td", { class: "num" }, w),
        el("td", { class: "num" }, m),
        el("td", { class: "num" }, String(servedCount)),
        el("td", {}, el("span", { class: "pill " + statusPillClass }, statusPillText)),
        el("td", { class: "row" }, ...actionRow)
      )
    );
  }
  loadServerlessTenants(list);
  applyReadonlyIfNotAdmin();
}

function applyReadonlyIfNotAdmin() {
  if (!window.AICF || window.AICF.can.tenants) return;
  document.querySelectorAll("main button, main input, main select, .card button, .card input, .card select").forEach(e => { e.disabled = true; });
}

async function loadServerlessTenants(gatewayList) {
  const slTb = document.getElementById("sl-tenant-rows");
  if (!slTb) return;
  let slData = {};
  try {
    const r = await api("/api/serverless/v1/status");
    if (r && r.ok) slData = await r.json();
  } catch {}
  const usageMap = slData.usage || {};
  const polMap = slData.policies || {};
  const idSet = new Set();
  const rows = [];
  for (const t of (gatewayList || [])) {
    idSet.add(t.id);
    rows.push({ id: t.id, name: t.name });
  }
  for (const tid of Object.keys({ ...polMap, ...usageMap })) {
    if (!idSet.has(tid)) {
      idSet.add(tid);
      rows.push({ id: tid, name: tid });
    }
  }
  slTb.replaceChildren();
  if (!rows.length) {
    slTb.append(el("tr", {}, el("td", { colspan: 9, class: "mut" }, "Chưa có dữ liệu tenant Serverless.")));
    return;
  }
  const canEdit = Boolean(window.AICF && window.AICF.can.tenants);
  for (const item of rows) {
    const tid = item.id;
    const u = usageMap[tid] || { calls: 0, idle_records: 0, gpu_ms: 0, gpu_seconds: 0, vram_mib_s: 0 };
    const p = polMap[tid] || { serverless_enabled: false, gpu_allowed: false, max_vram_mb: 8192, max_concurrency: 4 };

    const cbEn = el("input", { type: "checkbox", class: "sl-pol-enabled", "aria-label": "Bật Serverless " + tid });
    cbEn.checked = !!p.serverless_enabled;
    const cbGpu = el("input", { type: "checkbox", class: "sl-pol-gpu", "aria-label": "Cho phép GPU " + tid });
    cbGpu.checked = !!p.gpu_allowed;
    const inVram = el("input", {
      type: "number", min: "0", step: "256", value: String(p.max_vram_mb ?? 8192),
      class: "sl-pol-vram", style: "width:92px", "aria-label": "VRAM tối đa " + tid
    });
    const inConc = el("input", {
      type: "number", min: "0", step: "1", value: String(p.max_concurrency ?? 4),
      class: "sl-pol-conc", style: "width:76px", "aria-label": "Đồng thời Serverless " + tid
    });
    const btnSave = el("button", {
      type: "button",
      class: "sm primary sl-pol-save",
      "data-tenant": tid,
      onclick: async () => {
        const res = await api(`/api/serverless/v1/policies/${encodeURIComponent(tid)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            serverless_enabled: cbEn.checked,
            gpu_allowed: cbGpu.checked,
            max_vram_mb: +inVram.value,
            max_concurrency: +inConc.value,
          }),
        });
        const m = document.getElementById("sl-tenant-msg");
        if (res && res.ok) {
          m.textContent = `Đã lưu hạn mức Serverless cho ${tid}.`;
          m.className = "pill ok";
        } else {
          m.textContent = `Lỗi lưu hạn mức Serverless (${res ? res.status : "err"}).`;
          m.className = "pill err";
        }
        loadServerlessTenants(gatewayList);
      }
    }, "Lưu hạn mức");

    if (!canEdit) {
      cbEn.disabled = true; cbGpu.disabled = true; inVram.disabled = true; inConc.disabled = true; btnSave.disabled = true;
    }

    slTb.append(
      el("tr", { "data-tenant": tid },
        el("td", {}, el("strong", {}, item.name), item.name !== tid ? el("span", { class: "mut mono", style: "margin-left:6px" }, `(${tid})`) : null),
        el("td", { class: "num mono" }, `${u.calls || 0} lượt · ${u.idle_records || 0} idle`),
        el("td", { class: "num mono sl-gpu-sec" }, `${Number(u.gpu_seconds || 0).toFixed(3)} s (${u.gpu_ms || 0} ms)`),
        el("td", { class: "num mono sl-vram-mibs" }, `${u.vram_mib_s || 0} MiB·s`),
        el("td", {}, cbEn),
        el("td", {}, cbGpu),
        el("td", { class: "num" }, inVram),
        el("td", { class: "num" }, inConc),
        el("td", {}, btnSave)
      )
    );
  }
  applyReadonlyIfNotAdmin();
}

document.getElementById("add").onsubmit = async e => {
  e.preventDefault();
  const r = await api("/api/tenants", {
    method: "POST",
    body: JSON.stringify({
      name: document.getElementById("n").value.trim(),
      weight: +document.getElementById("w").value,
      max_inflight: +document.getElementById("m").value,
    }),
  });
  if (r.status === 201) {
    const d = await r.json();
    showKey(d.api_key);
    msg("Đã tạo tenant " + d.tenant.name + " thành công.");
    document.getElementById("n").value = "";
  } else {
    msg("Lỗi: " + (await r.text()));
  }
  load();
};

load();
window.AICF_READY.then(() => {
  load();
  if (window.AICF.can.tenants) return;
  applyReadonlyIfNotAdmin();
  if (!document.querySelector(".readonly-banner")) {
    const b = el("div", { class: "readonly-banner", role: "status" }, "Chế độ chỉ xem: vai trò " + (window.AICF.role || "chưa xác thực") + " không được tạo/sửa/xóa người dùng hoặc đổi khóa API. Cần vai trò admin.");
    document.body.insertBefore(b, document.body.children[1] || null);
  }
});
