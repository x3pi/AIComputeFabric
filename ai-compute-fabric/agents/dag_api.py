"""Read-only HTTP view of agent workflows and Elastic Agent Fabric telemetry (screen 4a + Phase E).

    GET /workflows                     list with per-state counts
    GET /workflows/<id>                nodes, dependencies and status of every step
    GET /metrics                       Prometheus metrics for NATS JetStream task bus
    GET /supervisor/recommendations    propose-only scaling recommendations (D-FAB-4)
    GET /bus/overview                  structured JSON snapshot of live agents (AICF_AGENTS KV), queues & supervisor
    GET /bus/demo?action=...           interactive dev/demo actions (spawn, drain, kill, burst, workflow, reset)

It reads the orchestrator's own tables, so it also works for workflows another process is running.
It never returns task inputs or outputs (they can hold the documents the agent is reading). It has no
authentication of its own: run it on loopback and let the site gateway proxy it (--agents-url), which
applies the admin roles."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
import time
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from agents.orchestrator import Orchestrator, Step
from agents.task_bus import (
    BusExecutor,
    BusMetricsCollector,
    BusWorker,
    ElasticSupervisor,
)
from agents.tasks import Task, TaskResult, TaskType

WF = re.compile(r"^/workflows/([^/]{1,200})/?$")


class _DemoSimExecutor:
    """Schema-compliant simulated executor for interactive Elastic Agent Fabric demo workers."""

    def __init__(self, worker_id: str, delay_s: float = 0.6):
        self.worker_id = worker_id
        self.delay_s = delay_s

    async def execute_async(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        delay = float(task.input.get("args", {}).get("sim_delay_s", self.delay_s)) if isinstance(task.input, dict) else self.delay_s
        await asyncio.sleep(max(0.05, min(delay, 5.0)))
        tp = task.type
        if tp == TaskType.TEXT_GENERATE:
            out: dict[str, Any] = {"text": "def solution(x):\n    return x", "citations": []}
        elif tp == TaskType.TOOL_EXECUTE:
            out = {"result": {"status": "PASS", "passed": 4, "failed": 0, "worker": self.worker_id}}
        elif tp == TaskType.RESULT_VALIDATE:
            out = {"ok": True, "errors": []}
        elif tp == TaskType.MODEL_EVALUATE:
            out = {"metric": task.input.get("metric", "accuracy"), "value": 0.94}
        elif tp == TaskType.RAG_RETRIEVE:
            out = {
                "hits": [{"source_id": "doc-1", "text": "AICF elastic agent bus specification"}],
                "citations": [{"source_id": "doc-1", "quote": "AICF elastic agent bus specification"}],
            }
        elif tp == TaskType.EMBEDDING:
            texts = task.input.get("texts", ["a"])
            out = {"vectors": [[0.12, 0.34, 0.56, 0.78] for _ in texts], "dim": 4}
        elif tp == TaskType.RERANK:
            docs = task.input.get("documents", ["a"])
            out = {"ranking": list(range(len(docs))), "scores": [0.95 for _ in docs]}
        elif tp == TaskType.DOCUMENT_PARSE:
            out = {"pages": ["parsed page 1"], "tables": []}
        elif tp == TaskType.OCR:
            out = {"text": "ocr text", "confidence": 0.99}
        elif tp == TaskType.AUDIO_VAD:
            out = {"segments": [[0.0, 1.2]]}
        elif tp == TaskType.AUDIO_ASR:
            out = {"text": "xin chao", "segments": []}
        elif tp == TaskType.AUDIO_TTS:
            out = {"audio_ref": "demo.wav", "duration_s": 1.25}
        elif tp == TaskType.AUDIO_ENHANCE:
            out = {"audio_ref": "enhanced.wav"}
        elif tp == TaskType.TRAIN_STEP:
            out = {
                "run_id": task.input.get("run_id", "run-1"),
                "step": int(task.input.get("step", 1)),
                "loss": 0.31,
                "update_ref": "ckpt.safetensors",
            }
        else:
            doc = task.input.get("document", {"source_id": "s1", "text": "ok"})
            out = {
                "data": {},
                "citations": [{"source_id": doc.get("source_id", "s1"), "quote": (doc.get("text", "ok") or "ok")[:20]}],
            }
        return TaskResult(task_id=task.task_id, type=tp, output=out, worker_id=self.worker_id)

    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        return asyncio.run(self.execute_async(task, attempt, deps))


class ElasticDemoPool:
    """Manages live coroutine BusWorkers on the shared NATS bus for dashboard inspection and elastic scaling demos."""

    def __init__(self, nats_url: str, dsn: str = "", seed_defaults: bool = True):
        self.nats_url = nats_url
        self.dsn = dsn
        self.bus_executor = BusExecutor(nats_url, task_timeout=25.0, max_queue_depth=256, max_tenant_inflight=128)
        self.loop = asyncio.new_event_loop()
        self._mu = threading.Lock()
        self._workers: dict[str, tuple[BusWorker, asyncio.Task]] = {}
        self._seq = 0
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="aicf-elastic-demo-pool")
        self._thread.start()
        if seed_defaults:
            self.seed_default_fleet()

    def _run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _next_id(self, prefix: str) -> str:
        with self._mu:
            self._seq += 1
            return f"{prefix}-{self._seq:02d}"

    async def _spawn_one_async(
        self,
        agent_id: str,
        *,
        kind: str = "executor",
        types: list[TaskType],
        labels: list[str],
        models: list[str],
        slots: int = 2,
        ctx_per_slot: int = 8192,
        tok_s_ref: float = 0.0,
        data_classes: list[str] | None = None,
        sim_delay_s: float = 0.6,
    ) -> str:
        w = BusWorker(
            self.nats_url,
            _DemoSimExecutor(agent_id, delay_s=sim_delay_s),
            name=agent_id,
            ack_wait=2.0,
            max_deliver=5,
            types=types,
            kind=kind,  # type: ignore[arg-type]
            labels=labels,
            models=models,
            slots=slots,
            ctx_per_slot=ctx_per_slot,
            tok_s_ref=tok_s_ref,
            data_classes=data_classes or ["PUBLIC", "INTERNAL"],
            agent_ttl=6.0,
        )
        t = asyncio.create_task(w.run_async())
        with self._mu:
            self._workers[agent_id] = (w, t)

        def _cleanup(_fut):
            with self._mu:
                cur = self._workers.get(agent_id)
                if cur and cur[1] is t:
                    self._workers.pop(agent_id, None)

        t.add_done_callback(_cleanup)
        await asyncio.sleep(0.15)
        return agent_id

    def seed_default_fleet(self) -> list[str]:
        async def _seed():
            spawned = []
            specs = [
                (
                    "agent-coder-01",
                    "compute",
                    [TaskType.TEXT_GENERATE, TaskType.MODEL_EVALUATE],
                    ["code", "general"],
                    ["Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"],
                    1,
                    32768,
                    129.5,
                    ["PUBLIC", "INTERNAL"],
                    0.55,
                ),
                (
                    "agent-sandbox-01",
                    "executor",
                    [TaskType.TOOL_EXECUTE, TaskType.RESULT_VALIDATE],
                    ["sandbox", "python"],
                    ["python:3.12-alpine"],
                    4,
                    8192,
                    0.0,
                    ["PUBLIC", "INTERNAL"],
                    0.50,
                ),
                (
                    "agent-sandbox-02",
                    "executor",
                    [TaskType.TOOL_EXECUTE, TaskType.RESULT_VALIDATE],
                    ["sandbox", "python"],
                    ["python:3.12-alpine"],
                    4,
                    8192,
                    0.0,
                    ["PUBLIC", "INTERNAL"],
                    0.50,
                ),
                (
                    "agent-rag-01",
                    "executor",
                    [TaskType.RAG_RETRIEVE, TaskType.EMBEDDING, TaskType.RERANK, TaskType.DOCUMENT_PARSE],
                    ["rag", "search"],
                    ["Qwen3-Embedding-0.6B", "bge-reranker-v2-m3"],
                    4,
                    8192,
                    0.0,
                    ["PUBLIC", "INTERNAL", "RESTRICTED"],
                    0.40,
                ),
            ]
            for aid, kind, types, labels, models, slots, ctx, tok_s, dcs, delay in specs:
                with self._mu:
                    if aid in self._workers:
                        continue
                await self._spawn_one_async(
                    aid,
                    kind=kind,
                    types=types,
                    labels=labels,
                    models=models,
                    slots=slots,
                    ctx_per_slot=ctx,
                    tok_s_ref=tok_s,
                    data_classes=dcs,
                    sim_delay_s=delay,
                )
                spawned.append(aid)
            return spawned

        return self._run(_seed(), timeout=15.0)

    def spawn_preset(self, preset: str = "sandbox", count: int = 1) -> list[str]:
        count = max(1, min(int(count), 25))

        async def _do():
            created = []
            for _ in range(count):
                if preset == "coder":
                    aid = self._next_id("agent-coder-dyn")
                    await self._spawn_one_async(
                        aid,
                        kind="compute",
                        types=[TaskType.TEXT_GENERATE, TaskType.MODEL_EVALUATE],
                        labels=["code", "dynamic"],
                        models=["Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"],
                        slots=1,
                        ctx_per_slot=32768,
                        tok_s_ref=129.5,
                        data_classes=["PUBLIC", "INTERNAL"],
                        sim_delay_s=0.45,
                    )
                elif preset == "rag":
                    aid = self._next_id("agent-rag-dyn")
                    await self._spawn_one_async(
                        aid,
                        kind="executor",
                        types=[TaskType.RAG_RETRIEVE, TaskType.EMBEDDING, TaskType.RERANK, TaskType.DOCUMENT_PARSE],
                        labels=["rag", "dynamic"],
                        models=["Qwen3-Embedding-0.6B", "bge-reranker-v2-m3"],
                        slots=4,
                        ctx_per_slot=8192,
                        tok_s_ref=0.0,
                        data_classes=["PUBLIC", "INTERNAL", "RESTRICTED"],
                        sim_delay_s=0.35,
                    )
                else:
                    aid = self._next_id("agent-sandbox-dyn")
                    await self._spawn_one_async(
                        aid,
                        kind="executor",
                        types=[TaskType.TOOL_EXECUTE, TaskType.RESULT_VALIDATE],
                        labels=["sandbox", "python", "dynamic"],
                        models=["python:3.12-alpine"],
                        slots=4,
                        ctx_per_slot=8192,
                        tok_s_ref=0.0,
                        data_classes=["PUBLIC", "INTERNAL"],
                        sim_delay_s=0.45,
                    )
                created.append(aid)
            return created

        return self._run(_do(), timeout=15.0)

    def drain_agents(self, count: int = 1, agent_id: str = "") -> list[str]:
        with self._mu:
            if agent_id:
                pair = self._workers.get(agent_id)
                targets = [(agent_id, pair[0])] if pair else []
            else:
                items = list(self._workers.items())
                # Prefer draining dynamically added workers first
                items.sort(key=lambda kv: (0 if "-dyn-" in kv[0] else 1, kv[0]), reverse=True)
                targets = [(aid, w) for aid, (w, _t) in items[: max(1, min(int(count), 25))]]
        drained = []
        for aid, w in targets:
            w.request_drain()
            drained.append(aid)
        return drained

    def kill_agents(self, count: int = 1, agent_id: str = "") -> list[str]:
        async def _do():
            with self._mu:
                if agent_id:
                    pair = self._workers.pop(agent_id, None)
                    targets = [(agent_id, pair[0], pair[1])] if pair else []
                else:
                    items = list(self._workers.items())
                    items.sort(key=lambda kv: (0 if "-dyn-" in kv[0] else 1, kv[0]), reverse=True)
                    chosen = items[: max(1, min(int(count), 25))]
                    targets = []
                    for aid, (w, t) in chosen:
                        self._workers.pop(aid, None)
                        targets.append((aid, w, t))
            killed = []
            for aid, w, t in targets:
                w._SimulateSuddenCrash = True  # skip graceful KV delete to demonstrate TTL & redelivery
                t.cancel()
                killed.append(aid)
            return killed

        return self._run(_do(), timeout=10.0)

    def burst_tasks(self, task_type: str = "TOOL_EXECUTE", count: int = 12, delay_s: float = 1.6) -> int:
        count = max(1, min(int(count), 60))
        tp = TaskType(task_type) if task_type in TaskType.__members__ else TaskType.TOOL_EXECUTE

        async def _fire():
            coros = []
            for i in range(count):
                tid = f"burst-{tp.value.lower()}-{uuid.uuid4().hex[:6]}-{i}"
                if tp == TaskType.TEXT_GENERATE:
                    inp: dict[str, Any] = {"messages": [{"role": "user", "content": f"Solve task {i}"}], "max_tokens": 128}
                elif tp == TaskType.RAG_RETRIEVE:
                    inp = {"query": f"elastic agent {i}", "tenant_id": "demo", "top_k": 3}
                else:
                    inp = {"tool": "pytest_sandbox", "args": {"candidate": i, "sim_delay_s": delay_s}}
                t = Task(task_id=tid, tenant_id="demo", type=tp, input=inp)
                coros.append(self.bus_executor.execute_async(t))
            await asyncio.gather(*coros, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(_fire(), self.loop)
        return count

    def launch_demo_workflow(self) -> str:
        wf_id = "bon-elastic-" + time.strftime("%H%M%S")
        if not self.dsn:
            return wf_id

        def _bg():
            orch = Orchestrator(self.dsn, self.bus_executor, max_concurrency=4, max_attempts=3)
            try:
                steps = [
                    Step(
                        "plan.variants",
                        Task(
                            task_id="plan.variants",
                            tenant_id="demo",
                            type=TaskType.TEXT_GENERATE,
                            input={"messages": [{"role": "user", "content": "Create 4 strategy prompts"}], "max_tokens": 128},
                        ),
                    ),
                ]
                for i in range(1, 5):
                    s_id = f"sample.cand_{i}"
                    t_id = f"sandbox.test_{i}"
                    steps.append(
                        Step(
                            s_id,
                            Task(
                                task_id=s_id,
                                tenant_id="demo",
                                type=TaskType.TEXT_GENERATE,
                                input={"messages": [{"role": "user", "content": f"Candidate {i}"}], "max_tokens": 256},
                            ),
                            ("plan.variants",),
                        )
                    )
                    steps.append(
                        Step(
                            t_id,
                            Task(
                                task_id=t_id,
                                tenant_id="demo",
                                type=TaskType.TOOL_EXECUTE,
                                input={"tool": "docker_sandbox_pytest", "args": {"candidate": i, "sim_delay_s": 1.1}},
                            ),
                            (s_id,),
                        )
                    )
                steps.append(
                    Step(
                        "select.consensus",
                        Task(
                            task_id="select.consensus",
                            tenant_id="demo",
                            type=TaskType.RESULT_VALIDATE,
                            input={"target_task_id": "sandbox.test_1"},
                        ),
                        tuple(f"sandbox.test_{i}" for i in range(1, 5)),
                    )
                )
                orch.run(wf_id, steps)
            except Exception:
                pass
            finally:
                orch.close()

        threading.Thread(target=_bg, daemon=True).start()
        return wf_id


def _build_overview(
    metrics_collector: Optional[BusMetricsCollector],
    supervisor: Optional[ElasticSupervisor],
) -> dict[str, Any]:
    if metrics_collector is None:
        return {
            "connected": False,
            "nats_url": "",
            "kv_bucket": "AICF_AGENTS",
            "stream": "AICF_TASKS",
            "live_agents": [],
            "queues": [],
            "recommendations": [],
            "rejected_counts": {},
            "completed_total": 0,
            "totals": {
                "live_agents": 0,
                "compute_agents": 0,
                "executor_agents": 0,
                "total_slots": 0,
                "queue_depth": 0,
                "inflight_tasks": 0,
                "redelivered_total": 0,
                "rejected_total": 0,
                "active_recommendations": 0,
            },
        }
    snap = metrics_collector.collect()
    sup = supervisor or ElasticSupervisor(propose_only=True)
    recs = sup.evaluate(snap)
    rec_by_type = {r.task_type: asdict(r) for r in recs}

    queues = []
    for t in TaskType:
        tp = t.value
        queues.append(
            {
                "task_type": tp,
                "queue_depth": snap.queue_depth_by_type.get(tp, 0),
                "inflight_tasks": snap.inflight_by_type.get(tp, 0),
                "live_agents": snap.live_agents_by_type.get(tp, 0),
                "wait_median_s": round(snap.median_wait_s_by_type.get(tp, 0.0), 4),
                "redelivered_total": snap.redelivered_by_type.get(tp, 0),
                "recommendation": rec_by_type.get(tp),
            }
        )

    compute_cnt = sum(1 for a in snap.live_agent_profiles if a.get("kind") == "compute")
    exec_cnt = sum(1 for a in snap.live_agent_profiles if a.get("kind") != "compute")
    total_slots = sum(int(a.get("slots", 1)) for a in snap.live_agent_profiles)
    active_recs = sum(1 for r in recs if r.action != "noop")

    return {
        "connected": True,
        "nats_url": metrics_collector.url,
        "kv_bucket": "AICF_AGENTS",
        "stream": "AICF_TASKS",
        "live_agents": snap.live_agent_profiles,
        "queues": queues,
        "recommendations": [asdict(r) for r in recs],
        "rejected_counts": snap.rejected_counts,
        "completed_total": snap.completed_total,
        "totals": {
            "live_agents": snap.live_agents_total,
            "compute_agents": compute_cnt,
            "executor_agents": exec_cnt,
            "total_slots": total_slots,
            "queue_depth": sum(snap.queue_depth_by_type.values()),
            "inflight_tasks": sum(snap.inflight_by_type.values()),
            "redelivered_total": sum(snap.redelivered_by_type.values()),
            "rejected_total": sum(snap.rejected_counts.values()),
            "active_recommendations": active_recs,
        },
    }


def make_handler(
    orch: Optional[Orchestrator] = None,
    metrics_collector: Optional[BusMetricsCollector] = None,
    supervisor: Optional[ElasticSupervisor] = None,
    demo_pool: Optional[ElasticDemoPool] = None,
):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _send_text(self, code: int, text: str, content_type: str = "text/plain; version=0.0.4"):
            b = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path == "/metrics" and metrics_collector is not None:
                return self._send_text(200, metrics_collector.render_prometheus())
            if path == "/supervisor/recommendations" and metrics_collector is not None:
                sup = supervisor or ElasticSupervisor(propose_only=True)
                recs = sup.evaluate(metrics_collector.collect())
                return self._send(200, [asdict(r) for r in recs])
            if path == "/bus/overview":
                return self._send(200, _build_overview(metrics_collector, supervisor))
            if path == "/bus/demo" and demo_pool is not None:
                act = (qs.get("action") or [""])[0]
                if act == "spawn":
                    preset = (qs.get("preset") or ["sandbox"])[0]
                    cnt = int((qs.get("count") or ["1"])[0])
                    spawned = demo_pool.spawn_preset(preset=preset, count=cnt)
                    return self._send(200, {"ok": True, "action": "spawn", "agents": spawned})
                if act == "drain":
                    aid = (qs.get("agent_id") or [""])[0]
                    cnt = int((qs.get("count") or ["1"])[0])
                    drained = demo_pool.drain_agents(count=cnt, agent_id=aid)
                    return self._send(200, {"ok": True, "action": "drain", "agents": drained})
                if act == "kill":
                    aid = (qs.get("agent_id") or [""])[0]
                    cnt = int((qs.get("count") or ["1"])[0])
                    killed = demo_pool.kill_agents(count=cnt, agent_id=aid)
                    return self._send(200, {"ok": True, "action": "kill", "agents": killed})
                if act == "burst":
                    tp = (qs.get("task_type") or ["TOOL_EXECUTE"])[0]
                    cnt = int((qs.get("count") or ["12"])[0])
                    delay = float((qs.get("delay_s") or ["1.6"])[0])
                    sent = demo_pool.burst_tasks(task_type=tp, count=cnt, delay_s=delay)
                    return self._send(200, {"ok": True, "action": "burst", "task_type": tp, "sent": sent})
                if act == "workflow":
                    wf_id = demo_pool.launch_demo_workflow()
                    return self._send(200, {"ok": True, "action": "workflow", "workflow": wf_id})
                if act == "reset":
                    spawned = demo_pool.seed_default_fleet()
                    return self._send(200, {"ok": True, "action": "reset", "agents": spawned})
                return self._send(400, {"error": f"unknown demo action {act!r}"})

            if orch is not None:
                if path == "/workflows":
                    return self._send(200, orch.workflows())
                m = WF.match(parsed.path)
                if m:
                    snap = orch.snapshot(unquote(m.group(1)))
                    return self._send(200, snap) if snap else self._send(404, {"error": "unknown workflow"})
            self._send(404, {"error": "not found"})

        def _no(self):
            self._send(405, {"error": "read-only"})
        do_POST = do_PUT = do_PATCH = do_DELETE = _no

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--listen", default="127.0.0.1:18200")
    ap.add_argument("--nats-url", default="")
    ap.add_argument("--no-seed-agents", action="store_true")
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)

    class NoExec:
        def execute(self, *x):
            raise RuntimeError("the dashboard API never executes tasks")

    orch = Orchestrator(a.dsn, NoExec())
    demo_pool = ElasticDemoPool(a.nats_url, dsn=a.dsn, seed_defaults=not a.no_seed_agents) if a.nats_url else None
    collector = (
        BusMetricsCollector(a.nats_url, executor=demo_pool.bus_executor if demo_pool else None)
        if a.nats_url
        else None
    )
    sup = (
        ElasticSupervisor(
            propose_only=True,
            scale_up_queue_depth=4,
            scale_down_queue_depth=0,
            scale_up_wait_s=1.0,
            scale_down_wait_s=0.1,
            cooldown_s=3.0,
        )
        if a.nats_url
        else None
    )
    ThreadingHTTPServer((host, int(port)), make_handler(orch, collector, sup, demo_pool)).serve_forever()


if __name__ == "__main__":
    main()
