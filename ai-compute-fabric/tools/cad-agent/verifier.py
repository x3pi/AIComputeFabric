"""Verification layer of the CAD agent (replaces the old "advisor guesses the final number, check once at the end" design).

Ideas taken from the literature (closed loop with numeric kernel measurements, step-level checks) and from our own measurements:
  * The advisor does NOT do arithmetic. It writes each expected volume as a FORMULA; this module evaluates it by code (`safe_eval`).
  * The plan is a list of steps with the volume of the whole part after each step (a "milestone"). After every operation that can change the
    solid, the harness measures the real part (read-only) and compares it with the next milestone (`Tracker.observe`). A wrong step is
    reported at the step where it happens, with measured numbers, not after the agent has said "done".
  * At the end `final_verdict` compares the real part with the last milestone and the planned bounding box.
  * `sketch_outside` is a geometry preflight: a sketch entirely outside the part can never cut it.
Everything here is pure Python standard library with no SolidWorks access, so it is unit-testable offline (test_verifier.py)."""
import ast, math, re

TOL = 1e-3            # relative volume tolerance for exact formulas (hand-written formulas for curved geometry are ~1e-4 off)
TOL_APPROX = 2e-3     # when the advisor marks a formula as a simplification. Must stay BELOW real misses seen on the cup (0.36% and 0.40%) and above the
                      # straight-cylinder simplification of a hole through a curved wall (0.011%)
BBOX_ABS_MM = 0.05
FEATURE = re.compile(r"cut|extrude|revolve|hole|shell|fillet|chamfer|boss|sweep|loft|pattern|mirror|disc|box|cylinder|sphere|cone|torus|prism|wedge|pocket|boolean|combine|union|subtract", re.I)

_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "atan": math.atan, "radians": math.radians,
          "degrees": math.degrees, "abs": abs, "min": min, "max": max}
_CONST = {"pi": math.pi, "e": math.e}


def safe_eval(expr):
    """Evaluate an arithmetic formula. Allows numbers, + - * / ** %, parentheses, pi, e and the functions above; nothing else. -> float. Raises ValueError."""
    if not isinstance(expr, str) or not expr.strip() or len(expr) > 300:
        raise ValueError("empty or too long")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        raise ValueError("syntax error")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return float(n.value)
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add): return a + b
            if isinstance(n.op, ast.Sub): return a - b
            if isinstance(n.op, ast.Mult): return a * b
            if isinstance(n.op, ast.Div): return a / b
            if isinstance(n.op, ast.Mod): return a % b
            if abs(b) > 12 or abs(a) > 1e6:
                raise ValueError("exponent too large")
            return a ** b
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords and 1 <= len(n.args) <= 4:
            return float(_FUNCS[n.func.id](*[ev(x) for x in n.args]))
        raise ValueError("not allowed: %s" % type(n).__name__)
    try:
        v = ev(tree)
    except (ZeroDivisionError, OverflowError, TypeError):
        raise ValueError("cannot evaluate")
    if not math.isfinite(v):
        raise ValueError("not finite")
    return v


def parse_spec(plan):
    """plan = advisor JSON {"steps":[{"do","volume_after","approx"}], "final_bbox_size":[f,f,f]|null, "pitfalls":[..]}.
    -> {"steps":[{"do","vol","approx","err"}], "milestones":[{"step","vol","approx"}], "bbox":[..]|None, "pitfalls":[..], "errors":[..]}"""
    spec = {"steps": [], "milestones": [], "bbox": None, "pitfalls": [], "errors": []}
    if not isinstance(plan, dict):
        return spec
    for i, s in enumerate(plan.get("steps") or []):
        if isinstance(s, str):
            s = {"do": s}
        if not isinstance(s, dict):
            continue
        st = {"do": str(s.get("do") or "")[:300], "vol": None, "approx": bool(s.get("approx")), "err": None}
        f = s.get("volume_after")
        if f not in (None, "", "null"):
            try:
                v = f if isinstance(f, (int, float)) and not isinstance(f, bool) else safe_eval(str(f))
                if not (v >= 0):
                    raise ValueError("negative")
                st["vol"] = float(v)
                spec["milestones"].append({"step": len(spec["steps"]) + 1, "vol": float(v), "approx": st["approx"]})
            except ValueError as e:
                st["err"] = str(e)
                spec["errors"].append("step %d volume formula rejected (%s)" % (len(spec["steps"]) + 1, e))
        spec["steps"].append(st)
    fb = plan.get("final_bbox_size")
    if isinstance(fb, list) and len(fb) == 3:
        try:
            spec["bbox"] = [float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else safe_eval(str(x)) for x in fb]
        except ValueError as e:
            spec["errors"].append("bbox formula rejected (%s)" % e)
    spec["pitfalls"] = [str(x)[:300] for x in (plan.get("pitfalls") or []) if x][:8]
    return spec


