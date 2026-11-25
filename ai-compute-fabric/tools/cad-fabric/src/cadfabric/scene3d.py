"""Scene agent: builds a multi-part, animated 3D scene in Blender. Replaces the old step-by-step CadQuery loop.

Roles (cost lives here):
  director - a STRONG model (the advisor the user picked). Plans once, is called again only for what the worker could not resolve,
             and reviews once at the end against the user's text. It never sees code or images in the normal path.
  worker   - the local Qwen. Writes ONE part at a time as raw bpy/bmesh code and fixes its own errors.
  gates    - plain code: plan schema, ast/regex checks of the worker's code, a Blender build and an independent scene verifier
             (rigid motion about the declared axis, coaxial intent, collisions, object scale, unit outliers). Same error twice,
             or MAX_TRIES attempts => the part is "stuck" and goes to the director.
Models are injected as callables so the loop is testable without Blender or an LLM:
  director(prompt, system) -> (text, usage)       worker(system, user) -> (text, usage)       usage = {input, output, usd, ...}
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import os
import re
import subprocess
import threading
import time

from .cq_backend import NOWIN

HERE = os.path.dirname(os.path.abspath(__file__))
from .blender import bxlib
COLORS = os.environ.get("BX_COLORS", "1") != "0"           # the director also chooses colours and finishes (applied by the runner)
RECIPES = os.environ.get("BX_RECIPES", "1") != "0"        # parts may be JSON recipes built by blender/bxlib.py (set 0 to force plain bmesh code)
DIRECTOR_OPS_ON = os.environ.get("BX_DIRECTOR_OPS", "1") != "0"   # the director writes the geometry recipes of simple parts into the plan (0: the worker writes them; cheaper for the director, less reliable)
GUARD = os.environ.get("BX_GUARD", "1") != "0"            # a repair round that makes the scene worse is undone
BESTOFN = max(1, int(os.environ.get("BX_BESTOFN", "2")))  # adaptive local worker retries/candidates per part (free tokens, gated before Blender)
AUTOMIRROR = os.environ.get("BX_AUTOMIRROR", "1") != "0"  # auto-deduplicate symmetric L/R worker pairs in Y=0 when director omits mirror_of
AUTOGROUP = os.environ.get("BX_AUTOGROUP", "1") != "0"    # auto-batch ungrouped plain worker parts sharing a clean family stem
BLENDER_BUILD = os.path.join(HERE, "blender", "blender_build.py")
BLENDER_RENDER = os.path.join(HERE, "blender", "blender_render.py")
MAX_TRIES = 4                  # worker attempts per part (all causes together)
GROUP_MAX = 4                  # parts written in one worker request when the plan groups them
FINISHES = ("matte", "gloss", "metal", "rubber", "emissive")
PART_ISSUES = ("not_coaxial", "object_scale", "not_rigid", "wrong_pivot", "wrong_axis", "coaxial_unknown", "scale_outlier", "object_transform", "size_mismatch", "position_mismatch")


def scale_policy(n_parts):
    """Adaptive orchestration parameters keyed by scene part count `n_parts` (preserves legacy behavior for N <= 25)."""
    if n_parts <= 25:
        pol = {"tier": "small", "group_max": GROUP_MAX, "workers": 1, "max_issues": 16, "delta_radius_mm": 60.0, "min_director": 10, "compact_review": False}
    elif n_parts <= 55:
        pol = {"tier": "medium", "group_max": max(GROUP_MAX, 6), "workers": 2, "max_issues": 28, "delta_radius_mm": 55.0, "min_director": 12, "compact_review": True}
    else:
        pol = {"tier": "large", "group_max": max(GROUP_MAX, 8), "workers": 3, "max_issues": 40, "delta_radius_mm": 45.0, "min_director": 15, "compact_review": True}
    env_w = int(os.environ.get("BX_WORKERS", "0") or 0)
    if env_w > 0:
        pol["workers"] = env_w
    return pol

RECIPE_OPS = """{"ops":[ {"op":"box","center":[x,y,z],"size":[dx,dy,dz],"rot":{"axis":[0,0,1],"deg":25},"bevel":2},
         {"op":"cylinder","a":[x,y,z],"b":[x,y,z],"r":20,"r2":15},                       (any direction; r2 = taper)
         {"op":"ring","center":[x,y,z],"axis":"y","r_out":300,"r_in":220,"width":120,"bevel":6},   (tire, rim, disc, washer)
         {"op":"disc","center":[x,y,z],"axis":"x","r":40,"thickness":10},
         {"op":"beam","points":[[x,y,z],[x,y,z],[x,y,z]],"w":50,"h":40,"bevel":3},        (bent bar / spar)
         {"op":"sphere","center":[x,y,z],"r":14},   {"op":"sphere","center":[x,y,z],"radii":[30,20,10]},
         {"op":"loft","sections":[{"x":0,"y":0,"z":800,"w":100,"h":60},{"x":300,"y":0,"z":860,"w":240,"h":140}],"round":3},   (streamlined shell along X; a section may add "round_top"/"round_bottom" (2 round, 6 flat and boxy) so a seat is flat on top and curved below; "smooth":3 adds interpolated sections so a few stations give a continuous curved body)
         {"op":"spokes","center":[x,y,z],"axis":"y","n":5,"r_in":45,"r_out":214,"w":24,"t":14},
         {"op":"tube","points":[[x,y,z],[x,y,z],[x,y,z]],"r":12},                           (bent ROUND tube with smooth joints: fork leg, handlebar, exhaust, spar)
         {"op":"panel","sections":[{"x":0,"y":0,"z":800,"w":100,"h":60},{"x":300,"y":0,"z":860,"w":240,"h":140}],"arc":[0,180],"thick":3} ]}   (thin curved SHEET: fairing, fender, side panel; arc [0,180] = upper half, [180,360] = lower half)"""

DIRECTOR_OPS = """GEOMETRY BY RECIPE, in tiers (every token you write costs; a local worker is free):
 - Plain parts (wheels, tires, rims, spokes, discs, rings, axles, straight cylinders/shocks/pegs, beams/swingarms, brackets/lugs/clamps, bolts, simple boxes/batteries/inverters/calipers, lamps): give a precise spec (shape, axis direction, hollow or solid, dimensions) plus center_mm and size_mm, and LEAVE OUT "ops" (even if a reference skill mentions recipe primitives — the worker builds those recipe primitives itself). For hollow rings/tires add "shape":{"kind":"ring"}; for tilted rods/forks add "shape":{"endpoints":[[x1,y1,z1],[x2,y2,z2]]}. For symmetric L/R pairs write one side and declare the other with "mirror_of". The worker builds them from the tested recipes below and the runner checks the bounding box and shape.
 - Parts whose look depends on your design judgement (lofted or panelled body shells, seats, tanks, tails, fairings, fenders, bent tube runs): write the "ops":[...] yourself in the plan (mm, WORLD coordinates, same format as below). The runner builds it exactly.
 - Only a shape neither tier can express is left for free code.
