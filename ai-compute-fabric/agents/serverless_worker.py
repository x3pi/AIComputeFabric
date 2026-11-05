"""Serverless worker (ADR-003): executes SERVERLESS_EXEC tasks from the bus inside warm sandboxes.

`ServerlessExecutor.execute()` is what `BusWorker` calls (in a thread). Order of operations is a security property:
  1. parse + verify the HMAC signature, expiry, nonce (nothing from the payload is acted on before this);
  2. tenant in the task envelope must equal the signed tenant; env_hash must equal the hash recomputed from the signed spec;
  3. data class / GPU policy;
  4. bundle/args integrity (sha256);
  5. environment (built in an isolated builder, never on the host);
  6. mark_started (atomic) - a redelivered call is refused, never run twice (D8);
  7. run in the warm sandbox. This process never unpickles anything: cloudpickle.loads exists only in agents/_warm_runner.py."""
from __future__ import annotations

import base64
import threading
import time
from pathlib import Path
from typing import Optional

from agents.env_cache import EnvCache, EnvError, EnvSpec
from agents.orchestrator import WorkerLost
from agents.sandbox import CallRefused, GpuDockerSandbox, SandboxBusy, SandboxError, ServerlessLimits, WarmDockerPool, WarmKey
from agents.sl_security import SignatureError, Verifier, sha256_hex
from agents.sl_store import MAX_INLINE_BYTES, BlobError
from agents.task_bus import SERVERLESS_DIRECT, BusWorker
from agents.tasks import ServerlessError, ServerlessExecIn, ServerlessExecOut, Task, TaskResult, TaskType

MAX_STD_BYTES = 64 * 1024       # per stream in the reply; the runner already caps at 256 KiB


