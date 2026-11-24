"""Runs inside Blender: build scene from a project dir, verify, write report.json.
Parts: <proj>/parts/<id>.py defines build(bpy, bmesh) creating ONE mesh object named <id>, metres, world frame, scale 1 (raw bpy by Qwen).
Joints/animation: deterministic from plan.json (this is the error-prone wiring, so it is not left to the small model)."""
import sys, os, json, math, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy, bmesh, mathutils
import verify_motion as vm
import bxguard, connectivity

args = sys.argv[sys.argv.index("--") + 1:]; proj = args[0]; smoke_part = args[1] if len(args) > 1 else None
if smoke_part:                                    # fast single-part smoke build (run while the worker keeps writing other parts)
    err = None; bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        f = os.path.join(proj, "parts", smoke_part + ".py"); ns = {}; exec(compile(open(f).read(), f, "exec"), ns); ns["build"](bpy, bxguard.wrap(bmesh))
        if smoke_part not in bpy.data.objects: err = "build() did not create an object named %r" % smoke_part
        else:
            o = bpy.data.objects[smoke_part]
            if len(o.data.vertices) == 0: err = "mesh has no vertices"
            elif len(o.data.vertices) > 150000: err = "mesh has %d vertices: far too heavy (limit 150000); use fewer segments" % len(o.data.vertices)
            elif any(abs(s - 1) > 1e-6 for s in o.scale) or any(abs(x) > 1e-9 for x in o.location) or any(abs(x) > 1e-9 for x in o.rotation_euler):
                err = "object transform must stay identity (bake all offsets into vertices)"
    except Exception:
        err = traceback.format_exc(limit=2)[-400:]; err += bxguard.explain(bmesh, err)
    json.dump(dict(error=err), open(os.path.join(proj, "smoke_%s.json" % smoke_part), "w")); raise SystemExit
plan = json.load(open(os.path.join(proj, "plan.json")))
report = dict(part_errors={}, issues=[], missing=[], ok=False)
bpy.ops.wm.read_factory_settings(use_empty=True)
import signal, time as _time
PART_SECONDS = int(os.environ.get("BX_PART_SECONDS", "25")); PART_MB = int(os.environ.get("BX_PART_MB", "2500"))
def _rss_mb():
    try:
        with open("/proc/self/status") as f:
            for l in f:
                if l.startswith("VmRSS"): return int(l.split()[1]) // 1024
    except OSError: pass
    return 0
_guard = {"t0": 0.0, "mb0": 0, "on": False}
def _tick(sig, frm):
    if not _guard["on"]: return
    if _time.time() - _guard["t0"] > PART_SECONDS:
        _guard["on"] = False; raise TimeoutError("build() ran longer than %d s: usually a whole-mesh bevel/subdivide inside a loop or far too many segments; keep each part simple" % PART_SECONDS)
    if _rss_mb() - _guard["mb0"] > PART_MB:
        _guard["on"] = False; raise MemoryError("build() used more than %d MB: the geometry explodes (a whole-mesh bevel/subdivide/solidify inside a loop?); modify only the new geometry" % PART_MB)
def _arm():
    _guard.update(t0=_time.time(), mb0=_rss_mb(), on=True)
def _disarm(): _guard["on"] = False
if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _tick); signal.setitimer(signal.ITIMER_REAL, 0.25, 0.25)
for p in plan["parts"]:
    f = os.path.join(proj, "parts", p["id"] + ".py")
    if not os.path.exists(f): report["missing"].append(p["id"]); continue
    try:
        _arm()
        ns = {}; exec(compile(open(f).read(), f, "exec"), ns); ns["build"](bpy, bxguard.wrap(bmesh))
        _disarm()
        if p["id"] not in bpy.data.objects: report["part_errors"][p["id"]] = "build() did not create an object named %r" % p["id"]
        else:
            o = bpy.data.objects[p["id"]]
            if o.name not in bpy.context.scene.collection.objects: bpy.context.scene.collection.objects.link(o)
    except Exception:
        _disarm()
        report["part_errors"][p["id"]] = traceback.format_exc(limit=2)[-400:]; report["part_errors"][p["id"]] += bxguard.explain(bmesh, report["part_errors"][p["id"]])
