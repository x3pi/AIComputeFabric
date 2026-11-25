// Interactive 3D viewer for STEP meshes and animated Blender GLB scenes.
// Everything is served from this app (/static), nothing is loaded from the internet.
import * as THREE from "./vendor/three.module.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const GLB_MAGIC = 0x46546C67;
const CHUNK_JSON = 0x4E4F534A;
const CHUNK_BIN = 0x004E4942;

const COMP_INFO = {
  5120: [Int8Array, 1, "getInt8", 127],
  5121: [Uint8Array, 1, "getUint8", 255],
  5122: [Int16Array, 2, "getInt16", 32767],
  5123: [Uint16Array, 2, "getUint16", 65535],
  5125: [Uint32Array, 4, "getUint32", 4294967295],
  5126: [Float32Array, 4, "getFloat32", 1],
};
const TYPE_COUNT = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };

function _readAccessor(gltf, binBuf, binOff, binLen, idx) {
  const acc = (gltf.accessors || [])[idx];
  if (!acc) return null;
  const info = COMP_INFO[acc.componentType];
  const nComp = TYPE_COUNT[acc.type];
  if (!info || !nComp) return null;
  const [Typed, bpe, getter, maxVal] = info;
  const bv = (gltf.bufferViews || [])[acc.bufferView || 0];
  if (!bv) return null;
  const relOff = (bv.byteOffset || 0) + (acc.byteOffset || 0);
  const absOff = binOff + relOff;
  const stride = bv.byteStride || (nComp * bpe);
  const count = acc.count || 0;
  let arr;
  if (relOff < 0 || absOff + Math.max(0, count - 1) * stride + nComp * bpe > binOff + binLen) {
    throw new Error("GLB accessor out of bounds");
  }
  if (stride === nComp * bpe) {
    const byteCount = count * nComp * bpe;
    if (absOff % bpe === 0) {
      arr = new Typed(binBuf, absOff, count * nComp);
    } else {
      arr = new Typed(binBuf.slice(absOff, absOff + byteCount));
    }
  } else {
    arr = new Typed(count * nComp);
    const dv = new DataView(binBuf);
    for (let i = 0; i < count; i++) {
      const row = absOff + i * stride;
      for (let c = 0; c < nComp; c++) {
        arr[i * nComp + c] = dv[getter](row + c * bpe, true);
      }
    }
  }
  if (acc.normalized && acc.componentType !== 5126) {
    const f = new Float32Array(arr.length);
    for (let i = 0; i < arr.length; i++) f[i] = Math.max(-1, arr[i] / maxVal);
    return f;
  }
  return arr;
}