class ServerlessExecutor:
    def __init__(self, *, worker_id: str, verifier: Verifier, env_cache: EnvCache, pool: WarmDockerPool, blobs, ledger,
                 gpu_enabled: bool = False, gpu_index: Optional[int] = None, allowed_data_classes=("PUBLIC", "INTERNAL"),
                 max_bundle_cache_bytes: int = 64 * 1024 * 1024, max_concurrent_builds: int = 1):
        self.worker_id, self.verifier, self.envs, self.pool = worker_id, verifier, env_cache, pool
        self.blobs, self.ledger = blobs, ledger
        self.gpu_enabled, self.gpu_index = gpu_enabled, gpu_index
        self.allowed_data_classes = set(allowed_data_classes)
        self._bundles: dict = {}
        self._bundle_bytes = 0
        self._max_bundle_bytes = max_bundle_cache_bytes
        self._mu = threading.Lock()
        self._builds = threading.Semaphore(max_concurrent_builds)
        self.counters = {"ok": 0, "error": 0, "rejected": 0, "signature_failures": 0, "duplicates": 0, "cold_starts": 0}

    # -- helpers
    def _out(self, status: str, t0: float, **kw) -> ServerlessExecOut:
        return ServerlessExecOut(status=status, total_ms=round((time.perf_counter() - t0) * 1000, 3), worker_id=self.worker_id, **kw)

    def _bad(self, status: str, t0: float, typ: str, msg: str, **kw) -> ServerlessExecOut:
        self.counters["rejected"] += 1
        return self._out(status, t0, error=ServerlessError(type=typ, message=msg), **kw)

    def _cache_bundle(self, sha: str, b64: str) -> None:
        with self._mu:
            if sha in self._bundles:
                return
            while self._bundles and self._bundle_bytes + len(b64) > self._max_bundle_bytes:
                old = self._bundles.pop(next(iter(self._bundles)))
                self._bundle_bytes -= len(old)
            self._bundles[sha] = b64
            self._bundle_bytes += len(b64)

    def _bundle_b64(self, inp: ServerlessExecIn) -> Optional[str]:
        """Return verified bundle (base64) or None if neither supplied nor cached. Raises ValueError on hash mismatch."""
        b64 = inp.bundle_b64
        if b64 is None and inp.bundle_ref:
            if inp.bundle_ref != inp.bundle_sha256:
                raise ValueError("bundle_ref must equal bundle_sha256")
            b64 = base64.b64encode(self.blobs.get(inp.bundle_ref)).decode()
        if b64 is not None:
            if sha256_hex(base64.b64decode(b64)) != inp.bundle_sha256:
                raise ValueError("bundle bytes do not match bundle_sha256")
            self._cache_bundle(inp.bundle_sha256, b64)
            return b64
        return self._bundles.get(inp.bundle_sha256)

    def _args_b64(self, inp: ServerlessExecIn) -> str:
        if inp.args_b64 is not None:
            return inp.args_b64
        if inp.args_ref:
            data = self.blobs.get(inp.args_ref)
            if sha256_hex(data) != inp.args_ref:
                raise ValueError("args blob does not match its hash")
            return base64.b64encode(data).decode()
        raise ValueError("call has neither args_b64 nor args_ref")

    # -- entry point (BusWorker calls this in a thread)
    def execute(self, task: Task, attempt: int = 1, deps: Optional[dict] = None) -> TaskResult:
        t0 = time.perf_counter()
        try:
            out = self._execute(task, attempt, t0)
        except WorkerLost:
            raise
        return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, output=out.model_dump(mode="json"), worker_id=self.worker_id)

    def _execute(self, task: Task, attempt: int, t0: float) -> ServerlessExecOut:
        try:
            inp = ServerlessExecIn.model_validate(task.input)
        except Exception as e:
            return self._bad("bad_request", t0, "BadRequest", f"invalid call: {type(e).__name__}")
        try:
            self.verifier.verify(inp, attempt)
        except SignatureError as e:
            self.counters["signature_failures"] += 1
            return self._bad("bad_request", t0, "SignatureError", str(e))
        if inp.tenant_id != task.tenant_id or inp.call_id != task.task_id:
            self.counters["signature_failures"] += 1
            return self._bad("bad_request", t0, "SignatureError", "envelope does not match the signed call")
        if task.data_class not in self.allowed_data_classes:
            return self._bad("bad_request", t0, "DataClassDenied", f"data_class {task.data_class} is not allowed on this worker")
        spec = EnvSpec(packages=tuple(inp.env.packages), python_version=inp.env.python_version, cuda_tag=inp.env.cuda_tag,
                       local_source_hash=inp.env.local_source_hash)
        try:
            if spec.env_hash() != inp.env_hash:
                return self._bad("bad_request", t0, "BadRequest", "env_hash does not match the environment spec")
        except EnvError as e:
            return self._bad("bad_request", t0, "EnvInvalid", str(e))
        want_gpu = inp.gpu is not None or inp.vram_mb > 0
        if want_gpu and not (self.gpu_enabled and self.gpu_index is not None):
            return self._bad("bad_request", t0, "GpuDisabled", "GPU functions are disabled on this worker")
        try:
            bundle = self._bundle_b64(inp)
            args = self._args_b64(inp)
        except (ValueError, BlobError) as e:
            return self._bad("bad_request", t0, "BadData", str(e))

        # environment (isolated builder; one build at a time per worker)
        built = False
        try:
            if not self.envs.is_ready(inp.env_hash):
                with self._builds:
                    _, built = self.envs.ensure(spec)
            env_dir = self.envs.env_dir(inp.env_hash)
        except EnvError as e:
            return self._bad("env_failed", t0, "EnvBuildFailed", str(e))

        key = WarmKey(inp.tenant_id, inp.env_hash, inp.fn_id)
        limits = ServerlessLimits(memory_mb=inp.memory_mb, cpus=inp.cpus, timeout_s=inp.timeout_s, keep_warm_s=inp.keep_warm_s,
                                  vram_mb=inp.vram_mb, gpu_index=self.gpu_index if want_gpu else None)
        sha = inp.bundle_sha256
        if bundle is None and not self.pool.has_loaded(key, limits, sha):
            return self._bad("bundle_missing", t0, "BundleMissing", "bundle is not cached on this worker; resend with bundle")

        frame = {"call_id": inp.call_id, "sha": sha, "args_b64": args, "args_format": inp.args_format, "result_format": inp.result_format}
        if inp.kind == "cls":
            frame["method"] = inp.method or ""
        try:
            resp, cold = self.pool.call(key, env_dir, limits, frame, bundle, inp.env_vars,
                                        before_run=lambda: self.ledger.mark_started(inp.call_id, attempt))
        except SandboxBusy as e:
            self.verifier.release(inp)
            raise WorkerLost(str(e)) from None              # nothing ran: let another worker take it
        except CallRefused:
            self.counters["duplicates"] += 1
            return self._out("duplicate", t0, error=ServerlessError(
                type="WorkerLost", message="an earlier delivery of this call already started and its worker did not answer"))
        except SandboxError as e:
            msg = str(e)
            status = "timeout" if "timed out" in msg else ("oom" if ("memory" in msg or "137" in msg) else "error")
            self.counters["error"] += 1
            return self._out(status, t0, error=ServerlessError(type="SandboxError", message=msg[:1000]), env_built=built)

        std = {"stdout": resp.get("stdout", "")[:MAX_STD_BYTES], "stderr": resp.get("stderr", "")[:MAX_STD_BYTES]}
        common = dict(cold_start=cold, env_built=built, exec_ms=float(resp.get("exec_ms", 0.0)), enter_ms=float(resp.get("enter_ms", 0.0)),
                      container=resp.get("container", ""), truncated=bool(resp.get("truncated")) or len(resp.get("stdout", "")) > MAX_STD_BYTES, **std)
        if cold:
            self.counters["cold_starts"] += 1
        if not resp.get("ok"):
            self.counters["error"] += 1
            e = resp.get("error", {})
            return self._out("error", t0, error=ServerlessError(type=str(e.get("type", "Error"))[:200], message=str(e.get("message", ""))[:2000],
                                                                 traceback=str(e.get("traceback", ""))[-8000:]), **common)
        raw = base64.b64decode(resp["result_b64"])
        self.counters["ok"] += 1
        if len(raw) > MAX_INLINE_BYTES:
            try:
                return self._out("ok", t0, result_ref=self.blobs.put(raw), result_format=resp["format"], **common)
            except BlobError as e:
                return self._out("error", t0, error=ServerlessError(type="ResultTooLarge", message=str(e)), **common)
        return self._out("ok", t0, result_b64=resp["result_b64"], result_format=resp["format"], **common)