_disarm()
if hasattr(signal, "setitimer"): signal.setitimer(signal.ITIMER_REAL, 0)
# --- lossless mesh optimisation, the same for every part whoever wrote it: merge duplicate vertices, dissolve vertices that sit on flat faces or straight edges
# (nothing visible changes), then smooth-shade with sharp edges kept where the surface really turns, so a low vertex count still renders as a clean surface
OPT = os.environ.get("BX_OPTIMIZE", "1") != "0"
SHARP = math.radians(float(os.environ.get("BX_SHARP_DEG", "35")))
LOD_MM = float(os.environ.get("BX_LOD_MM", "0.5")); LOD_MIN = int(os.environ.get("BX_LOD_MIN_VERTS", "300"))
from mathutils.bvhtree import BVHTree
report["opt"] = {}
def _tree(me):
    bm = bmesh.new(); bm.from_mesh(me); t = BVHTree.FromBMesh(bm); bm.free(); return t
def _within(a, b, tol):
    """Two-way surface check: every vertex of mesh a lies within tol of mesh b's surface and every vertex of b within tol of a's. Nothing visible changed."""
    if len(b.vertices) == 0 or len(a.vertices) == 0: return False
    ta, tb = _tree(a), _tree(b)
    return all((ta.find_nearest(v.co)[3] or 0) <= tol for v in b.vertices) and all((tb.find_nearest(v.co)[3] or 0) <= tol for v in a.vertices)
def _smooth(me):
    """Smooth-shade, keeping sharp edges where the surface really turns (a low vertex count then still renders as a clean surface)."""
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        for f in bm.faces: f.smooth = True
        for e in bm.edges:
            if len(e.link_faces) == 2: e.smooth = e.link_faces[0].normal.angle(e.link_faces[1].normal, 0.0) <= SHARP
        bm.to_mesh(me); me.update()
    finally:
        bm.free()
def _optimize(o):
    """Lossless clean-up: merge duplicate vertices and dissolve vertices on flat faces. Accepted only if the surface stays within LOD_MM both ways, so a thin
    sheet or any detail the dissolve would damage is left exactly as built."""
    me = o.data; v0 = len(me.vertices); bm = bmesh.new(); m2 = None
    try:
        bm.from_mesh(me)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-7)
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(0.5), verts=bm.verts, edges=bm.edges, use_dissolve_boundaries=False)
        m2 = bpy.data.meshes.new("_opt"); bm.to_mesh(m2)
        if len(m2.vertices) < v0 and _within(me, m2, LOD_MM * 0.001):
            old = me; nm = old.name; o.data = m2; m2.name = nm; bpy.data.meshes.remove(old); m2 = None
    except Exception:                                      # an optimisation never fails a part
        pass
    finally:
        bm.free()
        if m2 is not None: bpy.data.meshes.remove(m2)
    try: _smooth(o.data)
    except Exception: pass
    return v0, len(o.data.vertices)
# --- tolerance-bounded level of detail: a mesh with many vertices is decimated as far as the surface stays within LOD_MM of the original in BOTH directions
# (every new vertex lies within LOD_MM of the old surface and every old vertex within LOD_MM of the new one), so detail that matters is never lost and detail
# that does not is removed - the same rule for every part, whoever wrote it
def _lod(o):
    me = o.data; n0 = len(me.vertices)
    if n0 <= LOD_MIN: return n0, n0
    tol = LOD_MM * 0.001; t0 = _tree(me); pts0 = [v.co.copy() for v in me.vertices]
    best = None
    for ratio in (0.7, 0.5, 0.35, 0.25, 0.15, 0.1):
        tmp = bpy.data.objects.new("_lod_tmp", me.copy()); bpy.context.scene.collection.objects.link(tmp)
        try:
            md = tmp.modifiers.new("d", "DECIMATE"); md.decimate_type = "COLLAPSE"; md.ratio = ratio; md.use_collapse_triangulate = False
            dg = bpy.context.evaluated_depsgraph_get(); m2 = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg))
        finally:
            bpy.data.objects.remove(tmp, do_unlink=True)
        t1 = _tree(m2)
        ok = len(m2.vertices) > 0 and all((t0.find_nearest(v.co)[3] or 0) <= tol for v in m2.vertices) and all((t1.find_nearest(p)[3] or 0) <= tol for p in pts0)
        if ok and len(m2.vertices) < n0:
            if best is not None: bpy.data.meshes.remove(best)
            best = m2
        else:
            bpy.data.meshes.remove(m2)
            if not ok: break
    if best is None: return n0, n0
    old = o.data; o.data = best; best.name = old.name; bpy.data.meshes.remove(old); best.update()
    return n0, len(best.vertices)
