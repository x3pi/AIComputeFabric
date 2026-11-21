"""Benchmark runner. Pass = the KERNEL's volume of the final part is within the task tolerance of the kernel's volume of the reference solution
(the model's own report is never trusted)."""
import json, os, time
from . import agent, tasks
from .cq_backend import CadQueryBackend


def run_bench(llm, modes, reps, task_ids, out, cfg=None, emit=print):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    be = CadQueryBackend()
    try:
        exp = {t: tasks.exact(be, t) for t in task_ids}
        emit("exact (kernel): %s" % {k: round(v, 4) for k, v in exp.items()})
        for t in task_ids:
            for mode in modes:
                for rep in range(reps):
                    try:
                        r = agent.run(llm, be, tasks.TASKS[t]["text"], plan_mode=mode, log=lambda s: None, cfg=cfg)
                        vol, err = (r["measure"] or {}).get("volume"), ""
                    except Exception as e:                       # noqa: BLE001 - a failed run is a data point, not a crash of the benchmark
                        r, vol, err = {"steps": 0, "sec": 0, "warnings": 0, "gates": 0, "trace": []}, None, "%s: %s" % (type(e).__name__, str(e)[:200])
                    row = {"task": t, "mode": mode, "rep": rep, "pass": tasks.passed(vol, exp[t], tasks.TASKS[t]["tol"]), "vol": vol, "expected": exp[t],
                           "steps": r["steps"], "warnings": r["warnings"], "gates": r["gates"], "sec": r["sec"], "tokens": r.get("tokens"), "limit": r.get("ended_by_limit"),
                           "error": err, "final": (r.get("final") or "")[-300:], "plan_raw": r.get("plan_raw", ""), "trace": r["trace"]}
                    with open(out, "a", encoding="utf-8") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    emit("%-18s %-11s #%d %s vol=%s exp=%.4f steps=%d warn=%d gates=%d %ss %s" % (t, mode, rep, "PASS" if row["pass"] else "FAIL",
                         None if vol is None else round(vol, 4), exp[t], r["steps"], r["warnings"], r["gates"], r["sec"], err))
    finally:
        be.close()