Recipe format:
""" + RECIPE_OPS + "\n"

COLOR_RULE = """Give EVERY part a "color" and a "finish": choose ONE coherent, attractive palette for the whole object (a hero colour for the main body panels with one contrasting accent colour, dark matte or rubber for tires and mechanical parts, metal for discs, axles and fasteners, emissive for lamps), keep left/right pairs identical, and make the colours fit the TASK text; colours are applied by the runner, the worker never sees them. """
SYSTEM = """You direct a 3D build in Blender. You do not write geometry unless told; a worker model writes one part at a time and fixes its own errors.
You are called only to plan, to resolve what the worker could not, and for one final review against the TASK text.
Reply with ONE JSON object, no prose. Actions:
 {"action":"plan","parts":[{"id":"p1","spec":"what it is, size mm, where (world mm, Z up)","center_mm":[x,y,z],"size_mm":[dx,dy,dz],"ops":[recipe ops; ONLY for curved body shells/lofts/panels/bent tubes - omit on plain rings/tires/rims/spokes/discs/cylinders/axles/boxes/beams/brackets/lamps so the worker builds them],"free":true (only for a part that is meant to hang free),"mirror_of":"id" (+"plane":"y","offset_mm":0: reflected copy of another part's ops, so state a symmetric pair ONCE),"array":{"count":n,"step":[dx,dy,dz]} (n copies p_1..p_n, for rows of bolts, keys, fins),"color":"#RRGGBB","finish":"matte|gloss|metal|rubber|emissive","group":"optional batching hint only"}],
  "joints":[{"name":"j1","parent":"p1"|null,"children":["p2"],"origin_mm":[x,y,z],"axis":[x,y,z],"coaxial":["p2"],"span_rad":6.283}],
  "allowed_contacts":[["p1","p2"]]}
 {"action":"patch_plan","parts":[{"id":"p1","center_mm":[x,y,z],"ops":[...]}],"add_parts":[full part],"remove_parts":["p9"],"joints":[full joint, replaces the one with that name],"remove_joints":["j2"],"allowed_contacts_add":[["p1","p2"]],"allowed_contacts_remove":[["p3","p4"]],"notes":{"p5":"guidance for the worker"}}
   (a CHANGE to the plan: send only what changes, never the whole plan again; every key is optional; "parts" entries carry only the fields to change; "notes" makes the worker rewrite those parts)
 {"action":"write_part","id":"p1","note":"guidance for the worker"}   (worker retries with your note)
 {"action":"patch_part","id":"p1","code":"<python defining build(bpy, bmesh)>"}   (you write it yourself; use when the worker is stuck)
 {"action":"done"}
""" + (DIRECTOR_OPS if RECIPES and DIRECTOR_OPS_ON else "") + """Rules: every physical part in the TASK gets its own id and its own mesh (four posts = four parts); never merge parts. "group" lets the worker write 4-6 parts of the same family in one request; always assign shared group tags (e.g. "wheel_f", "brakes", "fasteners") on plain parts. Parts that should touch/overlap by design must be in allowed_contacts; joint children spin about axis through origin_mm (span_rad is SIGNED: sign follows the right-hand rule about axis); list a part in coaxial only if it is a body of revolution centred on the axis. Give EVERY part center_mm and size_mm: the centre and the extents (dx,dy,dz) of its world axis-aligned bounding box at rest, in mm (for a tilted part, the box of the tilted shape); they are measured after the build and the worker is told when its mesh differs, so make them consistent with the spec and with the other parts. Never alter explicit coordinates or dimensions stated in the TASK just to close a support gap: if TASK-given coordinates leave a part >10 mm from neighbours, mark it "free":true; only move parts or add brackets when the layout is your own design. When the TASK states a target part count (e.g. 50-60 parts), stay close to that range rather than multiplying tiny fins/fasteners far beyond it. Keep internal reasoning concise: omit "ops" on plain parts, set "group" on plain parts of the same family, and use "mirror_of" for every symmetric L/R pair (including curved spars, swingarms, fairings, forks, and pegs). """ + (COLOR_RULE if COLORS else "") + """In the final review compare the numbers you are shown (bounding boxes, joint spans) with the TASK text, including directions and signs, and fix what differs."""

WORKER_SYS = """You write Blender Python (bpy/bmesh) for ONE part. Output only a python code block defining build(bpy, bmesh).
Contract: build() creates exactly one MESH object named exactly as the part id (bpy.data.objects.new(id, mesh), do NOT link it; the runner links it).
Units: metres (convert from mm: /1000). Geometry is in WORLD coordinates at rest; object location stays (0,0,0), rotation 0, scale 1 (bake all offsets into vertices).
Use bmesh.ops (create_cube, create_cone, create_uvsphere, translate, rotate, scale) and bm.to_mesh. No bpy.ops. No imports other than inside build.
Geometry has a budget (a bmesh operator that makes the mesh explode is stopped and reported): keep parts light, apply an operator only to the geometry made in the current step, and read the REAL SIGNATURE in an error message instead of guessing keywords.
Verified pattern (copy its structure; bmesh.ops functions take the BMesh INSTANCE `bm`, never the module):
```python
def build(bpy, bmesh):
    from mathutils import Matrix
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)                                  # unit cube
    bmesh.ops.scale(bm, vec=(0.12, 0.08, 0.01), verts=bm.verts)          # size in metres
    bmesh.ops.translate(bm, vec=(0.0, 0.0, 0.005), verts=bm.verts)       # place in world
    # cylinder along Z: bmesh.ops.create_cone(bm, cap_ends=True, segments=48, radius1=r, radius2=r, depth=h)
    # rotate: bmesh.ops.rotate(bm, cent=(0,0,0), matrix=Matrix.Rotation(1.5708, 3, 'X'), verts=bm.verts)
    me = bpy.data.meshes.new("PARTID"); bm.to_mesh(me); bm.free()
    bpy.data.objects.new("PARTID", me)
```
""" + ("""
PREFERRED when the part is made of simple shapes: instead of code output ONE ```json block, a recipe of tested primitives (mm, WORLD coordinates; the runner builds the mesh, so there is nothing to get wrong in bmesh):
""" + RECIPE_OPS + """
Several ops in one recipe are merged into the ONE part mesh. Use python code only for shapes the recipe cannot express.""" if RECIPES else "")


# ---------------------------------------------------------------- deterministic gates (no model, no Blender)
def jparse(txt):
    s = txt or ""
    has_action = '"action"' in s
    best = None
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", s, re.S):
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                if "action" in obj:
                    return obj
                if best is None:
                    best = obj
        except ValueError:
            pass
    dec = json.JSONDecoder()
    i = s.find("{")
    while i >= 0:
        try:
            obj, end = dec.raw_decode(s, i)
            if isinstance(obj, dict):
                if "action" in obj:
                    return obj
                if best is None:
                    best = obj
            i = s.find("{", end)
            continue
        except ValueError:
            pass
        i = s.find("{", i + 1)
    return None if has_action else best


def code_of(txt):
    m = re.search(r"```(?:python)?\n(.*?)```", txt or "", re.S)
    return (m.group(1) if m else (txt or "")).strip() + "\n"


RECIPE_TEMPLATE = "# recipe part (built by bxlib)\ndef build(bpy, bmesh):\n    import bxlib\n    return bxlib.build(bpy, bmesh, %r, %r)\n"


def _extract_recipe(code):
    """Parse the recipe dict back out of a `# recipe part` file generated by RECIPE_TEMPLATE, or return None."""
    if not (code or "").startswith("# recipe part"):
        return None
    m = re.search(r"bxlib\.build\(bpy,\s*bmesh,\s*(?:'[^']*'|\"[^\"]*\"),\s*(.*)\)\s*$", code, re.S)
    if not m:
        return None
    try:
        rec = ast.literal_eval(m.group(1).strip())
        return rec if isinstance(rec, dict) else None
    except Exception:  # noqa: BLE001
        return None


def part_code(txt, pid, part_plan=None):
    """The worker's reply -> the part file: a JSON recipe becomes a call into bxlib (checked here), anything else is taken as python."""
    if RECIPES:
        m = re.search(r"```json\s*\n(.*?)```", txt or "", re.S)
        if m:
            try:
                rec = json.loads(m.group(1))
            except ValueError as e:
                return "# RECIPE ERROR: not valid JSON (%s)\n" % str(e)[:80]
            probs = bxlib.validate(rec)
            if probs:
                return "# RECIPE ERROR: " + "; ".join(probs[:4]) + "\n"
            if part_plan is not None:
                p_err = bxlib.check_plan_part(rec, part_plan)
                if p_err:
                    return "# RECIPE ERROR: " + p_err + "\n"
            return RECIPE_TEMPLATE % (pid, rec)
    return code_of(txt)


# ---------------------------------------------------------------- plan expansion: symmetry and arrays (deterministic, so the director states each shape once)
_AX = {"x": 0, "y": 1, "z": 2}


def _map_op(op, pt, dr, rot_flip):
    """Copy of a recipe op with every position passed through pt() and every direction through dr(); a reflection also flips the sense of a rotation."""
    o = json.loads(json.dumps(op))
    for k in ("center", "a", "b"):
        if isinstance(o.get(k), list):
            o[k] = pt(o[k])
    if isinstance(o.get("points"), list):
        o["points"] = [pt(q) for q in o["points"]]
    if isinstance(o.get("sections"), list):
        out = []
        for sec in o["sections"]:
            q = pt([sec["x"], sec["y"], sec["z"]]); out.append(dict(sec, x=q[0], y=q[1], z=q[2]))
        o["sections"] = out
    for k in ("axis", "up"):
        if isinstance(o.get(k), list):
            o[k] = dr(o[k])
    if isinstance(o.get("rot"), dict) and isinstance(o["rot"].get("axis"), list):
        o["rot"] = dict(o["rot"], axis=dr(o["rot"]["axis"]), deg=(-o["rot"]["deg"] if rot_flip else o["rot"]["deg"]))
    return o


def mirror_part_code(src_code, src_id, dst_id, plane="y", offset_mm=0.0):
    """Deterministically reflect a built worker part (recipe or raw bmesh) across plane=offset_mm into dst_id."""
    i = _AX[plane]
    rec = _extract_recipe(src_code)
    if rec and isinstance(rec.get("ops"), list):
        if not (plane != "y" and any(o.get("op") in ("loft", "panel") for o in rec["ops"])):
            pt = lambda q: [2 * offset_mm - q[k] if k == i else q[k] for k in range(3)]
            dr = lambda d: [-d[k] if k == i else d[k] for k in range(3)]
            return RECIPE_TEMPLATE % (dst_id, {"ops": [_map_op(o, pt, dr, True) for o in rec["ops"]]})
    off_m = float(offset_mm) * 0.001
    return (
        "# mirrored part (from %s across %s=%s mm)\n"
        "def build(bpy, bmesh):\n"
        "    src_code = %r\n"
        "    had = %r in bpy.data.objects\n"
        "    ns = {}\n"
        "    exec(compile(src_code, %r, 'exec'), ns)\n"
        "    ns['build'](bpy, bmesh)\n"
        "    src_obj = bpy.data.objects[%r]\n"
        "    bm = bmesh.new()\n"
        "    bm.from_mesh(src_obj.data)\n"
        "    for v in bm.verts:\n"
        "        v.co[%d] = %.12g - v.co[%d]\n"
        "    if len(bm.faces):\n"
        "        bmesh.ops.reverse_faces(bm, faces=bm.faces)\n"
        "        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)\n"
        "    me = bpy.data.meshes.new(%r)\n"
        "    bm.to_mesh(me); bm.free()\n"
        "    if not had:\n"
        "        sm = src_obj.data\n"
        "        bpy.data.objects.remove(src_obj, do_unlink=True)\n"
        "        if sm and sm.users == 0:\n"
        "            bpy.data.meshes.remove(sm)\n"
        "    return bpy.data.objects.new(%r, me)\n"
    ) % (src_id, plane, offset_mm, src_code, src_id, src_id + ".py", src_id, i, 2.0 * off_m, i, dst_id, dst_id)


def offset_part_code(src_code, src_id, dst_id, delta_mm):
    """Deterministically translate a built worker part (recipe or raw bmesh) by delta_mm [dx,dy,dz] into dst_id."""
    rec = _extract_recipe(src_code)
    if rec and isinstance(rec.get("ops"), list):
        mv = lambda q: [q[k] + delta_mm[k] for k in range(3)]
        return RECIPE_TEMPLATE % (dst_id, {"ops": [_map_op(o, mv, lambda d: d, False) for o in rec["ops"]]})
    dx, dy, dz = [float(delta_mm[k]) * 0.001 for k in range(3)]
    return (
        "# offset array part (from %s + %s mm)\n"
        "def build(bpy, bmesh):\n"
        "    src_code = %r\n"
        "    had = %r in bpy.data.objects\n"
        "    ns = {}\n"
        "    exec(compile(src_code, %r, 'exec'), ns)\n"
        "    ns['build'](bpy, bmesh)\n"
        "    src_obj = bpy.data.objects[%r]\n"
        "    bm = bmesh.new()\n"
        "    bm.from_mesh(src_obj.data)\n"
        "    for v in bm.verts:\n"
        "        v.co[0] += %.12g; v.co[1] += %.12g; v.co[2] += %.12g\n"
        "    me = bpy.data.meshes.new(%r)\n"
        "    bm.to_mesh(me); bm.free()\n"
        "    if not had:\n"
        "        sm = src_obj.data\n"
        "        bpy.data.objects.remove(src_obj, do_unlink=True)\n"
        "        if sm and sm.users == 0:\n"
        "            bpy.data.meshes.remove(sm)\n"
        "    return bpy.data.objects.new(%r, me)\n"
    ) % (src_id, delta_mm, src_code, src_id, src_id + ".py", src_id, dx, dy, dz, dst_id, dst_id)


def plan_view(plan):
    """The plan as the director sees it after the first call: the recipe of every part is replaced by a one-line marker. The geometry is already built and the bounding boxes
    in the review show where it is; resending every recipe on every call was most of the input tokens. A part is changed by sending its `ops` again; a part sent with the
    marker (or without ops) keeps its current geometry."""
    out = []
    for q in plan.get("parts", []):
        q = dict(q)
        q.pop("_mirror_from", None)
        q.pop("_offset_from", None)
        q.pop("_derived_kind", None)
        if isinstance(q.get("ops"), list):
            q["ops"] = "<%d recipe ops, built; send ops only to change this part>" % len(q["ops"])
        out.append(q)
    return dict(plan, parts=out)


def _bbox_gap_mm(a, b):
    d2 = 0.0
    for i in range(3):
        g = max(a[0][i] - b[1][i], b[0][i] - a[1][i], 0.0)
        d2 += g * g
    return d2 ** 0.5


def plan_delta_view(plan, rep, stuck, others):
    """Compact plan view for escalations on large scenes: keep full entries only for problematic parts and their neighborhood."""
    pv = plan_view(plan)
    parts = pv.get("parts", [])
    if len(parts) <= 16:
        return pv
    rad = scale_policy(len(parts))["delta_radius_mm"]
    focus = set((stuck or {}).keys())
    jmap = {j.get("name"): j for j in pv.get("joints", []) if j.get("name")}
    for i in (others or []):
        if i.get("part"):
            focus.add(i["part"])
        if i.get("pair"):
            focus.update(i["pair"].split("|"))
        for m in i.get("missing") or []:
            focus.add(m)
        jn = i.get("joint")
        if jn and jn in jmap:
            j = jmap[jn]
            if j.get("parent"):
                focus.add(j["parent"])
            focus.update(j.get("children") or [])
            focus.update(j.get("coaxial") or [])
    bb = (rep or {}).get("parts_bbox_mm") or {}
    if focus and bb:
        seed = list(focus)
        for pid in seed:
            if pid not in bb:
                continue
            for other, box in bb.items():
                if other not in focus and _bbox_gap_mm(bb[pid], box) <= rad:
                    focus.add(other)
    if not focus or len(focus) >= len(parts) - 4:
        return pv
    kept = [{k: v for k, v in q.items() if k not in ("color", "finish", "group")} for q in parts if q["id"] in focus]
    locked = [q["id"] for q in parts if q["id"] not in focus]
    return {"parts": kept, "healthy_locked_parts": locked, "joints": pv.get("joints", []), "allowed_contacts": pv.get("allowed_contacts", [])}


def review_plan_view(plan):
    """For scenes >24 parts, drop cosmetic fields (color/finish/group) during final review to keep prompt concise."""
    pv = plan_view(plan)
    parts = pv.get("parts", [])
    if len(parts) <= 24:
        return pv
    out = [{k: v for k, v in q.items() if k not in ("color", "finish", "group")} for q in parts]
    return dict(pv, parts=out)


def apply_patch(plan, act):
    """Apply a `patch_plan` action to the current (already expanded) plan. -> (new plan, errors). Only what is named changes; nothing else is resent or rebuilt."""
    import copy
    errs, new = [], copy.deepcopy(plan)
    parts = {p["id"]: p for p in new["parts"]}
    for q in act.get("parts") or []:
        if not isinstance(q, dict) or q.get("id") not in parts:
            errs.append("patch_plan: unknown part %r (use add_parts for a new one)" % (q.get("id") if isinstance(q, dict) else q)); continue
        if "mirror_of" not in q and any(k in q for k in ("ops", "center_mm", "size_mm", "spec", "shape")):
            for dk in ("_derived_kind", "_mirror_from", "_offset_from"):
                parts[q["id"]].pop(dk, None)
        for k, v in q.items():
            if k != "id":
                parts[q["id"]][k] = v
    for pid in act.get("remove_parts") or []:
        if pid not in parts:
            errs.append("patch_plan: cannot remove unknown part %r" % pid)
        parts.pop(pid, None)
    for q in act.get("add_parts") or []:
        if not isinstance(q, dict) or not q.get("id") or q["id"] in parts:
            errs.append("patch_plan: add_parts needs a new unique id (got %r)" % (q.get("id") if isinstance(q, dict) else q)); continue
        parts[q["id"]] = q
    new["parts"] = list(parts.values())
    gone = set(act.get("remove_parts") or [])
    joints = {j.get("name"): j for j in new.get("joints") or []}
    for j in act.get("joints") or []:
        if not isinstance(j, dict) or not j.get("name"):
            errs.append("patch_plan: a joint needs a name"); continue
        joints[j["name"]] = j
    for n in act.get("remove_joints") or []:
        joints.pop(n, None)
    new["joints"] = list(joints.values())
    pair = lambda x: tuple(sorted(x)) if isinstance(x, (list, tuple)) and len(x) == 2 else None
    contacts = {pair(c) for c in new.get("allowed_contacts") or [] if pair(c) and not (set(c) & gone)}
    contacts |= {pair(c) for c in act.get("allowed_contacts_add") or [] if pair(c)}
    contacts -= {pair(c) for c in act.get("allowed_contacts_remove") or []}
    new["allowed_contacts"] = sorted(list(c) for c in contacts)
    return new, errs


_LR_SUFFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*?)_(fl|fr|rl|rr|left|right|l|r)$", re.I)
_LR_PREFIX_RE = re.compile(r"^(fl|fr|rl|rr|left|right)_([A-Za-z0-9_]+)$", re.I)
_LR_MID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*?)_(fl|fr|rl|rr|left|right|l|r)_([A-Za-z0-9_]+)$", re.I)
_LR_LEGACY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{1,}?)(fl|fr|rl|rr|l|r)$", re.I)
_LR_SIDE = {"l": "l", "left": "l", "fl": "l", "rl": "l", "r": "r", "right": "r", "fr": "r", "rr": "r"}
_LR_SLOT = {"fl": "f", "fr": "f", "rl": "r", "rr": "r"}
_AG_STEM_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*?)(?:_(?:fl|fr|rl|rr|front|rear|left|right|lo|up|f|r|l|u|\d+)|\d+)$", re.I)


