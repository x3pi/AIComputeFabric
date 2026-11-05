"""BusDispatcher: when the core-NATS warm path may fall back to JetStream and when it must not (at-most-once)."""
import json

import nats.errors
import pytest

from agents.orchestrator import WorkerLost
from agents.task_bus import RemoteError, TaskExpiredError
from agents.tasks import Task, TaskResult, TaskType
from serverless.dispatch import MAX_DIRECT_BYTES, BusDispatcher


class Msg:
    def __init__(self, d):
        self.data = json.dumps(d).encode()


class FakeBx:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.requests, self.executed = reply, exc, [], []
        self.nc = self

    def request(self, subject, data, timeout):
        self.requests.append((subject, json.loads(data), timeout))
        if self.exc:
            raise self.exc
        return Msg(self.reply)

    def _run(self, x, timeout=None):
        return x

    def execute(self, task, attempt=1):
        self.executed.append((task.task_id, attempt))
        return TaskResult(task_id=task.task_id, type=task.type, output={"status": "ok", "via": "jetstream"}, worker_id="js")


def task(label="w-wk1", inp=None):
    return Task(task_id="t" * 12, tenant_id="a", type=TaskType.SERVERLESS_EXEC, input=inp or {"x": 1}, label=label, ttl_s=30)


OK = {"ok": True, "result": {"task_id": "t" * 12, "type": "SERVERLESS_EXEC", "output": {"status": "ok", "via": "direct"}, "worker_id": "wk1"}}


def test_pinned_call_goes_direct_to_the_worker_subject():
    bx = FakeBx(OK)
    d = BusDispatcher(bx)
    r = d(task(), 3)
    assert r.output["via"] == "direct" and bx.requests[0][0] == "aicf.sld.wk1" and bx.requests[0][1]["attempt"] == 3 and not bx.executed
    assert d.direct_calls == 1


def test_unpinned_or_gpu_label_uses_jetstream():
    for label in (None, "gpu-sl"):
        bx = FakeBx(OK)
        BusDispatcher(bx)(task(label), 1)
        assert not bx.requests and bx.executed


def test_no_responders_falls_back_because_nothing_ran():
    bx = FakeBx(exc=nats.errors.NoRespondersError())
    d = BusDispatcher(bx)
    assert d(task(), 1).output["via"] == "jetstream" and d.direct_fallbacks == 1


def test_busy_falls_back_because_nothing_ran():
    bx = FakeBx({"ok": False, "error": "busy"})
    assert BusDispatcher(bx)(task(), 1).output["via"] == "jetstream"


def test_timeout_is_worker_lost_and_never_falls_back():
    bx = FakeBx(exc=nats.errors.TimeoutError())
    with pytest.raises(WorkerLost):
        BusDispatcher(bx)(task(), 1)
    assert not bx.executed


@pytest.mark.parametrize("err,exc", [("WorkerLost: x", WorkerLost), ("TaskExpiredError: late", TaskExpiredError), ("ValueError: bad", RemoteError)])
def test_error_replies_are_mapped(err, exc):
    bx = FakeBx({"ok": False, "error": err})
    with pytest.raises(exc):
        BusDispatcher(bx)(task(), 1)
    assert not bx.executed


def test_oversized_payload_skips_the_direct_path():
    bx = FakeBx(OK)
    BusDispatcher(bx)(task(inp={"blob": "x" * MAX_DIRECT_BYTES}), 1)
    assert not bx.requests and bx.executed
