"""ServerlessExecutor with fake pool / env cache / stores: the order of checks is a security property (no Docker, no NATS)."""
import base64

import pytest

from agents.env_cache import EnvError, EnvSpec
from agents.orchestrator import WorkerLost
from agents.sandbox import CallRefused, SandboxBusy, SandboxError
from agents.serverless_worker import ServerlessExecutor
from agents.sl_security import Verifier, sha256_hex, sign
from agents.sl_store import MAX_INLINE_BYTES, MemoryBlobStore, MemoryStartedLedger
from agents.tasks import ServerlessExecIn, ServerlessExecOut, Task, TaskType

KEY = [b"k" * 40]
BUNDLE = b"bundle-bytes"
SPEC = EnvSpec()
H = SPEC.env_hash()


def b64(b):
    return base64.b64encode(b).decode()


class FakeEnvs:
    def __init__(self, ready=True, fail=None):
        self.ready, self.fail, self.built = ready, fail, 0

    def is_ready(self, h):
        return self.ready

    def env_dir(self, h):
        return "/nonexistent/" + h

    def ensure(self, spec):
        if self.fail:
            raise EnvError(self.fail)
        self.built += 1
        self.ready = True
        return "/x", True


class FakePool:
    def __init__(self):
        self.calls, self.loaded, self.script = [], set(), []

    def has_loaded(self, key, limits, sha):
        return (key, sha) in self.loaded

    def call(self, key, env_dir, limits, frame, bundle, env, before_run=None):
        step = self.script.pop(0) if self.script else {}
        if isinstance(step, SandboxBusy):               # like the real pool: the capacity check happens BEFORE the before_run hook
            raise step
        if before_run is not None and not before_run():
            raise CallRefused("dup")
        self.calls.append(dict(key=key, limits=limits, frame=frame, bundle=bundle, env=env))
        if isinstance(step, Exception):
            raise step
        self.loaded.add((key, frame["sha"]))
        return {"ok": True, "result_b64": b64(b"42"), "format": "cloudpickle", "stdout": "hi", "stderr": "", "exec_ms": 1.5, "container": "c1", **step}, \
            step.get("_cold", False)


def make(gpu_enabled=False, envs=None, **kw):
    pool = FakePool()
    ex = ServerlessExecutor(worker_id="wk", verifier=Verifier(KEY), env_cache=envs or FakeEnvs(), pool=pool, blobs=MemoryBlobStore(),
                            ledger=MemoryStartedLedger(), gpu_enabled=gpu_enabled, gpu_index=0 if gpu_enabled else None, **kw)
    return ex, pool


def body(**kw):
    b = {"tenant_id": "t1", "call_id": "c" * 16, "app_name": "app", "fn_id": "fn", "env_hash": H, "bundle_sha256": sha256_hex(BUNDLE),
         "bundle_b64": b64(BUNDLE), "args_b64": b64(b"args"), "env": {"packages": list(SPEC.packages)}}
    b.update(kw)
    return b


def task(inp: ServerlessExecIn, tenant="t1", data_class="INTERNAL"):
    return Task(task_id=inp.call_id, tenant_id=tenant, type=TaskType.SERVERLESS_EXEC, input=inp.model_dump(mode="json"), data_class=data_class)


def run(ex, **kw):
    attempt = kw.pop("attempt", 1)
    t = kw.pop("task_override", None)
    inp = sign(KEY, body(**kw), attempt)
    res = ex.execute(t or task(inp), attempt)
    return ServerlessExecOut.model_validate(res.output)


def test_happy_path():
    ex, pool = make()
    out = run(ex)
    assert out.status == "ok" and out.result_b64 == b64(b"42") and out.exec_ms == 1.5 and out.worker_id == "wk" and out.stdout == "hi"
    assert pool.calls[0]["key"].tenant_id == "t1" and pool.calls[0]["limits"].gpu_index is None


