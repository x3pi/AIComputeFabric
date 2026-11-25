"""Runs inside Blender. Domain-free assembly check: every part must be supported - touch the floor (z = 0) or lie within `gap_mm` of the surface of another part.
A part that does neither floats in the air, whatever the object is. Parts can be exempted with "free": true in the plan. Pure geometry, no names, no thresholds per object."""
import os
import bpy, bmesh
from mathutils.bvhtree import BVHTree

GAP_MM = float(os.environ.get("BX_FLOAT_MM", "10"))
SAMPLES = 300


def _tree(o):
    """BVH of the world-space surface plus surface sample points (vertices, edge midpoints, face centres) and the axis-aligned box. A box has only 8 corner vertices, so
    vertices alone miss the case of a big flat face lying 5 mm above a thin part."""
    me = o.data; bm = bmesh.new(); bm.from_mesh(me); bm.transform(o.matrix_world); t = BVHTree.FromBMesh(bm)
    pts = [v.co.copy() for v in bm.verts] + [(e.verts[0].co + e.verts[1].co) / 2 for e in bm.edges] + [f.calc_center_median() for f in bm.faces]
    lo = [min(p[i] for p in pts) for i in range(3)]; hi = [max(p[i] for p in pts) for i in range(3)]
    bm.free(); return t, pts, (lo, hi)


def _box_gap(a, b):
    """distance between two axis-aligned boxes (0 when they overlap): a lower bound of the surface distance, used to skip parts that are far apart"""
    d2 = 0.0
    for i in range(3):
        g = max(a[0][i] - b[1][i], b[0][i] - a[1][i], 0.0); d2 += g * g
    return d2 ** 0.5


def _near(pts, tree, best):
    for p in pts:
        loc = tree.find_nearest(p)
        if loc and loc[3] is not None and loc[3] < best:
            best = loc[3]
    return best


def floating(free=()):
    """-> list of dicts {kind:'floating_part', part, gap_mm}: parts whose closest approach to every other part and to the floor exceeds GAP_MM."""
    objs = [o for o in bpy.data.objects if o.type == "MESH" and len(o.data.vertices) and o.name not in free]
    allm = [o for o in bpy.data.objects if o.type == "MESH" and len(o.data.vertices)]
    trees = {o.name: _tree(o) for o in allm}
    tol = GAP_MM * 0.001; out = []
    for o in objs:
        t, pts, box = trees[o.name]
        if min(p.z for p in pts) <= tol:                       # stands on the floor
            continue
        step = max(1, len(pts) // SAMPLES); best = 1e9
        for other in allm:
            if other.name == o.name:
                continue
            tt, tp, tbox = trees[other.name]
            if _box_gap(box, tbox) >= best:
                continue
            best = _near(pts[::step], tt, best)                                    # this part's surface to the other part
            best = _near(tp[::max(1, len(tp) // SAMPLES)], t, best)                # and the other part's surface to this one
            if best <= tol:
                break
        if best > tol:
            out.append(dict(kind="floating_part", part=o.name, gap_mm=round(best * 1000, 1),
                            hint="the part is not supported: it touches neither the floor nor any other part (closest gap %.0f mm); if the TASK text gave explicit coordinates/sizes for this part, set \"free\": true on it instead of changing its given position or size; otherwise attach it with a bracket, lug, stay or move it into contact" % (best * 1000)))
    return out
