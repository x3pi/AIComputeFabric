"""Durable task bus on NATS JetStream (plan P2.2 / fabric/task-bus).

The orchestrator's Executor interface stays the same; BusExecutor.execute() publishes the task to a
work-queue stream and waits for the reply, a BusWorker on any PC pulls it, runs it with a normal
executor and replies. Delivery is at-least-once: a worker that dies before acknowledging leaves the
message unacknowledged and JetStream hands it to another worker after `ack_wait`, without the
orchestrator noticing. A task may therefore run twice (never in parallel unless a worker is stalled
longer than ack_wait without heartbeating); results are idempotent per step in the orchestrator, and
the first reply wins here.

NOT covered: NATS authentication/TLS (the test server is open on loopback), per-tenant subjects and
quotas, stream replication (single server), back-pressure when no worker is online beyond the timeout."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid

import nats
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError

from agents.orchestrator import WorkerLost
from agents.tasks import Task, TaskResult

STREAM = "AICF_TASKS"
SUBJECT = "aicf.task"


class RemoteError(Exception):
    """The worker ran the task and it failed in a way a retry will not fix (bad input, denied tool...)."""


async def ensure_stream(js, ack_wait: float, max_deliver: int):
    cfg = StreamConfig(name=STREAM, subjects=[SUBJECT + ".>"], retention=RetentionPolicy.WORK_QUEUE, duplicate_window=120)
    try:
        await js.stream_info(STREAM)
    except NotFoundError:
        await js.add_stream(cfg)


class BusExecutor:
    def __init__(self, url: str, task_timeout: float = 60.0, ack_wait: float = 10.0, max_deliver: int = 5):
        self.task_timeout, self.ack_wait, self.max_deliver = task_timeout, ack_wait, max_deliver
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.run_id = uuid.uuid4().hex[:8]
        self.nc = self._run(nats.connect(url))
        self.js = self.nc.jetstream()
        self._run(ensure_stream(self.js, ack_wait, max_deliver))

    def _run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        return self._run(self._execute(task, attempt, deps or {}), self.task_timeout + 15)

    async def _execute(self, task: Task, attempt: int, deps: dict) -> TaskResult:
        inbox = self.nc.new_inbox()
        got: asyncio.Queue = asyncio.Queue()
        async def on_reply(m):
            got.put_nowait(m.data)
        sub = await self.nc.subscribe(inbox, cb=on_reply)
        try:
            body = json.dumps({"task": task.model_dump(mode="json"), "attempt": attempt, "deps": deps, "reply": inbox})
            await self.js.publish(f"{SUBJECT}.{task.type.value}", body.encode(),
                                  headers={"Nats-Msg-Id": f"{self.run_id}/{task.task_id}/{attempt}"})
            try:
                raw = await asyncio.wait_for(got.get(), self.task_timeout)
            except asyncio.TimeoutError:
                raise WorkerLost(f"{task.task_id}: no reply in {self.task_timeout}s") from None
            reply = json.loads(raw)
            if not reply["ok"]:
                raise RemoteError(f"{task.task_id}: {reply['error']}")
            return TaskResult.model_validate(reply["result"])
        finally:
            await sub.unsubscribe()          # a second reply (task ran twice) goes nowhere: first one wins

    def close(self):
        self._run(self.nc.drain())
        self.loop.call_soon_threadsafe(self.loop.stop)


class BusWorker:
    """Pulls tasks, runs them with `executor` (any object with the Executor interface) and replies."""
    def __init__(self, url: str, executor, name: str = "worker", ack_wait: float = 10.0, max_deliver: int = 5, types: list | None = None):
        self.url, self.executor, self.name = url, executor, name
        self.ack_wait, self.max_deliver, self.types = ack_wait, max_deliver, types
        self.executed = 0
        self.stop = threading.Event()

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        nc = await nats.connect(self.url)
        js = nc.jetstream()
        await ensure_stream(js, self.ack_wait, self.max_deliver)
        subj = f"{SUBJECT}.>" if not self.types else None
        subs = []
        if subj:
            subs.append(await js.pull_subscribe(subj, durable="workers", config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, ack_wait=self.ack_wait, max_deliver=self.max_deliver)))
        else:
            for t in self.types:
                subs.append(await js.pull_subscribe(f"{SUBJECT}.{t}", durable=f"workers-{t}", config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, ack_wait=self.ack_wait, max_deliver=self.max_deliver)))
        while not self.stop.is_set():
            for sub in subs:
                try:
                    msgs = await sub.fetch(1, timeout=0.5)
                except (asyncio.TimeoutError, nats.errors.TimeoutError):
                    continue
                for m in msgs:
                    try:
                        await self._handle(nc, m)
                    except Exception as e:   # one bad message or a failed ack must never take the worker down
                        print(f"[{self.name}] dropping message: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                        try:
                            await m.term()
                        except Exception:
                            pass
        await nc.drain()

    async def _handle(self, nc, m):
        req = json.loads(m.data)
        task = Task.model_validate(req["task"])

        async def beat():            # keeps JetStream from redelivering a task that is merely slow
            while True:
                await asyncio.sleep(self.ack_wait / 3)
                await m.in_progress()
        hb = asyncio.create_task(beat())
        try:
            res = await asyncio.to_thread(self.executor.execute, task, req["attempt"], req["deps"])
            self.executed += 1
            await nc.publish(req["reply"], json.dumps({"ok": True, "result": res.model_dump(mode="json")}).encode())
            await m.ack()
        except WorkerLost:            # this machine lost its GPU: let another one take it
            await m.nak(delay=0.5)
        except Exception as e:        # a retry would fail the same way: tell the caller and stop redelivery
            await nc.publish(req["reply"], json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}).encode())
            await m.term()
        finally:
            hb.cancel()
