"""NATS JetStream task bus: delivery, redelivery after a worker dies, no double work for slow tasks,
poison tasks, and a whole orchestrator workflow with a worker killed mid-run.
Needs a JetStream server: AICF_NATS_URL (test stack: docker run -p 127.0.0.1:54222:4222 nats:2.10-alpine -js)
and, for the workflow test, AICF_PG_DSN. Skipped, not passed, without them."""
import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import nats
import pytest

from agents.orchestrator import Orchestrator, Step, WorkerLost
from agents.task_bus import STREAM, BusExecutor, RemoteError
from agents.tasks import Task, TaskType

URL = os.environ.get("AICF_NATS_URL")
PG = os.environ.get("AICF_PG_DSN")
pytestmark = pytest.mark.skipif(not URL, reason="set AICF_NATS_URL to a JetStream server (NOT passed without it)")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_stream():
    async def wipe():
        nc = await nats.connect(URL)
        js = nc.jetstream()
        try:
            await js.delete_stream(STREAM)
        except Exception:
            pass
        await nc.close()
    asyncio.run(wipe())
    yield


def spawn(name, tmp, delay=0.1, ack_wait="3", fail=(), lose=()):
    log = tmp / "log.txt"
    log.touch()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ACK_WAIT": ack_wait}
    return subprocess.Popen([sys.executable, "tests/integration/_bus_worker.py", URL, name, str(delay), str(log), *[f"fail={f}" for f in fail], *[f"lose={f}" for f in lose]],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), log


def task(tid):
    return Task(task_id=tid, tenant_id="acme", type=TaskType.TEXT_GENERATE, input={"messages": [{"role": "user", "content": "x"}]})


def events(log):
    return [l.split() for l in log.read_text().splitlines()]


@pytest.fixture
def procs():
    ps = []
    yield ps
    for p in ps:
        if p.poll() is None:
            p.kill()
        p.wait()


def test_round_trip_through_a_worker_process(tmp_path, procs):
    w, log = spawn("w1", tmp_path)
    procs.append(w)
    bx = BusExecutor(URL, task_timeout=30)
    res = bx.execute(task("t1"))
    assert res.task_id == "t1" and res.output["text"] == "t1 by w1" and res.worker_id == "w1"
    bx.close()


def test_a_worker_killed_mid_task_loses_nothing_and_the_caller_never_knows(tmp_path, procs):
    a, log = spawn("A", tmp_path, delay=4.0, ack_wait="2")
    procs.append(a)
    bx = BusExecutor(URL, task_timeout=40)
    import threading
    box = {}
    t = threading.Thread(target=lambda: box.update(res=bx.execute(task("long"))))
    t.start()
    for _ in range(100):
        if ("start", "A", "long") in map(tuple, events(log)):
            break
        time.sleep(0.1)
    a.send_signal(signal.SIGKILL)          # machine lost: no ack, no reply
    b, _ = spawn("B", tmp_path, delay=0.2, ack_wait="2")
    procs.append(b)
    t.join(40)
    assert box["res"].output["text"] == "long by B", box
    ev = [tuple(e) for e in events(log)]
    assert ("done", "A", "long") not in ev and ("done", "B", "long") in ev
    bx.close()


def test_a_slow_task_is_not_run_twice_because_the_worker_reports_progress(tmp_path, procs):
    w1, log = spawn("A", tmp_path, delay=5.0, ack_wait="2")     # 2.5 x ack_wait
    w2, _ = spawn("B", tmp_path, delay=5.0, ack_wait="2")
    procs += [w1, w2]
    bx = BusExecutor(URL, task_timeout=40)
    bx.execute(task("slow"))
    starts = [e for e in events(log) if e[0] == "start" and e[2] == "slow"]
    assert len(starts) == 1, f"redelivered while the worker was alive: {starts}"
    bx.close()


