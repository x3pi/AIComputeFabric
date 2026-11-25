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
    const rawMetal = pbr.metallicFactor !== undefined ? pbr.metallicFactor : 1.0;
    const matOpts = {
      color: col,
      metalness: Math.min(rawMetal, 0.58),
      roughness: pbr.roughnessFactor !== undefined ? pbr.roughnessFactor : 1.0,
      side: THREE.DoubleSide,
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
      g.computeBoundingBox();
      g.computeBoundingSphere();
      const mat = pr.material !== undefined && materials[pr.material] ? materials[pr.material] : defaultMat;
      prims.push({ geometry: g, material: mat, vCount: pos.length / 3 });
    }
    return prims;
  });

  const partsMap = new Map();
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
      const wrap = new THREE.Group();
      wrap.name = grp.name + "__wrap";
      grp.add(wrap);
      let entry = partsMap.get(grp.name);
      if (!entry) {
        entry = {
          id: grp.name,
          node: grp,
          wrap,
          meshes: [],
          edges: [],
          hidden: false,
          localExplodeDir: new THREE.Vector3(),
          restCenterWorld: new THREE.Vector3(),
          restSizeMm: [0, 0, 0],
        };
        partsMap.set(grp.name, entry);
      }
      for (const pr of meshData[nd.mesh]) {
        const mat = pr.material.clone();
        const origEm = mat.emissive ? mat.emissive.clone() : new THREE.Color(0x000000);
        const origEmI = mat.emissiveIntensity || 0;
        const m = new THREE.Mesh(pr.geometry, mat);
        m.name = grp.name;
        m.userData.partId = grp.name;
        m.userData.origEm = origEm;
        m.userData.origEmI = origEmI;
        wrap.add(m);
        entry.meshes.push(m);
        if (pr.vCount > 0 && pr.vCount <= 25000) {
          const em = edgeMat.clone();
          const ls = new THREE.LineSegments(new THREE.EdgesGeometry(pr.geometry, 34), em);
          ls.userData.origColor = em.color.clone();
          ls.userData.origOpacity = em.opacity;
          wrap.add(ls);
          entry.edges.push(ls);
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
  const partCount = partsMap.size || (gltf.meshes || []).length;
  return { root, clip, partCount, partsMap, materials, edgeMat, defaultMat };
}

export function parseStl(buffer) {
  const ab = buffer instanceof ArrayBuffer ? buffer : buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
  if (ab.byteLength < 15) throw new Error("Invalid STL file");
  const dv = new DataView(ab);
  let pos = null;
  if (ab.byteLength >= 84) {
    const triCount = dv.getUint32(80, true);
    if (84 + triCount * 50 === ab.byteLength) {
      pos = new Float32Array(triCount * 9);
      let k = 0;
      for (let i = 0; i < triCount; i++) {
        const base = 84 + i * 50;
        for (let v = 0; v < 3; v++) {
          const vo = base + 12 + v * 12;
          pos[k++] = dv.getFloat32(vo, true);
          pos[k++] = dv.getFloat32(vo + 4, true);
          pos[k++] = dv.getFloat32(vo + 8, true);
        }
      }
    }
  }
  if (!pos) {
    const txt = new TextDecoder("utf-8").decode(new Uint8Array(ab));
    const coords = [];
    const re = /vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g;
    let m;
    while ((m = re.exec(txt))) {
      coords.push(parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]));
    }
    if (coords.length < 9) throw new Error("Empty or unreadable STL file");
    pos = new Float32Array(coords);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.computeVertexNormals();
  return { geometry: g, triangles: pos.length / 9 };
}

