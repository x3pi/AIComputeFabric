// Interactive 3D view of a STEP file. The server triangulates it (stepmesh.py), three.js draws the triangles here.
// Everything is served from this app (/static), nothing is loaded from the internet.
import * as THREE from "./vendor/three.module.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

export async function showStep(host, mesh) {
  if (!mesh || !mesh.faces || !mesh.faces.length) throw new Error("this STEP file has no solid shapes to draw");
  const renderer = new THREE.WebGLRenderer({ antialias: true });            // throws when WebGL is not available: the caller falls back to the 2D drawing
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
  let alive = true;
  const fit = () => { const w = host.clientWidth || 300, h = host.clientHeight || 300; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); };
  const ro = new ResizeObserver(fit); ro.observe(host); fit();
  (function loop() { if (!alive) return; controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop); })();
  return {
    size: mesh.size || [size.x, size.y, size.z], parts: mesh.faces.length,
    dispose() { alive = false; ro.disconnect(); controls.dispose(); renderer.dispose(); group.traverse((o) => { if (o.geometry) o.geometry.dispose(); }); mat.dispose(); lineMat.dispose(); renderer.domElement.remove(); },
  };
}