export function parseGlb(buffer) {
  const ab = buffer instanceof ArrayBuffer ? buffer : buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
  if (ab.byteLength < 12) throw new Error("Invalid GLB file (too short)");
  const dv = new DataView(ab);
  if (dv.getUint32(0, true) !== GLB_MAGIC || dv.getUint32(4, true) !== 2) {
    throw new Error("Invalid GLB header");
  }
  const totalLen = Math.min(dv.getUint32(8, true), ab.byteLength);
  let gltf = null;
  let binOff = 0;
  let binLen = 0;
  let off = 12;
  while (off + 8 <= totalLen) {
    const cLen = dv.getUint32(off, true);
    const cType = dv.getUint32(off + 4, true);
    const dataOff = off + 8;
    if (dataOff + cLen > totalLen) break;
    if (cType === CHUNK_JSON) {
      const txt = new TextDecoder("utf-8").decode(new Uint8Array(ab, dataOff, cLen));
      gltf = JSON.parse(txt);
    } else if (cType === CHUNK_BIN) {
      binOff = dataOff;
      binLen = cLen;
    }
    off = dataOff + cLen;
  }
  if (!gltf) throw new Error("GLB missing JSON chunk");

  const materials = (gltf.materials || []).map((m) => {
    const pbr = m.pbrMetallicRoughness || {};
    const bcf = Array.isArray(pbr.baseColorFactor) ? pbr.baseColorFactor : [0.78, 0.80, 0.84, 1];
    const col = new THREE.Color().setRGB(bcf[0] ?? 0.78, bcf[1] ?? 0.80, bcf[2] ?? 0.84, THREE.LinearSRGBColorSpace);
    const matOpts = {
      color: col,
      metalness: pbr.metallicFactor !== undefined ? pbr.metallicFactor : 1.0,
      roughness: pbr.roughnessFactor !== undefined ? pbr.roughnessFactor : 1.0,
      side: m.doubleSided ? THREE.DoubleSide : THREE.FrontSide,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    };
    const em = m.emissiveFactor;
    if (Array.isArray(em) && (em[0] > 0 || em[1] > 0 || em[2] > 0)) {
      matOpts.emissive = new THREE.Color().setRGB(em[0] || 0, em[1] || 0, em[2] || 0, THREE.LinearSRGBColorSpace);
      matOpts.emissiveIntensity = 1.5;
    }
    if (m.alphaMode === "BLEND" && (bcf[3] ?? 1) < 1) {
      matOpts.transparent = true;
      matOpts.opacity = bcf[3];
    }
    return new THREE.MeshStandardMaterial(matOpts);
  });
  const defaultMat = new THREE.MeshStandardMaterial({ color: 0x8d9db0, metalness: 0.15, roughness: 0.55, side: THREE.DoubleSide });
  const dark = typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches;
  const edgeMat = new THREE.LineBasicMaterial({ color: dark ? 0x0b0c0e : 0x1e2228, transparent: true, opacity: 0.32 });

  const meshData = (gltf.meshes || []).map((meshDef) => {
    const prims = [];
    for (const pr of (meshDef.primitives || [])) {
      const attrs = pr.attributes || {};
      if (attrs.POSITION === undefined) continue;
      const g = new THREE.BufferGeometry();
      const pos = _readAccessor(gltf, ab, binOff, binLen, attrs.POSITION);
      g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      if (attrs.NORMAL !== undefined) {
        g.setAttribute("normal", new THREE.BufferAttribute(_readAccessor(gltf, ab, binOff, binLen, attrs.NORMAL), 3));
      } else {
        g.computeVertexNormals();
      }
      if (pr.indices !== undefined) {
        g.setIndex(new THREE.BufferAttribute(_readAccessor(gltf, ab, binOff, binLen, pr.indices), 1));
      }
      const mat = pr.material !== undefined && materials[pr.material] ? materials[pr.material] : defaultMat;
      prims.push({ geometry: g, material: mat, vCount: pos.length / 3 });
    }
    return prims;
  });

  const nodes = (gltf.nodes || []).map((nd, idx) => {
    const grp = new THREE.Group();
    grp.name = nd.name || ("node_" + idx);
    if (Array.isArray(nd.matrix) && nd.matrix.length === 16) {
      const m4 = new THREE.Matrix4().fromArray(nd.matrix);
      m4.decompose(grp.position, grp.quaternion, grp.scale);
    } else {
      if (Array.isArray(nd.translation)) grp.position.set(nd.translation[0], nd.translation[1], nd.translation[2]);
      if (Array.isArray(nd.rotation)) grp.quaternion.set(nd.rotation[0], nd.rotation[1], nd.rotation[2], nd.rotation[3]);
      if (Array.isArray(nd.scale)) grp.scale.set(nd.scale[0], nd.scale[1], nd.scale[2]);
    }
    if (nd.mesh !== undefined && meshData[nd.mesh]) {
      for (const pr of meshData[nd.mesh]) {
        const m = new THREE.Mesh(pr.geometry, pr.material);
        m.name = grp.name;
        grp.add(m);
        if (pr.vCount > 0 && pr.vCount <= 25000) {
          grp.add(new THREE.LineSegments(new THREE.EdgesGeometry(pr.geometry, 34), edgeMat));
        }
      }
    }
    return grp;
  });

  for (let i = 0; i < (gltf.nodes || []).length; i++) {
    for (const cIdx of (gltf.nodes[i].children || [])) {
      if (nodes[cIdx]) nodes[i].add(nodes[cIdx]);
    }
  }

  const root = new THREE.Group();
  const scDef = (gltf.scenes || [])[gltf.scene ?? 0];
  const rootIdxs = scDef && Array.isArray(scDef.nodes) ? scDef.nodes : nodes.map((_, i) => i).filter((i) => !gltf.nodes.some((n) => (n.children || []).includes(i)));
  for (const rIdx of rootIdxs) {
    if (nodes[rIdx]) root.add(nodes[rIdx]);
  }

  const tracks = [];
  let maxDuration = 0;
  for (const anim of (gltf.animations || [])) {
    const samplers = anim.samplers || [];
    for (const ch of (anim.channels || [])) {
      const sm = samplers[ch.sampler];
      const tgt = ch.target || {};
      const obj = tgt.node !== undefined ? nodes[tgt.node] : null;
      if (!sm || !obj || !tgt.path) continue;
      const times = _readAccessor(gltf, ab, binOff, binLen, sm.input);
      let vals = _readAccessor(gltf, ab, binOff, binLen, sm.output);
      if (!times || !vals || !times.length) continue;
      const dur = times[times.length - 1] || 0;
      if (dur > maxDuration) maxDuration = dur;
      const prop = tgt.path === "rotation" ? "quaternion" : tgt.path === "translation" ? "position" : tgt.path === "scale" ? "scale" : null;
      if (!prop) continue;
      const strideComp = prop === "quaternion" ? 4 : 3;
      if (sm.interpolation === "CUBICSPLINE") {
        const stripped = new Float32Array(times.length * strideComp);
        for (let k = 0; k < times.length; k++) {
          const base = k * 3 * strideComp + strideComp;
          for (let q = 0; q < strideComp; q++) stripped[k * strideComp + q] = vals[base + q];
        }
        vals = stripped;
      }
      const interp = sm.interpolation === "STEP" ? THREE.InterpolateDiscrete : THREE.InterpolateLinear;
      const track = prop === "quaternion"
        ? new THREE.QuaternionKeyframeTrack(obj.uuid + ".quaternion", times, vals, interp)
        : new THREE.VectorKeyframeTrack(obj.uuid + "." + prop, times, vals, interp);
      tracks.push(track);
    }
  }
  const clip = tracks.length ? new THREE.AnimationClip("motion", maxDuration || 1, tracks) : null;
  const partCount = (gltf.meshes || []).length;
  return { root, clip, partCount, materials, edgeMat, defaultMat };
}