def direct_handler(ex: "ServerlessExecutor"):
    """Handler for the core-NATS warm path: same executor, same signature checks, no JetStream involved."""
    def handle(req: dict) -> dict:
        try:
            task = Task.model_validate(req["task"])
            res = ex.execute(task, int(req.get("attempt", 1)))
            return {"ok": True, "result": res.model_dump(mode="json")}
        except WorkerLost as e:
            return {"ok": False, "error": "busy" if "pool is full" in str(e) else f"WorkerLost: {e}"}
    return handle


def build_worker(url: str, *, worker_id: str, key_file: str, store_dir: str, gpu_enabled: bool = False, gpu_index: Optional[int] = None,
                 slots: int = 4, max_containers: int = 8, nats_store=None, tls_config=None, security_policy=None) -> tuple:
    """Assemble a BusWorker around a ServerlessExecutor. Returns (BusWorker, ServerlessExecutor)."""
    from agents.sl_security import load_keys
    from agents.sl_store import NatsStore
    store = nats_store or NatsStore(url, identity=f"{worker_id}-store", tls_config=tls_config, security_policy=security_policy)
    pool = WarmDockerPool(GpuDockerSandbox(), max_containers=max_containers, worker_id=worker_id)
    killed = pool.kill_orphans()                       # containers left by a previous crashed run of this worker
    pool.start_reaper()
    ex = ServerlessExecutor(worker_id=worker_id, verifier=Verifier(load_keys(key_file)), env_cache=EnvCache(Path(store_dir)), pool=pool,
                            blobs=store, ledger=store, gpu_enabled=gpu_enabled, gpu_index=gpu_index)
    ex.orphans_killed_at_start = killed
    labels = [f"w-{worker_id}"] + (["gpu-sl"] if gpu_enabled else [])      # every extra label is one more JetStream subscription to poll
    if hasattr(store, "serve_direct"):
        store.serve_direct(f"{SERVERLESS_DIRECT}.{worker_id}", direct_handler(ex), max_concurrent=slots)
    bw = BusWorker(url, ex, name=worker_id, ack_wait=10.0, max_deliver=5, types=[TaskType.SERVERLESS_EXEC], kind="executor", labels=labels,
                   slots=slots, data_classes=["PUBLIC", "INTERNAL"], tls_config=tls_config, security_policy=security_policy)
    return bw, ex


if __name__ == "__main__":
    import argparse
    import signal

    ap = argparse.ArgumentParser(description="AICF serverless worker")
    ap.add_argument("--nats", default="nats://127.0.0.1:4222")
    ap.add_argument("--worker-id", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--store-dir", required=True, help="env cache directory (must be on a disk with free space)")
    ap.add_argument("--gpu-index", type=int, default=None)
    ap.add_argument("--gpu-enabled", action="store_true", help="allow GPU functions (off by default; see plan phase 3 gate)")
    ap.add_argument("--slots", type=int, default=4)
    a = ap.parse_args()
    bw, ex = build_worker(a.nats, worker_id=a.worker_id, key_file=a.key_file, store_dir=a.store_dir, gpu_enabled=a.gpu_enabled,
                          gpu_index=a.gpu_index, slots=a.slots)
    signal.signal(signal.SIGTERM, lambda *_: bw.request_drain())
    try:
        bw.run()
    finally:
        ex.pool.close()
