#!/usr/bin/env python3
"""Persistent CadQuery interpreter, one JSON request per line on stdin, one JSON reply per line on the real stdout.
Runs in its own process so a crash, a hang or a memory blow-up kills only this worker (the parent restarts it and replays the history).

Not a security boundary against a determined attacker: it blocks the obvious ways out (imports other than cadquery/math, file/process builtins,
dunder access) and limits memory; run the whole worker in a container when the model is not trusted."""
import re
import ast, builtins, contextlib, difflib, inspect, io, json, math, os, sys, traceback

_proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)                                  # OCC and stray prints go to stderr, never into the protocol stream
if os.name == "nt":                            # ...but on Windows OCCT writes to the Win32 standard handle, which dup2 does not change (STEP export corrupted the JSON line)
    import ctypes, msvcrt
    _nul = os.open(os.devnull, os.O_WRONLY)
    ctypes.windll.kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(_nul))
try:
    import resource
    lim = int(float(os.environ.get("CADFABRIC_MEM_GB", "6")) * 2**30)
    resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
except (ImportError, ValueError, OSError):
    pass
import cadquery as cq

ALLOWED_IMPORTS = {"cadquery", "math", "typing", "dataclasses", "itertools", "functools", "collections", "numpy"}
BAD_NAMES = {"open", "exec", "eval", "compile", "input", "breakpoint", "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
             "os", "sys", "subprocess", "importlib", "shutil", "socket", "ctypes", "pathlib", "builtins"}
SAFE = {k: getattr(builtins, k) for k in (
    "abs all any bool dict divmod enumerate filter float frozenset int isinstance issubclass iter len list map max min next pow print range repr reversed "
    "round set slice sorted str sum tuple type zip hash id callable chr ord format bytes bytearray complex object property staticmethod classmethod super "
    "Exception ValueError TypeError KeyError IndexError RuntimeError ZeroDivisionError AssertionError StopIteration ArithmeticError NotImplementedError "
    "True False None").split() if hasattr(builtins, k)}


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED_IMPORTS:
        raise ImportError("import of '%s' is not allowed (allowed: %s)" % (name, ", ".join(sorted(ALLOWED_IMPORTS))))
    return builtins.__import__(name, globals, locals, fromlist, level)


def _public(o):
    return [n for n in builtins.dir(o) if not n.startswith("_")]


def doc(obj=None):
    """Look an API up instead of guessing it: doc(cq.Wire) lists its public names, doc(cq.Workplane.loft) gives the signature and the first lines of its help."""
    if obj is None:
        obj = cq
    if inspect.isclass(obj) or inspect.ismodule(obj) or not callable(obj):
        return ", ".join(_public(obj))[:1500]
    try:
        sig = str(inspect.signature(obj))
    except (TypeError, ValueError):
        sig = "(...)"
    return (getattr(obj, "__name__", "?") + sig + "\n" + "\n".join((inspect.getdoc(obj) or "").splitlines()[:12]))[:1500]


_CLASSES = ("Workplane", "Sketch", "Assembly", "Shape", "Edge", "Wire", "Face", "Shell", "Solid", "Compound", "Vector", "Location", "Plane")


def _sig(f):
    try:
        return str(inspect.signature(f))
    except (TypeError, ValueError):
        return "(...)"


def find_api(word, limit=30):
    """Search every public CadQuery class for a name containing the word: find_api("vertic") -> lines like Shape.Vertices(self) -> ... Use it instead of guessing a name."""
    key = str(word).lower()
    out = []
    for cn in _CLASSES:
        c = getattr(cq, cn, None)
        for n in (_public(c) if c is not None else []):
            if key in n.lower():
                out.append("%s.%s%s" % (cn, n, _sig(getattr(c, n, None)) if callable(getattr(c, n, None)) else ""))
    return "\n".join(out[:limit]) or "(nothing named like '%s'; try a shorter word)" % word


def _resolve(dotted):
    o = cq
    for part in dotted.split("."):
        if part.startswith("_") or not hasattr(o, part):
            return None
        o = getattr(o, part)
    return o


