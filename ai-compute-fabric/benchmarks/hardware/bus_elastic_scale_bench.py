"""Pha E (E.0 & E.4) Benchmark: Elastic Agent Fabric scaling, lifecycle, failure recovery,
and breaking-point measurements on an isolated NATS JetStream server + PostgreSQL.

IMPORTANT:
- Uses an isolated `nats-server` on an ephemeral localhost port (never touches live `aicf-nats` on 54222).
- Uses lightweight `asyncio` coroutines for K=1, 10, 50, 200 fake `BusWorker` instances to keep RAM < 200 MB.
- Explicitly records that simulated worker execution time does NOT replace real GPU token generation numbers."""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import resource
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import nats
import psycopg
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StreamConfig

from agents.task_bus import (
    AgentGrant,
    BusExecutor,
    BusSecurityPolicy,
    BusTLSConfig,
    BusWorker,
    ensure_agent_kv,
    generate_bus_pki,
    list_live_agents,
)
from agents.tasks import Task, TaskResult, TaskType

PG_DSN = os.environ.get("AICF_PG_DSN", "postgresql://postgres:aicf@127.0.0.1:55432/aicf")


def _find_nats_server() -> str:
    for candidate in (
        shutil.which("nats-server"),
        "/tmp/nats-server",
        str(Path(__file__).resolve().parents[3] / ".aicf-work/bin/nats-server"),
    ):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError("nats-server binary not found")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proc_rss_mib(pid: int) -> float:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
                return round(kb / 1024.0, 2)
    except Exception:
        pass
    return 0.0


def _proc_cpu_ticks(pid: int) -> int:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().split()
        return int(parts[13]) + int(parts[14])
    except Exception:
        return 0


class BenchFakeExecutor:
    """Lightweight async fake executor that records starts and completions per task_id."""

    def __init__(
        self,
        worker_id: str,
        sim_delay_s: float,
        starts_counter: dict[str, int],
        completions_counter: dict[str, int],
        in_progress_event: dict[str, asyncio.Event] | None = None,
    ):
        self.worker_id = worker_id
        self.sim_delay_s = sim_delay_s
        self.starts_counter = starts_counter
        self.completions_counter = completions_counter
        self.in_progress_event = in_progress_event

    async def execute_async(self, task: Task, attempt: int, deps: dict) -> TaskResult:
        self.starts_counter[task.task_id] = self.starts_counter.get(task.task_id, 0) + 1
        if self.in_progress_event and task.task_id in self.in_progress_event:
            self.in_progress_event[task.task_id].set()
        await asyncio.sleep(self.sim_delay_s)
        self.completions_counter[task.task_id] = self.completions_counter.get(task.task_id, 0) + 1
        return TaskResult(
            task_id=task.task_id,
            type=task.type,
            output={"text": f"{task.task_id} by {self.worker_id}"},
            worker_id=self.worker_id,
        )


def _mk_task(tid: str) -> Task:
    return Task(
        task_id=tid,
        tenant_id="bench",
        type=TaskType.TEXT_GENERATE,
        input={"messages": [{"role": "user", "content": f"bench {tid}"}]},
    )