def _match_lr_id(pid):
    """Identify left/right naming patterns (suffix, prefix, mid-token) -> ((kind, stem, slot), 'l'|'r') or None."""
    s = str(pid or "")
    for kind, pat in ((1, _LR_SUFFIX_RE), (2, _LR_PREFIX_RE), (3, _LR_MID_RE), (4, _LR_LEGACY_RE)):
        m = pat.match(s)
        if not m:
            continue
        if kind == 2:
            tok, stem = m.group(1).lower(), m.group(2).lower()
        elif kind == 3:
            tok, stem = m.group(2).lower(), "%s_%s" % (m.group(1).lower(), m.group(3).lower())
        else:
            stem, tok = m.group(1).lower(), m.group(2).lower()
        if len(stem) < 2:
            continue
        return (kind, stem, _LR_SLOT.get(tok, "")), _LR_SIDE[tok]
    return None


def auto_group_key(pid):
    """Extract a clean alphabetical family stem (len >= 3) for batching ungrouped plain parts."""
    m = _AG_STEM_RE.match(str(pid or ""))
    if not m:
        return None
    stem = m.group(1)
    if len(stem) < 3 or not stem.isalpha():
        return None
    return stem.lower()


def _num3(v):
    return isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)


def _shapes_y_mirror(pL, pR):
    if "shape" not in pL and "shape" not in pR:
        return True
    sL, sR = pL.get("shape"), pR.get("shape")
    if not isinstance(sL, dict) or not isinstance(sR, dict) or sL.get("kind") != sR.get("kind"):
        return False
    epL, epR = sL.get("endpoints"), sR.get("endpoints")
    if epL is None and epR is None:
        return True
    if not (isinstance(epL, list) and len(epL) == 2 and isinstance(epR, list) and len(epR) == 2):
        return False
    if not all(_num3(q) for q in epL + epR):
        return False
    same_pt = lambda a, b: abs(a[0] - b[0]) <= 1.0 and abs(a[1] + b[1]) <= 1.0 and abs(a[2] - b[2]) <= 1.0
    return (same_pt(epL[0], epR[0]) and same_pt(epL[1], epR[1])) or (same_pt(epL[0], epR[1]) and same_pt(epL[1], epR[0]))


def _normalize_nested_joints(joints, pids):
    """Resolve joint names mistakenly placed inside another joint's `children`/`coaxial`/`parent`."""
    if not isinstance(joints, list):
        return joints
    jout = [dict(j) if isinstance(j, dict) else j for j in joints]
    jmap = {j["name"]: j for j in jout if isinstance(j, dict) and j.get("name") and j["name"] not in pids}
    if not jmap:
        return jout
    for j in jout:
        if not isinstance(j, dict):
            continue
        p = j.get("parent")
        if p in jmap:
            pj = jmap[p]
            cand = next((c for c in (pj.get("children") or []) if c in pids), None)
            if cand:
                j["parent"] = cand
        orig_kids = j.get("children")
        if isinstance(orig_kids, list):
            sub_jnames = [c for c in orig_kids if c in jmap]
            if sub_jnames:
                non_j = [c for c in orig_kids if c not in jmap]
                anchor = next((c for c in non_j if c in pids), None)
                for jn in sub_jnames:
                    jb = jmap[jn]
                    if jb.get("parent") in pids:
                        continue
                    if anchor:
                        jb["parent"] = anchor
                    else:
                        for kc in (jb.get("children") or []):
                            if kc not in jmap and kc not in non_j:
                                non_j.append(kc)
                j["children"] = non_j
        if isinstance(j.get("coaxial"), list):
            j["coaxial"] = [c for c in j["coaxial"] if c not in jmap]
    return jout