def test_a_task_that_fails_for_good_is_reported_once_and_not_redelivered(tmp_path, procs):
    w, log = spawn("A", tmp_path, delay=0.1, fail=["bad"])
    procs.append(w)
    bx = BusExecutor(URL, task_timeout=30)
    with pytest.raises(RemoteError, match="bad input for bad"):
        bx.execute(task("bad"))
    time.sleep(3)                          # longer than ack_wait: a redelivery would show up now
    assert len([e for e in events(log) if e[0] == "start"]) == 1
    bx.close()


def test_a_garbage_message_does_not_kill_the_worker(tmp_path, procs):
    async def poison():
        nc = await nats.connect(URL)
        js = nc.jetstream()
        from agents.task_bus import ensure_stream
        await ensure_stream(js, 3, 5)
        await js.publish("aicf.task.TEXT_GENERATE", b"this is not json")
        await js.publish("aicf.task.TEXT_GENERATE", b'{"task": {"nonsense": 1}, "attempt": 1, "deps": {}, "reply": "x"}')
        await nc.close()
    asyncio.run(poison())
    w, log = spawn("A", tmp_path)
    procs.append(w)
    bx = BusExecutor(URL, task_timeout=30)
    assert bx.execute(task("after-garbage")).output["text"] == "after-garbage by A"
    assert w.poll() is None, "the worker died"
    bx.close()


def test_a_worker_whose_gpu_was_reclaimed_hands_the_task_to_another_machine(tmp_path, procs):
    a, log = spawn("A", tmp_path, delay=0.1, lose=["gpu"])    # A always reports the GPU as taken for this task
    procs.append(a)
    bx = BusExecutor(URL, task_timeout=40)
    import threading
    box = {}
    t = threading.Thread(target=lambda: box.update(res=bx.execute(task("gpu"))))
    t.start()
    for _ in range(100):
        if ("start", "A", "gpu") in map(tuple, events(log)):
            break
        time.sleep(0.1)
    b, _ = spawn("B", tmp_path, delay=0.1)
    procs.append(b)
    t.join(40)
    assert box["res"].output["text"] == "gpu by B", box
    assert a.poll() is None, "a lost GPU must not kill the worker process"
    bx.close()


def test_with_no_worker_online_the_caller_gets_workerlost(tmp_path):
    bx = BusExecutor(URL, task_timeout=1.5)
    with pytest.raises(WorkerLost):
        bx.execute(task("nobody"))
    bx.close()


@pytest.mark.skipif(not PG, reason="needs AICF_PG_DSN")
def test_a_workflow_over_the_bus_survives_a_worker_being_killed(tmp_path, procs):
    a, log = spawn("A", tmp_path, delay=0.4, ack_wait="2")
    b, _ = spawn("B", tmp_path, delay=0.4, ack_wait="2")
    procs += [a, b]
    steps = []
    for d in range(6):
        prev = ()
        for st in ("parse", "clean", "summarise"):
            sid = f"doc{d}.{st}"
            steps.append(Step(sid, task(sid), prev))
            prev = (sid,)
    bx = BusExecutor(URL, task_timeout=60)
    o = Orchestrator(PG, bx, max_concurrency=4)
    wf = "bus-" + uuid.uuid4().hex[:8]

    import threading
    killer = threading.Timer(2.0, lambda: a.send_signal(signal.SIGKILL))
    killer.start()
    try:
        out = o.run(wf, steps)
    finally:
        killer.cancel()
        with __import__("psycopg").connect(PG, autocommit=True) as c:
            for t in ("aicf_dag", "aicf_results", "aicf_exec"):
                c.execute(f"DELETE FROM {t} WHERE workflow=%s", (wf,))
        o.close()
        bx.close()
    assert set(out) == {s.id for s in steps}
    assert all(v["output"]["text"].startswith(k + " by ") for k, v in out.items())
    assert a.poll() is not None, "worker A should have been killed"
    done_by = {e[1] for e in events(log) if e[0] == "done"}
    assert "B" in done_by