function _attachRenderPause(host, setPaused) {
  let offscreen = false;
  const update = () => setPaused(offscreen || (typeof document !== "undefined" && document.hidden));
  const onCv = (ev) => { offscreen = !!ev.skipped; update(); };
  host.addEventListener("contentvisibilityautostatechange", onCv);
  let io = null;
  if (typeof document !== "undefined" && !("contentVisibility" in document.documentElement.style) && typeof IntersectionObserver !== "undefined") {
    io = new IntersectionObserver((entries) => {
      offscreen = entries.some((e) => !e.isIntersecting);
      update();
    }, { rootMargin: "150px" });
    io.observe(host);
  }
  const onDoc = () => update();
  if (typeof document !== "undefined") document.addEventListener("visibilitychange", onDoc);
  return () => {
    host.removeEventListener("contentvisibilityautostatechange", onCv);
    if (io) io.disconnect();
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onDoc);
  };
}

export async function showStep(host, mesh) {
  if (!mesh || !mesh.faces || !mesh.faces.length) throw new Error("this STEP file has no solid shapes to draw");
  const renderer = new THREE.WebGLRenderer({ antialias: true });            // throws before replacing host content if WebGL is unavailable
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  renderer.setClearColor(dark ? 0x1b1d21 : 0xf3f4f6, 1);
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  host.replaceChildren(renderer.domElement);
  renderer.domElement.style.cssText = "display:block;width:100%;height:100%";
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x666a73, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(3, -4, 6); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.6); fill.position.set(-4, 3, 2); scene.add(fill);
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0x8d9db0, metalness: 0.15, roughness: 0.55, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
  const lineMat = new THREE.LineBasicMaterial({ color: dark ? 0x0b0c0e : 0x20242a });
  for (const f of mesh.faces) {                                              // one geometry per CAD face: smooth shading inside a face, crisp edges between faces
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(f.p, 3));
    g.setIndex(f.i);
    g.computeVertexNormals();
    group.add(new THREE.Mesh(g, mat));
    group.add(new THREE.LineSegments(new THREE.EdgesGeometry(g, 28), lineMat));
  }
  scene.add(group);
  const box = new THREE.Box3().setFromObject(group), size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 1e-6);
  const camera = new THREE.PerspectiveCamera(35, 1, radius / 100, radius * 100);
  camera.up.set(0, 0, 1);
  camera.position.copy(center).add(new THREE.Vector3(1, -1.25, 0.9).normalize().multiplyScalar(radius * 2.6));
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(center); controls.enableDamping = true; controls.update();
  let alive = true, paused = false, raf = 0;
  const fit = () => { const w = host.clientWidth || 300, h = host.clientHeight || 300; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); };
  const ro = new ResizeObserver(fit); ro.observe(host); fit();
  const loop = () => { raf = 0; if (!alive || paused) return; controls.update(); renderer.render(scene, camera); raf = requestAnimationFrame(loop); };
  const detachVis = _attachRenderPause(host, (p) => { paused = p; if (!p && alive && !raf) loop(); });
  loop();
  return {
    size: mesh.size || [size.x, size.y, size.z], parts: mesh.faces.length,
    dispose() { alive = false; if (raf) cancelAnimationFrame(raf); detachVis(); ro.disconnect(); controls.dispose(); renderer.dispose(); group.traverse((o) => { if (o.geometry) o.geometry.dispose(); }); mat.dispose(); lineMat.dispose(); renderer.domElement.remove(); },
  };
}