def describe(spec):
    """Text shown to the executor: the steps with the numbers the HARNESS computed."""
    lines = []
    for i, s in enumerate(spec["steps"], 1):
        extra = "   [expected volume after this step: %.1f mm3%s]" % (s["vol"], ", approximate" if s["approx"] else "") if s["vol"] is not None else ""
        lines.append("%d. %s%s" % (i, s["do"], extra))
    out = "Steps:\n" + "\n".join(lines)
    if spec["pitfalls"]:
        out += "\nPitfalls:\n" + "\n".join("- " + p for p in spec["pitfalls"])
    if spec["bbox"]:
        out += "\nExpected final bounding-box size: %s mm" % [round(x, 3) for x in spec["bbox"]]
    if spec["errors"]:
        out += "\n(not checked by the harness: %s)" % "; ".join(spec["errors"])
    return out


def _near(v, m):
    return abs(v - m["vol"]) <= (TOL_APPROX if m["approx"] else TOL) * max(m["vol"], 1e-9)


class Tracker:
    """Follows the real part's volume through the plan's milestones."""

    def __init__(self, spec):
        self.ms = spec["milestones"]
        self.reset()

    def reset(self):
        self.prev, self.k, self.checks = 0.0, 0, 0

    def observe(self, tool, vol):
        """Call after every operation that may change the solid, with the measured volume. -> (level, text) or None. level: ok | warn."""
        if vol is None or not self.ms:
            return None
        self.checks += 1
        changed = abs(vol - self.prev) > 1e-6 * max(1.0, abs(self.prev))
        if not changed:
            if FEATURE.search(tool) and self.k < len(self.ms) and not _near(vol, self.ms[self.k]):
                m = self.ms[self.k]
                return ("warn", "HARNESS CHECK: `%s` did not change the volume (still %.1f mm3). The plan expects %.1f mm3 at step %d, so material still has to be "
                        "%s. If this operation was meant to add or remove material, it did nothing: read the feature list and the error text, "
                        "then fix the cause instead of continuing." % (tool, vol, m["vol"], m["step"], "removed" if m["vol"] < vol else "added"))
            return None
        old, self.prev = self.prev, vol
        for j in range(self.k, len(self.ms)):
            if _near(vol, self.ms[j]):
                self.k = j + 1
                return ("ok", "HARNESS CHECK: volume %.1f mm3 matches the plan at step %d." % (vol, self.ms[j]["step"]))
        if self.k >= len(self.ms):
            return ("warn", "HARNESS CHECK: the plan was already complete (%.1f mm3) but the volume is now %.1f mm3. Check that this change was intended." %
                    (self.ms[-1]["vol"], vol))
        m = self.ms[self.k]
        d_meas, d_exp = vol - old, m["vol"] - old
        hint = ("Less material than planned remains: the operation removed too much (too large, too deep or too long, or it cut a part that should stay)."
                if vol < m["vol"] else "More material than planned remains: the operation removed too little or added too much (a cut that missed, or only partly cut).")
        return ("warn", "HARNESS CHECK: after `%s` the volume is %.1f mm3 (changed by %+.1f mm3), but the plan's next milestone (step %d) is %.1f mm3 "
                "(expected change %+.1f mm3). Difference %+.1f mm3 (%+.2f%%). %s Compare with the feature list before going on."
                % (tool, vol, d_meas, m["step"], m["vol"], d_exp, vol - m["vol"], 100 * (vol - m["vol"]) / max(m["vol"], 1e-9), hint))

    def final_verdict(self, vol, bbox, spec):
        """-> list of mismatch strings (empty = the real part agrees with the plan)."""
        bad = []
        if vol is not None and self.ms:
            m = self.ms[-1]
            if not _near(vol, m):
                bad.append("volume %.1f mm3 measured vs %.1f expected by the plan (%+.2f%%); %d of %d planned volume milestones were reached"
                           % (vol, m["vol"], 100 * (vol - m["vol"]) / max(m["vol"], 1e-9), self.k, len(self.ms)))
        eb = spec.get("bbox")
        if eb and bbox and len(bbox) == 3 and any(abs(a - b) > max(BBOX_ABS_MM, 1e-3 * b) for a, b in zip(sorted(bbox), sorted(eb))):
            bad.append("bounding box %s measured vs %s expected" % ([round(x, 2) for x in sorted(bbox)], [round(x, 2) for x in sorted(eb)]))
        return bad


