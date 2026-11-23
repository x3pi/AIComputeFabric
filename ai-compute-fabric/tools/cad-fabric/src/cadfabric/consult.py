"""Ask the selected advisor for help in the MIDDLE of a run, when the local model is stuck or its work keeps disagreeing with the plan.
The advisor sees what a human colleague would: the request, the plan, the last few cells with their results and the current measurement. It answers in plain text
with a diagnosis and a concrete next step; that text is shown to the local model as advice. The advisor never touches the part and has no tools.
Only used when the user picked a CLI/API advisor (or Codex) as the planner, so no new service is contacted, and at most MAX_PER_RUN times per run."""
from . import advisors, planners

MAX_PER_RUN = 2
FAILS_TO_ASK = 3                                 # failed cells (not necessarily in a row) before the advisor is asked
COOLDOWN = 3                                     # steps between two consultations
REPLY_MAX = 2500

ASK = ("A smaller local model is building a CAD part with CadQuery and is in trouble. Below are the request, the plan, its last cells with results and the current measurement.\n"
       "Find the ROOT CAUSE: is the request misread (a number given the wrong role, a feature never built, the wrong body), is a cut or feature on the wrong axis or side, "
       "or is the plan itself wrong? Check the request against the measurements yourself.\n"
       "Reply in plain text, at most 12 lines: first `DIAGNOSIS:` (what is wrong and why), then `NEXT:` (the concrete next steps, with the CadQuery calls to use and the volume the part "
       "should have afterwards, computed from the request). Do not rewrite everything if only one step is wrong.")


ASK_FINAL = ("A smaller local model is building a CAD part with CadQuery and says it is DONE. Below are the request, the plan, its last cells with results and the final measurement.\n"
             "Act as an independent reviewer. Check EVERY requirement of the request against the measurements (sizes, hollow or solid, number and place of holes and cuts, volume). "
             "Compute the volume the part should have from the request yourself and compare it with the measurement.\n"
             "Reply in plain text, at most 10 lines. First line exactly `VERDICT: OK` if every requirement is met, otherwise `VERDICT: PROBLEMS`. "
             "Then `DIAGNOSIS:` (which requirement is not met and the evidence) and `NEXT:` (the concrete fix, the CadQuery calls to use, and the volume expected afterwards).")


def verdict_ok(text):
    return str(text or "").lstrip().upper().startswith("VERDICT: OK")


def _cell(t, i):
    code = str(t.get("code") or "")[:700]
    return "--- cell %d: %s%s\n%s" % (i, "ok" if t.get("ok") else "ERROR", (" volume %.2f" % t["vol"]) if t.get("vol") is not None else "", code + (("\nerror: " + str(t.get("err"))[:300]) if t.get("err") else ""))


def digest(request, plan_text, trace, measure_text, reason, final=False, earlier=()):
    cells = [t for t in trace if t.get("tool")][-4:]
    n0 = len([t for t in trace if t.get("tool")]) - len(cells) + 1
    prior = ("\n\nEARLIER ADVICE YOU ALREADY GAVE IN THIS RUN (do not repeat it; if the same problem persists, say what is different now and be more specific):\n" + "\n---\n".join(x[:600] for x in earlier[-2:])) if earlier else ""
    return "%s\n\nREQUEST:\n%s\n\nPLAN:\n%s\n\nWHY HELP WAS REQUESTED: %s\n\nLAST CELLS:\n%s\n\nCURRENT MEASUREMENT: %s%s" % (
        ASK_FINAL if final else ASK, request[:3000], (plan_text or "(no plan)")[:1500], reason, "\n".join(_cell(t, n0 + i) for i, t in enumerate(cells)) or "(none yet)", measure_text or "(no part yet)", prior)


def ask(cfg, mode, prompt):
    """-> (text, error). Same routing as the planner: a configured advisor, or Codex."""
    adv = advisors.find(cfg, mode)
    if adv:
        return advisors.ask(adv, prompt, cfg)
    if mode == "codex":
        return planners.ask_codex(prompt, cfg)
    if mode == "codex-relay":
        return planners.ask_relay(prompt, cfg)
    return "", "no advisor selected"


def possible(cfg, mode):
    return bool(mode) and mode not in ("off", "nothink", "think") and (advisors.find(cfg, mode) is not None or mode in ("codex", "codex-relay"))
