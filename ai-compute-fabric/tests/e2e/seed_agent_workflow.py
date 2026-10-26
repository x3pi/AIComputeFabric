"""Runs a small agent DAG and holds one step until the hold file disappears, so a browser can look at
a workflow that is half done. usage: seed_agent_workflow.py DSN WORKFLOW HOLDFILE"""
import os
import sys
import time

from agents.orchestrator import Orchestrator, Step
from agents.tasks import Task, TaskResult, TaskType

dsn, wf, hold = sys.argv[1:4]


class Ex:
    def execute(self, task, attempt, deps):
        if task.task_id == "merge.hold":
            while os.path.exists(hold):
                time.sleep(0.1)
        time.sleep(0.05)
        return TaskResult(task_id=task.task_id, type=task.type, output={"text": "private text of " + task.task_id}, worker_id="w1")


def st(sid, deps=()):
    return Step(sid, Task(task_id=sid, tenant_id="acme", type=TaskType.TEXT_GENERATE, input={"messages": [{"role": "user", "content": "private " + sid}]}), tuple(deps))


steps = []
for d in range(4):
    steps += [st(f"doc{d}.parse"), st(f"doc{d}.clean", [f"doc{d}.parse"]), st(f"doc{d}.summarise", [f"doc{d}.clean"])]
steps += [st("merge.hold", [f"doc{d}.summarise" for d in range(4)]), st("report", ["merge.hold"])]
o = Orchestrator(dsn, Ex(), max_concurrency=3)
o.run(wf, steps)
o.close()