def sketch_outside(d, bb):
    """Preflight. d = add_sketch reply (dict), bb = (min, max) of the part. -> message or None when the sketch profile lies entirely outside the part."""
    try:
        o, xa, ya, pts = d["origin_mm"], d["x_axis"], d["y_axis"], d["points_mm"]
        world = [[o[k] + p[0] * xa[k] + p[1] * ya[k] for k in range(3)] for p in pts]
        lo, hi = [min(w[k] for w in world) for k in range(3)], [max(w[k] for w in world) for k in range(3)]
    except (KeyError, TypeError, IndexError, ValueError):
        return None
    gap = [max(bb[0][k] - hi[k], lo[k] - bb[1][k], 0.0) for k in range(3)]
    if max(gap) <= 0.01:
        return None
    k = max(range(3), key=lambda i: gap[i])
    return ("HARNESS CHECK: this sketch lies entirely OUTSIDE the part: along %s its profile spans %.1f..%.1f mm but the part spans %.1f..%.1f mm "
            "(gap %.1f mm). A cut or extrusion from it cannot touch the part. Move the sketch plane/profile so it passes through the part "
            "(compare with the part's bounding box and the plane origin/normal) instead of retrying the same operation." %
            ("XYZ"[k], lo[k], hi[k], bb[0][k], bb[1][k], gap[k]))


PLAN_PROMPT = (
    "You are planning a CAD job that a smaller model will carry out through the tools listed below. Reply with ONE JSON object and nothing else:\n"
    '{"steps": [{"do": "short imperative step naming the tool, the axis and the plane", "volume_after": "<formula or null>", "approx": false}], '
    '"final_bbox_size": ["<formula>", "<formula>", "<formula>"], "pitfalls": ["what commonly goes wrong with this request and how to check it"]}\n'
    "Rules:\n"
    "- Do NOT do arithmetic yourself. `volume_after` is a FORMULA (a string) that the harness evaluates by code: the volume in mm3 of the WHOLE part after that step. "
    "Allowed: numbers, + - * / ** %, parentheses, pi, e, sqrt, sin, cos, tan, atan, radians, abs, min, max. Example: \"pi*20**2*50 - pi*17**2*47\".\n"
    "- Use null for steps that leave the solid unchanged (planes, sketches, views). Put a formula on every step that adds or removes material.\n"
    "- Set \"approx\": true when your formula simplifies curved geometry (for example a hole through a curved wall written as a straight cylinder); "
    "the harness then accepts a larger error. Otherwise the formula must be exact.\n"
    "- final_bbox_size: formulas for the overall bounding-box size of the finished part, or null when the request does not determine it.\n"
    "- Prefer the simplest tool that does the job. The executor does not know the tools' geometry conventions, so say which axis or plane each step uses. "
    "Do not invent requirements.")
