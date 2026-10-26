"""Screen 4a back end: workflow status as stored by the orchestrator and served read-only.
Needs PostgreSQL (AICF_PG_DSN); skipped, not passed, without it."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import pytest

from agents.dag_api import make_handler
from agents.orchestrator import Orchestrator, ResultRejected, Step, WorkerLost
from agents.tasks import Task, TaskResult, TaskType

DSN = os.environ.get("AICF_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set AICF_PG_DSN to a PostgreSQL URL (NOT passed without it)")
SECRET = "SECRET-DOCUMENT-TEXT-7731"


def step(sid, deps=()):
    return Step(sid, Task(task_id=sid, tenant_id="acme", type=TaskType.TEXT_GENERATE,
                          input={"messages": [{"role": "user", "content": SECRET}]}), tuple(deps))


def ok(task):
    return TaskResult(task_id=task.task_id, type=task.type, output={"text": "answer " + SECRET}, worker_id="w1")


class Gate:
    """Executes instantly except for the step in `hold`, which waits until released."""
    def __init__(self, hold=None, lose=(), bad=()):
        self.hold, self.lose, self.bad = hold, set(lose), set(bad)
        self.entered, self.release = threading.Event(), threading.Event()

    def execute(self, task, attempt, deps):
        if task.task_id == self.hold:
            self.entered.set()
            assert self.release.wait(20)
        if task.task_id in self.lose:
            raise WorkerLost(task.task_id)
        if task.task_id in self.bad:
            return TaskResult(task_id=task.task_id, type=task.type, output={"text": ""}, worker_id="w1")
        return ok(task)


_made = []


def wf_id():
    _made.append("dag-" + uuid.uuid4().hex[:8])
    return _made[-1]


@pytest.fixture(autouse=True, scope="module")
def _cleanup():
    yield
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as c:      # leave the shared database as found
        for t in ("aicf_dag", "aicf_results", "aicf_exec"):
            c.execute(f"DELETE FROM {t} WHERE workflow = ANY(%s)", (_made,))


def test_live_status_pending_running_ok_then_done():
    g = Gate(hold="b")
    o = Orchestrator(DSN, g, max_concurrency=2)
    wf = wf_id()
    steps = [step("a"), step("b", ["a"]), step("c", ["b"]), step("d")]
    t = threading.Thread(target=lambda: o.run(wf, steps))
    t.start()
    assert g.entered.wait(20)
    snap = o.snapshot(wf)
    st = {n["id"]: n["status"] for n in snap["nodes"]}
    assert st == {"a": "ok", "b": "running", "c": "pending", "d": "ok"}, st
    assert snap["state"] == "running" and snap["counts"] == {"ok": 2, "running": 1, "pending": 1}
    assert next(n for n in snap["nodes"] if n["id"] == "c")["deps"] == ["b"]
    assert next(n for n in snap["nodes"] if n["id"] == "b")["running_for_s"] >= 0
    g.release.set()
    t.join(20)
    snap = o.snapshot(wf)
    assert snap["state"] == "done" and snap["counts"] == {"ok": 4}
    o.close()


def test_a_result_rejected_by_the_validator_and_a_step_that_keeps_losing_workers_are_failures():
    o = Orchestrator(DSN, Gate(bad={"x"}), max_concurrency=1, max_attempts=3)
    wf = wf_id()
    with pytest.raises(ResultRejected):
        o.run(wf, [step("x")])
    s = o.snapshot(wf)
    assert s["nodes"][0]["status"] == "rejected" and s["state"] == "failed"
    o.close()
    o = Orchestrator(DSN, Gate(lose={"y"}), max_concurrency=1, max_attempts=3)
    wf = wf_id()
    with pytest.raises(WorkerLost):
        o.run(wf, [step("y")])
    n = o.snapshot(wf)["nodes"][0]
    assert n["status"] == "failed" and n["attempts"] == 3
    o.close()


def test_retrying_is_visible_between_attempts():
    class Slow(Gate):
        def execute(self, task, attempt, deps):
            if attempt == 1:
                raise WorkerLost(task.task_id)
            self.entered.set()
            assert self.release.wait(20)
            return ok(task)
    g = Slow()
    o = Orchestrator(DSN, g, max_concurrency=1, max_attempts=5)
    wf = wf_id()
    t = threading.Thread(target=lambda: o.run(wf, [step("r")]))
    t.start()
    assert g.entered.wait(20)
    n = o.snapshot(wf)["nodes"][0]
    assert n["status"] == "running" and n["attempts"] == 2    # attempt 1 was lost, attempt 2 is in flight
    g.release.set()
    t.join(20)
    assert o.snapshot(wf)["nodes"][0]["status"] == "ok"
    o.close()


def test_http_view_is_read_only_and_never_leaks_task_content():
    o = Orchestrator(DSN, Gate(), max_concurrency=2)
    wf = wf_id()
    o.run(wf, [step("a"), step("b", ["a"])])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(o))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode()

    code, body = get("/workflows")
    assert code == 200 and wf in body
    code, body = get(f"/workflows/{wf}")
    snap = json.loads(body)
    assert code == 200 and [n["id"] for n in snap["nodes"]] == ["a", "b"]
    assert SECRET not in body and "answer" not in body, "task inputs and outputs must never leave the database"
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        req = urllib.request.Request(base + f"/workflows/{wf}", data=b"{}", method=method)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/workflows/nope-" + uuid.uuid4().hex, timeout=10)
    assert e.value.code == 404
    srv.shutdown()
    o.close()
