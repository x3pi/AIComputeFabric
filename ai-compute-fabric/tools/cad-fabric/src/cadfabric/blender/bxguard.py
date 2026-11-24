"""Runs inside Blender. Generic safety net for untrusted part code (written by a small model), with no list of 'bad' operators:
  - wrap(bmesh): the module handed to build() checks the size of the BMesh after EVERY bmesh.ops call, so any operator in any loop that makes the geometry
    explode is stopped at the iteration where it happens (budget in vertices, and a growth-factor limit), with an error that names the line;
  - explain(traceback): for an operator called with the wrong arguments, append the operator's real signature taken from Blender itself, so the fix
    does not depend on prompt rules written for one particular mistake.
Limits (environment): BX_PART_VERTS (default 150000), BX_GROWTH (default 8: one call may not multiply a mesh above 20000 vertices by more than this)."""
import os, re

MAX_VERTS = int(os.environ.get("BX_PART_VERTS", "150000"))
GROWTH = float(os.environ.get("BX_GROWTH", "8"))
FLOOR = 20000


class GeometryBudget(Exception):
    pass


class _Ops:
    def __init__(self, ops, types, seen):
        self._ops, self._types, self._seen = ops, types, seen

    def __getattr__(self, name):
        fn = getattr(self._ops, name)
        if not callable(fn):
            return fn

        def call(*a, **k):
            bms = [x for x in list(a) + list(k.values()) if isinstance(x, self._types.BMesh)]
            r = fn(*a, **k)
            for bm in bms[:1]:
                n, prev = len(bm.verts), self._seen.get(id(bm), 0)
                self._seen[id(bm)] = n
                if n > MAX_VERTS or (n > FLOOR and prev and n > GROWTH * prev):
                    raise GeometryBudget("bmesh.ops.%s made the mesh %d vertices (was %d; limit %d): the geometry is exploding - an operator applied to the WHOLE mesh again and again, "
                                         "or far too many segments. Apply it only to the geometry created in this step, or once at the end." % (name, n, prev, MAX_VERTS))
            return r
        return call


class _Bmesh:
    def __init__(self, bmesh):
        self._b = bmesh
        self.ops = _Ops(bmesh.ops, bmesh.types, {})

    def __getattr__(self, name):
        return getattr(self._b, name)


def wrap(bmesh):
    return _Bmesh(bmesh)


def explain(bmesh, tb):
    """Extra text for an error: the real signature of the bmesh operator named in the traceback when it was called with a bad argument."""
    try:
        m = re.search(r"bmesh\.ops\.(\w+)\(", tb) or re.search(r"\b(\w+): keyword", tb)
        if m and re.search(r"keyword|argument|TypeError|required", tb):
            doc = (getattr(bmesh.ops, m.group(1)).__doc__ or "").strip()
            if doc:
                return "\nREAL SIGNATURE (from Blender): " + " ".join(doc.split())[:500]
    except Exception:                                       # noqa: BLE001
        pass
    return ""