def test_bad_signature_runs_nothing():
    ex, pool = make()
    inp = sign(KEY, body(), 1).model_copy(update={"fn_id": "tampered"})
    out = ServerlessExecOut.model_validate(ex.execute(task(inp), 1).output)
    assert out.status == "bad_request" and out.error.type == "SignatureError" and not pool.calls and ex.counters["signature_failures"] == 1


def test_wrong_key_expired_and_replay():
    ex, pool = make()
    inp = sign([b"z" * 40], body(), 1)
    assert ServerlessExecOut.model_validate(ex.execute(task(inp), 1).output).status == "bad_request"
    inp = sign(KEY, body(call_id="d" * 16), 1, ttl_s=-100)
    assert "expired" in ServerlessExecOut.model_validate(ex.execute(task(inp), 1).output).error.message
    inp = sign(KEY, body(call_id="e" * 16), 1)
    assert ServerlessExecOut.model_validate(ex.execute(task(inp), 1).output).status == "ok"
    out = ServerlessExecOut.model_validate(ex.execute(task(inp), 1).output)
    assert out.status == "bad_request" and "replayed" in out.error.message and len(pool.calls) == 1


def test_attempt_is_part_of_the_signature():
    ex, pool = make()
    inp = sign(KEY, body(), 1)
    assert ServerlessExecOut.model_validate(ex.execute(task(inp), 2).output).status == "bad_request" and not pool.calls


def test_envelope_tenant_and_call_id_must_match_signed_values():
    ex, pool = make()
    inp = sign(KEY, body(), 1)
    out = ServerlessExecOut.model_validate(ex.execute(task(inp, tenant="victim"), 1).output)
    assert out.status == "bad_request" and not pool.calls
    t = task(inp)
    t = t.model_copy(update={"task_id": "other-call-id"})
    assert ServerlessExecOut.model_validate(ex.execute(t, 1).output).status == "bad_request" and not pool.calls


def test_restricted_data_class_refused_by_worker():
    ex, pool = make()
    inp = sign(KEY, body(), 1)
    out = ServerlessExecOut.model_validate(ex.execute(task(inp, data_class="RESTRICTED"), 1).output)
    assert out.status == "bad_request" and out.error.type == "DataClassDenied" and not pool.calls


def test_env_hash_must_match_spec():
    ex, pool = make()
    out = run(ex, env_hash="f" * 64)
    assert out.status == "bad_request" and not pool.calls
    out = run(ex, env={"packages": ["git+https://x/y"]}, call_id="g" * 16)
    assert out.status == "bad_request" and out.error.type == "EnvInvalid"


def test_gpu_disabled_by_default_and_enabled_with_flag():
    ex, pool = make()
    out = run(ex, gpu="any", vram_mb=1024)
    assert out.status == "bad_request" and out.error.type == "GpuDisabled" and not pool.calls
    assert run(ex, vram_mb=512, call_id="h" * 16).error.type == "GpuDisabled"
    ex2, pool2 = make(gpu_enabled=True)
    assert run(ex2, gpu="any", vram_mb=1024).status == "ok"
    assert pool2.calls[0]["limits"].gpu_index == 0 and pool2.calls[0]["limits"].vram_mb == 1024


def test_bundle_hash_mismatch_is_refused():
    ex, pool = make()
    out = run(ex, bundle_b64=b64(b"different"))
    assert out.status == "bad_request" and out.error.type == "BadData" and not pool.calls


def test_bundle_ref_loaded_from_blob_store_and_checked():
    ex, pool = make()
    ref = ex.blobs.put(BUNDLE)
    out = run(ex, bundle_b64=None, bundle_ref=ref)
    assert out.status == "ok" and pool.calls[0]["bundle"] == b64(BUNDLE)
    out = run(ex, bundle_b64=None, bundle_ref="0" * 64, call_id="i" * 16)
    assert out.status == "bad_request"


