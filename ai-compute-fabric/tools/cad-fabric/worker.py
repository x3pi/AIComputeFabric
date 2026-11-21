#!/usr/bin/env python3
"""Persistent CadQuery interpreter, one JSON request per line on stdin, one JSON reply per line on the real stdout.
Runs in its own process so a crash, a hang or a memory blow-up kills only this worker (the parent restarts it and replays the history).

Not a security boundary against a determined attacker: it blocks the obvious ways out (imports other than cadquery/math, file/process builtins,
dunder access) and limits memory; run the whole worker in a container when the model is not trusted."""
import ast, builtins, contextlib, io, json, math, os, sys, traceback

_proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)                                  # OCC and stray prints go to stderr, never into the protocol stream
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
    NS.update({"__builtins__": dict(SAFE), "cq": cq, "math": math, "Workplane": cq.Workplane, "Vector": cq.Vector, "Location": cq.Location})
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
        if k in ("cq", "math", "__builtins__", "Workplane", "Vector", "Location"):
            continue
        if isinstance(v, (cq.Workplane, cq.Shape, cq.Assembly)) and as_shape(v) is not None and before.get(k) is not id(v):
            cand = v
    if "result" in NS and as_shape(NS["result"]) is not None and before.get("result") is not id(NS["result"]):
        cand = NS["result"]
    return cand


def measure(shape):
    bb = shape.BoundingBox()
    c = shape.Center()
    return {"volume": float(shape.Volume()), "bbox_size": [bb.xlen, bb.ylen, bb.zlen], "bbox_min": [bb.xmin, bb.ymin, bb.zmin], "bbox_max": [bb.xmax, bb.ymax, bb.zmax],
            "com": [c.x, c.y, c.z], "faces": len(shape.Faces()), "edges": len(shape.Edges()), "vertices": len(shape.Vertices()),
            "solids": len(shape.Solids()), "valid": bool(shape.isValid())}


def run(code):
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
        return {"ok": False, "error": "%s: %s%s" % (type(e).__name__, str(e)[:400], where), "output": out.getvalue()[-2000:]}
    part = pick_part(before)
    if part is not None:
        STATE["part"] = part
    return {"ok": True, "output": out.getvalue()[-2000:], "changed": part is not None}


def handle(req):
    op = req.get("op")
    if op == "exec":
        r = run(req["code"])
    elif op == "reset":
        STATE.update(fresh()); r = {"ok": True}
    elif op == "export":
        sh = as_shape(STATE["part"]) if STATE["part"] is not None else None
        if sh is None:
            return {"ok": False, "error": "no part to export"}
        cq.exporters.export(cq.Workplane(obj=sh), req["path"], "STEP")
        return {"ok": True}
    elif op == "ping":
        return {"ok": True}
    else:
        return {"ok": False, "error": "unknown op"}
    sh = as_shape(STATE["part"]) if STATE["part"] is not None else None
    r["measure"] = measure(sh) if sh is not None else None
    return r


for line in sys.stdin:
    try:
        rep = handle(json.loads(line))
    except BaseException as e:                       # noqa: BLE001
        rep = {"ok": False, "error": "worker error: %s: %s" % (type(e).__name__, str(e)[:300])}
    _proto.write(json.dumps(rep) + "\n")
