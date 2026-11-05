"""Dispatch of one signed SERVERLESS_EXEC task: warm hot path over core NATS, everything else through the durable JetStream queue.

* Pinned call (label `w-<worker>`): `aicf.sld.<worker>` request/reply. No polling, ~1 ms. If nobody listens (NoResponders) or the
  worker answers `busy`, NOTHING ran, so the call falls back to JetStream. A timeout is different: the function may have started, so
  it becomes WorkerLost (D8: never silently re-run).
* Anything else: BusExecutor (JetStream work queue, redelivery before start, backpressure)."""
from __future__ import annotations

import json

from agents.orchestrator import WorkerLost
from agents.task_bus import SERVERLESS_DIRECT, RemoteError, TaskExpiredError
from agents.tasks import Task, TaskResult

MAX_DIRECT_BYTES = 200 * 1024


class BusDispatcher:
    def __init__(self, bx):
        self.bx = bx
        self.direct_calls = self.direct_fallbacks = 0

    def __call__(self, task: Task, attempt: int) -> TaskResult:
        if task.label and task.label.startswith("w-"):
            res = self._direct(task, attempt)
            if res is not None:
                return res
            self.direct_fallbacks += 1
        return self.bx.execute(task, attempt=attempt)

    def _direct(self, task: Task, attempt: int):
        import nats.errors
        data = json.dumps({"task": task.model_dump(mode="json"), "attempt": attempt}).encode()
        if len(data) > MAX_DIRECT_BYTES:
            return None
        timeout = (task.ttl_s or 60.0) + 2.0
        subj = f"{SERVERLESS_DIRECT}.{task.label[2:]}"
        try:
            msg = self.bx._run(self.bx.nc.request(subj, data, timeout=timeout), timeout + 5)
        except nats.errors.NoRespondersError:
            return None
        except (nats.errors.TimeoutError, TimeoutError):
            raise WorkerLost(f"{task.task_id}: no reply from {task.label} in {timeout:.0f}s") from None
        reply = json.loads(msg.data)
        self.direct_calls += 1
        if reply.get("ok"):
            return TaskResult.model_validate(reply["result"])
        err = str(reply.get("error", "unknown"))
        if err == "busy":
            return None
        if err.startswith("WorkerLost"):
            raise WorkerLost(err)
        if err.startswith("TaskExpiredError"):
            raise TaskExpiredError(err)
        raise RemoteError(f"{task.task_id}: {err}")
