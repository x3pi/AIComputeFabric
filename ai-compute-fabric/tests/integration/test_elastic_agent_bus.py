"""Integration tests for Pha E (E.1, E.3): Elastic Agent Fabric lifecycle & observability.

Covers:
- AICF_AGENTS JetStream KV registry with capabilities (labels, models, slots, ctx_per_slot, tok_s_ref, task_types).
- Lifecycle: join -> heartbeat -> graceful drain (no lost work, no duplicate work) -> crash TTL expiry & redelivery.
- Subject routing `aicf.task.<type>[.<label>]` with backward compatibility.
- Adding agents mid-run increases throughput without central config changes or restarts (Check Pass a).
- Prometheus `/metrics` exposition and propose-only `ElasticSupervisor` with anti-oscillation cooldown (E.3)."""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import nats
import pytest

from agents.dag_api import make_handler
from agents.task_bus import (
    CONTROL_SUBJECT,
    BusExecutor,
    BusMetricsCollector,
    BusMetricsSnapshot,
    BusWorker,
    ElasticSupervisor,
    list_live_agents,
    task_subject,
)
from agents.tasks import Task, TaskResult, TaskType


def _find_nats_server() -> str | None:
    for candidate in (
        shutil.which("nats-server"),
        "/tmp/nats-server",
        str(Path(__file__).resolve().parents[3] / ".aicf-work/bin/nats-server"),
    ):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def isolated_nats():
    bin_path = _find_nats_server()
    if not bin_path:
        pytest.skip("nats-server binary not found")
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="aicf_nats_e1_") as store_dir:
        proc = subprocess.Popen(
            [bin_path, "-a", "127.0.0.1", "-p", str(port), "-js", "-sd", store_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"nats://127.0.0.1:{port}"
        # Wait for port ready
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        try:
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class AsyncFakeExecutor:
    def __init__(self, worker_id: str, delay_s: float = 0.05):
        self.worker_id = worker_id
        self.delay_s = delay_s
        self.starts: list[str] = []
        self.completions: list[str] = []

    async def execute_async(self, task: Task, attempt: int, deps: dict) -> TaskResult:
        self.starts.append(task.task_id)
        await asyncio.sleep(self.delay_s)
        self.completions.append(task.task_id)
        return TaskResult(
            task_id=task.task_id,
            type=task.type,
            output={"text": f"{task.task_id} by {self.worker_id}"},
            worker_id=self.worker_id,
        )


def _mk_task(tid: str, tp: TaskType = TaskType.TEXT_GENERATE, label: str | None = None, data_class: str = "INTERNAL") -> Task:
    return Task(
        task_id=tid,
        tenant_id="acme",
        type=tp,
        input={"messages": [{"role": "user", "content": f"prompt {tid}"}]},
        label=label,
        data_class=data_class,  # type: ignore[arg-type]
    )


def test_agent_registry_join_heartbeat_drain_and_ttl_expiry(isolated_nats):
    """E.1 + Check Pass (b): Join writes capabilities to AICF_AGENTS KV, heartbeat keeps alive,
    graceful drain finishes in-flight task without loss or duplicate and deletes KV key,
    and simulated crash expires via TTL."""

    async def _scenario():
        bx = BusExecutor(isolated_nats, task_timeout=15.0, ack_wait=2.0)
        nc = await nats.connect(isolated_nats)
        js = nc.jetstream()
        try:
            ex1 = AsyncFakeExecutor("agent-code-1", delay_s=0.4)
            w1 = BusWorker(
                isolated_nats,
                ex1,
                name="agent-code-1",
                ack_wait=2.0,
                types=[TaskType.TEXT_GENERATE],
                kind="compute",
                labels=["code", "qwen"],
                models=["Qwen3-Coder-30B-A3B"],
                slots=2,
                ctx_per_slot=32768,
                tok_s_ref=130.0,
                agent_ttl=1.2,
            )
            t_w1 = asyncio.create_task(w1.run_async())
            for _ in range(50):
                if w1.ready.is_set():
                    break
                await asyncio.sleep(0.05)
            assert w1.ready.is_set()

            # 1. Verify join & capability declaration in AICF_AGENTS KV
            agents = await list_live_agents(js, task_type=TaskType.TEXT_GENERATE, label="code")
            assert len(agents) == 1
            a1 = agents[0]
            assert a1.agent_id == "agent-code-1"
            assert a1.kind == "compute"
            assert a1.labels == ["code", "qwen"]
            assert a1.models == ["Qwen3-Coder-30B-A3B"]
            assert a1.slots == 2
            assert a1.ctx_per_slot == 32768
            assert a1.tok_s_ref == 130.0

            # 2. Verify heartbeat keeps agent alive beyond initial agent_ttl (1.2s)
            await asyncio.sleep(1.5)
            agents_after_hb = await list_live_agents(js)
            assert [a.agent_id for a in agents_after_hb] == ["agent-code-1"]

            # 3. Trigger graceful drain mid-task: in-flight task must finish once, KV entry deleted
            task_fut = asyncio.create_task(bx.execute_async(_mk_task("drain-task-1", label="code")))
            for _ in range(50):
                if "drain-task-1" in ex1.starts:
                    break
                await asyncio.sleep(0.02)
            assert "drain-task-1" in ex1.starts

            # Send remote drain via NATS control subject
            await nc.publish(f"{CONTROL_SUBJECT}.agent-code-1", b"drain")
            res = await task_fut
            assert res.output["text"] == "drain-task-1 by agent-code-1"
            await asyncio.wait_for(t_w1, timeout=5.0)
            assert ex1.starts.count("drain-task-1") == 1
            assert ex1.completions.count("drain-task-1") == 1

            # KV entry must be gone immediately after drain
            assert await list_live_agents(js) == []

            # 4. Simulate sudden crash (cancel coroutine without KV delete) -> TTL expires entry
            ex2 = AsyncFakeExecutor("agent-crash-2", delay_s=0.1)
            w2 = BusWorker(
                isolated_nats,
                ex2,
                name="agent-crash-2",
                ack_wait=1.5,
                types=[TaskType.TEXT_GENERATE],
                agent_ttl=0.9,
            )
            t_w2 = asyncio.create_task(w2.run_async())
            for _ in range(50):
                if w2.ready.is_set():
                    break
                await asyncio.sleep(0.02)
            assert len(await list_live_agents(js)) == 1
            # Simulate abrupt network/process loss before KV delete by cancelling and waiting > agent_ttl
            w2.stop.set()
            # Re-insert a stale KV record as if the process died with SIGKILL before cleanup
            await t_w2
            kv = await js.key_value("AICF_AGENTS")
            stale_prof = w2._build_profile()
            await kv.put("agent-crash-2", stale_prof.model_dump_json().encode())
            assert len(await list_live_agents(js)) == 1
            await asyncio.sleep(1.1)
            assert await list_live_agents(js) == []
        finally:
            await nc.close()
            bx.close()

    asyncio.run(_scenario())


def test_label_routing_and_backward_compatibility(isolated_nats):
    """E.1: `aicf.task.<type>[.<label>]` routes labeled tasks only to matching workers while
    preserving unlabeled `aicf.task.<type>` behavior."""
    assert task_subject(TaskType.TEXT_GENERATE) == "aicf.task.TEXT_GENERATE"
    assert task_subject(_mk_task("t1", label="code")) == "aicf.task.TEXT_GENERATE.code"

    async def _scenario():
        bx = BusExecutor(isolated_nats, task_timeout=10.0, ack_wait=2.0)
        ex_general = AsyncFakeExecutor("w-general", delay_s=0.02)
        ex_code = AsyncFakeExecutor("w-code", delay_s=0.02)

        w_general = BusWorker(
            isolated_nats,
            ex_general,
            name="w-general",
            types=[TaskType.TEXT_GENERATE],
            labels=["general"],
        )
        w_code = BusWorker(
            isolated_nats,
            ex_code,
            name="w-code",
            types=[TaskType.TEXT_GENERATE],
            labels=["code"],
            labeled_only=True,
        )
        t_gen = asyncio.create_task(w_general.run_async())
        t_code = asyncio.create_task(w_code.run_async())
        try:
            for _ in range(50):
                if w_general.ready.is_set() and w_code.ready.is_set():
                    break
                await asyncio.sleep(0.02)

            # Labeled "code" tasks must ONLY be executed by w-code
            for i in range(4):
                r = await bx.execute_async(_mk_task(f"code-{i}", label="code"))
                assert r.worker_id == "w-code"

            # Unlabeled tasks must be executed by w-general (since w-code is labeled_only=True)
            for i in range(4):
                r = await bx.execute_async(_mk_task(f"plain-{i}", label=None))
                assert r.worker_id == "w-general"
        finally:
            w_general.request_drain()
            w_code.request_drain()
            await asyncio.gather(t_gen, t_code)
            bx.close()

    asyncio.run(_scenario())


def test_adding_agents_mid_run_increases_throughput_without_restart(isolated_nats):
    """Check Pass (a): Adding agents mid-run increases throughput immediately without modifying
    central config or restarting any existing process."""

    async def _scenario():
        bx = BusExecutor(isolated_nats, task_timeout=20.0, ack_wait=3.0)
        workers: list[BusWorker] = []
        tasks_coros: list[asyncio.Task] = []

        def _add_workers(count: int, prefix: str):
            for i in range(count):
                wname = f"{prefix}-{i}"
                ex = AsyncFakeExecutor(wname, delay_s=0.15)
                w = BusWorker(
                    isolated_nats,
                    ex,
                    name=wname,
                    types=[TaskType.TEXT_GENERATE],
                    ack_wait=3.0,
                )
                workers.append(w)
                tasks_coros.append(asyncio.create_task(w.run_async()))

        try:
            # Phase 1: 1 worker running 6 tasks (0.15s each -> ~0.9s sequential)
            _add_workers(1, "w-init")
            for _ in range(50):
                if all(w.ready.is_set() for w in workers):
                    break
                await asyncio.sleep(0.02)

            t0 = time.perf_counter()
            await asyncio.gather(*[bx.execute_async(_mk_task(f"p1-{i}")) for i in range(6)])
            dt_1 = time.perf_counter() - t0
            tps_1 = 6.0 / dt_1

            # Phase 2: Add 9 more workers on the fly (total 10 workers) WITHOUT restarting bx or w-init-0
            _add_workers(9, "w-added")
            for _ in range(50):
                if all(w.ready.is_set() for w in workers):
                    break
                await asyncio.sleep(0.02)

            t1 = time.perf_counter()
            await asyncio.gather(*[bx.execute_async(_mk_task(f"p2-{i}")) for i in range(20)])
            dt_10 = time.perf_counter() - t1
            tps_10 = 20.0 / dt_10

            # With 10 workers, throughput must be significantly higher (at least 3x) than with 1 worker
            assert tps_10 > tps_1 * 3.0, f"throughput did not scale: 1 worker={tps_1:.2f} t/s, 10 workers={tps_10:.2f} t/s"
        finally:
            for w in workers:
                w.request_drain()
            await asyncio.gather(*tasks_coros)
            bx.close()

    asyncio.run(_scenario())


def test_prometheus_metrics_and_propose_only_supervisor(isolated_nats):
    """E.3: Prometheus `/metrics` format matches `Inference.Metrics` and `ElasticSupervisor` is
    strictly propose-only with hysteresis and anti-oscillation cooldown."""
    # Supervisor must refuse auto-apply / non-propose-only mode (Rule 0 / D-FAB-4)
    with pytest.raises(PermissionError, match="propose_only=True"):
        ElasticSupervisor(propose_only=False)

    async def _scenario():
        bx = BusExecutor(isolated_nats, task_timeout=10.0, ack_wait=2.0)
        ex = AsyncFakeExecutor("w-m1", delay_s=0.05)
        w = BusWorker(isolated_nats, ex, name="w-m1", types=[TaskType.TEXT_GENERATE], ack_wait=2.0)
        tw = asyncio.create_task(w.run_async())
        try:
            for _ in range(50):
                if w.ready.is_set():
                    break
                await asyncio.sleep(0.02)

            await bx.execute_async(_mk_task("m-1"))
            await bx.execute_async(_mk_task("m-2"))

            collector = BusMetricsCollector(isolated_nats, executor=bx)
            snap = await collector.collect_async(bx.js)
            assert snap.live_agents_total == 1
            assert snap.live_agents_by_type["TEXT_GENERATE"] == 1
            assert snap.completed_total == 2
            assert snap.median_wait_s_by_type["TEXT_GENERATE"] >= 0.0

            prom = collector.render_prometheus(snap)
            assert "# HELP aicf_bus_queue_depth" in prom
            assert "# TYPE aicf_bus_queue_depth gauge" in prom
            assert 'aicf_bus_live_agents{task_type="TEXT_GENERATE"} 1' in prom
            assert 'aicf_bus_queue_wait_median_seconds{task_type="TEXT_GENERATE"}' in prom
            assert 'aicf_bus_rejected_total{reason="queue_full"} 0' in prom

            # Verify HTTP /metrics and /supervisor/recommendations endpoint in dag_api
            sup = ElasticSupervisor(
                propose_only=True,
                scale_up_queue_depth=4,
                scale_down_queue_depth=0,
                cooldown_s=30.0,
            )
            srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, collector, sup))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{srv.server_port}"
                with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
                    assert r.status == 200
                    assert "text/plain" in r.headers.get("Content-Type", "")
                    body = r.read().decode()
                    assert 'aicf_bus_live_agents{task_type="TEXT_GENERATE"} 1' in body

                with urllib.request.urlopen(base + "/supervisor/recommendations", timeout=5) as r:
                    assert r.status == 200
                    recs = json.loads(r.read().decode())
                    assert isinstance(recs, list) and len(recs) == 15
                    assert all(item["requires_human_approval"] is True and item["applied"] is False for item in recs)
            finally:
                srv.shutdown()

            # Test supervisor hysteresis & anti-oscillation cooldown directly
            high_snap = BusMetricsSnapshot(
                queue_depth_by_type={"TEXT_GENERATE": 12},
                inflight_by_type={"TEXT_GENERATE": 2},
                redelivered_by_type={"TEXT_GENERATE": 0},
                live_agents_by_type={"TEXT_GENERATE": 2},
                live_agents_total=2,
                median_wait_s_by_type={"TEXT_GENERATE": 3.5},
                rejected_counts={"queue_full": 0},
                completed_total=10,
            )
            r1 = sup.evaluate(high_snap, task_types=[TaskType.TEXT_GENERATE], now=1000.0)[0]
            assert r1.action == "scale_up"
            assert r1.recommended_agents > 2
            assert r1.requires_human_approval is True and r1.applied is False

            # Immediate re-evaluation within cooldown_s (30s) must return noop (anti-oscillation)
            r2 = sup.evaluate(high_snap, task_types=[TaskType.TEXT_GENERATE], now=1010.0)[0]
            assert r2.action == "noop" and "cooldown" in r2.reason

            # After cooldown (t=1040.0), idle queue triggers scale_down
            idle_snap = BusMetricsSnapshot(
                queue_depth_by_type={"TEXT_GENERATE": 0},
                inflight_by_type={"TEXT_GENERATE": 0},
                redelivered_by_type={"TEXT_GENERATE": 0},
                live_agents_by_type={"TEXT_GENERATE": 3},
                live_agents_total=3,
                median_wait_s_by_type={"TEXT_GENERATE": 0.01},
                rejected_counts={"queue_full": 0},
                completed_total=20,
            )
            r3 = sup.evaluate(idle_snap, task_types=[TaskType.TEXT_GENERATE], now=1040.0)[0]
            assert r3.action == "scale_down" and r3.recommended_agents == 2
        finally:
            w.request_drain()
            await tw
            bx.close()

    asyncio.run(_scenario())