def api_hint(e):
    """What the real API offers, for the two mistakes a model makes most: a name that does not exist and a wrong call signature."""
    try:
        if isinstance(e, AttributeError) and getattr(e, "obj", None) is not None and getattr(e, "name", None):
            names = _public(e.obj)
            key = e.name.lower()
            subs = [n for n in names if len(key) >= 4 and (key[-5:] in n.lower() or n.lower() in key)]
            near = difflib.get_close_matches(e.name, names, n=6, cutoff=0.5)
            pick = list(dict.fromkeys(near + subs))[:8]
            other = []                                   # the same idea often lives on another class (Edge.makeSpline, not Wire.makeSpline)
            for cn in ("Workplane", "Sketch", "Edge", "Wire", "Face", "Shell", "Solid", "Compound", "Vector", "Location", "Plane"):
                c = getattr(cq, cn, None)
                if c is not None and c is not e.obj:
                    pn = _public(c)
                    other += ["%s.%s" % (cn, n) for n in dict.fromkeys(difflib.get_close_matches(e.name, pn, n=2, cutoff=0.6) + [n for n in pn if len(n) >= 6 and (n.lower() in key or key in n.lower())])][:3]
            if other:
                pick += ["(on other classes) " + ", ".join(other[:6])]
            return "\nAPI HINT: %s has no '%s'. %s" % (getattr(e.obj, "__name__", type(e.obj).__name__), e.name, ("Closest real names: " + ", ".join(pick) + ". ") if pick else "") + ("Signatures: " + "; ".join("%s%s" % (n, _sig(getattr(e.obj, n))) for n in pick[:3] if not n.startswith("(") and callable(getattr(e.obj, n, None))) + ". " if any(not n.startswith("(") and callable(getattr(e.obj, n, None)) for n in pick[:3]) else "") + "Search all classes: find_api('word'); all names: doc(%s)" % (getattr(e.obj, "__name__", "obj"))
        if isinstance(e, TypeError):
            m = re.match(r"^([A-Za-z_][\w.]*)\(\)", str(e))
            f = _resolve(m.group(1)) if m else None
            if f is None and m and "." in m.group(1):
                f = _resolve(m.group(1).split(".", 1)[-1])
            if callable(f):
                return "\nAPI HINT: real signature: %s%s" % (m.group(1), inspect.signature(f))
    except BaseException:                                # noqa: BLE001 - a hint must never hide the real error
        pass
    return ""


SAFE["dir"] = lambda o=None: _public(o if o is not None else cq)             # names only; dunder names stay hidden
SAFE["__import__"] = _import
SAFE["__build_class__"] = builtins.__build_class__
SAFE["__name__"] = "cell"


def check(code):
    """AST screen. -> error text or None."""
    try:
        tree = ast.parse(code, "<cell>")
    except SyntaxError as e:
        return "SyntaxError: %s (line %s)" % (e.msg, e.lineno)
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and (n.id in BAD_NAMES or (n.id.startswith("__") and n.id not in ("__name__",))):
            return "name '%s' is not allowed in CAD code (line %s)" % (n.id, n.lineno)
        if isinstance(n, ast.Attribute) and n.attr.startswith("_"):
            return "attribute '%s' is not allowed (private/dunder access, line %s)" % (n.attr, n.lineno)
    return None


NS = {}


def fresh():
    NS.clear()
    NS.update({"__builtins__": dict(SAFE), "cq": cq, "doc": doc, "find_api": find_api, "math": math, "Workplane": cq.Workplane, "Vector": cq.Vector, "Location": cq.Location})
    return {"part": None}


STATE = fresh()


def as_shape(obj):
    if isinstance(obj, cq.Workplane):
        vals = [v for v in obj.vals() if isinstance(v, cq.Shape)]
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else cq.Compound.makeCompound(vals)
    if isinstance(obj, cq.Shape):
        return obj
    if isinstance(obj, cq.Assembly):
        return obj.toCompound()
    return None


def pick_part(before):
    """The part after a cell: `result` if the cell assigned it, else the last new/rebound Workplane or Shape."""
    cand = None
    for k, v in NS.items():
        if k in ("cq", "doc", "find_api", "math", "__builtins__", "Workplane", "Vector", "Location"):
            continue
        if isinstance(v, (cq.Workplane, cq.Shape, cq.Assembly)) and as_shape(v) is not None and before.get(k) is not id(v):
            cand = v
    if "result" in NS and as_shape(NS["result"]) is not None and before.get("result") is not id(NS["result"]):
        cand = NS["result"]
    return cand


INTERFERENCE_BUDGET_S = 8.0


