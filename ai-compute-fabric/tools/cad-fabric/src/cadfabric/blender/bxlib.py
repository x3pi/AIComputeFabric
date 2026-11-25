"""Deterministic part library: a worker describes a part as a JSON recipe of primitive operations (mm, world coordinates) and this module builds
the mesh. The common shapes (boxes, tubes between two points, rings, beams along a polyline, lofted shells, spokes, spheres) are written and tested
once here, so the small model does not have to get bmesh right for each part. `validate` is pure Python; `build` runs inside Blender.

recipe = {"ops": [ {"op": "box", "center": [x,y,z], "size": [dx,dy,dz], "rot": {"axis": [..], "deg": 25}, "bevel": 2}, ... ]}
 box      center, size, optional rot (about the centre), optional bevel (mm)
 cylinder a, b (end centres), r, optional r2 (taper), seg, bevel
 ring     center, axis ("x"|"y"|"z" or a vector), r_out, r_in, width, seg, bevel
 beam     points (polyline, >=2), w, h (rectangular section), optional up (default [0,0,1]), bevel
 sphere   center, r (or radii [rx,ry,rz]), seg
 loft     sections [{"x": station along X, "y": centre y, "z": centre z, "w": width (Y), "h": height (Z)}, ...] (>=2), optional round (2=ellipse, 4=squarish), n.
          Shape controls: a section may carry its own round / round_top / round_bottom (exponent of the upper / lower half: big = flat and boxy, 2 = round, 1 = pointed),
          so one body can be flat on top and round underneath; smooth: k (0-8) inserts k interpolated sections between each pair (Catmull-Rom), so a few sections give a
          continuous curved body instead of straight facets
 spokes   center, axis, n, r_in, r_out, w (tangential width), t (thickness along the axis)
 disc     like cylinder but given as center, axis, r, thickness
 tube     points (polyline, >=2), r: a bent ROUND tube (fork leg, handlebar, exhaust, frame spar, brake line) with smooth joints
 panel    sections as in loft, optional arc [deg0, deg1] (default [0,180] = the upper half; [0,360] wraps all round), thick (mm, default 3): a thin curved SHEET of
          that thickness (fairing, fender, side panel, seat pan) instead of a solid loft
"""
import math, os

M = 0.001
OPS = {"box", "cylinder", "ring", "beam", "sphere", "loft", "spokes", "disc", "tube", "panel"}
AXES = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}


TESS_MM = float(os.environ.get("BX_TESS_MM", "0.4"))        # largest allowed gap between a true circle and its polygon (sagitta), in mm
HARD_DEG = 30.0                                              # edges flatter than this are smooth surface, not features
SCENE_PARTS = 0


def _seg(o, r_mm, lo=12, hi=96):
    """Adaptive tessellation: the fewest segments whose chord error stays within TESS_MM for radius r_mm (a 20 mm pin gets 16, a 300 mm tire 56), a multiple of 4.
    An explicit "seg" in the recipe always wins."""
    if "seg" in o:
        return max(3, int(o["seg"]))
    r = max(float(r_mm), 1e-6)
    tess = max(TESS_MM, 0.6) if (SCENE_PARTS > 55 and r < 25.0 and "BX_TESS_MM" not in os.environ) else TESS_MM
    n = math.ceil(math.pi / math.acos(max(-1.0, min(1.0, 1.0 - min(tess, r) / r))))
    return int(min(hi, max(lo, 4 * math.ceil(n / 4))))


def _hard_edges(bm, deg=HARD_DEG):
    """Edges worth beveling: boundary edges and edges where the surface turns by more than `deg` degrees. The seams of a smooth cylinder are not."""
    lim, out = math.radians(deg), []
    for e in bm.edges:
        if len(e.link_faces) != 2 or e.link_faces[0].normal.angle(e.link_faces[1].normal, 0.0) > lim:
            out.append(e)
    return out


def _vec(v, n=3):
    return isinstance(v, (list, tuple)) and len(v) == n and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)


def _num(v, lo=None):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (lo is None or v > lo)


def _axis(a):
    if isinstance(a, str) and a in AXES:
        return AXES[a]
    return tuple(a) if _vec(a) and any(a) else None


def _ordered_sections(secs):
    return list(reversed(secs)) if len(secs) >= 2 and secs[0]["x"] > secs[-1]["x"] else secs


