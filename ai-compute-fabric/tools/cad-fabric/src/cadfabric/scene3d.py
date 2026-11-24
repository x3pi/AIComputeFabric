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
import json
import os
import re
import subprocess
import time

from .cq_backend import NOWIN

HERE = os.path.dirname(os.path.abspath(__file__))
from .blender import bxlib
COLORS = os.environ.get("BX_COLORS", "1") != "0"           # the director also chooses colours and finishes (applied by the runner)
RECIPES = os.environ.get("BX_RECIPES", "1") != "0"        # parts may be JSON recipes built by blender/bxlib.py (set 0 to force plain bmesh code)
DIRECTOR_OPS_ON = os.environ.get("BX_DIRECTOR_OPS", "1") != "0"   # the director writes the geometry recipes of simple parts into the plan (0: the worker writes them; cheaper for the director, less reliable)
GUARD = os.environ.get("BX_GUARD", "1") != "0"            # a repair round that makes the scene worse is undone
BLENDER_BUILD = os.path.join(HERE, "blender", "blender_build.py")
BLENDER_RENDER = os.path.join(HERE, "blender", "blender_render.py")
MAX_TRIES = 4                  # worker attempts per part (all causes together)
GROUP_MAX = 4                  # parts written in one worker request when the plan groups them
FINISHES = ("matte", "gloss", "metal", "rubber", "emissive")
PART_ISSUES = ("not_coaxial", "object_scale", "not_rigid", "wrong_pivot", "wrong_axis", "coaxial_unknown", "scale_outlier", "object_transform", "size_mismatch", "position_mismatch")

RECIPE_OPS = """{"ops":[ {"op":"box","center":[x,y,z],"size":[dx,dy,dz],"rot":{"axis":[0,0,1],"deg":25},"bevel":2},
         {"op":"cylinder","a":[x,y,z],"b":[x,y,z],"r":20,"r2":15},                       (any direction; r2 = taper)
         {"op":"ring","center":[x,y,z],"axis":"y","r_out":300,"r_in":220,"width":120,"bevel":6},   (tire, rim, disc, washer)
         {"op":"disc","center":[x,y,z],"axis":"x","r":40,"thickness":10},
         {"op":"beam","points":[[x,y,z],[x,y,z],[x,y,z]],"w":50,"h":40,"bevel":3},        (bent bar / spar)
         {"op":"sphere","center":[x,y,z],"r":14},   {"op":"sphere","center":[x,y,z],"radii":[30,20,10]},
         {"op":"loft","sections":[{"x":0,"y":0,"z":800,"w":100,"h":60},{"x":300,"y":0,"z":860,"w":240,"h":140}],"round":3},   (streamlined shell along X)
         {"op":"spokes","center":[x,y,z],"axis":"y","n":5,"r_in":45,"r_out":214,"w":24,"t":14},
         {"op":"tube","points":[[x,y,z],[x,y,z],[x,y,z]],"r":12},                           (bent ROUND tube with smooth joints: fork leg, handlebar, exhaust, spar)
         {"op":"panel","sections":[{"x":0,"y":0,"z":800,"w":100,"h":60},{"x":300,"y":0,"z":860,"w":240,"h":140}],"arc":[0,180],"thick":3} ]}   (thin curved SHEET: fairing, fender, side panel; arc [0,180] = upper half, [180,360] = lower half)"""

DIRECTOR_OPS = """GEOMETRY BY RECIPE (preferred, deterministic, no worker needed): for every part made of simple shapes - wheels, tires, rims, spokes, hubs, discs, axles, tubes, beams, brackets, boxes, lamps, lofted body shells - put the geometry yourself in the plan as "ops":[...] on that part (mm, WORLD coordinates, same format as below; the part's center_mm/size_mm then follow from it). The runner builds it exactly, with nothing to get wrong in code. Omit "ops" only for genuinely free-form shapes; the worker then writes code for them.
Recipe format:
""" + RECIPE_OPS + "\n"