export async function showStl(host, buffer) {
  const { geometry, triangles } = parseStl(buffer);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  renderer.setClearColor(dark ? 0x1b1d21 : 0xf4f5f7, 1);
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  host.replaceChildren(renderer.domElement);
  renderer.domElement.style.cssText = "display:block;width:100%;height:100%";
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x6f7580, 1.3));
  const key = new THREE.DirectionalLight(0xffffff, 1.75); key.position.set(3, -4, 6); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.85); fill.position.set(-4, 3, 2); scene.add(fill);
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0x8d9db0, metalness: 0.2, roughness: 0.5, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
  const lineMat = new THREE.LineBasicMaterial({ color: dark ? 0x0b0c0e : 0x20242a, transparent: true, opacity: 0.35 });
  group.add(new THREE.Mesh(geometry, mat));
  if (triangles <= 60000) group.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 32), lineMat));
  scene.add(group);
  const box = new THREE.Box3().setFromObject(group), size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 1e-3);
  const camera = new THREE.PerspectiveCamera(35, 1, Math.max(radius / 200, 1e-4), radius * 100);
  camera.up.set(0, 0, 1);
  camera.position.copy(center).add(new THREE.Vector3(1, -1.25, 0.9).normalize().multiplyScalar(radius * 2.5));
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(center); controls.enableDamping = true; controls.update();
  let alive = true, paused = false, raf = 0;
  const fit = () => { const w = host.clientWidth || 300, h = host.clientHeight || 300; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); };
  const ro = new ResizeObserver(fit); ro.observe(host); fit();
  const loop = () => { raf = 0; if (!alive || paused) return; controls.update(); renderer.render(scene, camera); raf = requestAnimationFrame(loop); };
  const detachVis = _attachRenderPause(host, (p) => { paused = p; if (!p && alive && !raf) loop(); });
  loop();
  return {
    size: [size.x, size.y, size.z],
    parts: triangles,
    dispose() { alive = false; if (raf) cancelAnimationFrame(raf); detachVis(); ro.disconnect(); controls.dispose(); renderer.dispose(); geometry.dispose(); mat.dispose(); lineMat.dispose(); renderer.domElement.remove(); },
  };
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