def _mono_x(secs):
    return len(secs) < 2 or all(secs[k]["x"] < secs[k + 1]["x"] for k in range(len(secs) - 1)) or all(secs[k]["x"] > secs[k + 1]["x"] for k in range(len(secs) - 1))


def _bad_sections(o, w):
    """extra checks for loft / panel sections: optional round values and smooth"""
    e = []
    for x in o.get("sections") or []:
        if isinstance(x, dict):
            for q in ("round", "round_top", "round_bottom"):
                if q in x and not (_num(x[q]) and 1 <= x[q] <= 12): e.append(w + "section %s must be a number from 1 to 12" % q); break
    if "smooth" in o and not (isinstance(o["smooth"], int) and not isinstance(o["smooth"], bool) and 0 <= o["smooth"] <= 8):
        e.append(w + "smooth must be an integer 0-8")
    return e



def validate(recipe):
    """-> list of readable problems (empty = fine)."""
    e = []
    if not isinstance(recipe, dict) or not isinstance(recipe.get("ops"), list) or not recipe["ops"]:
        return ['recipe must be {"ops": [ ... ]} with at least one operation']
    for i, o in enumerate(recipe["ops"]):
        t = o.get("op") if isinstance(o, dict) else None
        w = "op %d (%s): " % (i, t)
        if t not in OPS:
            e.append("op %d: unknown op %r (use %s)" % (i, t, ", ".join(sorted(OPS)))); continue
        if "bevel" in o and not _num(o["bevel"], -1e-9):
            e.append(w + "bevel must be a number >= 0")
        if t == "box":
            if not _vec(o.get("center")) or not _vec(o.get("size")) or not all(x > 0 for x in o.get("size", [0])):
                e.append(w + "needs center [x,y,z] and size [dx,dy,dz] > 0")
            if "rot" in o and not (isinstance(o["rot"], dict) and _axis(o["rot"].get("axis")) and _num(o["rot"].get("deg"))):
                e.append(w + 'rot must be {"axis": [..], "deg": number}')
        elif t == "cylinder":
            if not _vec(o.get("a")) or not _vec(o.get("b")) or o.get("a") == o.get("b") or not _num(o.get("r"), 0) or ("r2" in o and not _num(o["r2"], -1e-9)):
                e.append(w + "needs different end centres a, b [x,y,z] and r > 0 (optional r2 >= 0)")
        elif t == "ring":
            if not _vec(o.get("center")) or not _axis(o.get("axis")) or not _num(o.get("r_out"), 0) or not _num(o.get("r_in"), -1e-9) or not _num(o.get("width"), 0):
                e.append(w + 'needs center, axis ("x","y","z" or vector), r_out > r_in >= 0, width > 0')
            elif o["r_in"] >= o["r_out"]:
                e.append(w + "r_in must be smaller than r_out")
        elif t == "beam":
            pts = o.get("points")
            if not (isinstance(pts, list) and len(pts) >= 2 and all(_vec(p) for p in pts)) or not _num(o.get("w"), 0) or not _num(o.get("h"), 0):
                e.append(w + "needs points [[x,y,z],...] (2 or more), w > 0, h > 0")
            elif any(pts[k] == pts[k + 1] for k in range(len(pts) - 1)):
                e.append(w + "two consecutive points are equal")
        elif t == "sphere":
            r = o.get("radii")
            if not _vec(o.get("center")) or not (_num(o.get("r"), 0) or (_vec(r) and all(x > 0 for x in r))):
                e.append(w + "needs center and r > 0 (or radii [rx,ry,rz])")
        elif t == "loft":
            s = o.get("sections")
            if not (isinstance(s, list) and len(s) >= 2 and all(isinstance(x, dict) and all(_num(x.get(k)) for k in ("x", "y", "z")) and _num(x.get("w"), 0) and _num(x.get("h"), 0) for x in s)):
                e.append(w + "sections need 2 or more of {x,y,z,w>0,h>0}")
            elif not _mono_x(s):
                e.append(w + "section x values must be strictly monotonic (increasing or decreasing)")
            e += _bad_sections(o, w)
        elif t == "tube":
            pts = o.get("points")
            if not (isinstance(pts, list) and len(pts) >= 2 and all(_vec(p) for p in pts)) or not _num(o.get("r"), 0):
                e.append(w + "needs points [[x,y,z],...] (2 or more) and r > 0")
            elif any(pts[k] == pts[k + 1] for k in range(len(pts) - 1)):
                e.append(w + "two consecutive points are equal")
        elif t == "panel":
            sec = o.get("sections"); arc = o.get("arc", [0, 180])
            if not (isinstance(sec, list) and len(sec) >= 2 and all(isinstance(x, dict) and all(_num(x.get(k)) for k in ("x", "y", "z")) and _num(x.get("w"), 0) and _num(x.get("h"), 0) for x in sec)):
                e.append(w + "sections need 2 or more of {x,y,z,w>0,h>0}")
            elif not _mono_x(sec):
                e.append(w + "section x values must be strictly monotonic (increasing or decreasing)")
            if not (isinstance(arc, list) and len(arc) == 2 and all(_num(a) for a in arc) and 0 < arc[1] - arc[0] <= 360):
                e.append(w + "arc must be [deg0, deg1] with 0 < deg1-deg0 <= 360")
            if "thick" in o and not _num(o["thick"], 0):
                e.append(w + "thick must be a number > 0")
            e += _bad_sections(o, w)
        elif t == "spokes":
            if not _vec(o.get("center")) or not _axis(o.get("axis")) or not isinstance(o.get("n"), int) or not 1 <= o["n"] <= 48 or not all(_num(o.get(k), 0) for k in ("r_in", "r_out", "w", "t")):
                e.append(w + "needs center, axis, n (1..48), r_in, r_out, w, t > 0")
        elif t == "disc":
            if not _vec(o.get("center")) or not _axis(o.get("axis")) or not _num(o.get("r"), 0) or not _num(o.get("thickness"), 0):
                e.append(w + "needs center, axis, r > 0, thickness > 0")
    return e