if OPT:
    for _o in [x for x in bpy.data.objects if x.type == "MESH" and len(x.data.vertices)]:
        try:
            a, b = _optimize(_o); c = a
            try: _, c = _lod(_o)
            except Exception: c = b
            report["opt"][_o.name] = (a, c)
        except Exception: pass
bpy.context.view_layer.update()
# --- colours and finishes (deterministic: the director chooses them, the runner applies them)
FIN = {"matte": (0.75, 0.0), "gloss": (0.12, 0.0), "metal": (0.28, 1.0), "rubber": (0.92, 0.0), "emissive": (0.4, 0.0)}
def _lin(c): return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
_mats = {}
report["parts_color"] = {}
for p in plan["parts"]:
    o = bpy.data.objects.get(p["id"]); col = p.get("color")
    if o is None or o.type != "MESH" or not (isinstance(col, str) and len(col) == 7 and col[0] == "#"):
        continue
    try:
        fin = p.get("finish") if p.get("finish") in FIN else "matte"; key = (col.lower(), fin)
        if key not in _mats:
            rgb = [int(col[i:i + 2], 16) / 255 for i in (1, 3, 5)]; lin = [_lin(x) for x in rgb]
            m = bpy.data.materials.new("bx_%s_%s" % (col[1:].lower(), fin)); m.use_nodes = True
            b = m.node_tree.nodes.get("Principled BSDF"); rough, metal = FIN[fin]
            b.inputs["Base Color"].default_value = (lin[0], lin[1], lin[2], 1); b.inputs["Roughness"].default_value = rough; b.inputs["Metallic"].default_value = metal
            if fin == "emissive":
                b.inputs["Emission Color"].default_value = (lin[0], lin[1], lin[2], 1); b.inputs["Emission Strength"].default_value = 3.0
            m.diffuse_color = (lin[0], lin[1], lin[2], 1); m.roughness = rough; m.metallic = metal; _mats[key] = m
        o.data.materials.clear(); o.data.materials.append(_mats[key]); report["parts_color"][p["id"]] = col.lower()
    except Exception:
        pass
# --- rest-pose geometry gate (before any joint is wired): every part must be a clean object whose world bbox matches what the plan says
from mathutils import Vector as _V
for p in plan["parts"]:
    o = bpy.data.objects.get(p["id"])
    if o is None or o.type != "MESH" or not o.data.vertices:
        continue
    if max(abs(x) for x in o.location) > 1e-6 or max(abs(x) for x in o.rotation_euler) > 1e-6 or max(abs(x - 1) for x in o.scale) > 1e-6:
        report["issues"].append(dict(kind="object_transform", part=p["id"], note="object location/rotation/scale must stay identity; bake the placement into the vertices"))
        continue
    vs = [o.matrix_world @ v.co for v in o.data.vertices]
    lo = [min(v[i] for v in vs) * 1000 for i in range(3)]; hi = [max(v[i] for v in vs) * 1000 for i in range(3)]
    ctr, size = [(a + b) / 2 for a, b in zip(lo, hi)], [b - a for a, b in zip(lo, hi)]
    ec, es = p.get("center_mm"), p.get("size_mm")
    if isinstance(es, list) and len(es) == 3:
        bad = [i for i in range(3) if abs(size[i] - es[i]) > max(6.0, 0.2 * abs(es[i]))]
        if bad:
            report["issues"].append(dict(kind="size_mismatch", part=p["id"], expected_mm=es, got_mm=[round(x, 1) for x in size])); continue
    if isinstance(ec, list) and len(ec) == 3:
        tol = max(8.0, 0.08 * ((sum(x * x for x in (es if isinstance(es, list) and len(es) == 3 else size))) ** 0.5))
        if max(abs(ctr[i] - ec[i]) for i in range(3)) > tol:
            report["issues"].append(dict(kind="position_mismatch", part=p["id"], expected_center_mm=ec, got_center_mm=[round(x, 1) for x in ctr], tol_mm=round(tol, 1)))
