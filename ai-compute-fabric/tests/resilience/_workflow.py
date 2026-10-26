"""Shared pieces of the checkpoint/resume tests: a 50-step document workflow and simulated workers."""
import hashlib
import json
import sys
import time

from agents.orchestrator import Orchestrator, Step, WorkerLost
from agents.tasks import Task, TaskResult, TaskType

STAGES = ["parse", "clean", "chunk", "summarise", "index"]
DOCS = 10          # 10 documents x 5 stages = 50 steps


def workflow() -> list:
    steps = []
    for d in range(DOCS):
        prev = ()
        for st in STAGES:
            sid = f"doc{d}.{st}"
            steps.append(Step(sid, Task(task_id=sid, tenant_id="acme", type=TaskType.TEXT_GENERATE,
                                        input={"messages": [{"role": "user", "content": f"{st} document {d}"}]}), prev))
            prev = (sid,)
    return steps


def expected(steps: list) -> dict:
    """Reference answer computed without any orchestrator."""
    out = {}
    for s in steps:
        out[s.id] = digest(s.id, {d: out[d] for d in s.deps})
    return out


def digest(step_id: str, dep_texts: dict) -> str:
    h = hashlib.sha256(step_id.encode())
    for k in sorted(dep_texts):
        h.update(k.encode() + dep_texts[k].encode())
    return h.hexdigest()


class SimWorker:
    """Deterministic stand-in for a GPU worker. `lose` = {(step, attempt)} that get reclaimed mid-task."""
    def __init__(self, delay=0.1, lose=(), corrupt=()):
        self.delay, self.lose, self.corrupt = delay, set(lose), set(corrupt)

    def execute(self, task, attempt, deps):
        time.sleep(self.delay)
        if (task.task_id, attempt) in self.lose:
            raise WorkerLost(task.task_id)
        text = digest(task.task_id, {k: v["output"]["text"] for k, v in deps.items()})
        out = {"text": text}
        if task.task_id in self.corrupt:
            out = {"text": text, "citations": [{"source_id": "ghost", "quote": "x"}], "unexpected": 1}
        return TaskResult(task_id=task.task_id, type=task.type, output=out, worker_id="sim")


if __name__ == "__main__":   # subprocess entry: python -m tests.resilience._workflow DSN WF run|resume DELAY OUT
    dsn, wf, mode, delay, out_path = sys.argv[1:6]
    o = Orchestrator(dsn, SimWorker(float(delay)), max_concurrency=3)
    res = o.run(wf, workflow(), resume=(mode == "resume"))
    json.dump({k: v["output"]["text"] for k, v in res.items()}, open(out_path, "w"))