export async function showGlb(host, buffer, opts = {}) {
  const parsed = parseGlb(buffer);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  renderer.setClearColor(dark ? 0x1b1d21 : 0xf4f5f7, 1);
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  if ("outputColorSpace" in renderer && THREE.SRGBColorSpace) renderer.outputColorSpace = THREE.SRGBColorSpace;
  host.replaceChildren(renderer.domElement);
  renderer.domElement.style.cssText = "display:block;width:100%;height:100%";

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x6f7580, 1.35));
  const key = new THREE.DirectionalLight(0xffffff, 1.85); key.position.set(2.5, 4.0, 3.0); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.95); fill.position.set(-3.0, 2.0, -2.0); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.75); rim.position.set(0.0, 1.5, -4.0); scene.add(rim);
  scene.add(parsed.root);
  parsed.root.updateMatrixWorld(true);

  const box = new THREE.Box3().setFromObject(parsed.root);
  const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 0.1);
  const grid = new THREE.GridHelper(radius * 2.2, 12, dark ? 0x3b4048 : 0xc5c9d0, dark ? 0x2a2d33 : 0xe0e3e8);
  grid.position.set(center.x, isFinite(box.min.y) ? box.min.y - 0.001 : 0, center.z);
  scene.add(grid);

  const camera = new THREE.PerspectiveCamera(35, 1, Math.max(radius / 200, 0.001), radius * 100);
  camera.up.set(0, 1, 0);
  const homeDir = new THREE.Vector3(1.15, 0.72, 1.35).normalize();
  const resetView = () => {
    camera.position.copy(center).addScaledVector(homeDir, radius * 2.35);
    controls.target.copy(center);
    controls.update();
  };
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  resetView();

  const mixer = parsed.clip ? new THREE.AnimationMixer(parsed.root) : null;
  const action = mixer ? mixer.clipAction(parsed.clip) : null;
  const duration = parsed.clip ? parsed.clip.duration : 0;
  const hasAnimation = !!(action && duration > 0);
  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let playing = hasAnimation && (opts.autoplay !== undefined ? !!opts.autoplay : !reduced);
  if (action) {
    action.setLoop(THREE.LoopRepeat, Infinity);
    action.play();
    mixer.setTime(0);
  }

  let alive = true, visPaused = false, raf = 0, lastT = performance.now();
  const fit = () => {
    const w = host.clientWidth || 320, h = host.clientHeight || 240;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  const ro = new ResizeObserver(fit);
  ro.observe(host);
  fit();

  const emitTick = () => {
    if (typeof opts.onTick === "function" && action && duration > 0) {
      opts.onTick((action.time % duration) / duration);
    }
  };
  const renderNow = () => { controls.update(); renderer.render(scene, camera); };
  const loop = (now) => {
    raf = 0;
    if (!alive || visPaused) return;
    const dt = Math.min(0.05, Math.max(0, ((now || performance.now()) - lastT) / 1000));
    lastT = now || performance.now();
    if (mixer && playing) {
      mixer.update(dt);
      emitTick();
    }
    renderNow();
    raf = requestAnimationFrame(loop);
  };
  const detachVis = _attachRenderPause(host, (p) => {
    visPaused = p;
    if (!p && alive && !raf) { lastT = performance.now(); loop(lastT); }
  });
  loop(lastT);

  return {
    size: [size.x * 1000, size.z * 1000, size.y * 1000],
    parts: parsed.partCount,
    hasAnimation,
    duration,
    get playing() { return playing; },
    setPlaying(b) {
      if (!hasAnimation) return;
      playing = !!b;
      lastT = performance.now();
      renderNow();
    },
    seek(norm) {
      if (!mixer || !duration) return;
      const clamped = Math.max(0, Math.min(1, Number(norm) || 0));
      mixer.setTime(clamped * duration);
      emitTick();
      renderNow();
    },
    resetView,
    dispose() {
      alive = false;
      if (raf) cancelAnimationFrame(raf);
      detachVis();
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      parsed.root.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
      grid.geometry.dispose();
      grid.material.dispose();
      parsed.materials.forEach((m) => m.dispose());
      parsed.edgeMat.dispose();
      parsed.defaultMat.dispose();
      renderer.domElement.remove();
    },
  };
}
