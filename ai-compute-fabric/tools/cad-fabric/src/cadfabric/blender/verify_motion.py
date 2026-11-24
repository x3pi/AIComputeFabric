"""Independent motion verifier that reads the Blender SCENE only (no SDK needed), so it works for raw bpy.
Joint declaration convention (custom properties on an Empty; children are objects parented to it):
    bx_joint   = "hinge"           joint kind (only hinge is checked for now)
    bx_axis    = [x, y, z]         rotation axis, world frame at rest
    bx_coaxial = "wheel,axle"      optional: bodies of revolution that must spin about that axis
The joint position is the Empty's world location at rest. Parts are MESH objects.
Checks: object scale, rigid motion of every child about the declared axis through the declared point,
coaxial intent, collisions between parts that are not parent/child/siblings of a joint."""
import bpy, bmesh
import os
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

TOL_MM = 0.05
GROUND_TOL_MM = float(os.environ.get("BX_GROUND_TOL_MM", "5"))        # a part resting on the ground (z=0) may dip at most this far below it while moving


def _eval(o):
    dg = bpy.context.evaluated_depsgraph_get(); eo = o.evaluated_get(dg); me = eo.to_mesh()
    P = np.array([(eo.matrix_world @ v.co)[:] for v in me.vertices]); eo.to_mesh_clear(); return P


def _tree(o):
    dg = bpy.context.evaluated_depsgraph_get(); eo = o.evaluated_get(dg); me = eo.to_mesh()
    bm = bmesh.new(); bm.from_mesh(me); bm.transform(eo.matrix_world)
    t = BVHTree.FromBMesh(bm); bm.free(); eo.to_mesh_clear(); return t


def _rigid(A, B):
    """Kabsch: R,t with B ~ R A + t; returns R,t,residual(max)"""
    ca, cb = A.mean(0), B.mean(0); H = (A - ca).T @ (B - cb); U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T)); D = np.diag([1, 1, d]); R = Vt.T @ D @ U.T
    t = cb - R @ ca; return R, t, float(np.abs((A @ R.T + t) - B).max())


def _axis_angle(R):
    ang = float(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))
    if ang < 1e-6: return np.array([0, 0, 1.0]), 0.0
    w, v = np.linalg.eig(R); k = int(np.argmin(np.abs(w - 1))); ax = np.real(v[:, k]); return ax / np.linalg.norm(ax), ang


def _rel(j, meshes, rest, c, parent_cache):
    """kid c's current vertices expressed in the frame of the joint's parent part (nested joints: the parent may itself be moving), so a joint is judged by its OWN motion"""
    B = _eval(meshes[c])
    if not j["parent"] or j["parent"] not in meshes:
        return B
    if j["parent"] not in parent_cache:
        Rp, tp, _ = _rigid(rest[j["parent"]], _eval(meshes[j["parent"]]))
        parent_cache[j["parent"]] = (Rp, tp)
    Rp, tp = parent_cache[j["parent"]]
    return (B - tp) @ Rp                                    # Rp^T (B - tp) for row vectors


def _pivot_perp(R, t, a, origin):
    """distance of the declared pivot to the true fixed line of the motion, solved in the plane perpendicular to the declared axis (a 3x3 least squares is
    unstable: the matrix is singular along the axis and float32 vertex noise is amplified by 1e8)"""
    u = np.cross(a, [1, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1, 0]); u /= np.linalg.norm(u); v = np.cross(a, u)
    Bm = np.array([u, v]); M = Bm @ (np.eye(3) - R) @ Bm.T; rhs = Bm @ t
    q = np.linalg.solve(M, rhs)                              # pivot coordinates in the (u, v) plane
    return float(np.hypot(q[0] - u @ origin, q[1] - v @ origin))


def joints():
    return [o for o in bpy.data.objects if o.type == "EMPTY" and "bx_joint" in o.keys()]


