"""Ask the selected advisor for help in the MIDDLE of a run, when the local model is stuck or its work keeps disagreeing with the plan.
The advisor sees everything the run has: the full request (with attached files), the full plan, EVERY cell with its complete code, output and error, all earlier advice and the current measurement. Nothing is cut except, past SAFETY_CHARS, the oldest cells. It answers in plain text
with a diagnosis and a concrete next step; that text is shown to the local model as advice. The advisor never touches the part and has no tools.
Only used when the user picked a CLI/API advisor (or Codex) as the planner, so no new service is contacted, and at most MAX_PER_RUN times per run."""
from . import advisors, planners

MAX_PER_RUN = 2
FAILS_TO_ASK = 3                                 # failed cells (not necessarily in a row) before the advisor is asked
COOLDOWN = 3                                     # steps between two consultations
REPLY_MAX = 12000
SAFETY_CHARS = 600000                            # only to stay inside a model context window; the oldest cells are dropped first

ASK = ("A smaller local model is building a CAD part with CadQuery and is in trouble. Below are the full request, the plan, ALL its cells with complete code, outputs and errors, and the current measurement.\n"
       "Find the ROOT CAUSE: is the request misread (a number given the wrong role, a feature never built, the wrong body), is a cut or feature on the wrong axis or side, "
       "or is the plan itself wrong? Check the request against the measurements yourself.\n"
       "Reply in plain text, as long as the problem needs: first `DIAGNOSIS:` (what is wrong and why), then `NEXT:` (the concrete next steps, with the CadQuery calls to use and the volume the part "
       "should have afterwards, computed from the request). Do not rewrite everything if only one step is wrong.")


ASK_FINAL = ("A smaller local model is building a CAD part with CadQuery and says it is DONE. Below are the full request, the plan, ALL its cells with complete code, outputs and errors, and the final measurement.\n"
             "Act as an independent reviewer. Check EVERY requirement of the request against the measurements (sizes, hollow or solid, number and place of holes and cuts, volume). "
             "Compute the volume the part should have from the request yourself and compare it with the measurement.\n"
             "Reply in plain text. First line exactly `VERDICT: OK` if every requirement is met, otherwise `VERDICT: PROBLEMS`. "
             "Then `DIAGNOSIS:` (which requirement is not met and the evidence) and `NEXT:` (the concrete fix, the CadQuery calls to use, and the volume expected afterwards).")


def verdict_ok(text):
    return str(text or "").lstrip().upper().startswith("VERDICT: OK")


def _cell(t, i):
    code = str(t.get("code") or "")
    return "--- cell %d: %s%s\n%s" % (i, "ok" if t.get("ok") else "ERROR", (" volume %.2f" % t["vol"]) if t.get("vol") is not None else "", code + (("\nerror: " + str(t.get("err"))) if t.get("err") else "") + (("\noutput: " + str(t.get("out"))) if t.get("out") else ""))


def digest(request, plan_text, trace, measure_text, reason, final=False, earlier=()):
    cells = [t for t in trace if t.get("tool")]
    n0 = 1
    prior = ("\n\nEARLIER ADVICE YOU ALREADY GAVE IN THIS RUN (do not repeat it; if the same problem persists, say what is different now and be more specific):\n" + "\n---\n".join(earlier)) if earlier else ""
    blocks = [_cell(t, n0 + i) for i, t in enumerate(cells)]
    while len(blocks) > 1 and sum(len(b) for b in blocks) > SAFETY_CHARS:
        blocks.pop(0)                                    # context-window safety only
    return "%s\n\nREQUEST:\n%s\n\nPLAN:\n%s\n\nWHY HELP WAS REQUESTED: %s\n\nALL CELLS (oldest first):\n%s\n\nCURRENT MEASUREMENT: %s%s" % (
        ASK_FINAL if final else ASK, request, plan_text or "(no plan)", reason, "\n".join(blocks) or "(none yet)", measure_text or "(no part yet)", prior)


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