last = 1
for j in plan.get("joints", []):
    names = [j["parent"]] if j.get("parent") else []
    if any(n not in bpy.data.objects for n in names): report["issues"].append(dict(kind="joint_skipped", joint=j["name"], reason="missing parent part")); continue
    have = [c for c in j["children"] if c in bpy.data.objects]
    if not have: report["issues"].append(dict(kind="joint_skipped", joint=j["name"], reason="missing part")); continue
    if len(have) < len(j["children"]):                 # a partial scene still moves what exists; the missing parts are reported as missing
        report["issues"].append(dict(kind="joint_partial", joint=j["name"], missing=[c for c in j["children"] if c not in have]))
        j = dict(j, children=have, coaxial=[c for c in j.get("coaxial", []) if c in have])
    e = bpy.data.objects.new(j["name"], None); e.location = [v * 0.001 for v in j["origin_mm"]]; bpy.context.scene.collection.objects.link(e)
    bpy.context.view_layer.update()
    if j.get("parent"): par = bpy.data.objects[j["parent"]]; e.parent = par; e.matrix_parent_inverse = par.matrix_world.inverted()
    for c in j["children"]: o = bpy.data.objects[c]; o.parent = e; o.matrix_parent_inverse = e.matrix_world.inverted()
    ax = mathutils.Vector(j["axis"]).normalized()
    e["bx_joint"] = "hinge"; e["bx_axis"] = list(ax); e["bx_coaxial"] = ",".join(j.get("coaxial", []))
    span = float(j.get("span_rad", 0)); e["bx_span"] = span; n = int(j.get("frames", 48)); last = max(last, 1 + n)
    e.rotation_mode = "QUATERNION"
    for i in range(n + 1):
        e.rotation_quaternion = mathutils.Quaternion(ax, span * i / n); e.keyframe_insert("rotation_quaternion", frame=1 + i)
    for fc in e.animation_data.action.fcurves:
        for kp in fc.keyframe_points: kp.interpolation = "LINEAR"
bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, last
if True:                                           # always save the scene (a partial one too); the motion verifier only judges a complete one
    if not report["missing"] and not report["part_errors"]:
        r = vm.verify(list(range(1, last + 1)), extra_allowed=[tuple(x) for x in plan.get("allowed_contacts", [])])
        report["issues"] += r["issues"]
        try:                                                # every part must be supported (floor or another part); parts with "free": true are exempt
            report["issues"] += connectivity.floating([p["id"] for p in plan["parts"] if p.get("free")])
        except Exception:                                   # a failing check never fails the build
            pass
    report["partial"] = bool(report["missing"] or report["part_errors"])
    report["parts_built"] = sorted(o.name for o in bpy.data.objects if o.type == "MESH")
    bpy.context.scene.frame_set(1); bb = {}
    for o in bpy.data.objects:
        if o.type == "MESH":
            vs = [o.matrix_world @ v.co for v in o.data.vertices]
            bb[o.name] = [[round(min(v[i] for v in vs) * 1000, 1) for i in range(3)], [round(max(v[i] for v in vs) * 1000, 1) for i in range(3)]]
    report["parts_bbox_mm"] = bb
    vol = fa = ed = ve = 0
    for o in bpy.data.objects:
        if o.type == "MESH":
            m = bmesh.new(); m.from_mesh(o.data); m.transform(o.matrix_world)
            try: vol += abs(m.calc_volume()) * 1e9           # m3 -> mm3
            except Exception: pass
            fa += len(m.faces); ed += len(m.edges); ve += len(m.verts); m.free()
    report["totals"] = dict(volume_mm3=round(vol, 1), faces=fa, edges=ed, vertices=ve)
    report["totals"]["vertices_before_opt"] = sum(a for a, _b in report["opt"].values()) if report["opt"] else ve
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(proj, "scene.blend"))
report["ok"] = not (report["missing"] or report["part_errors"] or report["issues"])
json.dump(report, open(os.path.join(proj, "report.json"), "w"), indent=1)
print("BX_DONE", report["ok"])
