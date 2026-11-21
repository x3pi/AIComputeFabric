"""Backend-agnostic CAD agent loop: plan (formulas evaluated by code) -> execute code -> measure after EVERY cell -> step check against the plan -> final gate.
The executor model never sees the plan's arithmetic as its own: the harness evaluates the advisor's formulas and compares them with the kernel's measurements."""
import json, os, time
from . import verifier as V
from . import planners

MAX_STEPS = int(os.environ.get("CADFABRIC_MAX_STEPS", "14"))
GATE_MAX = 2

SYSTEM = """You build CAD parts by writing CadQuery (Python) code that runs in a persistent session. Units are millimetres.
Rules:
- Call `execute_cadquery(code)`. `cq`, `math`, `Workplane`, `Vector` and `Location` are already imported (write `cq.Workplane(...)` or `Workplane(...)`); variables persist between calls. Assign the finished solid to `result`.
- After every call the harness measures the real part (volume, bounding box, centre of mass, face/edge/vertex counts, validity). Those numbers are facts; never compute a volume by hand, read it from the measurement.
- Build the part step by step (one feature per call) so a mistake shows up right where it happens. `undo()` removes the last successful call.
- Conventions: Workplane("XY") sketches in the XY plane and extrudes along +Z; Workplane("XZ") extrudes along -Y; Workplane("YZ") along +X. `.revolve(360, (0,0,0), (0,1,0))` revolves about the workplane's local Y axis.
  `.faces(">Z").workplane()` starts a sketch on the top face; `.hole(d)`, `.cboreHole`, `.shell(-t)` after selecting the face to open; boolean with `.cut(other)`, `.union(other)`.
- To drill through a curved wall, cut a cylinder placed where you want the hole and measure: do not assume the removed volume is a straight cylinder.
- If a call returns an error or a HARNESS CHECK line, find the cause before continuing; do not repeat the same code with small changes.
- Notes marked "HARNESS CHECK" come from the program, not from you. If you are certain a planned number is wrong, say so with the arithmetic instead of bending the part to it.
- When the part is finished, reply briefly in the user's language: what you built, the dimensions, and the measured volume."""

TOOLS = [
    {"type": "function", "function": {"name": "execute_cadquery", "description": "Run CadQuery code in the persistent session; returns printed output, errors and the measurement of the part.",
                                      "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "undo", "description": "Undo the last successful execute_cadquery call.", "parameters": {"type": "object", "properties": {}}}},
]


def run(llm, backend, request, plan_mode="nothink", planner=None, log=print, cfg=None, history=None, should_stop=None, on_event=None):
    """-> dict: ok, final text, final measure, steps, checks, warnings, plan. plan_mode: off | nothink | think | codex | codex-relay. `planner(request)->(spec, raw)` overrides."""
    t0 = time.time()
    ev = on_event or (lambda kind, **kw: None)         # structured events for a UI: plan, cell, gate
    follow_up = history is not None                  # a follow-up edit keeps the part and the conversation; there is no plan for it
    if not follow_up:
        backend.reset()
    spec = tracker = None
    raw_plan = ""
    if plan_mode != "off" and not follow_up:
        spec, raw_plan = (planner or (lambda r: planners.make_plan(llm, r, plan_mode, cfg)))(request)
        if spec:
            tracker = V.Tracker(spec)
    if plan_mode != "off" and not follow_up:
        pj = planners.extract_json(raw_plan) or {}
        ev("plan", mode=plan_mode, ok=bool(spec), describe=V.describe(spec) if spec else "", milestones=len(spec["milestones"]) if spec else 0,
           pitfalls=[str(x)[:300] for x in (pj.get("pitfalls") or [])][:6], note=raw_plan[:raw_plan.index(")") + 1] if raw_plan.startswith("(") and ")" in raw_plan else "")
    log("plan (%s): %s" % (plan_mode, "none" if not spec else "%d volume milestones" % len(spec["milestones"])) if plan_mode != "off" else "plan: off")
    msgs = history if follow_up else [{"role": "system", "content": SYSTEM}]
    user = request
    if spec:
        user += ("\n\nADVISOR PLAN (advice only; verify each step on the real part). The numbers were computed by the harness from the advisor's formulas. "
                 "After each cell that changes the solid the harness reports `HARNESS CHECK` lines.\n" + V.describe(spec))
    msgs.append({"role": "user", "content": user})
    trace, warns, gates, tokens, final = [], 0, 0, 0, ""
    stopped = False
    for step in range(MAX_STEPS):
        if should_stop and should_stop():
            stopped = True
            break
        m = llm.chat(msgs, tools=TOOLS, think=False)
        tokens += (m.pop("_usage", {}) or {}).get("completion_tokens", 0)
        msgs.append(m)
        calls = m.get("tool_calls") or []
        if not calls:
            final = m["content"]
            bad = tracker.final_verdict(*_vb(backend), spec) if tracker else []
            if bad and gates < GATE_MAX:
                gates += 1
                note = "HARNESS FINAL CHECK failed: " + "; ".join(bad) + ". Fix the part (use undo() if needed) or explain with arithmetic why the plan is wrong."
                msgs.append({"role": "user", "content": note}); trace.append({"gate": note}); log("  gate: " + note[:160]); ev("gate", text=note[:400])
                continue
            break
        for c in calls:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            name = c["function"]["name"]
            if name == "execute_cadquery":
                res = backend.execute(str(args.get("code", "")))
                text = ("OK" if res.ok else "ERROR: " + res.error) + ("\nprinted: " + res.output.strip() if res.output.strip() else "")
                if res.measure:
                    text += "\nMEASURE: " + res.measure.text()
                if res.ok and res.measure and tracker:
                    obs = tracker.observe("execute", res.measure.volume, feature=bool(V.FEATURE.search(str(args.get("code", "")))))
                    if obs:
                        text += "\n" + obs[1]; warns += obs[0] == "warn"
                trace.append({"tool": name, "ok": res.ok, "vol": res.measure.volume if res.measure else None, "err": res.error[:200]})
                log("  step %d: %s%s" % (len(trace), "ok" if res.ok else "ERROR " + res.error[:100], "  volume %.2f mm3" % res.measure.volume if res.measure else ""))
                chk = text[text.index("HARNESS CHECK"):][:300].replace("\n", " ") if "HARNESS CHECK" in text else ""
                if chk:
                    log("    " + chk[:200])
                ev("cell", n=len(trace), code=str(args.get("code", ""))[:4000], ok=res.ok, error=res.error[:400], output=res.output.strip()[:300],
                   volume=res.measure.volume if res.measure else None, check=chk)
            elif name == "undo":
                ok = backend.undo(); mm = backend.measure()
                text = ("undone" if ok else "nothing to undo") + ("\nMEASURE: " + mm.text() if mm else "")
                if ok and tracker:
                    tracker.reset(); [tracker.observe("replay", h) for h in []]      # volume history is not replayed; the tracker restarts from the next measurement
                trace.append({"tool": name, "ok": ok})
            else:
                text = "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": text[:4000]})
    mm = backend.measure()
    return {"final": final, "measure": mm.as_dict() if mm else None, "steps": len([t for t in trace if t.get("tool")]), "warnings": warns, "gates": gates,
            "checks": tracker.checks if tracker else 0, "plan": spec["milestones"] if spec else None, "plan_raw": raw_plan[:1500], "trace": trace, "tokens": tokens,
            "sec": round(time.time() - t0, 1), "ended_by_limit": not final and not stopped, "stopped": stopped, "history": msgs}


def _vb(backend):
    m = backend.measure()
    return (m.volume, m.bbox_size) if m else (None, None)
