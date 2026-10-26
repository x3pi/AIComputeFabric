"""Workflow orchestrator on LangGraph with a PostgreSQL checkpoint store (plan P5.1).

A workflow is a DAG of typed tasks. Each node runs one task on a worker. LangGraph checkpoints to
PostgreSQL after every node, so if the orchestrator process dies, `resume` continues from the last
checkpoint on any machine: finished nodes are not run again, only the ones that were in flight.
When a worker is reclaimed by its user mid-task (WorkerLost), the task is retried on another worker."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Callable, Optional, Protocol

import psycopg
import psycopg.types.json
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from typing_extensions import TypedDict

from agents.tasks import Task, TaskResult
from agents.validator import validate_result


class WorkerLost(Exception):
    """The worker's GPU was reclaimed (or it vanished) while it ran the task."""


class ResultRejected(Exception):
    pass


class Executor(Protocol):
    def execute(self, task: Task, attempt: int, deps: dict) -> TaskResult: ...


@dataclass(frozen=True)
class Step:
    id: str
    task: Task
    deps: tuple = ()


def merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class State(TypedDict):
    results: Annotated[dict, merge]


RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS aicf_results (
  workflow TEXT NOT NULL, step TEXT NOT NULL, result JSONB NOT NULL, stored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workflow, step))"""

EXEC_DDL = """
CREATE TABLE IF NOT EXISTS aicf_exec (
  id BIGSERIAL PRIMARY KEY, workflow TEXT NOT NULL, step TEXT NOT NULL, attempt INT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ, outcome TEXT)"""


class Orchestrator:
    def __init__(self, dsn: str, executor: Executor, max_concurrency: int = 3, max_attempts: int = 5):
        self.dsn, self.executor, self.max_concurrency, self.max_attempts = dsn, executor, max_concurrency, max_attempts
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(EXEC_DDL)
            c.execute(RESULTS_DDL)

    # ---- durable execution log (one row per attempt; survives a crash of this process) ----
    def _log_start(self, wf: str, step: str, attempt: int) -> int:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            return c.execute("INSERT INTO aicf_exec(workflow, step, attempt) VALUES (%s,%s,%s) RETURNING id", (wf, step, attempt)).fetchone()[0]

    def _log_end(self, row: int, outcome: str, store: Optional[tuple] = None):
        """Records the outcome; for a successful step the result is stored in the SAME transaction,
        so 'finished' and 'result durable' can never disagree after a crash."""
        with psycopg.connect(self.dsn) as c:
            c.execute("UPDATE aicf_exec SET finished_at = now(), outcome = %s WHERE id = %s", (outcome, row))
            if store:
                wf, step, result = store
                c.execute("INSERT INTO aicf_results(workflow, step, result) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                          (wf, step, psycopg.types.json.Jsonb(result)))

    def _stored(self, wf: str, step: str):
        with psycopg.connect(self.dsn) as c:
            r = c.execute("SELECT result FROM aicf_results WHERE workflow=%s AND step=%s", (wf, step)).fetchone()
        return r[0] if r else None

    def executions(self, wf: str) -> list:
        with psycopg.connect(self.dsn, row_factory=dict_row) as c:
            return c.execute("SELECT step, attempt, outcome, finished_at IS NOT NULL AS finished FROM aicf_exec WHERE workflow=%s ORDER BY id", (wf,)).fetchall()

    # ---- graph ----
    def _node(self, wf: str, step: Step) -> Callable:
        def run(state: State) -> dict:
            done = self._stored(wf, step.id)      # finished before a crash: reuse, never execute twice
            if done is not None:
                return {"results": {step.id: done}}
            attempt = 0
            while True:
                attempt += 1
                row = self._log_start(wf, step.id, attempt)
                try:
                    res = self.executor.execute(step.task, attempt, {d: state["results"][d] for d in step.deps})
                except WorkerLost:
                    self._log_end(row, "worker_lost")
                    if attempt >= self.max_attempts:
                        raise
                    continue
                rep = validate_result(step.task, res)
                if not rep.ok:   # a result that fails validation is never stored or passed on
                    self._log_end(row, "rejected")
                    raise ResultRejected(f"{step.id}: {'; '.join(rep.errors)}")
                payload = res.model_dump(mode="json")
                self._log_end(row, "ok", store=(wf, step.id, payload))
                return {"results": {step.id: payload}}
        return run

    def _compile(self, wf: str, steps: list, saver):
        ids = {s.id for s in steps}
        g = StateGraph(State)
        for s in steps:
            g.add_node(s.id, self._node(wf, s))
        dependents = {s.id: [] for s in steps}
        for s in steps:
            for d in s.deps:
                if d not in ids:
                    raise ValueError(f"{s.id} depends on unknown step {d}")
                dependents[d].append(s.id)
            if not s.deps:
                g.add_edge(START, s.id)
            elif len(s.deps) == 1:
                g.add_edge(s.deps[0], s.id)
            else:
                g.add_edge(list(s.deps), s.id)   # wait for ALL dependencies
        for sid, outs in dependents.items():
            if not outs:
                g.add_edge(sid, END)
        return g.compile(checkpointer=saver)

    def run(self, wf: str, steps: list, resume: bool = False) -> dict:
        cfg = {"configurable": {"thread_id": wf}, "max_concurrency": self.max_concurrency, "recursion_limit": 10_000}
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            saver = PostgresSaver(conn)
            saver.setup()
            graph = self._compile(wf, steps, saver)
            out = graph.invoke(None if resume else {"results": {}}, cfg, durability="sync")
        return out["results"]