COLOR_RULE = """Give EVERY part a "color" and a "finish": choose ONE coherent, attractive palette for the whole object (a hero colour for the main body panels with one contrasting accent colour, dark matte or rubber for tires and mechanical parts, metal for discs, axles and fasteners, emissive for lamps), keep left/right pairs identical, and make the colours fit the TASK text; colours are applied by the runner, the worker never sees them. """
SYSTEM = """You direct a 3D build in Blender. You do not write geometry unless told; a worker model writes one part at a time and fixes its own errors.
You are called only to plan, to resolve what the worker could not, and for one final review against the TASK text.
Reply with ONE JSON object, no prose. Actions:
 {"action":"plan","parts":[{"id":"p1","spec":"what it is, size mm, where (world mm, Z up)","center_mm":[x,y,z],"size_mm":[dx,dy,dz],"ops":[recipe ops, optional but preferred for simple shapes],"color":"#RRGGBB","finish":"matte|gloss|metal|rubber|emissive","group":"optional batching hint only"}],
  "joints":[{"name":"j1","parent":"p1"|null,"children":["p2"],"origin_mm":[x,y,z],"axis":[x,y,z],"coaxial":["p2"],"span_rad":6.283}],
  "allowed_contacts":[["p1","p2"]]}
 {"action":"write_part","id":"p1","note":"guidance for the worker"}   (worker retries with your note)
 {"action":"patch_part","id":"p1","code":"<python defining build(bpy, bmesh)>"}   (you write it yourself; use when the worker is stuck)
 {"action":"done"}
""" + (DIRECTOR_OPS if RECIPES and DIRECTOR_OPS_ON else "") + """Rules: every physical part in the TASK gets its own id and its own mesh (four posts = four parts); never merge parts. "group" only lets the worker write several separate parts in one request; use the same group name for parts of the same kind. Parts that should touch/overlap by design must be in allowed_contacts; joint children spin about axis through origin_mm (span_rad is SIGNED: sign follows the right-hand rule about axis); list a part in coaxial only if it is a body of revolution centred on the axis. Give EVERY part center_mm and size_mm: the centre and the extents (dx,dy,dz) of its world axis-aligned bounding box at rest, in mm (for a tilted part, the box of the tilted shape); they are measured after the build and the worker is told when its mesh differs, so make them consistent with the spec and with the other parts. """ + (COLOR_RULE if COLORS else "") + """In the final review compare the numbers you are shown (bounding boxes, joint spans) with the TASK text, including directions and signs, and fix what differs."""

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
    try:
        return json.loads(re.search(r"\{.*\}", txt or "", re.S).group(0))
    except Exception:  # noqa: BLE001
        return None


def code_of(txt):
    m = re.search(r"```(?:python)?\n(.*?)```", txt or "", re.S)
    return (m.group(1) if m else (txt or "")).strip() + "\n"


RECIPE_TEMPLATE = "# recipe part (built by bxlib)\ndef build(bpy, bmesh):\n    import bxlib\n    return bxlib.build(bpy, bmesh, %r, %r)\n"


