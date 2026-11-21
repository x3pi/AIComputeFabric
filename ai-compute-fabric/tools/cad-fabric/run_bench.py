#!/usr/bin/env python3
"""python run_bench.py --modes off,think --reps 3 [--tasks cup_side_hole,step_shaft] [--out results.jsonl]
Pass = the KERNEL's volume of the final part is within the task tolerance of the kernel's volume of the reference solution (the model's own report is not trusted)."""
import argparse, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from cq_backend import CadQueryBackend
from llm import LLM
import agent, tasks

ap = argparse.ArgumentParser()
ap.add_argument("--modes", default="off,think"); ap.add_argument("--reps", type=int, default=3); ap.add_argument("--tasks", default=",".join(tasks.TASKS))
ap.add_argument("--out", default=os.path.join(HERE, "results", "bench-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
a = ap.parse_args()
os.makedirs(os.path.dirname(a.out), exist_ok=True)
llm, be = LLM(), CadQueryBackend()
exp = {t: tasks.exact(be, t) for t in a.tasks.split(",")}
print("exact (kernel):", {k: round(v, 4) for k, v in exp.items()}, flush=True)
for t in a.tasks.split(","):
    for mode in a.modes.split(","):
        for rep in range(a.reps):
            try:
                r = agent.run(llm, be, tasks.TASKS[t]["text"], plan_mode=mode, log=lambda s: None)
                vol = (r["measure"] or {}).get("volume"); err = ""
            except Exception as e:                                  # noqa: BLE001 - a failed run is a data point, not a crash of the benchmark
                r, vol, err = {"steps": 0, "sec": 0, "warnings": 0, "gates": 0, "trace": []}, None, "%s: %s" % (type(e).__name__, str(e)[:200])
            row = {"task": t, "mode": mode, "rep": rep, "pass": tasks.passed(vol, exp[t], tasks.TASKS[t]["tol"]), "vol": vol, "expected": exp[t], "steps": r["steps"],
                   "warnings": r["warnings"], "gates": r["gates"], "sec": r["sec"], "tokens": r.get("tokens"), "limit": r.get("ended_by_limit"), "error": err,
                   "final": (r.get("final") or "")[-300:], "plan_raw": r.get("plan_raw", ""), "trace": r["trace"]}
            open(a.out, "a", encoding="utf-8").write(json.dumps(row, ensure_ascii=False) + "\n")
            print("%-16s %-6s #%d %s vol=%s exp=%.4f steps=%d warn=%d gates=%d %ss %s" % (t, mode, rep, "PASS" if row["pass"] else "FAIL", None if vol is None else round(vol, 4),
                  exp[t], r["steps"], r["warnings"], r["gates"], r["sec"], err), flush=True)
be.close()
