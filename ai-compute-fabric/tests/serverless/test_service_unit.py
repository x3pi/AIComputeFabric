"""ServerlessService with an injected dispatcher: policy, quotas, pinning, retries, blobs, error mapping (no NATS, no Docker)."""
import base64
import threading
from types import SimpleNamespace

import pytest

from agents.sl_store import MAX_INLINE_BYTES, MemoryBlobStore
from agents.tasks import ServerlessExecIn, TaskResult, TaskType
from agents.task_bus import QueueFullError, TaskExpiredError
from agents.orchestrator import WorkerLost
from serverless.quota import Quotas, TenantPolicy
from serverless.service import ServerlessService, ServiceError

KEY = [b"k" * 40]


def b64(b):
    return base64.b64encode(b).decode()


def req(**kw):
    r = {"app_name": "app", "fn_id": "fn", "bundle_b64": b64(b"BUNDLE"), "args_b64": b64(b"ARGS"), "env": {}}
    r.update(kw)
    return r


class Rig:
    def __init__(self, policies=None, workers=("w1", "w2"), gpu_workers=()):
        self.tasks = []
        self.script = []                     # list of dicts (output overrides) or exceptions, consumed per dispatch
        self.workers = [SimpleNamespace(agent_id=w, labels=[f"w-{w}", "cpu-sl"]) for w in workers] + \
                       [SimpleNamespace(agent_id=w, labels=[f"w-{w}", "gpu-sl"]) for w in gpu_workers]
        pol = policies if policies is not None else {"t1": TenantPolicy(serverless_enabled=True, max_retries=2, max_keep_warm_s=600),
                                                    "t2": TenantPolicy(serverless_enabled=True)}
        self.blobs = MemoryBlobStore()
        self.now = [1000.0]
        self.svc = ServerlessService(keys=KEY, quotas=Quotas(pol), dispatch=self.dispatch, live_workers=lambda: self.workers, blobs=self.blobs,
                                     clock=lambda: self.now[0])

    def dispatch(self, task, attempt):
        self.tasks.append((task, attempt))
        step = self.script.pop(0) if self.script else {}
        if isinstance(step, Exception):
            raise step
        out = {"status": "ok", "result_b64": b64(b"R"), "result_format": "cloudpickle", "worker_id": step.pop("_worker", "w1"), **step}
        return TaskResult(task_id=task.task_id, type=TaskType.SERVERLESS_EXEC, output=out, worker_id=out["worker_id"])

    def inp(self, i=-1):
        return ServerlessExecIn.model_validate(self.tasks[i][0].input)


def err(fn, *a, **k):
    with pytest.raises(ServiceError) as e:
        fn(*a, **k)
    return e.value


def test_happy_path_returns_result_and_signed_task():
    r = Rig()
    out = r.svc.invoke("t1", req())
    assert out["status"] == "ok" and out["result_b64"] == b64(b"R") and out["call_id"]
    t, att = r.tasks[0]
    assert t.type == TaskType.SERVERLESS_EXEC and t.tenant_id == "t1" and t.task_id == out["call_id"] and att == 1
    i = r.inp()
    assert i.tenant_id == "t1" and i.bundle_b64 == b64(b"BUNDLE") and len(i.sig.mac) == 64 and len(i.env_hash) == 64


@pytest.mark.parametrize("tenant", ["", None, "bad tenant", "a;b", "../x"])
def test_bad_tenant_identity_is_401(tenant):
    assert err(Rig().svc.invoke, tenant, req()).status == 401


def test_unlisted_and_disabled_tenant_is_403_and_nothing_is_dispatched():
    r = Rig()
    assert err(r.svc.invoke, "stranger", req()).code == "serverless_disabled"
    r2 = Rig(policies={"t1": TenantPolicy(serverless_enabled=False)})
    assert err(r2.svc.invoke, "t1", req()).status == 403
    assert not r.tasks and not r2.tasks


def test_restricted_data_never_runs_even_if_policy_lists_it():
    r = Rig(policies={"t1": TenantPolicy(serverless_enabled=True, allowed_data_classes=("PUBLIC", "INTERNAL", "RESTRICTED"))})
    assert err(r.svc.invoke, "t1", req(data_class="RESTRICTED")).code == "data_class_denied"