def part_code(txt, pid):
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
            return RECIPE_TEMPLATE % (pid, rec)
    return code_of(txt)


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
        self.step = 0
        os.makedirs(os.path.join(workdir, "parts"), exist_ok=True)

    # -- model calls
    def ask_director(self, prompt):
        if self.stop():
            raise Stopped()
        system = SYSTEM + self.director_skill_text()
        if self.images:                                   # the pictures go to the director once, with its first message
            imgs, self.images = self.images, []
            text, u = self.director(prompt, system, images=imgs)
        else:
            text, u = self.director(prompt, system)
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

    def ask_worker(self, user):
        if self.stop():
            raise Stopped()
        text, u = self.worker(WORKER_SYS + self.worker_skill_text(user), user)
        self.usage["worker"].append(u or {})
        return text

    # -- files
    def pf(self, pid):
        return os.path.join(self.dir, "parts", pid + ".py")

    def spec(self, pid):
        p = next((p for p in self.plan["parts"] if p["id"] == pid), None)
        if not p:
            return ""
        extra = ""
        if isinstance(p.get("center_mm"), list) and isinstance(p.get("size_mm"), list):
            extra = " | REQUIRED world bbox at rest: centre %s mm, size %s mm (checked after the build)" % (p["center_mm"], p["size_mm"])
        return p["spec"] + extra

    def pick_skills(self, plan):
        known = set((self.skills or {}).get("worker_names") or (self.skills or {}).get("names") or [])
        want = plan.get("skills") if isinstance(plan.get("skills"), list) else []
        self.skill_names = [n for n in want if isinstance(n, str) and n in known][:3]

    def set_plan(self, plan):
        keep = {p["id"] for p in plan["parts"]}
        old = self.plan
        self.plan = plan
        with open(os.path.join(self.dir, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=1)
        if old:                                           # a part whose spec changed must be rewritten, a removed part must go
            for p in old["parts"]:
                if p["id"] not in keep and os.path.exists(self.pf(p["id"])):
                    os.unlink(self.pf(p["id"]))
            newspec = {p["id"]: p["spec"] for p in plan["parts"]}
            for p in old["parts"]:
                if p["id"] in keep and newspec[p["id"]] != p["spec"]:
                    self.err[p["id"]] = "the spec of this part changed; rewrite it"
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
        prev = ""
        if os.path.exists(self.pf(pid)):
            with open(self.pf(pid), encoding="utf-8") as f:
                prev = "\nPrevious code:\n```python\n" + f.read() + "```"
        if error:
            prev += "\nThis code FAILED with:\n" + error + "\nFix that error."
        code = part_code(self.ask_worker("Part id: %s\nSpec: %s\nNote: %s%s" % (pid, self.spec(pid), self.notes.get(pid, ""), prev)), pid)
        self.tries[pid] = self.tries.get(pid, 0) + 1
        with open(self.pf(pid), "w", encoding="utf-8") as f:
            f.write(code)
        return code

    def write_group(self, pids):
        specs = "\n".join("- %s: %s" % (p, self.spec(p)) for p in pids)
        txt = self.ask_worker("Write ALL of these parts. For each part output a line `# PART <id>` then one code block (python defining build(bpy, bmesh)" + (", or a json recipe" if RECIPES else "") +
                              ") for that part (object named exactly the id).\n" + specs)
        got = set()
        for m in re.finditer(r"#\s*PART\s+(\S+)\s*\n```(python|json)?\n(.*?)```", txt, re.S):
            if m.group(1) in pids:
                with open(self.pf(m.group(1)), "w", encoding="utf-8") as f:
                    f.write(part_code("```%s\n%s```" % (m.group(2) or "python", m.group(3)), m.group(1)) if m.group(2) == "json" else m.group(3).strip() + "\n")
                self.tries[m.group(1)] = 1
                got.add(m.group(1))
        return got

    def settle(self, pid, error):
        """record a failure; True when the worker should try again"""
        s = sig(error)
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
            self.step += 1
            self.ev("cell", n=self.step, ok=not err, code=code, error=err or "", output="", part=pid)
            if not err:
                self.err.pop(pid, None)
                return
            self.err[pid] = err
            if not self.settle(pid, err):
                return
            code = None

    def work(self):
        """bring every part to green or to stuck. -> (report, issues the worker cannot resolve)"""
        todo = [p["id"] for p in self.plan["parts"] if p["id"] not in self.stuck and (not os.path.exists(self.pf(p["id"])) or p["id"] in self.err)]
        groups, singles = {}, []
        for p in self.plan["parts"]:
            if p["id"] in todo and p.get("group"):
                groups.setdefault(p["group"], []).append(p["id"])
            elif p["id"] in todo:
                singles.append([p["id"]])
        batches = singles + [g[k:k + GROUP_MAX] for g in groups.values() for k in range(0, len(g), GROUP_MAX)]
        for pids in batches:
            got = self.write_group(pids) if len(pids) > 1 else set()
            for pid in pids:
                self.attempt(pid, err=self.err.get(pid), have_code=(pid in got))
        self.ev("phase", what="build")
        rep = self.build()
        ids = {p["id"] for p in self.plan["parts"]}
        undone = False
        for _ in range(MAX_TRIES):
            redo = {pid: e for pid, e in rep.get("part_errors", {}).items() if pid not in self.stuck}
            for i in rep.get("issues", []):                  # single-part issues are fixable by the worker; pair/joint issues go to the director
                if i.get("part") in ids and i["kind"] in PART_ISSUES and i["part"] not in self.stuck:
                    redo.setdefault(i["part"], json.dumps(i))
            if not redo:
                break
            saved = {}
            for pid in redo:
                if os.path.exists(self.pf(pid)):
                    with open(self.pf(pid), encoding="utf-8") as f:
                        saved[pid] = f.read()
            before = self.score(rep)
            for pid, e in redo.items():
                self.attempt(pid, err=e, external=True)
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
        rows = ["%s: %s (worker tries %d)" % (p["id"], ("STUCK - " + self.stuck[p["id"]]) if p["id"] in self.stuck else "built", self.tries.get(p["id"], 0)) for p in self.plan["parts"]]
        return "PARTS\n" + "\n".join(rows) + "\nISSUES\n" + ("\n".join(json.dumps(i) for i in others[:12]) or "none")

    def review_text(self, rep):
        bb = "\n".join("%s: lo%s hi%s mm" % (k, v[0], v[1]) for k, v in sorted(rep.get("parts_bbox_mm", {}).items()))
        jt = "\n".join("%s: parent=%s children=%s origin=%s axis=%s span_rad=%s" % (j["name"], j.get("parent"), j["children"], j["origin_mm"], j["axis"], j["span_rad"])
                       for j in self.plan.get("joints", []))
        return "ALL CHECKS PASSED. FINAL REVIEW against the TASK text.\nBOUNDING BOXES at rest\n" + bb + "\nJOINTS\n" + jt + "\nReply done, or fix what differs from the TASK."

    def describe(self):
        lines = ["%s: %s" % (p["id"], p["spec"]) for p in self.plan["parts"]]
        for j in self.plan.get("joints", []):
            lines.append("khớp %s: %s -> %s, trục %s, tâm %s mm, góc %.3f rad" % (j["name"], j.get("parent") or "(nền)", "+".join(j["children"]), j["axis"], j["origin_mm"], float(j["span_rad"])))
        return "\n".join(lines)


def _usage_totals(sc):
    t = {}
    for role in ("director", "worker"):
        us = sc.usage[role]
        t[role] = {"calls": len(us), "input": sum(u.get("input", 0) for u in us), "cache_read": sum(u.get("cache_read", 0) for u in us),
                   "cache_write": sum(u.get("cache_write", 0) for u in us), "output": sum(u.get("output", 0) for u in us),
                   "usd": round(sum(u.get("usd", 0) or 0 for u in us), 4), "estimated": any(u.get("estimated") for u in us)}
    return t


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
        msg = ("CURRENT SCENE (already built)\nPLAN: " + json.dumps(plan) + "\n" + sc.review_text(rep0) +
               "\nThe user now asks: " + request + "\nReply with the action(s) needed (plan to change the structure, write_part/patch_part to change a part), or done if nothing changes.")
    else:
        ev("phase", what="plan", mode="director")
        msg = "TASK: " + request + ("\n%d picture(s) are attached: study them and write what you see (shapes, proportions, style) into the part specs, because the worker cannot see them." % len(sc.images) if sc.images else "") + "\nStart with the plan action."
    task = request
    calls, reviewed, done_fixes, ok, stopped, why = 0, 0, 0, False, False, ""
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
        while calls < max_director:
            if cached is not None and calls == 0:
                txt = json.dumps(cached)
                ev("phase", what="plan", mode="cache")
            else:
                txt = sc.ask_director(msg)
            calls += 1
            act = jparse(txt)
            if not act:
                msg = "TASK: " + task + "\nYour reply was not one JSON object. Reply with one JSON object."
                continue
            a = act.get("action")
            if a == "plan":
                errs = validate_plan(act)
                if errs:
                    msg = "TASK: " + task + "\nPLAN REJECTED by static checks:\n- " + "\n- ".join(errs) + "\nSend a corrected plan."
                    continue
                sc.set_plan(act)
                sc.pick_skills(act)
                plan = act
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
            elif a == "write_part" and plan and act.get("id") in {p["id"] for p in plan["parts"]}:
                pid = act["id"]
                sc.notes[pid] = act.get("note", "")
                sc.stuck.pop(pid, None)
                sc.sigs[pid] = []
                sc.tries[pid] = 0
                sc.err[pid] = "the director asked for a rewrite"
            elif a == "patch_part" and plan and act.get("id") in {p["id"] for p in plan["parts"]}:
                pid = act["id"]
                with open(sc.pf(pid), "w", encoding="utf-8") as f:
                    f.write(code_of(act.get("code", "")))
                sc.stuck.pop(pid, None)
                sc.sigs[pid] = []
                sc.tries[pid] = 1
                sc.err.pop(pid, None)
            elif a == "done" and plan is None:
                msg = "TASK: " + task + "\nNo plan yet. Send the plan action."
                continue
            elif a == "done":
                rep = sc.build()
                fixable = rep.get("part_errors") or rep.get("missing") or any(i.get("part") and i.get("kind") in PART_ISSUES for i in rep.get("issues", []))
                if fixable and done_fixes < 2:                # "done" is not accepted while a part still fails to build: the worker gets another go
                    done_fixes += 1
                else:
                    ok = bool(rep.get("ok"))
                    break
            else:
                msg = "TASK: " + task + "\nUnknown action or no plan. Reply with a valid action."
                continue
            rep, others = sc.work()
            if sc.stuck or others:
                msg = "TASK: " + task + "\nPLAN: " + json.dumps(plan) + "\nWORKER REPORT (the worker could not resolve these)\n" + sc.worker_report(others)
            elif reviewed < 2:
                reviewed += 1
                ev("phase", what="review")
                msg = "TASK: " + task + "\nPLAN: " + json.dumps(plan) + "\n" + sc.review_text(rep)
                views = sc.render_views() if review_images else []
                if views:                                         # one look at the result: iso, side, top at rest, and iso at the end of the motion
                    sc.images = views
                    msg += ("\nPICTURES attached (Workbench renders, each part in its own colour): iso, side, top at rest, and iso at the end of the motion. Besides the numbers, judge the look: "
                            "missing or floating parts, wrong proportions, parts in the wrong place, motion that looks wrong. Fix real defects with write_part or patch_part; do not ask for cosmetic changes.")
            else:
                ok = bool(rep.get("ok"))
                break
        else:
            why = "đã dùng hết %d lượt hỏi cố vấn mà chưa xong" % max_director
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
    with open(os.path.join(workdir, "usage.json"), "w", encoding="utf-8") as f:
        json.dump(tot, f, indent=1)
    return {"final": final, "measure": None, "steps": sc.step, "warnings": len(issues), "gates": 0, "sec": sec, "stuck": why if (not ok and not stopped) else "",
            "ended_by_limit": bool(why and not ok), "stopped": stopped, "ok": ok, "usage": tot, "scene": scene, "report": rep, "budget": None, "ctx_tokens": [],
            "history": {"scene3d": True, "workdir": workdir, "task": task}}
