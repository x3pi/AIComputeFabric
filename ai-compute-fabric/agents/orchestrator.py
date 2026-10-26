"""Workflow orchestrator on LangGraph with a PostgreSQL checkpoint store (plan P5.1).

A workflow is a DAG of typed tasks. Each node runs one task on a worker. LangGraph checkpoints to
PostgreSQL after every node, so if the orchestrator process dies, `resume` continues from the last
checkpoint on any machine: finished nodes are not run again, only the ones that were in flight.
When a worker is reclaimed by its user mid-task (WorkerLost), the task is retried on another worker."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Callable, Optional, Protocol

import psycopg
import psycopg.types.json
from psycopg_pool import ConnectionPool
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

DAG_DDL = """
CREATE TABLE IF NOT EXISTS aicf_dag (
  workflow TEXT PRIMARY KEY, steps JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""


class Orchestrator:
    def __init__(self, dsn: str, executor: Executor, max_concurrency: int = 3, max_attempts: int = 5, durability: str = "sync"):
        self.dsn, self.executor, self.max_concurrency, self.max_attempts = dsn, executor, max_concurrency, max_attempts
        self.durability = durability
        # one pool for the execution log AND LangGraph's checkpoint writes: opening a connection per write
        # was the scaling bottleneck (efficiency fell from 0.75 to 0.54 between 8 and 40 workers)
        self.pool: ConnectionPool[Any] = ConnectionPool(self.dsn, min_size=2, max_size=max(8, max_concurrency + 4), open=True, kwargs={"autocommit": True, "row_factory": dict_row})
        with self.pool.connection() as c:
            c.execute(EXEC_DDL)
            c.execute(RESULTS_DDL)
            c.execute(DAG_DDL)

    # ---- durable execution log (one row per attempt; survives a crash of this process) ----
    def _log_start(self, wf: str, step: str, attempt: int) -> int:
        with self.pool.connection() as c:
            return c.execute("INSERT INTO aicf_exec(workflow, step, attempt) VALUES (%s,%s,%s) RETURNING id", (wf, step, attempt)).fetchone()["id"]

    def _log_end(self, row: int, outcome: str, store: Optional[tuple] = None):
        """Records the outcome; for a successful step the result is stored in the SAME transaction,
        so 'finished' and 'result durable' can never disagree after a crash."""
        with self.pool.connection() as c, c.transaction():
            c.execute("UPDATE aicf_exec SET finished_at = now(), outcome = %s WHERE id = %s", (outcome, row))
            if store:
                wf, step, result = store
                c.execute("INSERT INTO aicf_results(workflow, step, result) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                          (wf, step, psycopg.types.json.Jsonb(result)))

    def _stored(self, wf: str, step: str):
        with self.pool.connection() as c:
            r = c.execute("SELECT result FROM aicf_results WHERE workflow=%s AND step=%s", (wf, step)).fetchone()
        return r["result"] if r else None

    def executions(self, wf: str) -> list:
        with self.pool.connection() as c:
            return c.execute("SELECT step, attempt, outcome, finished_at IS NOT NULL AS finished FROM aicf_exec WHERE workflow=%s ORDER BY id", (wf,)).fetchall()

    # ---- read side for the dashboard (never returns task inputs or outputs) ----
    def _record_dag(self, wf: str, steps: list):
        shape = [{"id": s.id, "type": s.task.type.value, "tenant": s.task.tenant_id, "deps": list(s.deps)} for s in steps]
        with self.pool.connection() as c:
            c.execute("INSERT INTO aicf_dag(workflow, steps) VALUES (%s,%s) ON CONFLICT DO NOTHING", (wf, psycopg.types.json.Jsonb(shape)))

    def workflows(self, limit: int = 50) -> list:
        with self.pool.connection() as c:
            rows = c.execute("SELECT workflow, created_at, jsonb_array_length(steps) AS steps FROM aicf_dag ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        out = []
        for r in rows:
            snap = self.snapshot(r["workflow"])
            if snap is None:                       # removed between the listing and the lookup
                continue
            out.append({"workflow": r["workflow"], "created_at": r["created_at"].isoformat(), "steps": r["steps"], "counts": snap["counts"], "state": snap["state"]})
        return out

    def snapshot(self, wf: str) -> Optional[dict]:
        """Status of every step: pending, running, retrying, ok, rejected (failed validation, final)
        or failed (worker lost on every attempt). A step left 'running' by a crashed orchestrator looks
        the same as a live one until the workflow is resumed: the log cannot tell them apart."""
        with self.pool.connection() as c:
            dag = c.execute("SELECT steps, created_at FROM aicf_dag WHERE workflow=%s", (wf,)).fetchone()
            if dag is None:
                return None
            ex = c.execute("SELECT step, attempt, outcome, started_at, finished_at FROM aicf_exec WHERE workflow=%s ORDER BY id", (wf,)).fetchall()
            done = {r["step"] for r in c.execute("SELECT step FROM aicf_results WHERE workflow=%s", (wf,)).fetchall()}
            now = c.execute("SELECT now() AS t").fetchone()["t"]
        last: dict = {}
        attempts: dict = {}
        for r in ex:
            last[r["step"]] = r
            attempts[r["step"]] = attempts.get(r["step"], 0) + 1
        nodes = []
        for st in dag["steps"]:
            l = last.get(st["id"])
            if st["id"] in done:
                status = "ok"
            elif l is None:
                status = "pending"
            elif l["finished_at"] is None:
                status = "running"
            elif l["outcome"] == "rejected":
                status = "rejected"
            elif l["outcome"] == "worker_lost":
                status = "failed" if l["attempt"] >= self.max_attempts else "retrying"
            else:
                status = "pending"
            node = {"id": st["id"], "type": st["type"], "tenant": st["tenant"], "deps": st["deps"], "status": status, "attempts": attempts.get(st["id"], 0)}
            if l is not None:
                node["last_started"] = l["started_at"].isoformat()
                if status == "running":
                    node["running_for_s"] = round((now - l["started_at"]).total_seconds(), 1)
                elif l["finished_at"] is not None:
                    node["last_duration_s"] = round((l["finished_at"] - l["started_at"]).total_seconds(), 2)
            nodes.append(node)
        counts: dict = {}
        for n in nodes:
            counts[n["status"]] = counts.get(n["status"], 0) + 1
        state = ("failed" if counts.get("failed") or counts.get("rejected") else "done" if counts.get("ok") == len(nodes)
                 else "running" if counts.get("running") or counts.get("retrying") else "idle")
        return {"workflow": wf, "created_at": dag["created_at"].isoformat(), "state": state, "counts": counts, "nodes": nodes}

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
        dependents: dict[str, list] = {s.id: [] for s in steps}
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
        self._record_dag(wf, steps)
        cfg = {"configurable": {"thread_id": wf}, "max_concurrency": self.max_concurrency, "recursion_limit": 10_000}
        saver = PostgresSaver(self.pool)  # type: ignore[arg-type]   # the pool is configured with row_factory=dict_row, which is what PostgresSaver needs
        saver.setup()
        graph = self._compile(wf, steps, saver)
        out = graph.invoke(None if resume else {"results": {}}, cfg, durability=self.durability)
        return out["results"]

    def close(self):
        self.pool.close()