def build(bpy, bmesh, pid, recipe):
    """Create ONE mesh object named pid from the recipe (world coordinates, metres; object transform identity). Raises ValueError on a bad recipe."""
    probs = validate(recipe)
    if probs:
        raise ValueError("bad recipe: " + "; ".join(probs[:4]))
    bm = bmesh.new()
    for o in recipe["ops"]:
        _emit(bm, bmesh, bpy, o)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(pid)
    bm.to_mesh(me)
    bm.free()
    return bpy.data.objects.new(pid, me)


# ---- internals (Blender only) -------------------------------------------------------------------------------------------
def _basis(d, up=(0, 0, 1)):
    """3x3 matrix whose X column is the unit direction d, Z column the part of `up` perpendicular to d."""
    from mathutils import Matrix, Vector
    x = Vector(d).normalized()
    u = Vector(up)
    z = u - x * u.dot(x)
    if z.length < 1e-6:
        z = Vector((0, 1, 0)) - x * x.y if abs(x.y) < 0.9 else Vector((1, 0, 0)) - x * x.x
    z.normalize()
    y = z.cross(x)
    return Matrix((x, y, z)).transposed()


def _z_to(d):
    from mathutils import Vector
    return Vector((0, 0, 1)).rotation_difference(Vector(d).normalized()).to_matrix()


def _emit(bm, bmesh, bpy, o):
    t = o["op"]
    bev = float(o.get("bevel", 0)) * M
    tmp = bmesh.new()
    try:
        {"box": _box, "cylinder": _cyl, "ring": _ring, "beam": _beam, "sphere": _sphere, "loft": _loft, "spokes": _spokes, "disc": _disc, "tube": _tube, "panel": _panel}[t](tmp, bmesh, o)
        if bev > 0 and t not in ("sphere",):
            safe = tmp.copy()
            try:                                           # a failing bevel never fails the part: it is simply left out
                hard = _hard_edges(safe)
                bmesh.ops.bevel(safe, geom=hard, offset=bev, segments=(1 if bev * 1000 < 2.5 else 2), affect="EDGES") if hard else None
                if len(safe.faces) > 0 and all(v.is_valid for v in safe.verts):
                    tmp.free(); tmp = safe
                else:
                    safe.free()
            except Exception:                              # noqa: BLE001
                safe.free()
        me = bpy.data.meshes.new("_bxlib_tmp")
        tmp.to_mesh(me)
        bm.from_mesh(me)                                   # appends to bm
        bpy.data.meshes.remove(me)
    finally:
        tmp.free()


def _place(bm, bmesh, mat3, center_mm):
    from mathutils import Matrix, Vector
    m = mat3.to_4x4(); m.translation = Vector(center_mm) * M
    bmesh.ops.transform(bm, matrix=m, verts=bm.verts)