def expand_plan(plan):
    """Expand `array` and `mirror_of` parts into ordinary parts. -> (plan, errors).
    {"id":"p","ops":[..],"array":{"count":n,"step":[dx,dy,dz]}}   becomes p_1 .. p_n, each moved by k*step
    {"id":"q","mirror_of":"p","plane":"y","offset_mm":0}           becomes a copy of p reflected in the plane (default y=0); spec, colour, finish and group are inherited"""
    errs, parts, arr_map = [], [], {}
    for p in plan.get("parts", []):
        if isinstance(p.get("ops"), str):                      # the marker of plan_view echoed back: keep the geometry that is already built
            p = {k: v for k, v in p.items() if k != "ops"}
        a = p.get("array")
        if a is None:
            parts.append(p); continue
        n, st = (a or {}).get("count"), (a or {}).get("step")
        has_ops_arr = bool(p.get("ops"))
        has_bbox_arr = (isinstance(p.get("center_mm"), list) and len(p["center_mm"]) == 3 and
                        isinstance(p.get("size_mm"), list) and len(p["size_mm"]) == 3)
        if not (isinstance(n, int) and 2 <= n <= 64 and isinstance(st, list) and len(st) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in st)) or not (has_ops_arr or has_bbox_arr):
            errs.append("part %s: array needs count 2..64, step [dx,dy,dz] and ops (or center_mm and size_mm)" % p.get("id")); parts.append(p); continue
        arr_ids = []
        base_id = "%s_1" % p["id"]
        for k in range(n):
            mv = lambda q, k=k: [q[i] + st[i] * k for i in range(3)]
            c = {x: y for x, y in p.items() if x not in ("array", "id", "ops", "center_mm", "shape", "_offset_from")}
            cid = "%s_%d" % (p["id"], k + 1)
            c["id"] = cid
            if has_ops_arr:
                c["ops"] = [_map_op(o, mv, lambda d: d, False) for o in p["ops"]]
                if k > 0:
                    c["_derived_kind"] = "array"
            elif k > 0:
                c["_offset_from"] = {"src": base_id, "delta_mm": [st[i] * k for i in range(3)]}
                c["_derived_kind"] = "array"
            if isinstance(p.get("center_mm"), list):
                c["center_mm"] = mv(p["center_mm"])
            if isinstance(p.get("shape"), dict):
                sh = json.loads(json.dumps(p["shape"]))
                if isinstance(sh.get("endpoints"), list) and len(sh["endpoints"]) == 2:
                    sh["endpoints"] = [mv(q) for q in sh["endpoints"]]
                c["shape"] = sh
            arr_ids.append(cid)
            parts.append(c)
        if p.get("id"):
            arr_map[p["id"]] = arr_ids
    byid = {p.get("id"): p for p in parts}
    out = []
    mir_rels = []
    for p in parts:
        src = p.get("mirror_of")
        if src is None:
            out.append(p); continue
        base = byid.get(src); pl = str(p.get("plane", "y")); off = p.get("offset_mm", 0)
        has_ops = bool(base and base.get("ops"))
        has_bbox = bool(base and isinstance(base.get("center_mm"), list) and len(base["center_mm"]) == 3 and isinstance(base.get("size_mm"), list) and len(base["size_mm"]) == 3)
        if base is None or base.get("mirror_of") or (not has_ops and not has_bbox):
            errs.append("part %s: mirror_of %r must name another part that has ops (or center_mm and size_mm)" % (p.get("id"), src)); out.append(p); continue
        if pl not in _AX or not isinstance(off, (int, float)) or isinstance(off, bool):
            errs.append("part %s: plane must be x, y or z and offset_mm a number" % p.get("id")); out.append(p); continue
        if has_ops and pl != "y" and any(o.get("op") in ("loft", "panel") for o in base["ops"]):
            errs.append("part %s: loft and panel can only be mirrored in plane y" % p.get("id")); out.append(p); continue
        i = _AX[pl]
        def pt(q, i=i, off=off):
            q = list(q); q[i] = 2 * off - q[i]; return q
        def dr(d, i=i):
            d = list(d); d[i] = -d[i]; return d
        c = {x: y for x, y in base.items() if x not in ("id", "ops", "center_mm", "size_mm", "mirror_of", "shape", "_mirror_from", "_derived_kind")}
        c.update({x: y for x, y in p.items() if x not in ("mirror_of", "plane", "offset_mm")})
        c["spec"] = p.get("spec") or "mirror of %s in plane %s" % (src, pl)
        c["_derived_kind"] = "mirror"
        if has_ops:
            c["ops"] = [_map_op(o, pt, dr, True) for o in base["ops"]]
        else:
            c["_mirror_from"] = {"src": src, "plane": pl, "offset_mm": off}
        mir_rels.append((src, p.get("id"), pl, round(float(off), 3)))
        for k in ("center_mm", "size_mm"):
            if isinstance(base.get(k), list) and k not in p:
                c[k] = pt(base[k]) if k == "center_mm" else list(base[k])
        if isinstance(base.get("shape"), dict) and "shape" not in p:
            sh = json.loads(json.dumps(base["shape"]))
            if isinstance(sh.get("endpoints"), list) and len(sh["endpoints"]) == 2:
                sh["endpoints"] = [pt(q) for q in sh["endpoints"]]
            c["shape"] = sh
        out.append(c)
    res = dict(plan, parts=out)
    if arr_map:
        def _ex_ids(xs):
            res_ids = []
            for x in xs or []:
                res_ids.extend(arr_map.get(x, [x]))
            return res_ids
        if isinstance(plan.get("joints"), list):
            res["joints"] = [
                dict(j, children=_ex_ids(j.get("children")), coaxial=_ex_ids(j.get("coaxial"))) if isinstance(j, dict) else j
                for j in plan["joints"]
            ]
        if isinstance(plan.get("allowed_contacts"), list):
            exp_c = []
            for pr in plan["allowed_contacts"]:
                if isinstance(pr, (list, tuple)) and len(pr) == 2:
                    for a in arr_map.get(pr[0], [pr[0]]):
                        for b in arr_map.get(pr[1], [pr[1]]):
                            exp_c.append([a, b])
                else:
                    exp_c.append(pr)
            res["allowed_contacts"] = exp_c
    if AUTOMIRROR:
        derived_srcs = {p["_mirror_from"]["src"] for p in out if isinstance(p.get("_mirror_from"), dict) and p["_mirror_from"].get("src")}
        derived_srcs |= {p["_offset_from"]["src"] for p in out if isinstance(p.get("_offset_from"), dict) and p["_offset_from"].get("src")}
        candidates = {}
        for idx_p, p in enumerate(out):
            if p.get("ops") or p.get("_mirror_from") or p.get("_offset_from") or p.get("mirror_of") or p.get("array"):
                continue
            if not (_num3(p.get("center_mm")) and _num3(p.get("size_mm"))):
                continue
            m = _match_lr_id(p.get("id"))
            if m:
                key_lr, side = m
                candidates.setdefault(key_lr, {})[side] = (idx_p, p)
        for _key_lr, sides in candidates.items():
            if "l" in sides and "r" in sides:
                idxL, pL = sides["l"]
                idxR, pR = sides["r"]
                if pR.get("id") in derived_srcs:
                    continue
                cL, sL = pL["center_mm"], pL["size_mm"]
                cR, sR = pR["center_mm"], pR["size_mm"]
                if (abs(cL[0] - cR[0]) <= 1.0 and abs(cL[1] + cR[1]) <= 1.0 and abs(cL[1]) >= 5.0 and abs(cL[2] - cR[2]) <= 1.0
                        and all(abs(sL[k] - sR[k]) <= 1.0 for k in range(3)) and _shapes_y_mirror(pL, pR)):
                    pR_new = dict(pR)
                    pR_new["_mirror_from"] = {"src": pL["id"], "plane": "y", "offset_mm": 0}
                    pR_new["_derived_kind"] = "mirror"
                    out[idxR] = pR_new
                    derived_srcs.add(pL["id"])
                    mir_rels.append((pL["id"], pR["id"], "y", 0.0))
    if mir_rels and isinstance(res.get("allowed_contacts"), list):
        byid_out = {p.get("id"): p for p in out if p.get("id")}
        mir_by_key, mir_list = {}, {}
        for src_id, dst_id, pl, off in mir_rels:
            if src_id in byid_out and dst_id in byid_out:
                mir_by_key[(src_id, pl, off)] = dst_id
                mir_by_key[(dst_id, pl, off)] = src_id
                mir_list.setdefault(src_id, []).append((dst_id, pl, off))
                mir_list.setdefault(dst_id, []).append((src_id, pl, off))

        def _straddles(part, pl, off):
            idx = _AX[pl]
            if isinstance(part.get("ops"), list) and part["ops"]:
                bb = bxlib.recipe_bbox({"ops": part["ops"]})
                if bb:
                    return bb[0][idx] - 2.0 <= off <= bb[1][idx] + 2.0
            if _num3(part.get("center_mm")) and _num3(part.get("size_mm")):
                c, s = part["center_mm"][idx], part["size_mm"][idx]
                return (c - s / 2.0) - 2.0 <= off <= (c + s / 2.0) + 2.0
            return False

        sym_pairs, bad_contacts = set(), []
        for pr in res["allowed_contacts"]:
            if not (isinstance(pr, (list, tuple)) and len(pr) == 2 and all(isinstance(x, str) for x in pr)):
                bad_contacts.append(pr); continue
            a, b = pr
            sym_pairs.add(tuple(sorted((a, b))))
            if a in byid_out and b in byid_out:
                for a_m, pl, off in mir_list.get(a, ()):
                    if (b, pl, off) in mir_by_key:
                        sym_pairs.add(tuple(sorted((a_m, mir_by_key[(b, pl, off)]))))
                    elif _straddles(byid_out[b], pl, off):
                        sym_pairs.add(tuple(sorted((a_m, b))))
                for b_m, pl, off in mir_list.get(b, ()):
                    if (a, pl, off) not in mir_by_key and _straddles(byid_out[a], pl, off):
                        sym_pairs.add(tuple(sorted((a, b_m))))
        res["allowed_contacts"] = [list(pr) for pr in sorted(sym_pairs)] + bad_contacts
    pids = {p.get("id") for p in out if p.get("id")}
    if isinstance(res.get("joints"), list):
        res["joints"] = _normalize_nested_joints(res["joints"], pids)
    return res, errs


def validate_plan(plan):
    e = []
    ids = [p.get("id") for p in plan.get("parts", [])]
    if not ids:
        e.append("no parts")
    if len(set(ids)) != len(ids):
        e.append("duplicate part ids")
    for p in plan.get("parts", []):
        if not p.get("id") or not p.get("spec") or not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,40}$", str(p.get("id"))):
            e.append("part needs a simple id (letters, digits, _) and a spec: %r" % (p,))
        if "color" in p and not re.match(r"^#[0-9a-fA-F]{6}$", str(p["color"])):
            e.append("part %s: color must look like #RRGGBB" % p.get("id"))
        if "finish" in p and p["finish"] not in FINISHES:
            e.append("part %s: finish must be one of %s" % (p.get("id"), ", ".join(FINISHES)))
        if "ops" in p:
            probs = bxlib.validate({"ops": p["ops"]}) if RECIPES else ["ops need BX_RECIPES"]
            if probs:
                e.append("part %s: ops: %s" % (p.get("id"), "; ".join(probs[:3])))
        for k in ("center_mm", "size_mm"):
            if k in p and not (isinstance(p[k], list) and len(p[k]) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in p[k])):
                e.append("part %s: %s must be 3 numbers" % (p.get("id"), k))
        if "shape" in p:
            sh = p["shape"]
            if not isinstance(sh, dict):
                e.append("part %s: shape must be an object" % p.get("id"))
            else:
                if "kind" in sh and not isinstance(sh["kind"], str):
                    e.append("part %s: shape.kind must be a string" % p.get("id"))
                if "endpoints" in sh:
                    eps = sh["endpoints"]
                    if not (isinstance(eps, list) and len(eps) == 2 and all(isinstance(q, list) and len(q) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in q) for q in eps)):
                        e.append("part %s: shape.endpoints must be [[x,y,z],[x,y,z]]" % p.get("id"))
    for j in plan.get("joints", []):
        for k in ("name", "children", "origin_mm", "axis", "span_rad"):
            if k not in j:
                e.append("joint %s missing %s" % (j.get("name"), k))
        if len(j.get("origin_mm", [])) != 3 or len(j.get("axis", [])) != 3:
            e.append("joint %s origin_mm/axis need 3 numbers" % j.get("name"))
        elif not any(j["axis"]):
            e.append("joint %s axis is zero" % j.get("name"))
        for c in j.get("children", []) + ([j["parent"]] if j.get("parent") else []) + j.get("coaxial", []):
            if c not in ids:
                e.append("joint %s references unknown part %s" % (j.get("name"), c))
    for pr in plan.get("allowed_contacts", []):
        if len(pr) != 2 or any(x not in ids for x in pr):
            e.append("allowed_contacts bad pair %r" % (pr,))
    return e


def _mem_cap(gb=None):
    """Runs in the child before Blender starts (POSIX): cap its address space so one runaway part cannot take the machine's memory
    (BX_MEM_GB for the whole scene, default 10; BX_PART_GB for the single-part probe, default 3; 0 = no cap)."""
    try:
        import resource
        gb = float(os.environ.get("BX_MEM_GB", "10")) if gb is None else float(gb)
        if gb > 0:
            resource.setrlimit(resource.RLIMIT_AS, (int(gb * 2**30), int(gb * 2**30)))
    except Exception:                                       # noqa: BLE001
        pass