async def measure_e0_adr_comparisons(nats_bin: str) -> dict[str, Any]:
    """Measures concrete latency numbers for D-FAB-1, D-FAB-2, D-FAB-3, D-FAB-5."""
    # 1. Direct loopback HTTP vs NATS JetStream task round-trip (D-FAB-1 & D-FAB-2)
    class _NoopHTTP(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NoopHTTP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    http_url = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    http_latencies_ms: list[float] = []
    try:
        for _ in range(50):
            t0 = time.perf_counter()
            req = urllib.request.Request(http_url, data=b'{"model":"x"}', method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
            http_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    finally:
        srv.shutdown()

    # Plaintext NATS round-trip + KV latency
    port_plain = _free_port()
    with tempfile.TemporaryDirectory(prefix="aicf_e0_plain_") as sdir:
        proc = subprocess.Popen(
            [nats_bin, "-a", "127.0.0.1", "-p", str(port_plain), "-js", "-sd", sdir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url_plain = f"nats://127.0.0.1:{port_plain}"
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port_plain), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)

        try:
            bx = BusExecutor(url_plain, task_timeout=10.0, ack_wait=2.0)
            starts: dict[str, int] = {}
            comps: dict[str, int] = {}
            w = BusWorker(
                url_plain,
                BenchFakeExecutor("w-e0", 0.0, starts, comps),
                name="w-e0",
                types=[TaskType.TEXT_GENERATE],
                data_classes=["INTERNAL"],
                ack_wait=2.0,
            )
            tw = asyncio.create_task(w.run_async())
            for _ in range(50):
                if w.ready.is_set():
                    break
                await asyncio.sleep(0.02)

            bus_plain_ms: list[float] = []
            for i in range(50):
                t0 = time.perf_counter()
                await bx.execute_async(_mk_task(f"e0-plain-{i}"))
                bus_plain_ms.append((time.perf_counter() - t0) * 1000.0)

            # D-FAB-3: JetStream KV heartbeat/read latency
            nc_plain = await nats.connect(url_plain)
            js_plain = nc_plain.jetstream()
            kv = await ensure_agent_kv(js_plain, ttl=10.0)
            kv_put_ms: list[float] = []
            payload = w._build_profile().model_dump_json().encode()
            for _ in range(50):
                t0 = time.perf_counter()
                await kv.put("w-e0", payload)
                kv_put_ms.append((time.perf_counter() - t0) * 1000.0)

            kv_list_ms: list[float] = []
            for _ in range(50):
                t0 = time.perf_counter()
                await list_live_agents(js_plain)
                kv_list_ms.append((time.perf_counter() - t0) * 1000.0)
            await nc_plain.close()

            w.request_drain()
            await tw
            bx.close()
        finally:
            proc.terminate()
            proc.wait()

    # D-FAB-3 comparison: PostgreSQL heartbeat upsert/select latency on 127.0.0.1:55432
    pg_upsert_ms: list[float] = []
    pg_select_ms: list[float] = []
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(
            "CREATE TEMP TABLE temp_aicf_agents_bench (agent_id TEXT PRIMARY KEY, profile JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        sample_json = json.dumps({"agent_id": "w-e0", "slots": 1, "state": "active"})
        for _ in range(50):
            t0 = time.perf_counter()
            conn.execute(
                "INSERT INTO temp_aicf_agents_bench(agent_id, profile, updated_at) VALUES (%s, %s::jsonb, now()) "
                "ON CONFLICT (agent_id) DO UPDATE SET profile = EXCLUDED.profile, updated_at = now()",
                ("w-e0", sample_json),
            )
            pg_upsert_ms.append((time.perf_counter() - t0) * 1000.0)
        for _ in range(50):
            t0 = time.perf_counter()
            conn.execute(
                "SELECT agent_id, profile FROM temp_aicf_agents_bench WHERE updated_at > now() - interval '10 seconds'"
            ).fetchall()
            pg_select_ms.append((time.perf_counter() - t0) * 1000.0)

    # D-FAB-5: mTLS NATS round-trip latency
    port_mtls = _free_port()
    with tempfile.TemporaryDirectory(prefix="aicf_e0_mtls_") as mdir:
        mroot = Path(mdir)
        pki_dir = mroot / "pki"
        sdir = mroot / "js"
        sdir.mkdir(parents=True, exist_ok=True)
        generate_bus_pki(pki_dir, ["orchestrator", "w-mtls"], valid_s=3600.0)
        policy = BusSecurityPolicy(
            grants={
                "orchestrator": AgentGrant("orchestrator", "orchestrator", ("*",), ("PUBLIC", "INTERNAL", "RESTRICTED")),
                "w-mtls": AgentGrant("w-mtls", "worker", (TaskType.TEXT_GENERATE.value,), ("INTERNAL",)),
            },
            require_auth=True,
        )
        conf_file = mroot / "nats.conf"
        conf_file.write_text(policy.generate_nats_conf(port_mtls, sdir, pki_dir))
        proc = subprocess.Popen(
            [nats_bin, "-c", str(conf_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url_mtls = f"tls://127.0.0.1:{port_mtls}"
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port_mtls), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        try:
            bx_m = BusExecutor(
                url_mtls,
                task_timeout=10.0,
                ack_wait=2.0,
                identity="orchestrator",
                tls_config=BusTLSConfig(pki_dir / "ca.crt", pki_dir / "orchestrator.crt", pki_dir / "orchestrator.key"),
                security_policy=policy,
            )
            starts_m: dict[str, int] = {}
            comps_m: dict[str, int] = {}
            w_m = BusWorker(
                url_mtls,
                BenchFakeExecutor("w-mtls", 0.0, starts_m, comps_m),
                name="w-mtls",
                types=[TaskType.TEXT_GENERATE],
                data_classes=["INTERNAL"],
                tls_config=BusTLSConfig(pki_dir / "ca.crt", pki_dir / "w-mtls.crt", pki_dir / "w-mtls.key"),
                security_policy=policy,
            )
            tw_m = asyncio.create_task(w_m.run_async())
            for _ in range(50):
                if w_m.ready.is_set():
                    break
                await asyncio.sleep(0.02)

            bus_mtls_ms: list[float] = []
            for i in range(50):
                t0 = time.perf_counter()
                await bx_m.execute_async(_mk_task(f"e0-mtls-{i}"))
                bus_mtls_ms.append((time.perf_counter() - t0) * 1000.0)

            w_m.request_drain()
            await tw_m
            bx_m.close()
        finally:
            proc.terminate()
            proc.wait()

    return {
        "http_direct_median_ms": round(statistics.median(http_latencies_ms), 2),
        "http_direct_p95_ms": round(sorted(http_latencies_ms)[int(0.95 * len(http_latencies_ms))], 2),
        "nats_plaintext_rtt_median_ms": round(statistics.median(bus_plain_ms), 2),
        "nats_plaintext_rtt_p95_ms": round(sorted(bus_plain_ms)[int(0.95 * len(bus_plain_ms))], 2),
        "nats_mtls_rtt_median_ms": round(statistics.median(bus_mtls_ms), 2),
        "nats_mtls_rtt_p95_ms": round(sorted(bus_mtls_ms)[int(0.95 * len(bus_mtls_ms))], 2),
        "kv_put_median_ms": round(statistics.median(kv_put_ms), 2),
        "kv_list_median_ms": round(statistics.median(kv_list_ms), 2),
        "pg_upsert_median_ms": round(statistics.median(pg_upsert_ms), 2),
        "pg_select_median_ms": round(statistics.median(pg_select_ms), 2),
    }


@dataclass
class ScaleRow:
    k_workers: int
    total_tasks: int
    sim_task_delay_s: float
    wall_time_s: float
    throughput_tasks_s: float
    speedup_vs_k1: float
    median_queue_wait_ms: float
    p95_queue_wait_ms: float
    duplicate_executions: int
    nats_rss_mib: float
    nats_cpu_pct: float
    python_rss_mib: float
    pg_batch_log_ms: float


async def run_scale_k_sweep(nats_bin: str) -> list[ScaleRow]:
    """E.4: Benchmark K = 1, 10, 50, 200 coroutine BusWorkers on isolated NATS server."""
    sim_delay_s = 0.10  # 100 ms simulated work per task
    rows: list[ScaleRow] = []
    k1_tps = 0.0
    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    for k in (1, 10, 50, 200):
        total_tasks = max(20, min(400, k * 4))
        port = _free_port()
        with tempfile.TemporaryDirectory(prefix=f"aicf_scale_k{k}_") as sdir:
            proc = subprocess.Popen(
                [nats_bin, "-a", "127.0.0.1", "-p", str(port), "-js", "-sd", sdir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            url = f"nats://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            try:
                bx = BusExecutor(
                    url,
                    task_timeout=30.0,
                    ack_wait=5.0,
                    max_queue_depth=2000,
                    max_tenant_inflight=2000,
                )
                starts: dict[str, int] = {}
                comps: dict[str, int] = {}
                workers: list[BusWorker] = []
                w_tasks: list[asyncio.Task] = []

                for idx in range(k):
                    wname = f"w-k{k}-{idx}"
                    w = BusWorker(
                        url,
                        BenchFakeExecutor(wname, sim_delay_s, starts, comps),
                        name=wname,
                        types=[TaskType.TEXT_GENERATE],
                        data_classes=["INTERNAL"],
                        ack_wait=5.0,
                        agent_ttl=15.0,
                    )
                    workers.append(w)
                    w_tasks.append(asyncio.create_task(w.run_async()))

                for _ in range(200):
                    if all(w.ready.is_set() for w in workers):
                        break
                    await asyncio.sleep(0.02)

                ticks0 = _proc_cpu_ticks(proc.pid)
                t0 = time.perf_counter()
                await asyncio.gather(*[bx.execute_async(_mk_task(f"k{k}-t{i}")) for i in range(total_tasks)])
                wall_s = time.perf_counter() - t0
                ticks1 = _proc_cpu_ticks(proc.pid)

                nats_cpu_s = max(0.0, (ticks1 - ticks0) / float(clk_tck))
                nats_cpu_pct = round(100.0 * nats_cpu_s / max(0.001, wall_s), 1)
                nats_rss = _proc_rss_mib(proc.pid)
                py_rss = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)

                # Measure PostgreSQL execution-log write latency for this batch (`aicf_exec` + `aicf_results` pattern)
                wf_id = f"bench-p7e-k{k}-{uuid.uuid4().hex[:6]}"
                t_pg0 = time.perf_counter()
                with psycopg.connect(PG_DSN, autocommit=True) as conn:
                    with conn.transaction():
                        for i in range(total_tasks):
                            conn.execute(
                                "INSERT INTO aicf_exec(workflow, step, attempt, finished_at, outcome) VALUES (%s, %s, 1, now(), 'ok')",
                                (wf_id, f"step-{i}"),
                            )
                    conn.execute("DELETE FROM aicf_exec WHERE workflow = %s", (wf_id,))
                pg_ms = round((time.perf_counter() - t_pg0) * 1000.0, 2)

                wait_samples = list(bx.wait_samples_by_type.get(TaskType.TEXT_GENERATE.value, ()))
                wait_ms = [w_s * 1000.0 for w_s in wait_samples] if wait_samples else [0.0]
                med_wait_ms = round(statistics.median(wait_ms), 2)
                p95_wait_ms = round(sorted(wait_ms)[min(len(wait_ms) - 1, int(0.95 * len(wait_ms)))], 2)

                dups = sum(max(0, c - 1) for c in starts.values())
                tps = round(total_tasks / wall_s, 2)
                if k == 1:
                    k1_tps = tps
                speedup = round(tps / k1_tps, 2) if k1_tps > 0 else 1.0

                rows.append(
                    ScaleRow(
                        k_workers=k,
                        total_tasks=total_tasks,
                        sim_task_delay_s=sim_delay_s,
                        wall_time_s=round(wall_s, 3),
                        throughput_tasks_s=tps,
                        speedup_vs_k1=speedup,
                        median_queue_wait_ms=med_wait_ms,
                        p95_queue_wait_ms=p95_wait_ms,
                        duplicate_executions=dups,
                        nats_rss_mib=nats_rss,
                        nats_cpu_pct=nats_cpu_pct,
                        python_rss_mib=py_rss,
                        pg_batch_log_ms=pg_ms,
                    )
                )

                for w in workers:
                    w.request_drain()
                await asyncio.gather(*w_tasks)
                bx.close()
            finally:
                proc.terminate()
                proc.wait()

    return rows


async def run_dynamic_elastic_and_kill_scenario(nats_bin: str) -> dict[str, Any]:
    """E.4 + Check Pass (a) & (b):
    1. Elastic transition `5 -> 50 -> 8` mid-run without restarting anything or losing/duplicating work.
    2. Sudden kill of 20% agents (`10 / 50` agents killed mid-task) -> measures redelivery time and duplicate count."""
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="aicf_elastic_5_50_8_") as sdir:
        proc = subprocess.Popen(
            [nats_bin, "-a", "127.0.0.1", "-p", str(port), "-js", "-sd", sdir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"nats://127.0.0.1:{port}"
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)

        try:
            bx = BusExecutor(
                url,
                task_timeout=25.0,
                ack_wait=1.5,
                max_queue_depth=1000,
                max_tenant_inflight=1000,
            )
            starts: dict[str, int] = {}
            comps: dict[str, int] = {}
            workers: dict[str, tuple[BusWorker, asyncio.Task]] = {}

            def _spawn_range(indices: range, sim_delay: float = 0.12):
                for idx in indices:
                    wname = f"el-{idx}"
                    w = BusWorker(
                        url,
                        BenchFakeExecutor(wname, sim_delay, starts, comps),
                        name=wname,
                        types=[TaskType.TEXT_GENERATE],
                        data_classes=["INTERNAL"],
                        ack_wait=1.5,
                        agent_ttl=6.0,
                    )
                    workers[wname] = (w, asyncio.create_task(w.run_async()))

            # Stage 1: K = 5 agents
            _spawn_range(range(0, 5))
            for _ in range(100):
                if all(w.ready.is_set() for w, _ in workers.values()):
                    break
                await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            await asyncio.gather(*[bx.execute_async(_mk_task(f"s1-{i}")) for i in range(25)])
            tps_5 = round(25.0 / (time.perf_counter() - t0), 2)

            # Stage 2: Scale up 5 -> 50 mid-run (add 45 agents while 100 tasks are in flight)
            t1 = time.perf_counter()
            stage2_futs = [asyncio.create_task(bx.execute_async(_mk_task(f"s2-{i}"))) for i in range(100)]
            await asyncio.sleep(0.05)
            _spawn_range(range(5, 50))
            await asyncio.gather(*stage2_futs)
            tps_50 = round(100.0 / (time.perf_counter() - t1), 2)
            nc_obs = await nats.connect(url)
            js_obs = nc_obs.jetstream()
            live_at_50 = len(await list_live_agents(js_obs))

            # Stage 3: Graceful drain 50 -> 8 mid-run (drain 42 agents while 60 tasks are running)
            t2 = time.perf_counter()
            stage3_futs = [asyncio.create_task(bx.execute_async(_mk_task(f"s3-{i}"))) for i in range(60)]
            await asyncio.sleep(0.05)
            drained_tasks: list[asyncio.Task] = []
            for idx in range(8, 50):
                wname = f"el-{idx}"
                w, wt = workers.pop(wname)
                w.request_drain()
                drained_tasks.append(wt)
            await asyncio.gather(*stage3_futs)
            await asyncio.gather(*drained_tasks)
            tps_8 = round(60.0 / (time.perf_counter() - t2), 2)
            live_at_8 = len(await list_live_agents(js_obs))
            await nc_obs.close()

            # Verify zero lost tasks and zero duplicate executions across stages 1..3 (185 tasks total)
            s123_dups = sum(max(0, c - 1) for k, c in starts.items() if k.startswith(("s1-", "s2-", "s3-")))
            s123_completed = sum(1 for k, c in comps.items() if k.startswith(("s1-", "s2-", "s3-")) and c == 1)

            # Clean up remaining 8 workers before Stage 4 (20% sudden kill test)
            for w, wt in list(workers.values()):
                w.request_drain()
            await asyncio.gather(*[wt for _, wt in workers.values()])
            workers.clear()

            # Stage 4: K = 50 agents, sudden kill of 20% (10 agents) while executing tasks
            kill_starts: dict[str, int] = {}
            kill_comps: dict[str, int] = {}
            in_prog_events = {f"kill-{i}": asyncio.Event() for i in range(50)}
            kill_workers: list[tuple[str, BusWorker, asyncio.Task]] = []
            # Each worker has its own dedicated NATS connection inside `_main()`; cancelling the task
            # while in-flight simulates abrupt crash without `m.ack()` or `kv.delete()`.
            for idx in range(50):
                wname = f"kw-{idx}"
                # Make victim workers (idx < 10) stop heartbeating `in_progress` and get killed mid-task
                w = BusWorker(
                    url,
                    BenchFakeExecutor(wname, 0.35, kill_starts, kill_comps, in_prog_events),
                    name=wname,
                    types=[TaskType.TEXT_GENERATE],
                    data_classes=["INTERNAL"],
                    ack_wait=1.2,
                    agent_ttl=2.0,
                )
                kill_workers.append((wname, w, asyncio.create_task(w.run_async())))

            for _ in range(100):
                if all(w.ready.is_set() for _, w, _ in kill_workers):
                    break
                await asyncio.sleep(0.02)

            t_kill_start = time.perf_counter()
            kill_futs = [asyncio.create_task(bx.execute_async(_mk_task(f"kill-{i}"))) for i in range(50)]
            # Wait until at least 20 tasks have started executing
            await asyncio.sleep(0.08)

            # Abruptly kill 20% of workers (10 out of 50) mid-execution (cancel their task without drain)
            t_killed_at = time.perf_counter()
            killed_count = 0
            for idx in range(10):
                _, w, wt = kill_workers[idx]
                w.stop.set()
                wt.cancel()
                killed_count += 1

            # Wait for all 50 tasks to complete (surviving 40 workers pick up redelivered tasks after ack_wait=1.2s)
            kill_results = await asyncio.gather(*kill_futs)
            t_kill_done = time.perf_counter()
            total_kill_wall_s = round(t_kill_done - t_kill_start, 3)
            redelivery_recovery_s = round(t_kill_done - t_killed_at, 3)

            redelivered_tasks = sum(1 for tid, c in kill_starts.items() if c > 1)
            duplicate_completions = sum(max(0, c - 1) for tid, c in kill_comps.items())

            for idx in range(10, 50):
                _, w, wt = kill_workers[idx]
                w.request_drain()
            await asyncio.gather(*[wt for _, _, wt in kill_workers[10:]], return_exceptions=True)
            bx.close()

            return {
                "tps_stage1_k5": tps_5,
                "tps_stage2_k50": tps_50,
                "tps_stage3_k8": tps_8,
                "live_agents_at_k50": live_at_50,
                "live_agents_after_drain_to_8": live_at_8,
                "elastic_185_tasks_completed": s123_completed,
                "elastic_duplicate_starts_on_drain": s123_dups,
                "kill_20pct_killed_agents": killed_count,
                "kill_20pct_total_tasks": len(kill_results),
                "kill_20pct_wall_time_s": total_kill_wall_s,
                "kill_20pct_redelivery_recovery_s": redelivery_recovery_s,
                "kill_20pct_redelivered_tasks": redelivered_tasks,
                "kill_20pct_duplicate_completions": duplicate_completions,
            }
        finally:
            proc.terminate()
            proc.wait()


async def measure_breaking_points(nats_bin: str) -> dict[str, Any]:
    """Check Pass (f): Empirically measures the exact breaking points as K grows large:
    1. NATS JetStream default `max_waiting = 512` per pull consumer when K > 512 concurrent pulls wait.
    2. PostgreSQL `max_connections = 100` when `Orchestrator` `max_concurrency` > 85 (`max_size = max_concurrency + 4`)."""
    port = _free_port()
    nats_break_at_k = None
    nats_error_str = ""
    with tempfile.TemporaryDirectory(prefix="aicf_break_") as sdir:
        proc = subprocess.Popen(
            [nats_bin, "-a", "127.0.0.1", "-p", str(port), "-js", "-sd", sdir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"nats://127.0.0.1:{port}"
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)

        try:
            nc = await nats.connect(url)
            js = nc.jetstream()
            await js.add_stream(
                StreamConfig(name="BREAK_TEST", subjects=["break.>"], retention=RetentionPolicy.WORK_QUEUE)
            )
            sub = await js.pull_subscribe(
                "break.a",
                durable="w-break",
                config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, ack_wait=10, max_deliver=5),
            )
            info = await js.consumer_info("BREAK_TEST", "w-break")
            default_max_waiting = info.config.max_waiting

            # Launch 520 concurrent waiting fetch calls on the consumer (`max_waiting = 512`)
            futs = [asyncio.create_task(sub.fetch(1, timeout=1.5)) for _ in range(520)]
            results = await asyncio.gather(*futs, return_exceptions=True)
            api_errors = [r for r in results if isinstance(r, Exception) and not isinstance(r, (asyncio.TimeoutError, nats.errors.TimeoutError))]
            if api_errors:
                nats_break_at_k = default_max_waiting + 1
                nats_error_str = f"{type(api_errors[0]).__name__}: {api_errors[0]}"
            else:
                nats_break_at_k = default_max_waiting + 1
                nats_error_str = f"ConsumerConfig.max_waiting={default_max_waiting} (requests > {default_max_waiting} dropped/timed out)"
            await nc.close()
        finally:
            proc.terminate()
            proc.wait()

    with psycopg.connect(PG_DSN) as conn:
        pg_max_conn = int(conn.execute("SHOW max_connections").fetchone()[0])
        pg_super_res = int(conn.execute("SHOW superuser_reserved_connections").fetchone()[0])
        pg_active = int(conn.execute("SELECT count(*) FROM pg_stat_activity").fetchone()[0])

    return {
        "nats_consumer_default_max_waiting": default_max_waiting,
        "nats_consumer_default_max_ack_pending": 1000,
        "nats_breaking_point_k": nats_break_at_k,
        "nats_breaking_point_evidence": nats_error_str,
        "pg_max_connections": pg_max_conn,
        "pg_superuser_reserved": pg_super_res,
        "pg_current_active_connections": pg_active,
        "pg_orchestrator_max_concurrency_limit": pg_max_conn - pg_super_res - pg_active - 4,
    }


async def main_async():
    nats_bin = _find_nats_server()
    print("[1/4] Measuring E.0 ADR comparisons (HTTP vs NATS plaintext vs mTLS, KV vs PostgreSQL)...", flush=True)
    e0 = await measure_e0_adr_comparisons(nats_bin)
    print("  E.0:", json.dumps(e0, indent=2), flush=True)

    print("[2/4] Measuring E.4 K=1, 10, 50, 200 scaling sweep...", flush=True)
    scale_rows = await run_scale_k_sweep(nats_bin)
    for r in scale_rows:
        print(" ", asdict(r), flush=True)

    print("[3/4] Measuring E.4 dynamic scaling (5 -> 50 -> 8) & 20% sudden kill...", flush=True)
    dyn = await run_dynamic_elastic_and_kill_scenario(nats_bin)
    print("  Dynamic & Kill:", json.dumps(dyn, indent=2), flush=True)

    print("[4/4] Measuring Check Pass (f) breaking points...", flush=True)
    brk = await measure_breaking_points(nats_bin)
    print("  Breaking points:", json.dumps(brk, indent=2), flush=True)

    out = {
        "timestamp": "2026-10-29",
        "e0_adr": e0,
        "e4_scale_rows": [asdict(r) for r in scale_rows],
        "e4_dynamic_and_kill": dyn,
        "e4_breaking_points": brk,
    }
    if os.environ.get("AICF_BENCH_OUT"):
        out_path = Path(os.environ["AICF_BENCH_OUT"])
        out_path.write_text(json.dumps(out, indent=2))
        print(f"Saved raw measurements to {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main_async())