def _box(bm, bmesh, o):
    from mathutils import Matrix
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=[s * M for s in o["size"]], verts=bm.verts)
    r = Matrix.Identity(3)
    if "rot" in o:
        r = Matrix.Rotation(math.radians(o["rot"]["deg"]), 3, _axis(o["rot"]["axis"]))
    _place(bm, bmesh, r, o["center"])


def _cyl(bm, bmesh, o):
    a, b = o["a"], o["b"]
    d = [b[i] - a[i] for i in range(3)]
    length = math.sqrt(sum(x * x for x in d))
    r2 = o.get("r2", o["r"])
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=_seg(o, max(o["r"], r2)), radius1=o["r"] * M, radius2=r2 * M, depth=length * M)
    _place(bm, bmesh, _z_to(d), [(a[i] + b[i]) / 2 for i in range(3)])


def _disc(bm, bmesh, o):
    ax = _axis(o["axis"])
    _cyl(bm, bmesh, {"a": [o["center"][i] - ax[i] / math.sqrt(sum(x * x for x in ax)) * o["thickness"] / 2 for i in range(3)],
                     "b": [o["center"][i] + ax[i] / math.sqrt(sum(x * x for x in ax)) * o["thickness"] / 2 for i in range(3)], "r": o["r"], **({"seg": o["seg"]} if "seg" in o else {})})


def _ring(bm, bmesh, o):
    seg, ro, ri, h = _seg(o, o["r_out"]), o["r_out"] * M, o["r_in"] * M, o["width"] * M
    if ri <= 0:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=seg, radius1=ro, radius2=ro, depth=h)
    else:
        to, ti, bo, bi = [], [], [], []
        for k in range(seg):
            a = 2 * math.pi * k / seg; c, s = math.cos(a), math.sin(a)
            to.append(bm.verts.new((ro * c, ro * s, h / 2))); ti.append(bm.verts.new((ri * c, ri * s, h / 2)))
            bo.append(bm.verts.new((ro * c, ro * s, -h / 2))); bi.append(bm.verts.new((ri * c, ri * s, -h / 2)))
        for k in range(seg):
            n = (k + 1) % seg
            bm.faces.new((to[k], to[n], ti[n], ti[k])); bm.faces.new((bo[k], bi[k], bi[n], bo[n]))
            bm.faces.new((to[k], bo[k], bo[n], to[n])); bm.faces.new((ti[k], ti[n], bi[n], bi[k]))
    _place(bm, bmesh, _z_to(_axis(o["axis"])), o["center"])


def _beam(bm, bmesh, o):
    from mathutils import Vector
    pts = [Vector(p) for p in o["points"]]
    up = o.get("up", (0, 0, 1))
    for k in range(len(pts) - 1):
        d = pts[k + 1] - pts[k]
        seg = bmesh.new()
        bmesh.ops.create_cube(seg, size=1.0)
        extra = (o["w"] if k < len(pts) - 2 else 0) * 0.5                    # corners overlap a little so a bent beam has no gap
        bmesh.ops.scale(seg, vec=[(d.length + extra) * M, o["w"] * M, o["h"] * M], verts=seg.verts)
        mid = (pts[k] + pts[k + 1]) / 2 + d.normalized() * extra / 2
        _place(seg, bmesh, _basis(d, up), list(mid))
        me = bpy_mesh_from(seg)
        bm.from_mesh(me); _drop(me); seg.free()


def bpy_mesh_from(b):
    import bpy
    me = bpy.data.meshes.new("_bxlib_seg"); b.to_mesh(me); return me


def _drop(me):
    import bpy
    bpy.data.meshes.remove(me)