def verify(frames, tol_mm=TOL_MM, extra_allowed=()):
    issues = []; meshes = {o.name: o for o in bpy.data.objects if o.type == "MESH"}
    for n, o in meshes.items():
        sc = o.matrix_world.to_scale()
        if any(abs(x - 1.0) > 1e-6 for x in sc):
            issues.append(dict(kind="object_scale", part=n, scale=tuple(round(x, 6) for x in sc)))
    bpy.context.scene.frame_set(frames[0]); rest = {n: _eval(o) for n, o in meshes.items()}
    ext = {n: float((P.max(0) - P.min(0)).max()) for n, P in rest.items() if len(P)}      # units sanity: a part built in mm-as-metres is orders of magnitude off
    if len(ext) >= 3:
        med = float(np.median(list(ext.values())))
        for n, e in ext.items():
            if med > 0 and (e > 50 * med or e > 20.0):
                issues.append(dict(kind="scale_outlier", part=n, extent_m=round(e, 3), median_m=round(med, 4), hint="geometry must be in metres (divide mm by 1000)"))
    J = []
    for e in joints():
        kids = [c.name for c in e.children if c.type == "MESH"]
        J.append(dict(name=e.name, kind=e["bx_joint"], axis=np.array(e["bx_axis"], float) / np.linalg.norm(e["bx_axis"]),
                      origin=np.array(e.matrix_world.translation[:]), kids=kids,
                      parent=(e.parent.name if e.parent and e.parent.type == "MESH" else None),
                      coaxial=[x for x in e.get("bx_coaxial", "").split(",") if x]))
    allowed = {frozenset(p) for p in extra_allowed}
    for j in J:
        group = j["kids"] + ([j["parent"]] if j["parent"] else [])
        for a in group:
            for b in group:
                if a < b: allowed.add(frozenset((a, b)))
        for c in j["coaxial"]:           # intent: body of revolution centred on the axis
            if c not in rest: issues.append(dict(kind="coaxial_unknown", joint=j["name"], part=c)); continue
            P = rest[c]; cen = P.mean(0); w, V = np.linalg.eigh(np.cov((P - cen).T))
            par = max(abs(float(V[:, i] @ j["axis"])) for i in range(3))
            off = (cen - j["origin"]) - j["axis"] * float((cen - j["origin"]) @ j["axis"])
            if par < 1 - 1e-6 or np.linalg.norm(off) * 1000 > tol_mm:
                issues.append(dict(kind="not_coaxial", joint=j["name"], part=c, offset_mm=round(float(np.linalg.norm(off)) * 1000, 3)))
    for f in frames:
        bpy.context.scene.frame_set(f); pc = {}
        for j in J:
            if j["kind"] != "hinge": continue
            for c in j["kids"]:
                R, t, res = _rigid(rest[c], _rel(j, meshes, rest, c, pc))
                if res * 1000 > tol_mm:
                    issues.append(dict(kind="not_rigid", joint=j["name"], part=c, frame=f, err_mm=round(res * 1000, 3))); continue
                ax, ang = _axis_angle(R)
                if ang < 0.02: continue                          # too small to measure the axis and pivot above float32 noise; later frames judge the joint
                if np.linalg.norm(np.cross(ax, j["axis"])) > 1e-3 * max(1.0, 0.1 / ang):
                    issues.append(dict(kind="wrong_axis", joint=j["name"], part=c, frame=f)); continue
                off_mm = _pivot_perp(R, t, j["axis"], j["origin"]) * 1000
                if off_mm > tol_mm * max(1.0, 0.1 / ang):
                    issues.append(dict(kind="wrong_pivot", joint=j["name"], part=c, frame=f, off_mm=round(off_mm, 3)))
    for j in J:                       # declared motion intent: total swept angle (bx_span, rad) must be achieved
        e = bpy.data.objects[j["name"]]
        if "bx_span" not in e.keys() or not j["kids"]: continue
        c = j["kids"][0]; a = j["axis"]; pc2 = {}; v0 = np.cross(a, [1, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1, 0]); v0 /= np.linalg.norm(v0)
        seq = []
        for f in frames:
            bpy.context.scene.frame_set(f); pc2 = {}; R, _, _ = _rigid(rest[c], _rel(j, meshes, rest, c, pc2)); w = R @ v0
            seq.append(float(np.arctan2(np.cross(v0, w) @ a, v0 @ w)))
        un = np.unwrap(seq); rel = un - un[0]; span = float(rel[int(np.argmax(np.abs(rel)))])   # signed extreme excursion
        if abs(span - float(e["bx_span"])) > 0.02 * max(1e-9, abs(float(e["bx_span"]))) + 1e-3:
            issues.append(dict(kind="span_mismatch", joint=j["name"], expected=float(e["bx_span"]), measured=round(span, 4)))
    grounded = [n for n, P in rest.items() if len(P) and abs(float(P[:, 2].min())) * 1000 <= 1.0]      # parts that touch the ground at rest (z=0 is the floor)
    worst = {}
    for f in frames:
        bpy.context.scene.frame_set(f)
        for n in grounded:
            dip = -float(_eval(meshes[n])[:, 2].min()) * 1000
            if dip > GROUND_TOL_MM and dip > worst.get(n, (0, 0))[0]: worst[n] = (dip, f)
    for n, (dip, f) in worst.items():
        issues.append(dict(kind="ground_penetration", part=n, frame=f, depth_mm=round(dip, 1),
                           hint="the declared motion pushes a part that rests on the ground %.0f mm below it; check which joint moves it (a suspension must move the frame, not sink the wheel) or reduce its span" % dip))
    names = list(meshes); hits = {}
    for f in frames:
        bpy.context.scene.frame_set(f); T = {n: _tree(meshes[n]) for n in names}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if frozenset((a, b)) in allowed: continue
                if T[a].overlap(T[b]): hits.setdefault(f"{a}|{b}", []).append(f)
    for k, fl in hits.items(): issues.append(dict(kind="collision", pair=k, frames=fl))
    return dict(ok=not issues, issues=issues)
