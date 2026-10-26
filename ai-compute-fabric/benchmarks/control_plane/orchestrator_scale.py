#!/usr/bin/env python3
"""Orchestrator overhead with SIMULATED workers (plan P5, NOT the real B08).

B08 needs 8/20/40 real PCs. This measures only what this machine can: how much throughput the
orchestrator (LangGraph + PostgreSQL) itself loses when it feeds N workers whose task takes a fixed
time. throughput_efficiency = measured tasks/s / (N / task_time) - the ideal for N perfectly busy workers."""
import argparse, json, sys, time, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents.orchestrator import Orchestrator, Step  # noqa: E402
from agents.tasks import Task, TaskResult, TaskType  # noqa: E402


class Sim:
    def __init__(self, t): self.t = t
    def execute(self, task, attempt, deps):
        time.sleep(self.t)
        return TaskResult(task_id=task.task_id, type=task.type, output={"text": task.task_id}, worker_id="sim")


def steps(n):
    return [Step(f"d{i}", Task(task_id=f"d{i}", tenant_id="acme", type=TaskType.TEXT_GENERATE,
                               input={"messages": [{"role": "user", "content": str(i)}]})) for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--tasks", type=int, default=1000); ap.add_argument("--task-ms", type=int, default=200)
    ap.add_argument("--nodes", default="8,20,40")
    a = ap.parse_args()
    rows = []
    for n in map(int, a.nodes.split(",")):
        o = Orchestrator(a.dsn, Sim(a.task_ms / 1000), max_concurrency=n)
        t0 = time.time()
        res = o.run("scale-" + uuid.uuid4().hex[:8], steps(a.tasks))
        dt = time.time() - t0
        ideal = n / (a.task_ms / 1000)
        rows.append({"workers": n, "tasks": a.tasks, "seconds": round(dt, 2), "tasks_per_s": round(a.tasks / dt, 1),
                     "ideal_tasks_per_s": round(ideal, 1), "throughput_efficiency": round(a.tasks / dt / ideal, 3), "complete": len(res) == a.tasks})
        print(rows[-1], flush=True)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "kind": "SIMULATED workers - orchestrator overhead only, not B08", "task_ms": a.task_ms,
                                        "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