def _sphere(bm, bmesh, o):
    rr = o.get("radii") or [o["r"]] * 3
    useg = _seg(o, max(rr), lo=12, hi=48)
    bmesh.ops.create_uvsphere(bm, u_segments=useg, v_segments=max(6, useg // 2), radius=1.0)
    bmesh.ops.scale(bm, vec=[x * M for x in rr], verts=bm.verts)
    bmesh.ops.translate(bm, vec=[c * M for c in o["center"]], verts=bm.verts)


SECTION_KEYS = ("x", "y", "z", "w", "h", "round", "round_top", "round_bottom")


def _smooth_sections(secs, k):
    """k extra sections between every pair, Catmull-Rom through the given ones (end points repeated), every numeric key interpolated."""
    k = int(k)
    if k <= 0 or len(secs) < 2:
        return secs
    keys = [q for q in SECTION_KEYS if any(q in x for x in secs)]
    full = [{q: x.get(q, x.get("round", 2.5) if q.startswith("round") else x[q]) for q in keys} for x in secs]
    out = []
    for i in range(len(full) - 1):
        p0, p1, p2, p3 = full[max(i - 1, 0)], full[i], full[i + 1], full[min(i + 2, len(full) - 1)]
        for j in range(k + 1):
            t = j / (k + 1); t2, t3 = t * t, t * t * t
            out.append({q: 0.5 * ((2 * p1[q]) + (-p0[q] + p2[q]) * t + (2 * p0[q] - 5 * p1[q] + 4 * p2[q] - p3[q]) * t2 + (-p0[q] + 3 * p1[q] - 3 * p2[q] + p3[q]) * t3) for q in keys})
    out.append(dict(full[-1]))
    for x in out:
        x["w"] = max(x["w"], 1e-3); x["h"] = max(x["h"], 1e-3)
        for q in ("round", "round_top", "round_bottom"):
            if q in x: x[q] = max(x[q], 1.0)
    return out


def _ring_pt(s, a, p0):
    """offset (y, z) in mm of the section outline at angle a; the upper and lower halves may have their own exponent"""
    c, sn = math.cos(a), math.sin(a)
    p = float(s.get("round_top" if sn >= 0 else "round_bottom", s.get("round", p0)))
    return (s["w"] / 2) * math.copysign(abs(c) ** (2 / p), c), (s["h"] / 2) * math.copysign(abs(sn) ** (2 / p), sn)


def _loft(bm, bmesh, o):
    n, p = int(o.get("n", 16)), float(o.get("round", 2.5))
    rings = []
    for s in _smooth_sections(_ordered_sections(o["sections"]), o.get("smooth", 0)):
        ring = []
        for k in range(n):
            y, z = _ring_pt(s, 2 * math.pi * k / n, p)
            ring.append(bm.verts.new(((s["x"]) * M, (s["y"] + y) * M, (s["z"] + z) * M)))
        rings.append(ring)
    for a, b in zip(rings, rings[1:]):
        for k in range(n):
            m = (k + 1) % n
            bm.faces.new((a[k], a[m], b[m], b[k]))
    bm.faces.new(rings[0][::-1]); bm.faces.new(rings[-1])


def _piece(bm, bmesh, fn, o):
    """Build one primitive in its OWN BMesh and append it: the placement helpers move every vertex of the mesh they are given, so pieces must not share one."""
    seg = bmesh.new()
    try:
        fn(seg, bmesh, o)
        me = bpy_mesh_from(seg)
        bm.from_mesh(me); _drop(me)
    finally:
        seg.free()


def _tube(bm, bmesh, o):
    pts = o["points"]; r = o["r"]
    for a, b in zip(pts, pts[1:]):
        _piece(bm, bmesh, _cyl, {"a": a, "b": b, "r": r})
    for p in pts[1:-1]:                                    # a sphere at every bend closes the joint smoothly
        _piece(bm, bmesh, _sphere, {"center": p, "r": r})


def _panel(bm, bmesh, o):
    """A curved thin sheet: the loft sections restricted to the angular range `arc`, left open (no end caps) and given thickness by solidify."""
    a0, a1 = [math.radians(x) for x in o.get("arc", [0, 180])]
    full = abs((a1 - a0) - 2 * math.pi) < 1e-6
    p = float(o.get("round", 2.5)); n = max(6, int(o.get("n", 12)))
    cols = n if full else n + 1
    rows = []
    for s in _smooth_sections(_ordered_sections(o["sections"]), o.get("smooth", 0)):
        row = []
        for k in range(cols):
            y, z = _ring_pt(s, a0 + (a1 - a0) * k / n, p)
            row.append(bm.verts.new((s["x"] * M, (s["y"] + y) * M, (s["z"] + z) * M)))
        rows.append(row)
    for ra, rb in zip(rows, rows[1:]):
        for k in range(cols if full else cols - 1):
            m = (k + 1) % cols
            bm.faces.new((ra[k], ra[m], rb[m], rb[k]))
    bm.normal_update()                                     # faces made by hand have no normals yet; without them solidify adds no thickness
    bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=float(o.get("thick", 3.0)) * M)


def _spokes(bm, bmesh, o):
    from mathutils import Matrix, Vector
    ax = Vector(_axis(o["axis"])).normalized()
    base = _basis(ax if abs(ax.x) < 0.99 else Vector((0, 1, 0)))
    for k in range(int(o["n"])):
        seg = bmesh.new()
        bmesh.ops.create_cube(seg, size=1.0)
        length = o["r_out"] - o["r_in"]
        bmesh.ops.scale(seg, vec=[length * M, o["w"] * M, o["t"] * M], verts=seg.verts)
        bmesh.ops.translate(seg, vec=[(o["r_in"] + length / 2) * M, 0, 0], verts=seg.verts)       # radial, X
        bmesh.ops.rotate(seg, cent=(0, 0, 0), matrix=Matrix.Rotation(2 * math.pi * k / o["n"], 3, "Z"), verts=seg.verts)   # about the local axis
        me = bpy_mesh_from(seg)
        bm.from_mesh(me); _drop(me); seg.free()
    _place(bm, bmesh, _z_to(ax), o["center"])


# ---- pure-Python analytical bbox & plan conformance gate (runs outside Blender) ----------------------------------------
def _norm3(v):
    L = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / L, v[1] / L, v[2] / L) if L > 1e-12 else (0.0, 0.0, 1.0)


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _rot_vec(v, axis, deg):
    k = _norm3(axis)
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    kv = _cross3(k, v)
    kd = _dot3(k, v) * (1.0 - c)
    return (v[0] * c + kv[0] * s + k[0] * kd, v[1] * c + kv[1] * s + k[1] * kd, v[2] * c + kv[2] * s + k[2] * kd)


