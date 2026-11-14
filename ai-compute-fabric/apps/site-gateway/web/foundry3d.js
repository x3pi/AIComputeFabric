// Trình xem WebGL2 .glb v2 nội bộ + Điều khiển AICF 3D Asset Foundry (tuân thủ CSP script-src 'self').
(function () {
  "use strict";

  const $ = id => document.getElementById(id);
  const state = {
    jobs: [],
    selectedJobId: "",
    meshes: [],
    boundsCenter: [0, 0, 0],
    boundsRadius: 1.0,
    yaw: 0.65,
    pitch: 0.42,
    zoom: 2.6,
    wireframe: false,
    layerFilter: "lod0",
    animT: 0.0,
    playing: false,
    hasAnimation: false,
    gl: null,
    prog: null,
  };

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    const tok = typeof store !== "undefined" ? store.get("aicf_admin") : "";
    if (tok) h["Authorization"] = "Bearer " + tok;
    return h;
  }

  async function apiFetch(url, opts) {
    const o = Object.assign({ headers: authHeaders() }, opts || {});
    if (opts && opts.headers) {
      o.headers = Object.assign(authHeaders(), opts.headers);
    }
    const resp = await fetch(url, o);
    return resp;
  }

  // ---- BỘ ĐỌC NHỊ PHÂN GLB v2 + WEBGL2 ----
  function initGL() {
    const canvas = $("glb-canvas");
    const gl = canvas.getContext("webgl2", { antialias: true });
    if (!gl) return;
    state.gl = gl;

    const vsSrc = `#version 300 es
    precision highp float;
    in vec3 aPos;
    in vec3 aNorm;
    uniform mat4 uMVP;
    uniform mat4 uModel;
    out vec3 vN;
    void main() {
      vN = mat3(uModel) * aNorm;
      gl_Position = uMVP * vec4(aPos, 1.0);
    }`;

    const fsSrc = `#version 300 es
    precision highp float;
    in vec3 vN;
    uniform vec4 uColor;
    uniform float uWire;
    out vec4 fragColor;
    void main() {
      if (uWire > 0.5) {
        fragColor = vec4(0.38, 0.85, 0.98, 1.0);
        return;
      }
      vec3 n = normalize(vN);
      vec3 l1 = normalize(vec3(0.6, 0.8, 0.5));
      vec3 l2 = normalize(vec3(-0.5, 0.3, -0.7));
      float d = max(dot(n, l1), 0.0) * 0.72 + max(dot(n, l2), 0.0) * 0.25 + 0.22;
      fragColor = vec4(uColor.rgb * d, uColor.a);
    }`;

    function compile(type, src) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      return sh;
    }
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, vsSrc));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fsSrc));
    gl.linkProgram(prog);
    state.prog = prog;
    renderLoop();
  }

  function mat4Identity() {
    return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
  }
  function mat4Mul(a, b) {
    const out = new Float32Array(16);
    for (let c = 0; c < 4; c++) {
      for (let r = 0; r < 4; r++) {
        out[c * 4 + r] =
          a[r] * b[c * 4] +
          a[4 + r] * b[c * 4 + 1] +
          a[8 + r] * b[c * 4 + 2] +
          a[12 + r] * b[c * 4 + 3];
      }
    }
    return out;
  }
  function mat4Persp(fovy, aspect, near, far) {
    const f = 1.0 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  }
  function mat4LookAt(eye, ctr, up) {
    const zx = eye[0] - ctr[0], zy = eye[1] - ctr[1], zz = eye[2] - ctr[2];
    let len = Math.hypot(zx, zy, zz) || 1;
    const z0 = zx / len, z1 = zy / len, z2 = zz / len;
    const xx = up[1] * z2 - up[2] * z1, xy = up[2] * z0 - up[0] * z2, xz = up[0] * z1 - up[1] * z0;
    len = Math.hypot(xx, xy, xz) || 1;
    const x0 = xx / len, x1 = xy / len, x2 = xz / len;
    const y0 = z1 * x2 - z2 * x1, y1 = z2 * x0 - z0 * x2, y2 = z0 * x1 - z1 * x0;
    return new Float32Array([
      x0, y0, z0, 0,
      x1, y1, z1, 0,
      x2, y2, z2, 0,
      -(x0 * eye[0] + x1 * eye[1] + x2 * eye[2]),
      -(y0 * eye[0] + y1 * eye[1] + y2 * eye[2]),
      -(z0 * eye[0] + z1 * eye[1] + z2 * eye[2]),
      1,
    ]);
  }
  function mat4FromTRS(t, r, s) {
    const [x, y, z, w] = r || [0, 0, 0, 1];
    const [sx, sy, sz] = s || [1, 1, 1];
    const [tx, ty, tz] = t || [0, 0, 0];
    const x2 = x + x, y2 = y + y, z2 = z + z;
    const xx = x * x2, xy = x * y2, xz = x * z2;
    const yy = y * y2, yz = y * z2, zz = z * z2;
    const wx = w * x2, wy = w * y2, wz = w * z2;
    return new Float32Array([
      (1 - (yy + zz)) * sx, (xy + wz) * sx, (xz - wy) * sx, 0,
      (xy - wz) * sy, (1 - (xx + zz)) * sy, (yz + wx) * sy, 0,
      (xz + wy) * sz, (yz - wx) * sz, (1 - (xx + yy)) * sz, 0,
      tx, ty, tz, 1,
    ]);
  }

  function accessorData(gltf, binBytes, accIdx) {
    const acc = gltf.accessors[accIdx];
    const bv = gltf.bufferViews[acc.bufferView];
    const byteOffset = (bv.byteOffset || 0) + (acc.byteOffset || 0);
    const numComp = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 }[acc.type] || 1;
    const count = acc.count * numComp;
    const buf = binBytes.buffer;
    const absOff = binBytes.byteOffset + byteOffset;
    if (acc.componentType === 5126) return new Float32Array(buf, absOff, count);
    if (acc.componentType === 5123) return new Uint16Array(buf, absOff, count);
    if (acc.componentType === 5125) return new Uint32Array(buf, absOff, count);
    if (acc.componentType === 5121) return new Uint8Array(buf, absOff, count);
    return new Float32Array(0);
  }

  function classifyLayer(name) {
    const n = (name || "").toLowerCase();
    if (n.startsWith("ucx_") || n.includes("collider")) return "ucx";
    if (n.includes("_lod2")) return "lod2";
    if (n.includes("_lod1")) return "lod1";
    if (n.includes("_highpoly")) return "high";
    return "lod0";
  }

  function loadGLBBytes(arrayBuf) {
    const dv = new DataView(arrayBuf);
    if (dv.byteLength < 20 || dv.getUint32(0, true) !== 0x46546c67) {
      throw new Error("Tệp không phải GLB v2 hợp lệ");
    }
    let off = 12;
    let gltf = null;
    let bin = new Uint8Array(0);
    while (off + 8 <= dv.byteLength) {
      const len = dv.getUint32(off, true);
      const typ = dv.getUint32(off + 4, true);
      off += 8;
      if (typ === 0x4e4f534a) {
        const txt = new TextDecoder().decode(new Uint8Array(arrayBuf, off, len));
        gltf = JSON.parse(txt);
      } else if (typ === 0x004e4942) {
        bin = new Uint8Array(arrayBuf, off, len);
      }
      off += len;
    }
    if (!gltf || !gltf.nodes) throw new Error("GLB thiếu cảnh JSON");

    // Xóa VAO cũ
    const gl = state.gl;
    for (const m of state.meshes) {
      if (m.vao) gl.deleteVertexArray(m.vao);
    }
    state.meshes = [];

    // Đọc animation đơn giản cho node (translation / rotation)
    const animMap = {};
    let hasAnim = false;
    if (gltf.animations && gltf.animations.length > 0) {
      const anim = gltf.animations[0];
      for (const ch of anim.channels || []) {
        const samp = anim.samplers[ch.sampler];
        const times = accessorData(gltf, bin, samp.input);
        const vals = accessorData(gltf, bin, samp.output);
        if (times.length > 0 && ch.target && ch.target.node !== undefined) {
          hasAnim = true;
          animMap[ch.target.node] = animMap[ch.target.node] || {};
          animMap[ch.target.node][ch.target.path] = { times, vals };
        }
      }
    }
    state.hasAnimation = hasAnim;

    // Duyệt cây node
    const parentOf = {};
    (gltf.nodes || []).forEach((n, idx) => {
      (n.children || []).forEach(c => { parentOf[c] = idx; });
    });

    function computeNodeMatrix(idx, tNorm) {
      const n = gltf.nodes[idx];
      let tr = n.translation ? Array.from(n.translation) : [0, 0, 0];
      let rot = n.rotation ? Array.from(n.rotation) : [0, 0, 0, 1];
      const sc = n.scale ? Array.from(n.scale) : [1, 1, 1];
      const anim = animMap[idx];
      if (anim) {
        if (anim.translation && anim.translation.times.length > 0) {
          const { times, vals } = anim.translation;
          const k = Math.min(times.length - 1, Math.floor(tNorm * (times.length - 1)));
          tr = [vals[k * 3], vals[k * 3 + 1], vals[k * 3 + 2]];
        }
        if (anim.rotation && anim.rotation.times.length > 0) {
          const { times, vals } = anim.rotation;
          const k = Math.min(times.length - 1, Math.floor(tNorm * (times.length - 1)));
          rot = [vals[k * 4], vals[k * 4 + 1], vals[k * 4 + 2], vals[k * 4 + 3]];
        }
      }
      const local = n.matrix ? new Float32Array(n.matrix) : mat4FromTRS(tr, rot, sc);
      if (parentOf[idx] !== undefined) {
        return mat4Mul(computeNodeMatrix(parentOf[idx], tNorm), local);
      }
      return local;
    }

    let minX = 1e9, minY = 1e9, minZ = 1e9, maxX = -1e9, maxY = -1e9, maxZ = -1e9;
    (gltf.nodes || []).forEach((node, nIdx) => {
      if (node.mesh === undefined) return;
      const mesh = gltf.meshes[node.mesh];
      const layer = classifyLayer(node.name || mesh.name);
      for (const prim of mesh.primitives || []) {
        if (!prim.attributes || prim.attributes.POSITION === undefined) continue;
        const pos = accessorData(gltf, bin, prim.attributes.POSITION);
        const norm = prim.attributes.NORMAL !== undefined
          ? accessorData(gltf, bin, prim.attributes.NORMAL)
          : new Float32Array(pos.length);
        const idxArr = prim.indices !== undefined
          ? accessorData(gltf, bin, prim.indices)
          : null;

        let color = [0.76, 0.72, 0.66, 1.0];
        if (prim.material !== undefined && gltf.materials && gltf.materials[prim.material]) {
          const pbr = gltf.materials[prim.material].pbrMetallicRoughness;
          if (pbr && pbr.baseColorFactor) color = pbr.baseColorFactor;
        }
        if (layer === "ucx") color = [0.2, 0.85, 0.45, 0.6];

        const vao = gl.createVertexArray();
        gl.bindVertexArray(vao);
        const pb = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, pb);
        gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(0);
        gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);

        const nb = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, nb);
        gl.bufferData(gl.ARRAY_BUFFER, norm, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(1);
        gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);

        let ib = null, idxCount = pos.length / 3, idxType = 0;
        let lineIb = null, lineCount = 0;
        if (idxArr) {
          ib = gl.createBuffer();
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
          gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idxArr, gl.STATIC_DRAW);
          idxCount = idxArr.length;
          idxType = idxArr instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;

          const lines = new Uint32Array(Math.floor(idxArr.length / 3) * 6);
          let li = 0;
          for (let i = 0; i + 2 < idxArr.length; i += 3) {
            const a = idxArr[i], b = idxArr[i + 1], c = idxArr[i + 2];
            lines[li++] = a; lines[li++] = b;
            lines[li++] = b; lines[li++] = c;
            lines[li++] = c; lines[li++] = a;
          }
          lineIb = gl.createBuffer();
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, lineIb);
          gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, lines, gl.STATIC_DRAW);
          lineCount = lines.length;
        }

        const worldMat = computeNodeMatrix(nIdx, 0);
        for (let i = 0; i < pos.length; i += 3) {
          const vx = pos[i], vy = pos[i + 1], vz = pos[i + 2];
          const wx = worldMat[0] * vx + worldMat[4] * vy + worldMat[8] * vz + worldMat[12];
          const wy = worldMat[1] * vx + worldMat[5] * vy + worldMat[9] * vz + worldMat[13];
          const wz = worldMat[2] * vx + worldMat[6] * vy + worldMat[10] * vz + worldMat[14];
          if (wx < minX) minX = wx; if (wx > maxX) maxX = wx;
          if (wy < minY) minY = wy; if (wy > maxY) maxY = wy;
          if (wz < minZ) minZ = wz; if (wz > maxZ) maxZ = wz;
        }
        state.meshes.push({
          vao, ib, idxCount, idxType, lineIb, lineCount,
          color, layer, nodeIdx: nIdx, getMat: t => computeNodeMatrix(nIdx, t),
        });
      }
    });
    if (maxX > minX) {
      state.boundsCenter = [(minX + maxX) * 0.5, (minY + maxY) * 0.5, (minZ + maxZ) * 0.5];
      state.boundsRadius = Math.max(0.25, Math.hypot(maxX - minX, maxY - minY, maxZ - minZ) * 0.5);
    }
  }

  function renderLoop() {
    const gl = state.gl;
    if (!gl || !state.prog) return;
    if (state.playing) {
      state.animT = (state.animT + 0.012) % 1.0;
      $("v-anim-t").value = state.animT.toFixed(2);
    }
    const c = $("glb-canvas");
    gl.viewport(0, 0, c.width, c.height);
    gl.clearColor(0.08, 0.10, 0.14, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);

    const dist = state.boundsRadius * state.zoom;
    const cx = state.boundsCenter[0], cy = state.boundsCenter[1], cz = state.boundsCenter[2];
    const ex = cx + dist * Math.cos(state.pitch) * Math.sin(state.yaw);
    const ey = cy + dist * Math.sin(state.pitch);
    const ez = cz + dist * Math.cos(state.pitch) * Math.cos(state.yaw);
    const proj = mat4Persp(0.75, c.width / c.height, 0.02, 200.0);
    const view = mat4LookAt([ex, ey, ez], [cx, cy, cz], [0, 1, 0]);
    const vp = mat4Mul(proj, view);

    gl.useProgram(state.prog);
    const uMVP = gl.getUniformLocation(state.prog, "uMVP");
    const uModel = gl.getUniformLocation(state.prog, "uModel");
    const uColor = gl.getUniformLocation(state.prog, "uColor");
    const uWire = gl.getUniformLocation(state.prog, "uWire");

    for (const m of state.meshes) {
      if (state.layerFilter !== "all" && m.layer !== state.layerFilter) continue;
      const model = m.getMat(state.animT);
      const mvp = mat4Mul(vp, model);
      gl.bindVertexArray(m.vao);
      gl.uniformMatrix4fv(uMVP, false, mvp);
      gl.uniformMatrix4fv(uModel, false, model);
      gl.uniform4fv(uColor, new Float32Array(m.color));
      gl.uniform1f(uWire, 0.0);
      if (m.ib) {
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.ib);
        gl.drawElements(gl.TRIANGLES, m.idxCount, m.idxType, 0);
      } else {
        gl.drawArrays(gl.TRIANGLES, 0, m.idxCount);
      }
      if (state.wireframe && m.lineIb) {
        gl.uniform1f(uWire, 1.0);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.lineIb);
        gl.drawElements(gl.LINES, m.lineCount, gl.UNSIGNED_INT, 0);
      }
    }
    requestAnimationFrame(renderLoop);
  }

  // ---- API & UI ----
  async function refreshJobs() {
    try {
      const st = await apiFetch("/api/foundry3d/v1/status");
      if (st.status === 503) {
        $("f3d-health").textContent = "Đang tắt (-foundry3d-enabled=false)";
        $("f3d-health").className = "pill warn";
        return;
      }
      if (!st.ok) {
        $("f3d-health").textContent = "HTTP " + st.status;
        return;
      }
      const s = await st.json();
      if (s.readonly_gallery && $("create-box")) $("create-box").style.display = "none";
      $("f3d-health").textContent = `Mode ${s.mode} · Active ${s.tenant_active_jobs}`;
      $("f3d-health").className = "pill ok";

      const jr = await apiFetch("/api/foundry3d/v1/jobs");
      if (!jr.ok) return;
      const data = await jr.json();
      state.jobs = data.jobs || [];
      renderJobsList();
    } catch (e) {
      $("f3d-health").textContent = "Mất kết nối";
    }
  }

  // job_id dạng "<bộ>_m1_<mã bài>_<mốc thời gian>" -> tên ngắn đọc được
  function parseJob(j) {
    const m = /^(.*?)_m1_(.+?)_(\d{9,})$/.exec(j.job_id);
    const raw = m ? m[2] : j.job_id;
    const name = raw.replace(/^[a-z]\d{2}_/, "").replace(/_/g, " ");
    const code = (/^([a-z]\d{2})_/.exec(raw) || [])[1] || "";
    const suite = m ? m[1].replace(/_/g, " ") : "";
    const when = m ? new Date(parseInt(m[3], 10) * 1000) : (j.created_at ? new Date(j.created_at * 1000) : null);
    return { name: (code ? code.toUpperCase() + " · " : "") + name, suite, when };
  }

  // completed | working | interrupted | failed | review ("chưa đạt" thật: có báo cáo/lỗi cổng)
  function jobKind(j) {
    if (j.status === "completed") return "completed";
    if (WORKING.includes(j.status)) return "working";
    if (j.interrupted) return "interrupted";
    if (j.status === "failed") return "failed";
    return "review";
  }

  function renderJobsList() {
    const box = $("jobs-list");
    box.innerHTML = "";
    const q = (($("jobs-q") || {}).value || "").trim().toLowerCase();
    const flt = ($("jobs-filter") || {}).value || "all";
    const fltKind = { completed: "completed", needs_human_review: "review", running: "working", interrupted: "interrupted" }[flt];
    const list = state.jobs.filter(j => (flt === "all" || jobKind(j) === fltKind) &&
      (!q || (j.job_id + " " + (j.prompt || "")).toLowerCase().includes(q)));
    if ($("jobs-count")) $("jobs-count").textContent = `(${list.length}/${state.jobs.length})`;
    if (list.length === 0) {
      box.innerHTML = "<div class='mut'>Không có job nào khớp.</div>";
      return;
    }
    for (const j of list) {
      const p = parseJob(j);
      const kind = jobKind(j);
      const ok = kind === "completed";
      const working = kind === "working";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "job-item" + (j.job_id === state.selectedJobId ? " sel" : "");
      btn.title = j.job_id + "\n" + (j.prompt || "");
      const top = document.createElement("div");
      top.className = "job-top";
      const nm = document.createElement("span");
      nm.className = "job-name";
      nm.textContent = p.name;
      const bd = document.createElement("span");
      bd.className = "job-badge " + (ok ? "ok" : working ? "run" : kind === "interrupted" ? "idle" : "bad");
      bd.textContent = { completed: "Đạt", working: "Đang chạy…", interrupted: "Bị gián đoạn", failed: "Lỗi", review: "Chưa đạt" }[kind];
      if (kind === "interrupted") btn.title += "\nChạy bị ngắt giữa chừng (không có báo cáo QA) — không phải kết quả thật của bài này.";
      top.append(nm, bd);
      const sub = document.createElement("div");
      sub.className = "job-sub";
      const parts = [];
      if (p.suite) parts.push(p.suite);
      if (j.repair_count) parts.push("sửa " + j.repair_count + " lần");
      if (p.when) parts.push(p.when.toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }));
      sub.textContent = parts.join(" · ");
      btn.append(top, sub);
      if (j.prompt && j.prompt !== j.job_id) {
        const pr = document.createElement("div");
        pr.className = "job-prompt";
        pr.textContent = j.prompt;
        btn.appendChild(pr);
      }
      if (kind === "review" && j.error) {
        const why = document.createElement("div");
        why.className = "job-why";
        why.textContent = String(j.error).slice(0, 140);
        btn.appendChild(why);
      }
      btn.addEventListener("click", () => { selectJob(j.job_id); renderJobsList(); });
      box.appendChild(btn);
    }
  }

  async function selectJob(jobId) {
    state.selectedJobId = jobId;
    const r = await apiFetch(`/api/foundry3d/v1/jobs/${encodeURIComponent(jobId)}`);
    if (!r.ok) return;
    const job = await r.json();
    const arts = job.artifacts || [];
    const dl = $("dl-links");
    dl.innerHTML = "";
    for (const fn of ["asset.glb", "asset.fbx", "source.blend", "qa_report.json"]) {
      if (arts.some(a => a.filename === fn)) {
        const a = document.createElement("button");
        a.type = "button";
        a.className = "secondary";
        a.textContent = "⬇ " + fn;
        a.addEventListener("click", async () => {
          const resp = await apiFetch(`/api/foundry3d/v1/jobs/${encodeURIComponent(jobId)}/artifacts/${fn}`);
          if (!resp.ok) return;
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = `${jobId}_${fn}`;
          link.click();
          URL.revokeObjectURL(url);
        });
        dl.appendChild(a);
      }
    }

    if (arts.some(a => a.filename === "asset.glb")) {
      const gr = await apiFetch(`/api/foundry3d/v1/jobs/${encodeURIComponent(jobId)}/artifacts/asset.glb`);
      if (gr.ok) loadGLBBytes(await gr.arrayBuffer());
    }
    if (arts.some(a => a.filename === "qa_report.json")) {
      const qr = await apiFetch(`/api/foundry3d/v1/jobs/${encodeURIComponent(jobId)}/artifacts/qa_report.json`);
      if (qr.ok) renderQA(await qr.json());
    }
    // Tải tuần tự 6 góc ảnh để không vượt trần inflight
    const vg = $("views-grid");
    vg.innerHTML = "";
    for (const a of arts.filter(x => x.filename.startsWith("view_") && x.filename.endsWith(".png"))) {
      const ir = await apiFetch(`/api/foundry3d/v1/jobs/${encodeURIComponent(jobId)}/artifacts/${a.filename}`);
      if (!ir.ok) continue;
      const url = URL.createObjectURL(await ir.blob());
      const card = document.createElement("div");
      card.className = "view-card";
      const img = document.createElement("img");
      img.src = url;
      const cap = document.createElement("div");
      cap.textContent = a.filename;
      card.append(img, cap);
      vg.appendChild(card);
    }
  }

  function renderQA(qa) {
    const g = qa.geometry || {};
    $("qa-summary").textContent =
      `Tris LOD0: ${g.tris_lod0 || 0} | LOD1: ${g.tris_lod1 || 0} | LOD2: ${g.tris_lod2 || 0} | ` +
      `Collider: ${g.tris_collider || 0} | glTF Errors: ${(qa.gltf_validator || {}).numErrors || 0} | ` +
      `Repair loops: ${qa.repair_iteration || 0} | RSS: ${qa.peak_rss_mb || 0} MiB`;
    const box = $("qa-gates");
    box.innerHTML = "";
    for (const gate of g.gates || []) {
      const row = document.createElement("div");
      row.className = "gate-row";
      row.innerHTML = `<span><b>${gate.gate}</b>: ${gate.summary || ""}</span>`;
      const pill = document.createElement("span");
      pill.className = gate.passed ? "pill ok" : "pill bad";
      pill.textContent = gate.passed ? "ĐẠT" : "LỖI";
      row.appendChild(pill);
      box.appendChild(row);
    }
  }

  async function submitJob() {
    const payload = {
      prompt: $("f-prompt").value.trim(),
      archetype: $("f-archetype").value,
      target_engine: $("f-engine").value,
      poly_budget_lod0: parseInt($("f-budget").value, 10) || 5000,
      dimensions_m: [
        parseFloat($("f-dx").value) || 1.0,
        parseFloat($("f-dy").value) || 1.0,
        parseFloat($("f-dz").value) || 1.0,
      ],
      enable_vlm_critic: $("f-vlm").checked,
      bake_pbr: $("f-bake").checked,
    };
    $("f3d-msg").textContent = "Đang gửi job 3D...";
    const r = await apiFetch("/api/foundry3d/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      $("f3d-msg").textContent = `Lỗi HTTP ${r.status}: ${await r.text()}`;
      return;
    }
    const rec = await r.json();
    $("f3d-msg").textContent = `Đã tạo job ${rec.job_id}`;
    await refreshJobs();
    watchJob(rec.job_id);
  }

  // Thăm dò trạng thái job đang chạy tới khi kết thúc, rồi tự mở mô hình
  const WORKING = ["queued", "running", "queued_low_mem"];
  async function watchJob(jobId) {
    state.selectedJobId = jobId;
    for (let i = 0; i < 400; i++) {
      let job = null;
      try {
        const r = await apiFetch(`/api/foundry3d/v1/jobs/${encodeURIComponent(jobId)}`);
        if (r.ok) job = await r.json();
      } catch (e) { /* thử lại */ }
      if (job) {
        $("f3d-msg").textContent = `Job ${jobId}: ${job.status}${job.current_stage && job.current_stage !== job.status ? " · " + job.current_stage : ""}` +
          (job.repair_count ? ` · đã sửa ${job.repair_count} lần` : "");
        await refreshJobs();
        if (!WORKING.includes(job.status)) {
          await selectJob(jobId);
          return;
        }
      }
      await new Promise(res => setTimeout(res, 3000));
    }
  }

  window.addEventListener("DOMContentLoaded", () => {
    initGL();
    const c = $("glb-canvas");
    let drag = false, lx = 0, ly = 0;
    c.addEventListener("mousedown", e => { drag = true; lx = e.clientX; ly = e.clientY; });
    window.addEventListener("mouseup", () => { drag = false; });
    window.addEventListener("mousemove", e => {
      if (!drag) return;
      state.yaw -= (e.clientX - lx) * 0.01;
      state.pitch = Math.max(-1.4, Math.min(1.4, state.pitch + (e.clientY - ly) * 0.01));
      lx = e.clientX; ly = e.clientY;
    });
    c.addEventListener("wheel", e => {
      e.preventDefault();
      state.zoom = Math.max(0.8, Math.min(10.0, state.zoom + (e.deltaY > 0 ? 0.2 : -0.2)));
    }, { passive: false });

    $("v-wire").addEventListener("change", e => { state.wireframe = e.target.checked; });
    $("v-layer").addEventListener("change", e => { state.layerFilter = e.target.value; });
    $("v-anim-t").addEventListener("input", e => { state.animT = parseFloat(e.target.value) || 0; });
    $("v-play").addEventListener("click", () => { state.playing = !state.playing; });
    $("btn-submit").addEventListener("click", submitJob);
    $("btn-refresh").addEventListener("click", refreshJobs);
    $("btn-refresh2").addEventListener("click", refreshJobs);
    $("jobs-q").addEventListener("input", renderJobsList);
    $("jobs-filter").addEventListener("change", renderJobsList);
    refreshJobs();
  });
})();