function _createStudioEnv(renderer) {
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envScene = new THREE.Scene();
  envScene.background = new THREE.Color(0xdbe2ec);
  const specs = [
    [[3, 4, 3], [5, 5], 0xffffff],
    [[-3, 3, -2], [5, 5], 0xeaf2ff],
    [[0, 5, -3], [6, 6], 0xfff5e8],
    [[0, -3, 2], [6, 6], 0xd8e0ea],
  ];
  const meshes = [];
  for (const [pos, sz, hex] of specs) {
    const geom = new THREE.PlaneGeometry(sz[0], sz[1]);
    const mat = new THREE.MeshBasicMaterial({ color: hex, side: THREE.DoubleSide });
    const plane = new THREE.Mesh(geom, mat);
    plane.position.set(pos[0], pos[1], pos[2]);
    plane.lookAt(0, 0, 0);
    envScene.add(plane);
    meshes.push([geom, mat]);
  }
  const envRT = pmrem.fromScene(envScene, 0.04);
  for (const [geom, mat] of meshes) {
    geom.dispose();
    mat.dispose();
  }
  pmrem.dispose();
  return envRT;
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
  renderer.domElement.setAttribute("role", "img");
  renderer.domElement.setAttribute("aria-label", "Khung xem 3D tương tác");

  const scene = new THREE.Scene();
  const envRT = _createStudioEnv(renderer);
  scene.environment = envRT.texture;
  if ("environmentIntensity" in scene) scene.environmentIntensity = 0.9;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x6f7580, 1.35));
  const key = new THREE.DirectionalLight(0xffffff, 1.85); key.position.set(2.5, 4.0, 3.0); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.95); fill.position.set(-3.0, 2.0, -2.0); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.75); rim.position.set(0.0, 1.5, -4.0); scene.add(rim);
  scene.add(parsed.root);
  parsed.root.updateMatrixWorld(true);

  const box = new THREE.Box3().setFromObject(parsed.root);
  const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 0.1);
  const invLinear = new THREE.Matrix4();
  const tmpBox = new THREE.Box3();
  const tmpSz = new THREE.Vector3();
  for (const entry of parsed.partsMap.values()) {
    tmpBox.setFromObject(entry.wrap);
    tmpBox.getCenter(entry.restCenterWorld);
    tmpBox.getSize(tmpSz);
    entry.restSizeMm = [
      Math.round(tmpSz.x * 1000 * 10) / 10,
      Math.round(tmpSz.z * 1000 * 10) / 10,
      Math.round(tmpSz.y * 1000 * 10) / 10,
    ];
    invLinear.copy(entry.node.matrixWorld).setPosition(0, 0, 0).invert();
    entry.localExplodeDir.copy(entry.restCenterWorld).sub(center).applyMatrix4(invLinear);
  }
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
  // Rest-pose default: keep motion paused on load until the user clicks Play or scrubs.
  let playing = hasAnimation && !!opts.autoplay;
  if (action) {
    action.setLoop(THREE.LoopRepeat, Infinity);
    action.play();
    mixer.setTime(0);
    mixer.update(0);
  }

  let selectedId = null, hoveredId = null, isolatedId = null, explodeAmt = 0;
  const selEdgeColor = new THREE.Color(0xff9f1c);
  const hovEdgeColor = new THREE.Color(0x38bdf8);
  const applyVisibilityAndHighlight = () => {
    for (const entry of parsed.partsMap.values()) {
      entry.wrap.visible = isolatedId ? (entry.id === isolatedId) : !entry.hidden;
      const isSel = entry.id === selectedId;
      const isHov = !isSel && entry.id === hoveredId;
      for (const m of entry.meshes) {
        if (isSel || isHov) {
          m.material.emissive.copy(m.userData.origEmI > 0 ? m.userData.origEm : m.material.color);
          m.material.emissiveIntensity = m.userData.origEmI > 0 ? m.userData.origEmI : (isSel ? 0.12 : 0.06);
        } else {
          m.material.emissive.copy(m.userData.origEm);
          m.material.emissiveIntensity = m.userData.origEmI;
        }
      }
      for (const e of entry.edges) {
        e.material.color.copy(isSel ? selEdgeColor : (isHov ? hovEdgeColor : e.userData.origColor));
        e.material.opacity = isSel ? 1.0 : (isHov ? 0.85 : e.userData.origOpacity);
      }
    }
  };

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

  const selectPart = (id, silent = false) => {
    selectedId = (id && parsed.partsMap.has(id)) ? id : null;
    applyVisibilityAndHighlight();
    renderNow();
    if (!silent && typeof opts.onSelect === "function") opts.onSelect(selectedId);
    return selectedId;
  };

  if (getComputedStyle(host).position === "static") host.style.position = "relative";
  const tip = document.createElement("div");
  tip.className = "v3-tip";
  tip.hidden = true;
  tip.style.cssText = "position:absolute;pointer-events:none;z-index:5;padding:3px 8px;border-radius:6px;background:rgba(24,25,28,.88);color:#fff;font:12px/1.35 ui-monospace,Consolas,monospace;white-space:nowrap;box-shadow:0 3px 10px #0004;transform:translate(10px,-115%)";
  host.appendChild(tip);
  renderer.domElement.style.cursor = "grab";

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const raycastParts = (clientX, clientY) => {
    const rect = renderer.domElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return { hits: [], rect };
    ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    camera.updateMatrixWorld();
    parsed.root.updateMatrixWorld(true);
    raycaster.setFromCamera(ndc, camera);
    const targets = [];
    for (const entry of parsed.partsMap.values()) {
      if (entry.wrap.visible) targets.push(...entry.meshes);
    }
    return { hits: raycaster.intersectObjects(targets, false), rect };
  };
  const uniqueHitIds = (hits) => {
    const ids = [];
    for (const hit of hits) {
      const pid = hit.object?.userData?.partId;
      if (pid && !ids.includes(pid)) ids.push(pid);
    }
    return ids;
  };
  const showTip = (pid, clientX, clientY, rect, depthInfo = "") => {
    if (!pid) { tip.hidden = true; return; }
    const base = opts.partLabels?.[pid] || pid;
    tip.textContent = depthInfo ? `${base} ${depthInfo}` : base;
    tip.hidden = false;
    const hr = host.getBoundingClientRect();
    const x = Math.min(Math.max(8, clientX - hr.left), Math.max(8, hr.width - 150));
    const y = Math.max(28, clientY - hr.top);
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  };

  let downX = 0, downY = 0, downBtn = -1, dragging = false;
  let lastClickX = 0, lastClickY = 0;
  const onDown = (ev) => {
    downX = ev.clientX; downY = ev.clientY; downBtn = ev.button;
    dragging = ev.button === 0 || ev.button === 2;
  };
  const onMove = (ev) => {
    if (dragging) {
      if (Math.hypot(ev.clientX - downX, ev.clientY - downY) >= 5) {
        renderer.domElement.style.cursor = "grabbing";
        tip.hidden = true;
      }
      return;
    }
    const { hits, rect } = raycastParts(ev.clientX, ev.clientY);
    const ids = uniqueHitIds(hits);
    const hid = ids.length ? ids[0] : null;
    if (hid !== hoveredId) {
      hoveredId = hid;
      applyVisibilityAndHighlight();
      renderNow();
      if (typeof opts.onHover === "function") opts.onHover(hoveredId);
    }
    renderer.domElement.style.cursor = hid ? "pointer" : "grab";
    const depthInfo = (hid && ids.length > 1) ? `[1/${ids.length}]` : "";
    showTip(hid, ev.clientX, ev.clientY, rect, depthInfo);
  };
  const onLeave = () => {
    dragging = false;
    tip.hidden = true;
    renderer.domElement.style.cursor = "grab";
    if (hoveredId !== null) {
      hoveredId = null;
      applyVisibilityAndHighlight();
      renderNow();
      if (typeof opts.onHover === "function") opts.onHover(null);
    }
  };
  const onUp = (ev) => {
    const wasBtn = downBtn;
    dragging = false;
    downBtn = -1;
    if (wasBtn !== 0 || Math.hypot(ev.clientX - downX, ev.clientY - downY) >= 5) {
      renderer.domElement.style.cursor = hoveredId ? "pointer" : "grab";
      return;
    }
    const { hits, rect } = raycastParts(ev.clientX, ev.clientY);
    const ids = uniqueHitIds(hits);
    if (!ids.length) {
      selectPart(null);
      tip.hidden = true;
      return;
    }
    // Same-spot repeat clicks cycle cleanly through occluded parts behind the front mesh.
    const sameSpot = Math.hypot(ev.clientX - lastClickX, ev.clientY - lastClickY) < 5;
    let pid = ids[0];
    if (sameSpot && selectedId && ids.includes(selectedId) && ids.length > 1) {
      pid = ids[(ids.indexOf(selectedId) + 1) % ids.length];
    }
    lastClickX = ev.clientX;
    lastClickY = ev.clientY;
    hoveredId = pid;
    selectPart(pid);
    renderer.domElement.style.cursor = "pointer";
    const idx = ids.indexOf(pid);
    const depthInfo = ids.length > 1 ? `[${idx + 1}/${ids.length}]` : "";
    showTip(pid, ev.clientX, ev.clientY, rect, depthInfo);
  };
  const focusPart = (id) => {
    const entry = parsed.partsMap.get(id);
    if (!entry) return;
    parsed.root.updateMatrixWorld(true);
    const b = new THREE.Box3().setFromObject(entry.wrap);
    if (b.isEmpty()) return;
    const partCenter = b.getCenter(new THREE.Vector3());
    const partSize = b.getSize(new THREE.Vector3());
    const partRad = Math.max(partSize.x, partSize.y, partSize.z, 0.02) * 0.5;
    const dir = camera.position.clone().sub(controls.target);
    if (dir.lengthSq() < 1e-8) dir.copy(homeDir);
    else dir.normalize();
    const dist = Math.max(partRad * 4.2, radius * 0.18);
    controls.target.copy(partCenter);
    camera.position.copy(partCenter).addScaledVector(dir, dist);
    controls.update();
    renderNow();
  };
  const onWinUp = () => { dragging = false; downBtn = -1; };
  const onDblClick = (ev) => {
    const { hits } = raycastParts(ev.clientX, ev.clientY);
    const ids = uniqueHitIds(hits);
    if (!ids.length) return;
    const pid = (selectedId && ids.includes(selectedId)) ? selectedId : ids[0];
    selectPart(pid);
    focusPart(pid);
  };
  renderer.domElement.addEventListener("pointerdown", onDown, { capture: true });
  renderer.domElement.addEventListener("pointermove", onMove);
  renderer.domElement.addEventListener("pointerleave", onLeave);
  renderer.domElement.addEventListener("pointerup", onUp, { capture: true });
  renderer.domElement.addEventListener("dblclick", onDblClick, { capture: true });
  window.addEventListener("pointerup", onWinUp);
  loop(lastT);

  return {
    size: [size.x * 1000, size.z * 1000, size.y * 1000],
    parts: parsed.partCount,
    partIds: [...parsed.partsMap.keys()],
    hasAnimation,
    duration,
    get playing() { return playing; },
    get selected() { return selectedId; },
    get isolated() { return isolatedId; },
    get explode() { return explodeAmt; },
    getPartBoxMm(id) {
      const e = parsed.partsMap.get(id);
      return e ? e.restSizeMm : null;
    },
    selectPart,
    focusPart,
    getCameraState() {
      return { pos: camera.position.toArray(), target: controls.target.toArray() };
    },
    setCameraState(st) {
      if (!st || !Array.isArray(st.pos) || !Array.isArray(st.target)) return;
      camera.position.fromArray(st.pos);
      controls.target.fromArray(st.target);
      controls.update();
      renderNow();
    },
    setPartHidden(id, hidden) {
      const entry = parsed.partsMap.get(id);
      if (!entry) return;
      entry.hidden = !!hidden;
      if (isolatedId === id && entry.hidden) isolatedId = null;
      applyVisibilityAndHighlight();
      renderNow();
    },
    isPartHidden(id) {
      return !!parsed.partsMap.get(id)?.hidden;
    },
    setIsolated(id) {
      isolatedId = (id && parsed.partsMap.has(id)) ? id : null;
      if (isolatedId) parsed.partsMap.get(isolatedId).hidden = false;
      applyVisibilityAndHighlight();
      renderNow();
    },
    showAllParts() {
      isolatedId = null;
      for (const entry of parsed.partsMap.values()) entry.hidden = false;
      applyVisibilityAndHighlight();
      renderNow();
    },
    setExplode(norm) {
      explodeAmt = Math.max(0, Math.min(1, Number(norm) || 0));
      for (const entry of parsed.partsMap.values()) {
        entry.wrap.position.copy(entry.localExplodeDir).multiplyScalar(explodeAmt * 0.9);
      }
      renderNow();
    },
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
      renderer.domElement.removeEventListener("pointerdown", onDown, { capture: true });
      renderer.domElement.removeEventListener("pointermove", onMove);
      renderer.domElement.removeEventListener("pointerleave", onLeave);
      renderer.domElement.removeEventListener("pointerup", onUp, { capture: true });
      renderer.domElement.removeEventListener("dblclick", onDblClick, { capture: true });
      window.removeEventListener("pointerup", onWinUp);
      tip.remove();
      controls.dispose();
      envRT.dispose();
      renderer.dispose();
      parsed.root.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
      grid.geometry.dispose();
      grid.material.dispose();
      for (const entry of parsed.partsMap.values()) {
        entry.meshes.forEach((m) => m.material.dispose());
        entry.edges.forEach((e) => e.material.dispose());
      }
      parsed.materials.forEach((m) => m.dispose());
      parsed.edgeMat.dispose();
      parsed.defaultMat.dispose();
      renderer.domElement.remove();
    },
  };
}