def _basis_py(d, up=(0, 0, 1)):
    x = _norm3(d)
    u = tuple(float(q) for q in up)
    ux = _dot3(u, x)
    z = (u[0] - x[0] * ux, u[1] - x[1] * ux, u[2] - x[2] * ux)
    if math.sqrt(_dot3(z, z)) < 1e-6:
        if abs(x[1]) < 0.9:
            z = (-x[0] * x[1], 1.0 - x[1] * x[1], -x[2] * x[1])
        else:
            z = (1.0 - x[0] * x[0], -x[1] * x[0], -x[2] * x[0])
    z = _norm3(z)
    y = _cross3(z, x)
    return x, y, z


def _op_bbox(o):
    """Pure-Python world AABB (lo, hi) in mm for one validated recipe op."""
    lo = [float("inf"), float("inf"), float("inf")]
    hi = [float("-inf"), float("-inf"), float("-inf")]

    def add_pt(p):
        for i in range(3):
            if p[i] < lo[i]: lo[i] = float(p[i])
            if p[i] > hi[i]: hi[i] = float(p[i])

    def add_cyl(a, b, r1, r2):
        d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        u = _norm3(d)
        for i in range(3):
            s = math.sqrt(max(0.0, 1.0 - u[i] * u[i]))
            lo[i] = min(lo[i], a[i] - r1 * s, b[i] - r2 * s)
            hi[i] = max(hi[i], a[i] + r1 * s, b[i] + r2 * s)

    t = o["op"]
    if t == "box":
        hx, hy, hz = o["size"][0] / 2.0, o["size"][1] / 2.0, o["size"][2] / 2.0
        cx, cy, cz = o["center"]
        ax = _axis(o["rot"]["axis"]) if "rot" in o else None
        deg = o["rot"]["deg"] if "rot" in o else 0.0
        for sx in (-hx, hx):
            for sy in (-hy, hy):
                for sz in (-hz, hz):
                    v = _rot_vec((sx, sy, sz), ax, deg) if ax is not None else (sx, sy, sz)
                    add_pt((cx + v[0], cy + v[1], cz + v[2]))
    elif t == "cylinder":
        add_cyl(o["a"], o["b"], o["r"], o.get("r2", o["r"]))
    elif t == "disc":
        ax = _norm3(_axis(o["axis"]))
        c, half = o["center"], o["thickness"] / 2.0
        add_cyl([c[i] - ax[i] * half for i in range(3)], [c[i] + ax[i] * half for i in range(3)], o["r"], o["r"])
    elif t == "ring":
        ax = _norm3(_axis(o["axis"]))
        c, half = o["center"], o["width"] / 2.0
        add_cyl([c[i] - ax[i] * half for i in range(3)], [c[i] + ax[i] * half for i in range(3)], o["r_out"], o["r_out"])
    elif t == "spokes":
        ax = _norm3(_axis(o["axis"]))
        c, half = o["center"], o["t"] / 2.0
        add_cyl([c[i] - ax[i] * half for i in range(3)], [c[i] + ax[i] * half for i in range(3)], o["r_out"], o["r_out"])
    elif t == "sphere":
        rr = o.get("radii") or [o["r"]] * 3
        c = o["center"]
        for i in range(3):
            lo[i] = c[i] - rr[i]; hi[i] = c[i] + rr[i]
    elif t == "beam":
        pts, up, w, h = o["points"], o.get("up", (0, 0, 1)), o["w"], o["h"]
        for k in range(len(pts) - 1):
            p0, p1 = pts[k], pts[k + 1]
            d = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
            L = math.sqrt(_dot3(d, d))
            u = _norm3(d)
            extra = (w if k < len(pts) - 2 else 0.0) * 0.5
            mid = ((p0[0] + p1[0]) / 2.0 + u[0] * extra / 2.0, (p0[1] + p1[1]) / 2.0 + u[1] * extra / 2.0, (p0[2] + p1[2]) / 2.0 + u[2] * extra / 2.0)
            bx, by, bz = _basis_py(d, up)
            hx, hy, hz = (L + extra) / 2.0, w / 2.0, h / 2.0
            for sx in (-hx, hx):
                for sy in (-hy, hy):
                    for sz in (-hz, hz):
                        add_pt((mid[0] + sx * bx[0] + sy * by[0] + sz * bz[0],
                                mid[1] + sx * bx[1] + sy * by[1] + sz * bz[1],
                                mid[2] + sx * bx[2] + sy * by[2] + sz * bz[2]))
    elif t == "tube":
        pts, r = o["points"], o["r"]
        for a, b in zip(pts, pts[1:]):
            add_cyl(a, b, r, r)
        for p in pts[1:-1]:
            for i in range(3):
                lo[i] = min(lo[i], p[i] - r); hi[i] = max(hi[i], p[i] + r)
    elif t == "loft":
        n, p = int(o.get("n", 16)), float(o.get("round", 2.5))
        for s in _smooth_sections(_ordered_sections(o["sections"]), o.get("smooth", 0)):
            for k in range(n):
                y, z = _ring_pt(s, 2 * math.pi * k / n, p)
                add_pt((s["x"], s["y"] + y, s["z"] + z))
    elif t == "panel":
        a0, a1 = [math.radians(x) for x in o.get("arc", [0, 180])]
        full = abs((a1 - a0) - 2 * math.pi) < 1e-6
        p = float(o.get("round", 2.5)); n = max(6, int(o.get("n", 12)))
        cols = n if full else n + 1
        for s in _smooth_sections(_ordered_sections(o["sections"]), o.get("smooth", 0)):
            for k in range(cols):
                y, z = _ring_pt(s, a0 + (a1 - a0) * k / n, p)
                add_pt((s["x"], s["y"] + y, s["z"] + z))
    return lo, hi