def connectivity(solids, tol=0.05, budget_s=4.0):
    """Which solids touch or overlap (distance <= tol) and which hang in the air. Pairs are pre-filtered by bounding box (OCCT distance is slow); union-find makes the groups.
    Informational only: several separate solids are fine, but a part that touches nothing is usually a mistake. -> text, or '' for a single solid / when it cannot be computed."""
    import time as _t
    n = len(solids)
    if n < 2 or n > 60:
        return ""
    t0 = _t.perf_counter()
    try:
        bbs = [s.BoundingBox() for s in solids]
        par = list(range(n))

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]; i = par[i]
            return i
        inter = []                                           # (i, j, overlap volume): solids that share volume, not just touch
        skipped = 0

        def overlap_note():
            big = [(i, j, v) for i, j, v in inter if v > 0.01 * min(solids[i].Volume(), solids[j].Volume())]
            note = " (overlap check skipped for %d touching pair(s): too slow)" % skipped if skipped else ""
            if not big:
                return note
            big.sort(key=lambda t: -t[2] / max(min(solids[t[0]].Volume(), solids[t[1]].Volume()), 1e-9))
            return " INTERFERENCE (parts share volume; separate parts should only touch): " + "; ".join(
                "solids #%d and #%d overlap %.0f mm3 = %.0f%% of the smaller one" % (i, j, v, 100 * v / min(solids[i].Volume(), solids[j].Volume())) for i, j, v in big[:6]) + note
        for i in range(n):
            for j in range(i + 1, n):
                a, b = bbs[i], bbs[j]
                if a.xmax + tol < b.xmin or b.xmax + tol < a.xmin or a.ymax + tol < b.ymin or b.ymax + tol < a.ymin or a.zmax + tol < b.zmin or b.zmax + tol < a.zmin:
                    continue
                if _t.perf_counter() - t0 > budget_s:
                    return ""
                if solids[i].distance(solids[j]) <= tol:
                    par[find(i)] = find(j)
                    if _t.perf_counter() - t0 > INTERFERENCE_BUDGET_S:      # a boolean on curved solids can take minutes: skip the rest, never stall the cell
                        skipped += 1
                        continue
                    try:
                        bi, bj = bbs[i], bbs[j]
                        sh = max(0.0, min(bi.xmax, bj.xmax) - max(bi.xmin, bj.xmin)) * max(0.0, min(bi.ymax, bj.ymax) - max(bi.ymin, bj.ymin)) * max(0.0, min(bi.zmax, bj.zmax) - max(bi.zmin, bj.zmin))
                        if sh <= 0:                                          # boxes only touch: the solids cannot share volume
                            continue
                        v = solids[i].intersect(solids[j]).Volume()
                        if v > 1e-6:
                            inter.append((i, j, float(v)))
                    except BaseException:                    # noqa: BLE001 - a failed boolean just means "unknown"
                        pass
        groups = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        if len(groups) == 1:
            return "all %d solids touch or overlap (1 connected group)." % n + overlap_note()
        main = max(groups.values(), key=lambda g: sum(solids[k].Volume() for k in g))
        out = []
        for g in sorted(groups.values(), key=lambda g: -sum(solids[k].Volume() for k in g)):
            if g is main:
                continue
            best = None
            for i in g:
                for j in main:
                    if _t.perf_counter() - t0 > budget_s:
                        return "%d solids in %d groups (gap measurement timed out)" % (n, len(groups))
                    d = solids[i].distance(solids[j])
                    best = d if best is None else min(best, d)
            c = cq.Compound.makeCompound([solids[k] for k in g]).Center()
            out.append("FLOATING group of %d solid(s) near (%.0f, %.0f, %.0f), volume %.0f, %.2f mm from the main body" % (len(g), c.x, c.y, c.z, sum(solids[k].Volume() for k in g), best))
        return "%d solids in %d groups; main group has %d solid(s). %s" % (n, len(groups), len(main), "; ".join(out[:6])) + overlap_note()
    except BaseException:                                # noqa: BLE001 - a measurement helper must never break a cell
        return ""


def measure(shape):
    bb = shape.BoundingBox()
    c = shape.Center()
    return {"volume": float(shape.Volume()), "bbox_size": [bb.xlen, bb.ylen, bb.zlen], "bbox_min": [bb.xmin, bb.ymin, bb.zmin], "bbox_max": [bb.xmax, bb.ymax, bb.zmax],
            "com": [c.x, c.y, c.z], "faces": len(shape.Faces()), "edges": len(shape.Edges()), "vertices": len(shape.Vertices()),
            "solids": len(shape.Solids()), "valid": bool(shape.isValid()), "links": connectivity(shape.Solids())}