def test_args_ref():
    ex, pool = make()
    big = b"a" * (MAX_INLINE_BYTES + 5)
    ref = ex.blobs.put(big)
    assert run(ex, args_b64=None, args_ref=ref).status == "ok"
    assert base64.b64decode(pool.calls[0]["frame"]["args_b64"]) == big


def test_bundle_missing_when_neither_supplied_nor_cached_nor_loaded():
    ex, pool = make()
    out = run(ex, bundle_b64=None)
    assert out.status == "bundle_missing" and not pool.calls


def test_cached_bundle_is_used_without_resend():
    ex, pool = make()
    assert run(ex).status == "ok"
    assert run(ex, bundle_b64=None, call_id="j" * 16).status == "ok"
    assert pool.calls[1]["bundle"] == b64(BUNDLE)
    ex.pool.loaded.add((pool.calls[1]["key"], sha256_hex(BUNDLE)))


def test_environment_is_built_once_and_failure_is_reported():
    envs = FakeEnvs(ready=False)
    ex, _ = make(envs=envs)
    out = run(ex)
    assert out.status == "ok" and out.env_built is True and envs.built == 1
    assert run(ex, call_id="k" * 16).env_built is False
    ex2, pool2 = make(envs=FakeEnvs(ready=False, fail="wheels only"))
    out = run(ex2)
    assert out.status == "env_failed" and "wheels only" in out.error.message and not pool2.calls


def test_second_delivery_of_a_started_call_is_not_run_again():
    ex, pool = make()
    inp = sign(KEY, body(), 1)
    t = task(inp)
    assert ServerlessExecOut.model_validate(ex.execute(t, 1).output).status == "ok"
    ex.verifier.release(inp)                        # a different worker process would not have this nonce
    out = ServerlessExecOut.model_validate(ex.execute(t, 1).output)
    assert out.status == "duplicate" and out.error.type == "WorkerLost" and len(pool.calls) == 1


def test_busy_pool_raises_workerlost_without_marking_started_and_allows_redelivery():
    ex, pool = make()
    pool.script = [SandboxBusy("full")]
    inp = sign(KEY, body(), 1)
    with pytest.raises(WorkerLost):
        ex.execute(task(inp), 1)
    assert ServerlessExecOut.model_validate(ex.execute(task(inp), 1).output).status == "ok"      # same message redelivered: still runs


@pytest.mark.parametrize("msg,status", [("tool timed out after 3s", "timeout"), ("tool timed out", "timeout"), ("container killed, most likely out of memory (exit 137)", "oom"),
                                         ("container exited with 1", "error")])
def test_sandbox_failures_are_classified(msg, status):
    ex, pool = make()
    pool.script = [SandboxError(msg)]
    out = run(ex)
    assert out.status == status and out.error.type == "SandboxError"


def test_user_exception_is_reported_with_traceback():
    ex, pool = make()
    pool.script = [{"ok": False, "error": {"type": "ValueError", "message": "kaput", "traceback": "Traceback..."}}]
    out = run(ex)
    assert out.status == "error" and out.error.type == "ValueError" and out.error.traceback == "Traceback..."


def test_large_result_goes_to_blob_store_and_big_stdout_is_cut():
    ex, pool = make()
    big = b"r" * (MAX_INLINE_BYTES + 10)
    pool.script = [{"result_b64": b64(big), "stdout": "x" * 200_000}]
    out = run(ex)
    assert out.result_b64 is None and ex.blobs.get(out.result_ref) == big
    assert len(out.stdout) == 64 * 1024 and out.truncated is True


def test_garbage_task_input_is_bad_request_not_a_crash():
    ex, _ = make()
    t = Task(task_id="x" * 12, tenant_id="t1", type=TaskType.SERVERLESS_EXEC, input={"nonsense": 1})
    assert ServerlessExecOut.model_validate(ex.execute(t, 1).output).status == "bad_request"