def _part_cap():
    _mem_cap(os.environ.get("BX_PART_GB", "3"))


def static_check(code, pid):
    if code.startswith("# RECIPE ERROR:"):
        return code.splitlines()[0][2:]
    try:
        tree = ast.parse(code)
    except SyntaxError as x:
        return "SyntaxError line %s: %s" % (x.lineno, x.msg)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "build" for n in ast.walk(tree)):
        return "no def build(bpy, bmesh)"
    if re.search(r"\bbpy\.ops\.", code):
        return "bpy.ops is forbidden (use bmesh.ops on a BMesh instance)"
    if re.search(r"bmesh\.ops\.\w+\(\s*bmesh\b", code):
        return "bmesh.ops.* must receive the BMesh instance (bm = bmesh.new()), not the module"
    if ("'%s'" % pid) not in code and ('"%s"' % pid) not in code:
        return "object must be created with the exact name %r" % pid
    return None


def sig(err):
    lines = (err or "").strip().splitlines()
    return re.sub(r"\d+(\.\d+)?", "N", lines[-1] if lines else "")[:120]


# ---------------------------------------------------------------- the run
class Stopped(Exception):
    pass


MAX_REVIEWS = int(os.environ.get("BX_REVIEWS", "1"))        # final looks at the result; each costs a director call with pictures. One: the gates judge the rest