def _region(sh):
    if sh is None or sh.isNull() or sh.Volume() < 1e-6:
        return None
    bb = sh.BoundingBox()
    return {"volume": round(float(sh.Volume()), 3), "min": [round(bb.xmin, 3), round(bb.ymin, 3), round(bb.zmin, 3)], "max": [round(bb.xmax, 3), round(bb.ymax, 3), round(bb.zmax, 3)]}


def delta(before, after):
    """What this cell removed from / added to the part, as regions measured by the kernel. Lets the model SEE where a cut or a feature actually landed."""
    try:
        return {"removed": _region(before.cut(after)), "added": _region(after.cut(before))}
    except BaseException:                                # noqa: BLE001 - a failed boolean must never break the cell
        return None


def run(code, quiet=False):
    bad = check(code)
    if bad:
        return {"ok": False, "error": bad, "output": ""}
    before = {k: id(v) for k, v in NS.items()}
    saved = dict(NS)                                  # a failed cell must not leave half-applied bindings behind
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<cell>", "exec"), NS)
    except BaseException as e:                       # noqa: BLE001 - the model's code may raise anything
        NS.clear(); NS.update(saved)
        tb = traceback.extract_tb(e.__traceback__)
        line = next((f for f in reversed(tb) if f.filename == "<cell>"), None)
        where = " (line %d: %s)" % (line.lineno, (line.line or code.splitlines()[line.lineno - 1] if line and line.lineno <= len(code.splitlines()) else "").strip()[:160]) if line else ""
        return {"ok": False, "error": "%s: %s%s%s" % (type(e).__name__, str(e)[:400], where, api_hint(e)), "output": out.getvalue()[-2000:]}
    prev = as_shape(STATE["part"]) if STATE["part"] is not None else None
    part = pick_part(before)
    if part is not None:
        STATE["part"] = part
    r = {"ok": True, "output": out.getvalue()[-2000:], "changed": part is not None}
    if part is not None and prev is not None and as_shape(part) is not None and os.environ.get("CADFABRIC_NO_DELTA") != "1" and not quiet:   # quiet = a replay of saved cells: only the state matters
        r["delta"] = delta(prev, as_shape(part))
    return r


def handle(req):
    op = req.get("op")
    if op == "exec":
        r = run(req["code"], bool(req.get("quiet")))
    elif op == "reset":
        STATE.update(fresh()); r = {"ok": True}
    elif op == "export":
        sh = as_shape(STATE["part"]) if STATE["part"] is not None else None
        if sh is None:
            return {"ok": False, "error": "no part to export"}
        cq.exporters.export(cq.Workplane(obj=sh), req["path"], "STEP")
        return {"ok": True}
    elif op == "svg":
        sh = as_shape(STATE["part"]) if STATE["part"] is not None else None
        if sh is None:
            return {"ok": False, "error": "no part to draw"}
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.svg")
            cq.exporters.export(cq.Workplane(obj=sh), p, cq.exporters.ExportTypes.SVG,
                                opt={"width": 640, "height": 440, "marginLeft": 24, "marginTop": 24, "showAxes": False, "projectionDir": (1, -1, 0.8),
                                     "strokeWidth": 0.5, "strokeColor": (30, 30, 30), "hiddenColor": (170, 170, 170), "showHidden": True})
            svg = open(p, encoding="utf-8").read()
            if "viewBox" not in svg[:400]:                       # CadQuery gives fixed width/height only; a viewBox lets the page scale the drawing
                svg = re.sub(r"<svg\b", '<svg viewBox="0 0 640 440"', svg, count=1)
            return {"ok": True, "svg": svg}
    elif op == "noise" and os.environ.get("CADFABRIC_TEST_NOISE"):           # test hook: write to fd 1 the way a C library would
        os.write(1, b"NOISE FROM A C LIBRARY\n"); return {"ok": True}
    elif op == "ping":
        return {"ok": True}
    else:
        return {"ok": False, "error": "unknown op"}
    sh = as_shape(STATE["part"]) if STATE["part"] is not None else None
    r["measure"] = measure(sh) if (sh is not None and not req.get("quiet")) else None
    return r


for line in sys.stdin:
    try:
        rep = handle(json.loads(line))
    except BaseException as e:                       # noqa: BLE001
        rep = {"ok": False, "error": "worker error: %s: %s" % (type(e).__name__, str(e)[:300])}
    _proto.write(json.dumps(rep) + "\n")