def recipe_bbox(recipe):
    """Compute world-space (lo, hi) in mm for a recipe without launching Blender, or None if invalid."""
    if validate(recipe):
        return None
    lo = [float("inf"), float("inf"), float("inf")]
    hi = [float("-inf"), float("-inf"), float("-inf")]
    for o in recipe["ops"]:
        a, b = _op_bbox(o)
        for i in range(3):
            lo[i] = min(lo[i], a[i]); hi[i] = max(hi[i], b[i])
    return ([round(x, 3) for x in lo], [round(x, 3) for x in hi]) if lo[0] <= hi[0] else None


def bbox_error_mm(recipe, part_plan):
    """Continuous max error in mm between a recipe's analytical bbox and plan center_mm/size_mm (0.0 if none specified)."""
    bb = recipe_bbox(recipe)
    if not bb or not isinstance(part_plan, dict):
        return 0.0
    lo, hi = bb
    ctr = [(a + b) / 2.0 for a, b in zip(lo, hi)]
    sz = [b - a for a, b in zip(lo, hi)]
    err = 0.0
    es, ec = part_plan.get("size_mm"), part_plan.get("center_mm")
    if isinstance(es, list) and len(es) == 3:
        err = max(err, max(abs(sz[i] - es[i]) for i in range(3)))
    if isinstance(ec, list) and len(ec) == 3:
        err = max(err, max(abs(ctr[i] - ec[i]) for i in range(3)))
    return err


