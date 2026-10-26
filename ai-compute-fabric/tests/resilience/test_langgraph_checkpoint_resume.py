"""Resilience of the orchestrator (plan P5.1 gate).

A 50-step document workflow runs with 3 concurrent workers. Part 1: the orchestrator process is
SIGKILLed after 25 steps finish (what a lost machine looks like); a new process resumes from the
PostgreSQL checkpoint. Gate: 100 % correct results, and the number of re-executed tasks is at most
the number that were in flight at the kill. Part 2: workers reclaimed mid-task are retried elsewhere.
Needs PostgreSQL: set AICF_PG_DSN (the test stack uses the pgvector container on port 55432)."""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agents.orchestrator import Orchestrator, ResultRejected
from tests.resilience._workflow import DOCS, STAGES, SimWorker, expected, workflow

DSN = os.environ.get("AICF_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set AICF_PG_DSN to a PostgreSQL URL (NOT passed without it)")
ROOT = Path(__file__).resolve().parents[2]


def rows(wf):
    with psycopg.connect(DSN) as c:
        return c.execute("SELECT step, attempt, outcome, finished_at IS NOT NULL FROM aicf_exec WHERE workflow=%s ORDER BY id", (wf,)).fetchall()


def spawn(wf, mode, tmp, delay=0.12):
    out = tmp / f"{wf}-{mode}.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, "-m", "tests.resilience._workflow", DSN, wf, mode, str(delay), str(out)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT), out


@pytest.mark.parametrize("kill_after", [1, 9, 25, 25, 38, 49])   # 25 is the plan's case, run twice
def test_kill_then_resume_runs_only_unfinished_tasks(tmp_path, kill_after):
    assert len(workflow()) == DOCS * len(STAGES) == 50
    wf = "wf-" + uuid.uuid4().hex[:8]
    Orchestrator(DSN, SimWorker())   # creates the log table
    p, out1 = spawn(wf, "run", tmp_path)
    deadline = time.time() + 120
    while time.time() < deadline:
        if sum(1 for r in rows(wf) if r[2] == "ok") >= kill_after:
            break
        time.sleep(0.02)
    else:
        p.kill(); pytest.fail(f"workflow never reached {kill_after} finished steps")
    os.kill(p.pid, signal.SIGKILL)       # no cleanup, no goodbye
    p.wait()
    before = rows(wf)
    done_before = {r[0] for r in before if r[2] == "ok"}
    in_flight = {r[0] for r in before if not r[3]}
    assert kill_after <= len(done_before) <= 50 and len(in_flight) <= 3

    p2, out2 = spawn(wf, "resume", tmp_path)
    p2.wait(timeout=180)
    assert p2.returncode == 0, p2.stdout.read().decode()[-500:]

    assert json.loads(out2.read_text()) == expected(workflow()), "resumed run produced wrong results"
    after = [r for r in rows(wf)[len(before):]]   # attempts made by the resumed process
    rerun = {r[0] for r in after}
    assert not (rerun & done_before), f"finished steps were executed again: {sorted(rerun & done_before)}"
    ok_per_step = {}
    for r in rows(wf):
        if r[2] == "ok":
            ok_per_step[r[0]] = ok_per_step.get(r[0], 0) + 1
    assert len(ok_per_step) == 50 and set(ok_per_step.values()) == {1}, "every step must complete exactly once"
    reexecuted = sum(1 for r in after if r[0] in in_flight)
    assert reexecuted <= len(in_flight) <= 3, (reexecuted, in_flight)


def test_reclaimed_workers_are_retried_and_results_stay_correct():
    steps = workflow()
    lose = {("doc0.chunk", 1), ("doc3.parse", 1), ("doc3.parse", 2), ("doc9.index", 1)}
    wf = "wf-" + uuid.uuid4().hex[:8]
    res = Orchestrator(DSN, SimWorker(0.01, lose=lose), max_concurrency=3).run(wf, steps)
    assert {k: v["output"]["text"] for k, v in res.items()} == expected(steps)
    r = rows(wf)
    assert sum(1 for x in r if x[2] == "worker_lost") == len(lose) == 4
    assert sum(1 for x in r if x[2] == "ok") == 50
    assert len(r) == 50 + len(lose), "only the reclaimed attempts may be repeated"


def test_a_malformed_worker_result_is_rejected_and_never_stored():
    steps = workflow()
    wf = "wf-" + uuid.uuid4().hex[:8]
    with pytest.raises(ResultRejected, match="doc2.clean"):
        Orchestrator(DSN, SimWorker(0.0, corrupt={"doc2.clean"}), max_concurrency=3).run(wf, steps)
    r = rows(wf)
    assert any(x[0] == "doc2.clean" and x[2] == "rejected" for x in r)
    assert not any(x[0] == "doc2.clean" and x[2] == "ok" for x in r)
    assert not any(x[0].startswith("doc2.") and x[0] > "doc2.clean" and x[2] == "ok" and x[0] in ("doc2.chunk", "doc2.summarise", "doc2.index") for x in r), \
        "nothing downstream of a rejected result may run"


def test_workflow_with_unknown_dependency_is_refused():
    from agents.orchestrator import Step
    s = workflow()[:1] + [Step("x", workflow()[1].task, ("nope",))]
    with pytest.raises(ValueError, match="unknown step"):
        Orchestrator(DSN, SimWorker(0)).run("wf-" + uuid.uuid4().hex[:8], s)
