"""Worker process for the task-bus tests. usage: _bus_worker.py URL NAME DELAY LOGFILE [fail=TASKID]"""
import os
import sys
import time

from agents.orchestrator import WorkerLost
from agents.task_bus import BusWorker
from agents.tasks import TaskResult

url, name, delay, log = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
fail = {a.split("=", 1)[1] for a in sys.argv[5:] if a.startswith("fail=")}
lose = {a.split("=", 1)[1] for a in sys.argv[5:] if a.startswith("lose=")}   # GPU reclaimed on this machine


class Ex:
    def execute(self, task, attempt, deps):
        with open(log, "a") as f:
            f.write(f"start {name} {task.task_id}\n")
        time.sleep(delay)
        if task.task_id in lose:
            raise WorkerLost(task.task_id)
        if task.task_id in fail:
            raise ValueError("bad input for " + task.task_id)
        with open(log, "a") as f:
            f.write(f"done {name} {task.task_id}\n")
        return TaskResult(task_id=task.task_id, type=task.type, output={"text": f"{task.task_id} by {name}"}, worker_id=name)


BusWorker(url, Ex(), name=name, ack_wait=float(os.environ.get("ACK_WAIT", "3"))).run()
