"""Runs inside Blender. Domain-free assembly check: every part must be supported - touch the floor (z = 0) or lie within `gap_mm` of the surface of another part.
A part that does neither floats in the air, whatever the object is. Parts can be exempted with "free": true in the plan. Pure geometry, no names, no thresholds per object."""
import os
import bpy, bmesh
from mathutils.bvhtree import BVHTree

GAP_MM = float(os.environ.get("BX_FLOAT_MM", "10"))
SAMPLES = 300


def _tree(o):
    me = o.data; bm = bmesh.new(); bm.from_mesh(me); bm.transform(o.matrix_world); t = BVHTree.FromBMesh(bm); pts = [v.co.copy() for v in bm.verts]; bm.free(); return t, pts


def floating(free=()):
    """-> list of dicts {kind:'floating_part', part, gap_mm}: parts whose closest approach to every other part and to the floor exceeds GAP_MM."""
    objs = [o for o in bpy.data.objects if o.type == "MESH" and len(o.data.vertices) and o.name not in free]
    allm = [o for o in bpy.data.objects if o.type == "MESH" and len(o.data.vertices)]
    trees = {o.name: _tree(o) for o in allm}
    tol = GAP_MM * 0.001; out = []
    for o in objs:
        t, pts = trees[o.name]
        if min(p.z for p in pts) <= tol:                       # stands on the floor
            continue
        step = max(1, len(pts) // SAMPLES); best = 1e9
        for other in allm:
            if other.name == o.name: continue
            tt, tp = trees[other.name]
            for p in pts[::step]:
                loc = tt.find_nearest(p)
                if loc and loc[3] is not None and loc[3] < best:
                    best = loc[3]
            if best <= tol:
                break
        if best > tol:
            out.append(dict(kind="floating_part", part=o.name, gap_mm=round(best * 1000, 1),
                            hint="the part is not supported: it touches neither the floor nor any other part (closest gap %.0f mm); attach it with a bracket, lug, stay or move it into contact" % (best * 1000)))
    return out