def _pt_in_op(pt, o, tol):
    """True when 3D point pt (mm) lies within tol mm of op o's simplified envelope (catches mirrored tilt on boxes/cylinders/tubes/beams)."""
    t = o["op"]
    if t == "cylinder":
        a, b, r = o["a"], o["b"], max(o["r"], o.get("r2", o["r"])) + tol
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ab2 = _dot3(ab, ab)
        if ab2 < 1e-9:
            return math.sqrt(sum((pt[i] - a[i]) ** 2 for i in range(3))) <= r
        u = max(0.0, min(1.0, sum((pt[i] - a[i]) * ab[i] for i in range(3)) / ab2))
        proj = (a[0] + u * ab[0], a[1] + u * ab[1], a[2] + u * ab[2])
        return math.sqrt(sum((pt[i] - proj[i]) ** 2 for i in range(3))) <= r
    if t == "tube":
        pts, r = o["points"], o["r"]
        return any(_pt_in_op(pt, {"op": "cylinder", "a": p0, "b": p1, "r": r}, tol) for p0, p1 in zip(pts, pts[1:]))
    if t == "beam":
        pts, r = o["points"], max(o["w"], o["h"]) / 2.0
        return any(_pt_in_op(pt, {"op": "cylinder", "a": p0, "b": p1, "r": r}, tol) for p0, p1 in zip(pts, pts[1:]))
    if t == "box":
        c = o["center"]
        rel = (pt[0] - c[0], pt[1] - c[1], pt[2] - c[2])
        if "rot" in o:
            rel = _rot_vec(rel, _axis(o["rot"]["axis"]), -o["rot"]["deg"])
        return all(abs(rel[i]) <= o["size"][i] / 2.0 + tol for i in range(3))
    lo, hi = _op_bbox(o)
    return all(lo[i] - tol <= pt[i] <= hi[i] + tol for i in range(3))


def check_plan_part(recipe, part_plan):
    """Pure-Python gate checking recipe validity, world bbox (same tolerances as blender_build), and optional shape constraints. -> error or None."""
    probs = validate(recipe)
    if probs:
        return "; ".join(probs[:4])
    bb = recipe_bbox(recipe)
    if not bb:
        return "empty recipe"
    lo, hi = bb
    ctr = [(a + b) / 2.0 for a, b in zip(lo, hi)]
    sz = [b - a for a, b in zip(lo, hi)]
    if isinstance(part_plan, dict):
        es, ec = part_plan.get("size_mm"), part_plan.get("center_mm")
        if isinstance(es, list) and len(es) == 3:
            bad = [i for i in range(3) if abs(sz[i] - es[i]) > max(6.0, 0.2 * abs(es[i]))]
            if bad:
                return "size_mismatch: expected %s mm, recipe gives %s mm" % (es, [round(x, 1) for x in sz])
        if isinstance(ec, list) and len(ec) == 3:
            tol = max(8.0, 0.08 * math.sqrt(sum(x * x for x in (es if isinstance(es, list) and len(es) == 3 else sz))))
            if max(abs(ctr[i] - ec[i]) for i in range(3)) > tol:
                return "position_mismatch: expected centre %s mm, recipe gives %s mm (tol %.1f mm)" % (ec, [round(x, 1) for x in ctr], tol)
        sh = part_plan.get("shape")
        if isinstance(sh, dict):
            if sh.get("kind") == "ring" and not any(o.get("op") == "ring" and o.get("r_in", 0) > 0 for o in recipe["ops"]):
                return "shape mismatch: part must use a hollow ring op (r_in > 0), not a solid shape"
            eps = sh.get("endpoints")
            if isinstance(eps, list) and len(eps) == 2 and all(_vec(q) for q in eps):
                span = math.sqrt(sum((eps[1][i] - eps[0][i]) ** 2 for i in range(3)))
                tol_ep = max(12.0, 0.15 * span)
                for q in eps:
                    if not any(_pt_in_op(q, o, tol_ep) for o in recipe["ops"]):
                        return "shape mismatch: geometry misses required endpoint %s (check tilt/axis direction)" % (q,)
    return None