def test_gpu_requires_policy_and_gpu_worker():
    r = Rig()
    assert err(r.svc.invoke, "t1", req(gpu="any", vram_mb=1024)).code == "gpu_denied"
    r2 = Rig(policies={"t1": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096)}, workers=("w1",))
    assert err(r2.svc.invoke, "t1", req(gpu="any", vram_mb=1024)).status == 503      # only CPU workers online
    r3 = Rig(policies={"t1": TenantPolicy(serverless_enabled=True, gpu_allowed=True, max_vram_mb=4096)}, workers=(), gpu_workers=("g1",))
    r3.svc.invoke("t1", req(gpu="any", vram_mb=1024))
    assert r3.tasks[0][0].label == "gpu-sl"
    assert err(r3.svc.invoke, "t1", req(gpu="any", vram_mb=8192)).code == "quota_exceeded"


@pytest.mark.parametrize("field,value", [("memory_mb", 100000), ("timeout_s", 100000), ("keep_warm_s", 100000), ("retries", 3)])
def test_resource_limits_are_enforced(field, value):
    e = err(Rig().svc.invoke, "t2", req(**{field: value}))
    assert e.status in (400, 429)


@pytest.mark.parametrize("bad", [
    {"unknown": 1}, {"app_name": "a b"}, {"fn_id": "x;y"}, {"bundle_b64": "!!!"}, {"args_b64": 5}, {"data_class": "SECRET"}, {"kind": "cls"},
    {"timeout_s": -1}, {"memory_mb": True}, {"vram_mb": "8"}, {"retries": 99},
])
def test_malformed_requests_are_400(bad):
    assert err(Rig().svc.invoke, "t1", req(**bad)).status == 400


def test_non_object_body():
    assert err(Rig().svc.invoke, "t1", ["x"]).status == 400


def test_invalid_environment_is_400_and_never_dispatched():
    r = Rig()
    for pk in (["git+https://x/y"], ["numpy;rm"], ["--index-url=http://e"], ["../../x"]):
        assert err(r.svc.invoke, "t1", req(env={"packages": pk})).code == "env_invalid"
    assert err(r.svc.invoke, "t1", req(env={"python_version": "2.7"})).code == "env_invalid"
    assert not r.tasks


def test_no_worker_online_is_503_with_retry_after():
    e = err(Rig(workers=()).svc.invoke, "t1", req())
    assert e.status == 503 and e.retry_after > 0


def test_concurrency_limit_and_release_even_on_error():
    r = Rig(policies={"t1": TenantPolicy(serverless_enabled=True, max_concurrency=1)})
    gate, entered = threading.Event(), threading.Event()
    orig = r.dispatch

    def slow(task, attempt):
        entered.set()
        gate.wait(5)
        return orig(task, attempt)
    r.svc.dispatch = slow
    t = threading.Thread(target=lambda: r.svc.invoke("t1", req()))
    t.start()
    entered.wait(5)
    assert err(r.svc.invoke, "t1", req()).code == "concurrency_exceeded"
    gate.set()
    t.join()
    assert r.svc.quotas.inflight("t1") == 0
    r.svc.dispatch = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    err(r.svc.invoke, "t1", req())
    assert r.svc.quotas.inflight("t1") == 0


def test_large_bundle_and_args_go_through_the_blob_store():
    r = Rig()
    big = b"x" * (MAX_INLINE_BYTES + 1)
    r.svc.invoke("t1", req(bundle_b64=b64(big), args_b64=b64(big)))
    i = r.inp()
    assert i.bundle_b64 is None and i.bundle_ref == i.bundle_sha256 and i.args_b64 is None and r.blobs.get(i.args_ref) == big


def test_payload_over_blob_limit_is_413():
    assert err(Rig().svc.invoke, "t1", req(args_b64=b64(b"x" * (8 * 1024 * 1024 + 1)))).status == 413


def test_result_ref_is_fetched_and_inlined():
    r = Rig()
    ref = r.blobs.put(b"BIGRESULT")
    r.script = [{"result_b64": None, "result_ref": ref}]
    assert r.svc.invoke("t1", req())["result_b64"] == b64(b"BIGRESULT")


def test_warm_pinning_skips_bundle_and_targets_worker_label():
    r = Rig()
    r.script = [{"_worker": "w2"}, {"_worker": "w2"}]
    r.svc.invoke("t1", req())
    r.svc.invoke("t1", req())
    first, second = r.inp(0), r.inp(1)
    assert first.bundle_b64 and r.tasks[0][0].label is None
    assert second.bundle_b64 is None and second.bundle_ref is None and r.tasks[1][0].label == "w-w2"
    assert second.bundle_sha256 == first.bundle_sha256