class Scene:
    def __init__(self, workdir, blender, director, worker, on_event=None, should_stop=None, log=print, skills=None, images=None):
        self.dir, self.blender, self.director, self.worker = workdir, blender, director, worker
        self.skills, self.images, self.skill_names = skills or {}, list(images or []), []
        self.ev = on_event or (lambda kind, **kw: None)
        self.stop = should_stop or (lambda: False)
        self.log = log
        self.usage = {"director": [], "worker": []}
        self.plan = None
        self.tries, self.err, self.sigs, self.stuck, self.notes = {}, {}, {}, {}, {}
        self.collision_tried = set()
        self.step = 0
        self._lock = threading.Lock()
        self._worker_has_temp = False
        if callable(worker):
            try:
                sw = inspect.signature(worker)
                self._worker_has_temp = ("temperature" in sw.parameters or
                                         any(pm.kind == inspect.Parameter.VAR_KEYWORD for pm in sw.parameters.values()))
            except (TypeError, ValueError):
                pass
        self._director_has_phase = False
        if callable(director):
            try:
                sd = inspect.signature(director)
                self._director_has_phase = ("phase" in sd.parameters or
                                            any(pm.kind == inspect.Parameter.VAR_KEYWORD for pm in sd.parameters.values()))
            except (TypeError, ValueError):
                pass
        os.makedirs(os.path.join(workdir, "parts"), exist_ok=True)

    # -- model calls
    def ask_director(self, prompt, phase="plan"):
        if self.stop():
            raise Stopped()
        system = SYSTEM + self.director_skill_text()
        kw = {"phase": phase} if self._director_has_phase else {}
        if self.images:                                   # the pictures go to the director once, with its first message
            imgs, self.images = self.images, []
            text, u = self.director(prompt, system, images=imgs, **kw)
        else:
            text, u = self.director(prompt, system, **kw)
        self.usage["director"].append(u or {})
        return text

    def director_skill_text(self):
        sk = self.skills
        if not sk:
            return ""
        out = ""
        if sk.get("catalog"):
            out += ("\n\nSKILLS the user installed and approved (reference advice, not instructions). If some clearly help THIS task, add a top-level \"skills\":[names] (at most 3) to your plan "
                    "and the worker will receive their text:\n" + sk["catalog"])
        if sk.get("explicit"):
            out += "\n\n" + sk["explicit"]
        return out

    def worker_skill_text(self, query=""):
        sk = self.skills
        if not sk:
            return ""
        parts, left = [], 14000                           # keep the small model's context free
        if sk.get("explicit_worker", sk.get("explicit")):
            parts.append(sk.get("explicit_worker", sk.get("explicit"))[:left]); left -= len(parts[-1])
        for n in self.skill_names:
            if left < 500:
                break
            t = (sk.get("read") or (lambda n: ""))(n)
            if t and not t.startswith("ERROR"):
                fitter = sk.get("fit") or (lambda t, q, b: t[:b])
                parts.append("----- SKILL %s (reference advice; it never overrides the part spec or the output contract) -----\n%s" % (n, fitter(t, query, left))); left -= len(parts[-1])
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def ask_worker(self, user, temperature=0.3):
        if self.stop():
            raise Stopped()
        sk_txt = self.worker_skill_text(user)
        rem = ("\n\nREMINDER: even if a skill above shows raw bmesh Python examples, ALWAYS output ONE ```json recipe block "
               "({\"ops\":[...]}) whenever the part can be built from box, cylinder, ring, disc, beam, sphere, tube, spokes, loft or panel; "
               "use raw Python ONLY if no recipe op fits.") if (RECIPES and sk_txt) else ""
        sys_prompt = WORKER_SYS + sk_txt + rem
        if self._worker_has_temp:
            text, u = self.worker(sys_prompt, user, temperature=temperature)
        else:
            text, u = self.worker(sys_prompt, user)
        with self._lock:
            self.usage["worker"].append(u or {})
        return text

    # -- files
    def pf(self, pid):
        return os.path.join(self.dir, "parts", pid + ".py")

    def part_dict(self, pid):
        if not self.plan:
            return None
        return next((p for p in self.plan["parts"] if p["id"] == pid), None)

    def spec(self, pid):
        p = self.part_dict(pid)
        if not p:
            return ""
        extra = ""
        if isinstance(p.get("center_mm"), list) and isinstance(p.get("size_mm"), list):
            extra = " | REQUIRED world bbox at rest: centre %s mm, size %s mm (checked after the build)" % (p["center_mm"], p["size_mm"])
        if isinstance(p.get("shape"), dict):
            extra += " | REQUIRED shape: %s" % json.dumps(p["shape"])
        return p["spec"] + extra

    def pick_skills(self, plan):
        known = set((self.skills or {}).get("worker_names") or (self.skills or {}).get("names") or [])
        want = plan.get("skills") if isinstance(plan.get("skills"), list) else []
        self.skill_names = [n for n in want if isinstance(n, str) and n in known][:3]

    @staticmethod
    def _part_plan_sig(p):
        return (p.get("spec"), p.get("center_mm"), p.get("size_mm"), p.get("shape"))

    def set_plan(self, plan):
        keep = {p["id"] for p in plan["parts"]}
        old = self.plan
        self.plan = plan
        with open(os.path.join(self.dir, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=1)
        if old:                                           # a part whose spec/bbox/shape changed must be rewritten, a removed part must go
            for p in old["parts"]:
                if p["id"] not in keep and os.path.exists(self.pf(p["id"])):
                    os.unlink(self.pf(p["id"]))
            old_by = {p["id"]: p for p in old["parts"]}
            for p in plan["parts"]:
                pid = p["id"]
                if pid in old_by and not (p.get("ops") and RECIPES and DIRECTOR_OPS_ON):
                    if self._part_plan_sig(p) != self._part_plan_sig(old_by[pid]):
                        self.err[pid] = "the planned spec/bbox/shape of this part changed; rewrite it"
                        self.tries[pid] = 0
                        self.sigs[pid] = []
                        self.stuck.pop(pid, None)
                        self.collision_tried.discard(pid)
        for p in plan["parts"]:                           # parts the director gave as recipes are built here, with no worker call
            if p.get("ops") and RECIPES and DIRECTOR_OPS_ON:
                before = next((q.get("ops") for q in old["parts"] if q["id"] == p["id"]), None) if old else None
                if not os.path.exists(self.pf(p["id"])) or before != p["ops"]:
                    os.makedirs(os.path.dirname(self.pf(p["id"])), exist_ok=True)
                    with open(self.pf(p["id"]), "w", encoding="utf-8") as f:
                        f.write(RECIPE_TEMPLATE % (p["id"], {"ops": p["ops"]}))
                    self.err.pop(p["id"], None); self.tries.pop(p["id"], None); self.sigs.pop(p["id"], None); self.stuck.pop(p["id"], None)
        for k in list(self.tries):
            if k not in keep:
                for d in (self.tries, self.err, self.sigs, self.stuck, self.notes):
                    d.pop(k, None)

    def build(self):
        rp = os.path.join(self.dir, "report.json")
        try:
            os.unlink(rp)                                  # a crash must never be mistaken for the previous build's report
        except OSError:
            pass
        r = subprocess.run([self.blender, "-b", "--python", BLENDER_BUILD, "--", self.dir], capture_output=True, text=True, timeout=900, **(dict(preexec_fn=_mem_cap) if os.name != "nt" else {}), **NOWIN)
        try:
            with open(rp, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            tail = (r.stderr or r.stdout or "")[-300:]
            return {"ok": False, "missing": [], "part_errors": {}, "issues": [{"kind": "blender_failed", "rc": r.returncode, "tail": tail,
                    "hint": "Blender died while building (often out of memory: a part with exploding geometry, e.g. a whole-mesh bevel inside a loop)"}]}

    def render_views(self):
        """Small Workbench pictures of the built scene for the director's final review. -> [{name, mime, data}] (empty if Blender could not render)."""
        import base64
        blend = os.path.join(self.dir, "scene.blend")
        if not os.path.isfile(blend):
            return []
        try:
            subprocess.run([self.blender, "-b", blend, "--python", BLENDER_RENDER, "--", self.dir], capture_output=True, text=True, timeout=300, **NOWIN)
        except (subprocess.TimeoutExpired, OSError):
            return []
        out = []
        for n in ("iso", "side", "top", "iso_end_of_motion"):
            try:
                with open(os.path.join(self.dir, "views", n + ".png"), "rb") as f:
                    out.append({"name": n + ".png", "mime": "image/png", "data": base64.b64encode(f.read()).decode()})
            except OSError:
                pass
        return out

    # -- worker inner loop (the director is NOT called here)
    def write(self, pid, error=None):
        p = self.part_dict(pid)
        prev = ""
        if os.path.exists(self.pf(pid)):
            with open(self.pf(pid), encoding="utf-8") as f:
                prev = "\nPrevious code:\n```python\n" + f.read() + "```"
        if error:
            prev += "\nThis code FAILED with:\n" + error + "\nFix that error."
        prompt = "Part id: %s\nSpec: %s\nNote: %s%s" % (pid, self.spec(pid), self.notes.get(pid, ""), prev)
        n_cand = 1 if (p and p.get("ops") and RECIPES and DIRECTOR_OPS_ON) else BESTOFN
        temps = [0.2 if n_cand > 1 else 0.3] + [min(0.8, 0.45 + 0.15 * k) for k in range(n_cand - 1)]
        best_code, best_err_val, last_code = None, float("inf"), None
        hint = ""
        for temp in temps:
            raw = self.ask_worker(prompt + hint, temperature=temp)
            code = part_code(raw, pid, p)
            last_code = code
            serr = static_check(code, pid)
            if not serr:
                rec = _extract_recipe(code)
                score = bxlib.bbox_error_mm(rec, p) if (rec and p) else 0.0
                if score < best_err_val:
                    best_err_val, best_code = score, code
                if score <= 4.0:
                    break
            else:
                hint = "\nPrevious attempt failed static check: %s. Fix it." % serr
        chosen = best_code if best_code is not None else last_code
        with self._lock:
            self.tries[pid] = self.tries.get(pid, 0) + 1
        with open(self.pf(pid), "w", encoding="utf-8") as f:
            f.write(chosen)
        return chosen

    def write_group(self, pids):
        p_map = {p["id"]: p for p in self.plan["parts"]} if self.plan else {}
        specs = "\n".join("- %s: %s" % (p, self.spec(p)) for p in pids)
        fmt_hint = "one ```json recipe block (preferred for simple shapes) or python defining build(bpy, bmesh)" if RECIPES else "one python code block defining build(bpy, bmesh)"
        txt = self.ask_worker("Write ALL of these parts. For each part output a line `# PART <id>` then %s for that part (object named exactly the id).\n%s" % (fmt_hint, specs))
        got = set()
        for m in re.finditer(r"(?:#+|\*\*)\s*PART[:\s]+([A-Za-z0-9_]+)[:*\s]*\n+```(python|json)?[ \t]*\n(.*?)```", txt, re.S):
            pid, lang, body = m.group(1), (m.group(2) or "").lower(), m.group(3)
            if pid in pids:
                is_json = (lang == "json") or body.lstrip().startswith("{")
                with open(self.pf(pid), "w", encoding="utf-8") as f:
                    f.write(part_code("```json\n%s```" % body, pid, p_map.get(pid)) if is_json else body.strip() + "\n")
                with self._lock:
                    self.tries[pid] = 1
                got.add(pid)
        return got

    def _sync_mirrors(self, mirrors, offsets=None):
        for dst_id, m in mirrors.items():
            src_id = m["src"]
            if not os.path.exists(self.pf(src_id)) or src_id in self.err or src_id in self.stuck:
                continue
            with open(self.pf(src_id), encoding="utf-8") as f:
                src_code = f.read()
            dst_code = mirror_part_code(src_code, src_id, dst_id, m["plane"], m["offset_mm"])
            old = None
            if os.path.exists(self.pf(dst_id)):
                with open(self.pf(dst_id), encoding="utf-8") as f:
                    old = f.read()
            if old != dst_code:
                with open(self.pf(dst_id), "w", encoding="utf-8") as f:
                    f.write(dst_code)
            self.err.pop(dst_id, None)
            self.stuck.pop(dst_id, None)
        for dst_id, m in (offsets or {}).items():
            src_id = m["src"]
            if not os.path.exists(self.pf(src_id)) or src_id in self.err or src_id in self.stuck:
                continue
            with open(self.pf(src_id), encoding="utf-8") as f:
                src_code = f.read()
            dst_code = offset_part_code(src_code, src_id, dst_id, m["delta_mm"])
            old = None
            if os.path.exists(self.pf(dst_id)):
                with open(self.pf(dst_id), encoding="utf-8") as f:
                    old = f.read()
            if old != dst_code:
                with open(self.pf(dst_id), "w", encoding="utf-8") as f:
                    f.write(dst_code)
            self.err.pop(dst_id, None)
            self.stuck.pop(dst_id, None)

    def settle(self, pid, error):
        """record a failure; True when the worker should try again"""
        s = sig(error)
        with self._lock:
            self.sigs.setdefault(pid, []).append(s)
            if self.tries.get(pid, 0) >= MAX_TRIES:
                self.stuck[pid] = "max tries; last: " + ((error or "").strip().splitlines() or [""])[-1][:200]
                return False
            if self.sigs[pid][-2:].count(s) == 2:
                self.stuck[pid] = "same error twice: " + s
                return False
        return True

    def probe(self, pid):
        """Build ONE raw-code part alone in its own Blender process with a memory cap: a part that explodes is caught and named here, not in the whole scene.
        -> error text or None. Recipe parts (bxlib) are safe by construction and skipped. BX_PROBE=0 disables."""
        if os.environ.get("BX_PROBE", "1") == "0":
            return None
        try:
            with open(self.pf(pid), encoding="utf-8") as f:
                if f.read(24).startswith("# recipe part"):
                    return None
        except OSError:
            return None
        sj = os.path.join(self.dir, "smoke_%s.json" % pid)
        try:
            os.unlink(sj)
        except OSError:
            pass
        try:
            r = subprocess.run([self.blender, "-b", "--python", BLENDER_BUILD, "--", self.dir, pid], capture_output=True, text=True, timeout=120,
                               **(dict(preexec_fn=_part_cap) if os.name != "nt" else {}), **NOWIN)
        except subprocess.TimeoutExpired:
            return "build() took longer than 120 s: the geometry is far too heavy; use fewer segments and no whole-mesh bevel/subdivide in a loop"
        try:
            with open(sj, encoding="utf-8") as f:
                return json.load(f).get("error")
        except (OSError, ValueError):
            rc = getattr(r, "returncode", 0)
            if rc:
                return "build() crashed Blender (memory limit %s GB for one part): the geometry explodes; modify only the new geometry, never bevel/subdivide the whole mesh in a loop" % os.environ.get("BX_PART_GB", "3")
            return None                                    # no result and no crash (a stand-in Blender in tests): nothing to judge

    def attempt(self, pid, err=None, have_code=False, external=False):
        """write/fix until the static gates pass (Blender judges the rest afterwards) or the part is stuck"""
        if external and err and not self.settle(pid, err):
            return
        code = None
        if have_code:
            with open(self.pf(pid), encoding="utf-8") as f:
                code = f.read()
        for _ in range(MAX_TRIES + 1):
            if code is None:
                code = self.write(pid, err)
            err = static_check(code, pid) or self.probe(pid)
            with self._lock:
                self.step += 1
                step_n = self.step
                self.ev("cell", n=step_n, ok=not err, code=code, error=err or "", output="", part=pid)
            if not err:
                with self._lock:
                    self.err.pop(pid, None)
                return
            with self._lock:
                self.err[pid] = err
            if not self.settle(pid, err):
                return
            code = None

    def work(self):
        """bring every part to green or to stuck. -> (report, issues the worker cannot resolve)"""
        pol = scale_policy(len(self.plan["parts"]))
        gmax, n_workers = pol["group_max"], pol["workers"]
        mirrors = {p["id"]: p["_mirror_from"] for p in self.plan["parts"] if p.get("_mirror_from") and not p.get("ops") and not self.notes.get(p["id"])}
        offsets = {p["id"]: p["_offset_from"] for p in self.plan["parts"] if p.get("_offset_from") and not p.get("ops") and not self.notes.get(p["id"])}
        derived = set(mirrors) | set(offsets)
        todo = [p["id"] for p in self.plan["parts"] if p["id"] not in self.stuck and p["id"] not in derived and (not os.path.exists(self.pf(p["id"])) or p["id"] in self.err)]
        groups, singles, auto_cands = {}, [], {}
        for p in self.plan["parts"]:
            pid = p["id"]
            if pid not in todo:
                continue
            if p.get("group"):
                groups.setdefault(p["group"], []).append(pid)
            else:
                ag = auto_group_key(pid) if AUTOGROUP else None
                if ag:
                    auto_cands.setdefault(ag, []).append(pid)
                else:
                    singles.append([pid])
        ag_max = min(gmax, 4)
        batches = singles + [g[k:k + gmax] for g in groups.values() for k in range(0, len(g), gmax)]
        for g in auto_cands.values():
            if len(g) >= 2:
                batches.extend(g[k:k + ag_max] for k in range(0, len(g), ag_max))
            else:
                batches.append(g)

        def _run_batch(pids):
            got = self.write_group(pids) if len(pids) > 1 else set()
            for pid in pids:
                self.attempt(pid, err=self.err.get(pid), have_code=(pid in got))

        if n_workers <= 1 or len(batches) <= 1:
            for pids in batches:
                _run_batch(pids)
        else:
            with ThreadPoolExecutor(max_workers=min(n_workers, len(batches))) as ex:
                list(ex.map(_run_batch, batches))
        self._sync_mirrors(mirrors, offsets)
        self.ev("phase", what="build")
        rep = self.build()
        ids = {p["id"] for p in self.plan["parts"]}
        undone = False
        for _ in range(MAX_TRIES):
            redo = {pid: e for pid, e in rep.get("part_errors", {}).items() if pid not in self.stuck and pid not in derived}
            for i in rep.get("issues", []):                  # single-part issues are fixable by the worker; pair/joint issues go to the director
                if i.get("part") in ids and i["kind"] in PART_ISSUES and i["part"] not in self.stuck and i["part"] not in derived:
                    redo.setdefault(i["part"], json.dumps(i))
            for i in rep.get("issues", []):                  # rest-pose collision with overlap_mm: give a worker part 1 local trim attempt before escalating
                if i.get("kind") == "collision" and i.get("overlap_mm") and 1 in (i.get("frames") or []):
                    pair = str(i.get("pair") or "").split("|")
                    if len(pair) == 2:
                        a, b = pair
                        for cand, other in ((a, b), (b, a)):
                            cp = self.part_dict(cand)
                            if cp and not (cp.get("ops") and RECIPES and DIRECTOR_OPS_ON) and cand not in derived and cand not in self.stuck and cand not in redo and cand not in self.collision_tried:
                                self.collision_tried.add(cand)
                                redo[cand] = "rest-pose collision with %s (overlap box mm: %s); keep your required center_mm/size_mm but trim or reshape so it clears %s" % (other, i["overlap_mm"], other)
                                break
            if not redo:
                break
            saved = {}
            for pid in list(redo) + [d for d, m in mirrors.items() if m["src"] in redo] + [d for d, m in offsets.items() if m["src"] in redo]:
                if os.path.exists(self.pf(pid)):
                    with open(self.pf(pid), encoding="utf-8") as f:
                        saved[pid] = f.read()
            before = self.score(rep)
            if n_workers <= 1 or len(redo) <= 1:
                for pid, e in redo.items():
                    self.attempt(pid, err=e, external=True)
            else:
                with ThreadPoolExecutor(max_workers=min(n_workers, len(redo))) as ex:
                    list(ex.map(lambda kv: self.attempt(kv[0], err=kv[1], external=True), redo.items()))
            self._sync_mirrors(mirrors, offsets)
            new = self.build()
            if GUARD and self.score(new) > before:           # the repair made the scene worse: put the previous code back, keep the old report
                for pid, code in saved.items():
                    with open(self.pf(pid), "w", encoding="utf-8") as f:
                        f.write(code)
                self.regressions = getattr(self, "regressions", 0) + 1
                for pid in redo:                              # the undone round does not count as "the same error again"; MAX_TRIES still bounds the retries
                    if self.sigs.get(pid):
                        self.sigs[pid].pop()
                self.ev("phase", what="undo")
                undone = True
                continue
            undone = False
            rep = new
        if undone:
            rep = self.build()                                # the saved scene must match the files on disk
        self.err = dict(rep.get("part_errors", {}))
        for pid in rep.get("missing", []):
            self.stuck.setdefault(pid, "no valid code")
        others = [i for i in rep.get("issues", []) if not (i.get("part") and i["kind"] in PART_ISSUES and i["part"] not in self.stuck)]
        return rep, others

    # -- text shown to the director
    @staticmethod
    def score(rep):
        """how bad a build report is: failing parts, missing parts and every open issue"""
        return len(rep.get("part_errors", {})) * 2 + len(rep.get("missing", [])) * 2 + len(rep.get("issues", []))

    def worker_report(self, others):
        parts = self.plan["parts"]
        lim = scale_policy(len(parts))["max_issues"]
        prio = {"blender_failed": 0, "joint_skipped": 1, "joint_partial": 1, "span_mismatch": 2, "not_rigid": 2,
                "wrong_axis": 2, "wrong_pivot": 2, "ground_penetration": 3, "floating_part": 4, "collision": 5}
        ordered = sorted(others or (), key=lambda it: prio.get(it.get("kind"), 3))
        if len(parts) <= 16:
            rows = ["%s: %s (worker tries %d)" % (p["id"], ("STUCK - " + self.stuck[p["id"]]) if p["id"] in self.stuck else "built", self.tries.get(p["id"], 0)) for p in parts]
        else:
            bad = set(self.stuck.keys())
            for i in ordered:
                if i.get("part"):
                    bad.add(i["part"])
                if i.get("pair"):
                    bad.update(i["pair"].split("|"))
            rows = ["%s: %s (worker tries %d)" % (p["id"], ("STUCK - " + self.stuck[p["id"]]) if p["id"] in self.stuck else "built", self.tries.get(p["id"], 0))
                    for p in parts if p["id"] in bad or self.tries.get(p["id"], 0) > 1]
            clean = len(parts) - len(rows)
            if clean > 0:
                rows.append("... and %d other parts built cleanly" % clean)
        return "PARTS\n" + "\n".join(rows) + "\nISSUES\n" + ("\n".join(json.dumps(i) for i in ordered[:lim]) or "none")

    def review_text(self, rep):
        parts = self.plan.get("parts", []) if self.plan else []
        bb_map = rep.get("parts_bbox_mm", {})
        jt = "\n".join("%s: parent=%s children=%s origin=%s axis=%s span_rad=%s" % (j["name"], j.get("parent"), j["children"], j["origin_mm"], j["axis"], j["span_rad"])
                       for j in self.plan.get("joints", []))
        if len(parts) <= 35:
            bb = "\n".join("%s: lo%s hi%s mm" % (k, v[0], v[1]) for k, v in sorted(bb_map.items()))
            return "ALL CHECKS PASSED. FINAL REVIEW against the TASK text.\nBOUNDING BOXES at rest\n" + bb + "\nJOINTS\n" + jt + "\nReply done, or fix what differs from the TASK."
        joint_pids = set()
        for j in self.plan.get("joints", []):
            if j.get("parent"):
                joint_pids.add(j["parent"])
            joint_pids.update(j.get("children") or [])
            joint_pids.update(j.get("coaxial") or [])
        inv, notable, matched = [], [], 0
        for p in parts:
            pid = p["id"]
            if pid not in bb_map:
                notable.append("%s: MISSING" % pid)
                continue
            lo, hi = bb_map[pid]
            ctr = [(a + b) / 2.0 for a, b in zip(lo, hi)]
            sz = [b - a for a, b in zip(lo, hi)]
            inv.append("%s(%dx%dx%d)" % (pid, round(sz[0]), round(sz[1]), round(sz[2])))
            ec, es = p.get("center_mm"), p.get("size_mm")
            drift = (not isinstance(ec, list) or len(ec) != 3 or not isinstance(es, list) or len(es) != 3 or
                     any(abs(ctr[q] - ec[q]) > 3.0 or abs(sz[q] - es[q]) > 3.0 for q in range(3)))
            if pid in joint_pids or p.get("free") or drift:
                notable.append("%s: lo%s hi%s mm" % (pid, lo, hi))
            else:
                matched += 1
        bb_txt = "\n".join(notable) + ("\n... (%d static parts match planned center_mm/size_mm within 3 mm)" % matched if matched else "")
        return ("ALL CHECKS PASSED. FINAL REVIEW against the TASK text.\nPART INVENTORY (%d parts)\n%s\nBOUNDING BOXES at rest (joints, free, or >3 mm off plan)\n%s\nJOINTS\n%s\nReply done, or fix what differs from the TASK."
                % (len(parts), ", ".join(inv), bb_txt, jt))

    def describe(self):
        lines = ["%s: %s" % (p["id"], p["spec"]) for p in self.plan["parts"]]
        for j in self.plan.get("joints", []):
            lines.append("khớp %s: %s -> %s, trục %s, tâm %s mm, góc %.3f rad" % (j["name"], j.get("parent") or "(nền)", "+".join(j["children"]), j["axis"], j["origin_mm"], float(j["span_rad"])))
        return "\n".join(lines)


def _log_director(workdir, entry):
    try:
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "director_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _usage_totals(sc):
    t = {}
    for role in ("director", "worker"):
        us = sc.usage[role]
        t[role] = {"calls": len(us), "input": sum(u.get("input", 0) for u in us), "cache_read": sum(u.get("cache_read", 0) for u in us),
                   "cache_write": sum(u.get("cache_write", 0) for u in us), "output": sum(u.get("output", 0) for u in us),
                   "usd": round(sum(u.get("usd", 0) or 0 for u in us), 4), "estimated": any(u.get("estimated") for u in us)}
    d_in = t["director"]["input"] + t["director"]["cache_read"] + t["director"]["cache_write"]
    d_tok = d_in + t["director"]["output"]
    w_in = t["worker"]["input"] + t["worker"]["cache_read"] + t["worker"]["cache_write"]
    w_tok = w_in + t["worker"]["output"]
    parts = sc.plan.get("parts", []) if sc.plan else []
    n_parts = len(parts)
    is_der = lambda p: bool(p.get("_derived_kind") or p.get("_mirror_from") or p.get("_offset_from"))
    n_derived = sum(1 for p in parts if is_der(p))
    n_dir_ops = sum(1 for p in parts if p.get("ops") and not is_der(p))
    n_worker_parts = max(0, n_parts - n_derived - n_dir_ops)
    avg_w = (w_tok / n_worker_parts) if (n_worker_parts > 0 and w_tok > 0) else (1500.0 if n_derived > 0 else 0.0)
    derived_tok_est = int(round(n_derived * avg_w))
    baseline_tok = d_tok + w_tok + derived_tok_est
    saved_tok_pct = round(100.0 * (w_tok + derived_tok_est) / baseline_tok, 1) if baseline_tok > 0 else 0.0
    offload_calls_pct = round(100.0 * t["worker"]["calls"] / max(1, t["director"]["calls"] + t["worker"]["calls"]), 1)
    cache_hit_pct = round(100.0 * t["director"]["cache_read"] / d_in, 1) if d_in > 0 else 0.0
    t["savings"] = {"saved_tok_pct": saved_tok_pct, "offload_calls_pct": offload_calls_pct, "cache_hit_pct": cache_hit_pct,
                    "worker_tokens": w_tok, "director_tokens": d_tok, "derived_parts": n_derived,
                    "worker_parts": n_worker_parts, "director_ops_parts": n_dir_ops}
    return t


def extract_part_mentions(request, plan):
    """Extract ordered unique `@part_id` references present in `plan['parts']`."""
    if not isinstance(plan, dict):
        return []
    pids = {p.get("id") for p in (plan.get("parts") or ()) if p.get("id")}
    seen, out = set(), []
    for m in re.findall(r"(?:^|[\s,(])@([A-Za-z][A-Za-z0-9_]{0,40})\b", str(request or "")):
        if m in pids and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def run(request, workdir, blender, director, worker, on_event=None, should_stop=None, log=print, resume=False, max_director=10, skills=None, images=None, review_images=False, plan_cache=None):
    """-> dict like agent.run: final, steps, warnings, sec, history, stuck, stopped, ended_by_limit, ok, usage, scene, report."""
    t0 = time.time()
    sc = Scene(workdir, blender, director, worker, on_event, should_stop, log, skills, images)
    ev = sc.ev
    plan = None
    if resume:                                              # a follow-up: continue from the saved plan and the last report
        try:
            with open(os.path.join(workdir, "plan.json"), encoding="utf-8") as f:
                plan = json.load(f)
            sc.set_plan(plan)
            sc.pick_skills(plan)
            for p in plan["parts"]:                          # parts written by an older version: the current static rules apply to them too
                try:
                    with open(sc.pf(p["id"]), encoding="utf-8") as f:
                        bad = static_check(f.read(), p["id"])
                except OSError:
                    continue
                if bad:
                    sc.err[p["id"]] = bad
        except (OSError, ValueError):
            plan = None
    inner_ready = plan is not None
    if inner_ready:
        rep0 = {}
        try:
            with open(os.path.join(workdir, "report.json"), encoding="utf-8") as f:
                rep0 = json.load(f)
        except (OSError, ValueError):
            pass
        mentions = extract_part_mentions(request, plan)
        if mentions:
            dplan = plan_delta_view(plan, rep0, {m: "targeted" for m in mentions}, [])
            msg = ("CURRENT SCENE (already built)\nPLAN: " + json.dumps(dplan) +
                   "\nUSER TARGETED PART(S): " + ", ".join(mentions) +
                   " — modify ONLY the targeted part(s) (using write_part, patch_part, or a minimal patch_plan) unless connected parts or joints must also adjust." +
                   "\nThe user now asks: " + request)
        else:
            msg = ("CURRENT SCENE (already built)\nPLAN: " + json.dumps(plan_view(plan)) + "\n" + sc.review_text(rep0) +
                   "\nThe user now asks: " + request + "\nReply with the action(s) needed (plan for a new structure, patch_plan to change several things at once, write_part/patch_part for one part), or done if nothing changes.")
    else:
        ev("phase", what="plan", mode="director")
        msg = "TASK: " + request + ("\n%d picture(s) are attached: study them and write what you see (shapes, proportions, style) into the part specs, because the worker cannot see them." % len(sc.images) if sc.images else "") + "\nStart with the plan action."
    task = request
    calls, reviewed, extra_review_budget, done_fixes, ok, stopped, why = 0, 0, 0, 0, False, False, ""
    cur_phase = "followup" if inner_ready else "plan"
    default_cap = (max_director == 10)
    eff_max = max(max_director, scale_policy(len(plan["parts"]))["min_director"]) if (plan and default_cap) else max_director
    cache_file, cached, plan_saved = None, None, False
    if plan_cache and not inner_ready and not images and os.environ.get("BX_PLANCACHE", "1") != "0":          # an identical request (and setup) reuses its plan: no director call
        import hashlib
        key = hashlib.sha256(json.dumps([request, sorted((skills or {}).get("names") or []), (skills or {}).get("explicit", ""), SYSTEM], sort_keys=True).encode()).hexdigest()[:24]
        cache_file = os.path.join(plan_cache, key + ".json")
        try:
            with open(cache_file, encoding="utf-8") as f:
                cached = json.load(f)
            if validate_plan(cached):
                cached = None
        except (OSError, ValueError):
            cached = None
    rep = {}
    try:
        while calls < eff_max:
            t_call = time.time()
            if cached is not None and calls == 0:
                txt = json.dumps(cached)
                ev("phase", what="plan", mode="cache")
            else:
                txt = sc.ask_director(msg, phase=cur_phase)
            dt_call = round(time.time() - t_call, 1)
            calls += 1
            last_u = sc.usage["director"][-1] if sc.usage["director"] else {}
            act = jparse(txt)
            if not act:
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": None, "errs": ["jparse_failed"], "len": len(txt or ""), "head": (txt or "")[:300], "tail": (txt or "")[-300:], "usage": last_u})
                if len(txt or "") > 4000 and not (txt or "").rstrip().endswith("}"):
                    msg = ("TASK: " + task + "\nYour reply (%d chars) appears cut off before the closing '}' — keep internal thinking brief, omit \"ops\" on plain parts so the worker builds them, "
                           "use \"mirror_of\" for symmetric L/R pairs, and output ONE compact JSON object." % len(txt or ""))
                else:
                    msg = "TASK: " + task + "\nYour reply was not one JSON object. Reply with one JSON object."
                cur_phase = "patch"
                continue
            a = act.get("action")
            if a == "plan":
                act, xerrs = expand_plan(act)
                errs = xerrs or validate_plan(act)
                if errs:
                    _log_director(workdir, {"call": calls, "sec": dt_call, "action": "plan", "errs": errs[:15], "len": len(txt or ""), "usage": last_u})
                    prev_j = ("\nYOUR PREVIOUS JSON (fix ONLY the errors listed above and reply with the corrected JSON object; do not redesign from scratch):\n" + (txt or "")[:90000]) if len(txt or "") > 200 else ""
                    msg = "TASK: " + task + "\nPLAN REJECTED by static checks:\n- " + "\n- ".join(errs) + prev_j + "\nSend a corrected plan."
                    cur_phase = "patch"
                    continue
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": "plan", "errs": [], "parts": len(act.get("parts") or []), "len": len(txt or ""), "usage": last_u})
                sc.set_plan(act)
                sc.pick_skills(act)
                plan = act
                if reviewed >= 1:
                    extra_review_budget = 1
                if default_cap:
                    eff_max = max(eff_max, scale_policy(len(plan["parts"]))["min_director"])
                if cache_file and cached is None and not plan_saved:
                    plan_saved = True
                    try:
                        os.makedirs(plan_cache, exist_ok=True)
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(act, f)
                    except OSError:
                        pass
                ev("plan", mode="Blender", ok=True, describe=sc.describe(), milestones=len(plan["parts"]), pitfalls=[], note="")
                ev("phase", what="parts")
            elif a == "patch_plan" and plan:
                newp, perrs = apply_patch(plan, act)
                newp, xerrs = expand_plan(newp)
                errs = perrs + xerrs + validate_plan(newp)
                if errs:
                    _log_director(workdir, {"call": calls, "sec": dt_call, "action": "patch_plan", "errs": errs[:15], "len": len(txt or ""), "usage": last_u})
                    prev_j = ("\nYOUR PREVIOUS JSON (fix ONLY the errors listed above and reply with the corrected JSON object; do not redesign from scratch):\n" + (txt or "")[:60000]) if len(txt or "") > 200 else ""
                    msg = "TASK: " + task + "\nPATCH REJECTED by static checks (nothing was changed):\n- " + "\n- ".join(errs) + prev_j + "\nSend a corrected patch_plan."
                    cur_phase = "patch"
                    continue
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": "patch_plan", "errs": [], "parts": len(newp.get("parts") or []), "len": len(txt or ""), "usage": last_u})
                sc.set_plan(newp)
                plan = newp
                if reviewed >= 1 and (act.get("add_parts") or act.get("remove_parts")):
                    extra_review_budget = 1
                if default_cap:
                    eff_max = max(eff_max, scale_policy(len(plan["parts"]))["min_director"])
                for pid, note in (act.get("notes") or {}).items():
                    if pid in {p["id"] for p in plan["parts"]}:
                        sc.notes[pid] = str(note); sc.stuck.pop(pid, None); sc.sigs[pid] = []; sc.tries[pid] = 0; sc.err[pid] = "the director asked for a rewrite"
                ev("plan", mode="Blender", ok=True, describe=sc.describe(), milestones=len(plan["parts"]), pitfalls=[], note="patch")
                ev("phase", what="parts")
            elif a == "write_part" and plan and act.get("id") in {p["id"] for p in plan["parts"]}:
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": "write_part", "errs": [], "id": act["id"], "len": len(txt or ""), "usage": last_u})
                pid = act["id"]
                sc.notes[pid] = act.get("note", "")
                sc.stuck.pop(pid, None)
                sc.sigs[pid] = []
                sc.tries[pid] = 0
                sc.err[pid] = "the director asked for a rewrite"
            elif a == "patch_part" and plan and act.get("id") in {p["id"] for p in plan["parts"]}:
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": "patch_part", "errs": [], "id": act["id"], "len": len(txt or ""), "usage": last_u})
                pid = act["id"]
                with open(sc.pf(pid), "w", encoding="utf-8") as f:
                    f.write(code_of(act.get("code", "")))
                sc.stuck.pop(pid, None)
                sc.sigs[pid] = []
                sc.tries[pid] = 1
                sc.err.pop(pid, None)
            elif a == "done" and plan is None:
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": "done", "errs": ["no_plan_yet"], "len": len(txt or ""), "usage": last_u})
                msg = "TASK: " + task + "\nNo plan yet. Send the plan action."
                cur_phase = "patch"
                continue
            elif a == "done":
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": "done", "errs": [], "len": len(txt or ""), "usage": last_u})
                rep = sc.build()
                fixable = rep.get("part_errors") or rep.get("missing") or any(i.get("part") and i.get("kind") in PART_ISSUES for i in rep.get("issues", []))
                if fixable and done_fixes < 2:                # "done" is not accepted while a part still fails to build: the worker gets another go
                    done_fixes += 1
                else:
                    ok = bool(rep.get("ok"))
                    break
            else:
                _log_director(workdir, {"call": calls, "sec": dt_call, "action": a, "errs": ["unknown_or_no_plan"], "len": len(txt or ""), "usage": last_u})
                msg = "TASK: " + task + "\nUnknown action or no plan. Reply with a valid action."
                cur_phase = "patch"
                continue
            rep, others = sc.work()
            if sc.stuck or others:
                msg = "TASK: " + task + "\nPLAN: " + json.dumps(plan_delta_view(plan, rep, sc.stuck, others)) + "\nWORKER REPORT (the worker could not resolve these)\n" + sc.worker_report(others)
                cur_phase = "patch"
            elif reviewed < MAX_REVIEWS + extra_review_budget:
                reviewed += 1
                ev("phase", what="review")
                msg = "TASK: " + task + "\nPLAN: " + json.dumps(review_plan_view(plan)) + "\n" + sc.review_text(rep)
                cur_phase = "review"
                views = sc.render_views() if review_images else []
                if views:                                         # one look at the result: iso, side, top at rest, and iso at the end of the motion
                    sc.images = views
                    msg += ("\nPICTURES attached (Workbench renders, each part in its own colour): iso, side, top at rest, and iso at the end of the motion. Besides the numbers, judge the look: "
                            "missing or floating parts, wrong proportions, parts in the wrong place, motion that looks wrong. Fix real defects with patch_plan (several at once), write_part or patch_part; do not ask for cosmetic changes.")
            else:
                ok = bool(rep.get("ok"))
                break
        else:
            why = "đã dùng hết %d lượt hỏi cố vấn mà chưa xong" % eff_max
    except Stopped:
        stopped = True
    sec = round(time.time() - t0, 1)
    tot = _usage_totals(sc)
    scene = os.path.join(workdir, "scene.blend") if os.path.exists(os.path.join(workdir, "scene.blend")) else ""
    issues = rep.get("issues", []) if rep else []
    parts = len(plan["parts"]) if plan else 0
    if stopped:
        final = ""
    elif not plan:
        final = "Cố vấn không lập được kế hoạch."
        why = why or "không có kế hoạch"
    else:
        final = ("%s %d chi tiết trong Blender%s.\n\n%s\n\n" % ("Đã dựng cảnh" if ok else "Cảnh CHƯA hoàn tất:", parts, " (có chuyển động)" if plan.get("joints") else "", sc.describe()))
        if ok:
            final += "Kiểm tra độc lập: **đạt** (đủ chi tiết, không xuyên nhau, khớp quay đúng trục, kích thước hợp lý)."
        else:
            final += "Kiểm tra độc lập: **chưa đạt**.\n" + "\n".join("- " + json.dumps(i, ensure_ascii=False) for i in issues[:8])
            if rep.get("part_errors"):
                final += "\nChi tiết còn lỗi dựng: " + ", ".join(sorted(rep["part_errors"]))
            if sc.stuck:
                final += "\nChi tiết kẹt: " + ", ".join("%s (%s)" % kv for kv in sc.stuck.items())
        final += "\n\nChi phí: cố vấn %d lượt%s, Qwen %d lượt (%d token ra)." % (
            tot["director"]["calls"], (", %.4f USD" % tot["director"]["usd"]) if not tot["director"]["estimated"] else " (token ước lượng)", tot["worker"]["calls"], tot["worker"]["output"])
        sv = tot.get("savings") or {}
        if sv:
            final += "\nTiết kiệm token cố vấn: **~%.1f%%** (%d%% lượt gọi chuyển sang thực thi%s%s)." % (
                sv.get("saved_tok_pct", 0.0), round(sv.get("offload_calls_pct", 0.0)),
                (", %d chi tiết đối xứng/mảng 0 token" % sv["derived_parts"]) if sv.get("derived_parts") else "",
                (", cache đọc lại %.0f%%" % sv["cache_hit_pct"]) if sv.get("cache_hit_pct", 0.0) >= 10 else "")
    with open(os.path.join(workdir, "usage.json"), "w", encoding="utf-8") as f:
        json.dump(tot, f, indent=1)
    return {"final": final, "measure": None, "steps": sc.step, "warnings": len(issues), "gates": 0, "sec": sec, "stuck": why if (not ok and not stopped) else "",
            "ended_by_limit": bool(why and not ok), "stopped": stopped, "ok": ok, "usage": tot, "scene": scene, "report": rep, "budget": None, "ctx_tokens": [],
            "history": {"scene3d": True, "workdir": workdir, "task": task}}