def test_warm_entry_expires_and_is_dropped_when_worker_disappears():
    r = Rig()
    r.script = [{"_worker": "w2"}]
    r.svc.invoke("t1", req(keep_warm_s=10))
    r.now[0] += 11
    r.svc.invoke("t1", req(keep_warm_s=10))
    assert r.inp().bundle_b64 and r.tasks[1][0].label is None
    r.script = [{"_worker": "w2"}]
    r.svc.invoke("t1", req(keep_warm_s=100))
    r.workers = [w for w in r.workers if w.agent_id != "w2"]
    r.svc.invoke("t1", req(keep_warm_s=100))
    assert r.inp().bundle_b64 and r.tasks[-1][0].label is None


def test_keep_warm_zero_never_pins():
    r = Rig()
    r.svc.invoke("t1", req(keep_warm_s=0))
    r.svc.invoke("t1", req(keep_warm_s=0))
    assert r.tasks[1][0].label is None and r.inp().bundle_b64


def test_warm_entries_are_per_tenant_function_and_shape():
    r = Rig()
    r.script = [{"_worker": "w2"}]
    r.svc.invoke("t1", req())
    r.svc.invoke("t2", req())                                  # same function bytes, other tenant: never pinned to t1's container
    assert r.tasks[1][0].label is None and r.inp().bundle_b64
    r.svc.invoke("t1", req(memory_mb=1024))                    # other shape: other container
    assert r.tasks[2][0].label is None
    r.svc.invoke("t1", req(fn_id="other"))
    assert r.tasks[3][0].label is None


def test_bundle_missing_is_resent_with_bundle_and_new_sequence():
    r = Rig()
    r.script = [{"_worker": "w1"}, {"status": "bundle_missing", "result_b64": None}, {"_worker": "w1"}]
    r.svc.invoke("t1", req())
    out = r.svc.invoke("t1", req())
    assert out["status"] == "ok"
    seqs = [a for _, a in r.tasks]
    assert seqs == [1, 1, 2]
    assert r.inp().bundle_b64 and r.tasks[-1][1] != r.tasks[-2][1]


def test_worker_lost_not_retried_by_default_and_retried_when_opted_in():
    r = Rig()
    r.script = [WorkerLost("gone")]
    out = r.svc.invoke("t1", req())
    assert out["status"] == "worker_lost" and len(r.tasks) == 1
    r2 = Rig()
    r2.script = [WorkerLost("gone"), {"_worker": "w2"}]
    out = r2.svc.invoke("t1", req(retries=1))
    assert out["status"] == "ok" and out["attempts"] == 2 and [a for _, a in r2.tasks] == [1, 2]
    assert r2.tasks[1][0].task_id == r2.tasks[0][0].task_id


def test_user_errors_timeouts_are_never_retried():
    r = Rig()
    r.script = [{"status": "error", "error": {"type": "ValueError", "message": "x"}, "result_b64": None}]
    assert r.svc.invoke("t1", req(retries=2))["status"] == "error" and len(r.tasks) == 1
    r.script = [{"status": "timeout", "result_b64": None}]
    assert r.svc.invoke("t1", req(retries=2))["status"] == "timeout" and len(r.tasks) == 2


def test_duplicate_delivery_counts_as_retryable_infra_failure():
    r = Rig()
    r.script = [{"status": "duplicate", "result_b64": None}, {}]
    assert r.svc.invoke("t1", req(retries=1))["status"] == "ok"


@pytest.mark.parametrize("exc,status", [(QueueFullError("full"), 429), (TaskExpiredError("late"), 504), (RuntimeError("weird"), 502)])
def test_bus_errors_are_mapped(exc, status):
    r = Rig()
    r.script = [exc]
    assert err(r.svc.invoke, "t1", req()).status == status


def test_metrics_count_outcomes_and_rejections():
    r = Rig()
    r.svc.invoke("t1", req())
    err(r.svc.invoke, "stranger", req())
    text = r.svc.metrics.render()
    assert 'sl_calls_total{gpu="false",status="ok"} 1' in text and 'sl_quota_rejections_total{reason="serverless_disabled"} 1' in text
