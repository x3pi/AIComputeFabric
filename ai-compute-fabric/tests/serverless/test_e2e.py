"""Phase 2 gate: gateway (real Go binary) -> serverless service -> NATS JetStream (isolated container) -> BusWorker -> warm Docker sandbox.

Everything is isolated: NATS runs as container aicf-sl-test-nats on a free loopback port, the gateway on free ports, the worker is
named aicf-sl-test. Nothing of the running AICF stack is touched. CPU only (GPU stays behind its flag until the phase 3 gate)."""
import base64
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import cloudpickle
import pytest

from agents.sl_security import new_key_file, load_keys, sign
from agents.task_bus import BusExecutor, list_live_agents
from agents.tasks import ServerlessExecOut, Task, TaskType
from tests.serverless.util import by_value

by_value(__name__)
ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / ".aicf-work/sl-test/store"
WORKER = "aicf-sl-test"
ADMIN = "adm-token-for-tests"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None or shutil.which("go") is None or
                                subprocess.run(["docker", "info"], capture_output=True).returncode != 0, reason="needs docker and go")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- functions executed inside the sandbox ----------------------------------------------------------------------------
def square(x):
    return x * x


def explode():
    raise ValueError("kaput from the sandbox")


def sleeper(s):
    import time
    time.sleep(s)


def hog(mb):
    b = bytearray(mb * 1024 * 1024)
    for i in range(0, len(b), 4096):
        b[i] = 1
    return len(b)


def tmp_marker(mode):
    import os
    if mode == "write":
        open("/tmp/marker", "w").write("x")
    return os.path.exists("/tmp/marker")


def identity():
    import os
    return {"uid": os.getuid(), "hostname": os.uname().nodename[:4]}


class Embedder:
    loads = 0

    def load(self):
        Embedder.loads += 1

    def encode(self, x):
        return Embedder.loads, x * 2


# ---- infrastructure fixtures --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def nats_url():
    port = free_port()
    name = f"aicf-sl-test-nats-{port}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    r = subprocess.run(["docker", "run", "-d", "--name", name, "--label", "aicf.sl=1", "--label", "aicf.worker=nats-test", "-p", f"127.0.0.1:{port}:4222",
                        "--tmpfs", "/data:size=512m", "nats:2.10-alpine", "-js", "-sd", "/data"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    url = f"nats://127.0.0.1:{port}"
    import asyncio
    import nats

    async def ready():
        for _ in range(50):
            try:
                nc = await nats.connect(url, connect_timeout=1)
                await nc.close()
                return
            except Exception:
                await asyncio.sleep(0.2)
        raise RuntimeError("nats did not start")
    asyncio.run(ready())
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def secrets(tmp_path_factory):
    d = tmp_path_factory.mktemp("sl")
    new_key_file(d / "hmac.key")
    (d / "gw.secret").write_text("shared-secret-for-gateway-and-service\n")
    (d / "gw.secret").chmod(0o600)
    return d


@pytest.fixture(scope="module")
def worker(nats_url, secrets):
    from agents.serverless_worker import build_worker
    bw, ex = build_worker(nats_url, worker_id=WORKER, key_file=str(secrets / "hmac.key"), store_dir=str(STORE), slots=4)
    t = threading.Thread(target=bw.run, daemon=True)
    t.start()
    import asyncio
    import nats

    async def wait_live():
        nc = await nats.connect(nats_url)
        js = nc.jetstream()
        for _ in range(100):
            if await list_live_agents(js, TaskType.SERVERLESS_EXEC):
                await nc.close()
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("worker did not register")
    asyncio.run(wait_live())
    yield ex
    bw.request_drain()
    ex.pool.close()
    t.join(timeout=15)
    leftovers = ex.pool.docker_names()
    assert leftovers == [], f"orphan containers: {leftovers}"


@pytest.fixture(scope="module")
def stack(nats_url, secrets, worker, tmp_path_factory):
    from agents.sl_store import NatsStore
    from serverless.app import serve
    from serverless.quota import Quotas
    from serverless.service import ServerlessService

    d = tmp_path_factory.mktemp("gw")
    for out, pkg in (("gw", "."),):
        subprocess.run(["go", "build", "-o", str(d / out), pkg], cwd=ROOT / "apps/site-gateway", check=True)
    http_p, grpc_p, svc_p = free_port(), free_port(), free_port()
    gw = subprocess.Popen([str(d / "gw"), "-http", f"127.0.0.1:{http_p}", "-grpc", f"127.0.0.1:{grpc_p}", "-tenants-file", str(d / "tenants.json"),
                           "-admin-token", ADMIN, "-serverless-url", f"http://127.0.0.1:{svc_p}", "-serverless-secret-file", str(secrets / "gw.secret")],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{http_p}"

    def http(method, path, token=None, body=None, headers=None, raw=False):
        req = urllib.request.Request(base + path, method=method, data=None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()))
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                return e.code, json.loads(data)
            except ValueError:
                return e.code, data.decode(errors="replace")

    for _ in range(100):
        try:
            http("GET", "/api/whoami")
            break
        except OSError:
            time.sleep(0.1)
    keys = {}
    tenants = {}
    for name in ("alpha", "beta", "gamma"):
        st, doc = http("POST", "/api/tenants", ADMIN, {"name": name, "weight": 1})
        assert st == 201, doc
        keys[name], tenants[name] = doc["api_key"], doc["tenant"]["id"]
    policy = {tenants["alpha"]: dict(serverless_enabled=True, max_concurrency=8, max_retries=2, max_keep_warm_s=600, max_memory_mb=1024),
              tenants["beta"]: dict(serverless_enabled=True, max_concurrency=8, max_keep_warm_s=600)}      # gamma: not enabled
    (d / "policy.json").write_text(json.dumps({"tenants": policy}))
    bx = BusExecutor(nats_url, task_timeout=720.0, identity="sl-test-service", max_tenant_inflight=64)
    store = NatsStore(nats_url, identity="sl-test-service-store")
    live_cache = {"t": 0.0, "v": []}

    def live():
        if time.monotonic() - live_cache["t"] > 0.5:
            live_cache["v"], live_cache["t"] = bx._run(list_live_agents(bx.js, TaskType.SERVERLESS_EXEC)), time.monotonic()
        return live_cache["v"]
    from serverless.dispatch import BusDispatcher
    dispatcher = BusDispatcher(bx)
    svc = ServerlessService(keys=load_keys(secrets / "hmac.key"), quotas=Quotas.load(d / "policy.json"), dispatch=dispatcher,
                            live_workers=live, blobs=store)
    secret = (secrets / "gw.secret").read_text().strip()
    srv = serve(svc, secret, "127.0.0.1", svc_p)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleStack(gateway_url=base, dispatcher=dispatcher, http=http, keys=keys, tenants=tenants, svc=svc, svc_port=svc_p, secret=secret, bx=bx, nats_url=nats_url, secrets=secrets)
    srv.shutdown()
    gw.terminate()
    gw.wait(10)
    bx.close()
    store.close()


class SimpleStack:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def call(self, who, fn, *args, kind="function", method=None, token=None, **opts):
        bundle = cloudpickle.dumps({"kind": kind, "obj": fn, "enter": opts.pop("enter", []), "exit": []})
        body = {"app_name": "app", "fn_id": opts.pop("fn_id", fn.__name__), "kind": kind, "bundle_b64": base64.b64encode(bundle).decode(),
                "args_b64": base64.b64encode(cloudpickle.dumps((args, opts.pop("kwargs", {})))).decode(), **opts}
        if method:
            body["method"] = method
        return self.http("POST", "/api/serverless/v1/invoke", token or self.keys[who], body)


def result(doc):
    assert doc["status"] == "ok", doc
    return cloudpickle.loads(base64.b64decode(doc["result_b64"]))


# ---- tests -----------------------------------------------------------------------------------------------------------
def test_cold_warm_and_scale_to_zero_through_the_whole_chain(stack, worker):
    st, d1 = stack.call("alpha", square, 7, keep_warm_s=3)
    st2, d2 = stack.call("alpha", square, 8, keep_warm_s=3)
    assert (st, st2) == (200, 200) and result(d1) == 49 and result(d2) == 64
    assert d1["cold_start"] is True and d2["cold_start"] is False and d1["container"] == d2["container"]
    assert d1["worker_id"] == WORKER and d1["call_id"] != d2["call_id"]
    assert len(worker.pool.docker_names()) >= 1
    deadline = time.time() + 20
    while worker.pool.docker_names() and time.time() < deadline:
        time.sleep(0.3)
    assert worker.pool.docker_names() == [], "scale-to-zero: containers must disappear after keep_warm_s"
    st, d3 = stack.call("alpha", square, 3, keep_warm_s=0)
    assert result(d3) == 9 and d3["cold_start"] is True


def test_authentication_and_authorization_at_every_layer(stack):
    body = {"app_name": "a", "fn_id": "f", "bundle_b64": "AA==", "args_b64": "AA=="}
    assert stack.http("POST", "/api/serverless/v1/invoke", None, body)[0] == 401
    assert stack.http("POST", "/api/serverless/v1/invoke", "aicf_wrong", body)[0] == 401
    st, doc = stack.call("gamma", square, 2)                               # valid key, tenant not enabled in the policy
    assert st == 403 and doc["error"]["code"] == "serverless_disabled"
    assert stack.http("POST", "/api/serverless/v1/invoke", ADMIN, body)[0] == 403         # admin token has no tenant: calls need a tenant key
    # the service itself: no direct access with forged identity
    base = f"http://127.0.0.1:{stack.svc_port}"
    for hdr in ({"X-Aicf-Tenant": stack.tenants["alpha"], "X-Aicf-Role": "tenant"},
                {"X-Aicf-Tenant": stack.tenants["alpha"], "X-Aicf-Role": "tenant", "X-Aicf-Proxy-Secret": "wrong"}):
        req = urllib.request.Request(base + "/v1/invoke", data=json.dumps(body).encode(), method="POST", headers=hdr)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 401


def test_service_refuses_non_tenant_roles_even_with_the_secret(stack):
    body = {"app_name": "a", "fn_id": "f", "bundle_b64": "AA==", "args_b64": "AA=="}
    for role in ("admin", "operator", "viewer", ""):
        req = urllib.request.Request(f"http://127.0.0.1:{stack.svc_port}/v1/invoke", data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Aicf-Proxy-Secret": stack.secret, "X-Aicf-Tenant": stack.tenants["alpha"], "X-Aicf-Role": role})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 403


def test_gateway_strips_forged_identity_headers(stack):
    st, d = stack.call("alpha", square, 2, token=None)
    hdr = {"X-Aicf-Tenant": stack.tenants["beta"], "X-Aicf-Proxy-Secret": "x", "X-Aicf-Role": "admin"}
    bundle = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": identity, "enter": [], "exit": []})).decode()
    body = {"app_name": "a", "fn_id": "ident", "bundle_b64": bundle, "args_b64": base64.b64encode(cloudpickle.dumps(((), {}))).decode()}
    st, doc = stack.http("POST", "/api/serverless/v1/invoke", stack.keys["gamma"], body, headers=hdr)
    assert st == 403 and doc["error"]["code"] == "serverless_disabled"     # gamma stays gamma; header claiming beta is ignored


def test_python_exception_returns_remote_traceback(stack):
    st, d = stack.call("alpha", explode)
    assert st == 200 and d["status"] == "error" and d["error"]["type"] == "ValueError" and "kaput from the sandbox" in d["error"]["message"]
    assert "explode" in d["error"]["traceback"]


def test_timeout_and_oom_have_distinct_codes_and_recover(stack, worker):
    st, d = stack.call("alpha", sleeper, 30, timeout_s=2.0)
    assert st == 200 and d["status"] == "timeout"
    st, d = stack.call("alpha", hog, 600, memory_mb=128, fn_id="hog")
    assert st == 200 and d["status"] in ("oom", "error") and d["error"]["type"] == "SandboxError"
    st, d = stack.call("alpha", square, 5)
    assert result(d) == 25


def test_tenants_are_isolated_in_containers_and_tmp(stack, worker):
    _, a = stack.call("alpha", tmp_marker, "write", fn_id="mark", keep_warm_s=30)
    _, b = stack.call("beta", tmp_marker, "read", fn_id="mark", keep_warm_s=30)
    _, a2 = stack.call("alpha", tmp_marker, "read", fn_id="mark", keep_warm_s=30)
    assert result(a) is True and result(b) is False and result(a2) is True
    assert a["container"] != b["container"]


def test_sandbox_identity_is_unprivileged(stack):
    _, d = stack.call("alpha", identity)
    assert result(d)["uid"] == 65534


def test_class_enter_runs_once_across_calls(stack):
    cls_calls = []
    for i in range(5):
        _, d = stack.call("alpha", Embedder, i, kind="cls", method="encode", enter=["load"], fn_id="embedder", keep_warm_s=30)
        cls_calls.append(result(d))
    assert [c[0] for c in cls_calls] == [1] * 5 and [c[1] for c in cls_calls] == [0, 2, 4, 6, 8]


def test_bad_requests_are_rejected_before_any_container(stack, worker):
    before = len(worker.pool.snapshot())
    st, d = stack.http("POST", "/api/serverless/v1/invoke", stack.keys["alpha"], {"app_name": "a", "fn_id": "f", "bundle_b64": "AA==", "args_b64": "AA==", "evil": 1})
    assert st == 400
    st, d = stack.http("POST", "/api/serverless/v1/invoke", stack.keys["alpha"], {"app_name": "a", "fn_id": "f", "bundle_b64": "AA==", "args_b64": "AA==",
                                                                                   "env": {"packages": ["git+https://evil/x"]}})
    assert st == 400 and d["error"]["code"] == "env_invalid"
    st, d = stack.http("POST", "/api/serverless/v1/invoke", stack.keys["alpha"], b"not json")
    assert st == 400
    assert len(worker.pool.snapshot()) == before


def test_gpu_function_refused_while_gpu_is_off(stack):
    st, d = stack.call("alpha", square, 2, gpu="any", vram_mb=1024)
    assert st == 403 and d["error"]["code"] == "gpu_denied"              # policy
    # even a policy that allowed it would be stopped by the worker (see test_worker_unit::test_gpu_disabled_by_default_and_enabled_with_flag)


def test_retries_only_after_infrastructure_loss(stack):
    st, d = stack.call("alpha", explode, retries=2)
    assert d["status"] == "error" and d["attempts"] == 1                    # a user exception is never retried


def test_warm_calls_use_the_direct_nats_path(stack):
    stack.call("alpha", square, 1, keep_warm_s=60, fn_id="direct")
    before = stack.dispatcher.direct_calls
    for i in range(3):
        _, d = stack.call("alpha", square, i, keep_warm_s=60, fn_id="direct")
        assert result(d) == i * i and d["cold_start"] is False
    assert stack.dispatcher.direct_calls >= before + 3


# ---- signature enforcement on the bus (the worker is the last line of defence) ----------------------------------------
def bus_exec(stack, inp, tenant="alpha_tenant", attempt=1, data_class="INTERNAL"):
    bx = BusExecutor(stack.nats_url, task_timeout=60.0, identity=f"sl-attacker-{uuid.uuid4().hex[:6]}")
    try:
        t = Task(task_id=inp.call_id, tenant_id=inp.tenant_id if tenant == "alpha_tenant" else tenant, type=TaskType.SERVERLESS_EXEC,
                 input=inp.model_dump(mode="json"), data_class=data_class, ttl_s=60)
        return ServerlessExecOut.model_validate(bx.execute(t, attempt=attempt).output)
    finally:
        bx.close()


def forged_body(stack, **kw):
    from agents.env_cache import EnvSpec
    bundle = cloudpickle.dumps({"kind": "function", "obj": square, "enter": [], "exit": []})
    spec = EnvSpec()
    import hashlib
    b = {"tenant_id": stack.tenants["alpha"], "call_id": uuid.uuid4().hex, "app_name": "a", "fn_id": "forged", "env_hash": spec.env_hash(),
         "env": {"packages": list(spec.packages)}, "bundle_sha256": hashlib.sha256(bundle).hexdigest(), "bundle_b64": base64.b64encode(bundle).decode(),
         "args_b64": base64.b64encode(cloudpickle.dumps(((6,), {}))).decode()}
    b.update(kw)
    return b


def test_bus_level_forgery_is_rejected_without_starting_a_container(stack, worker):
    keys = load_keys(stack.secrets / "hmac.key")
    good = sign(keys, forged_body(stack), 1)
    n_calls = worker.counters["ok"]
    assert bus_exec(stack, good).status == "ok"                                              # the genuine thing works
    assert worker.counters["ok"] == n_calls + 1
    sig_fail = worker.counters["signature_failures"]
    tampered = sign(keys, forged_body(stack), 1).model_copy(update={"args_b64": base64.b64encode(cloudpickle.dumps(((999,), {}))).decode()})
    assert bus_exec(stack, tampered).error.type == "SignatureError"
    wrong_key = sign([b"attacker-key-" * 4], forged_body(stack), 1)
    assert bus_exec(stack, wrong_key).error.type == "SignatureError"
    expired = sign(keys, forged_body(stack), 1, ttl_s=-60)
    assert "expired" in bus_exec(stack, expired).error.message
    other_tenant = sign(keys, forged_body(stack), 1)
    assert bus_exec(stack, other_tenant, tenant=stack.tenants["beta"]).error.type == "SignatureError"      # envelope tenant != signed tenant
    assert worker.counters["signature_failures"] == sig_fail + 4
    assert worker.counters["ok"] == n_calls + 1


def test_bus_level_replay_is_rejected(stack, worker):
    keys = load_keys(stack.secrets / "hmac.key")
    s = sign(keys, forged_body(stack), 1)
    assert bus_exec(stack, s).status == "ok"
    out = bus_exec(stack, s)                                                                  # new BusExecutor => new JetStream msg id, same nonce
    assert out.status == "bad_request" and "replayed" in out.error.message


def direct_request(stack, task_dict, attempt=1):
    msg = stack.bx._run(stack.bx.nc.request(f"aicf.sld.{WORKER}", json.dumps({"task": task_dict, "attempt": attempt}).encode(), timeout=30), 40)
    return json.loads(msg.data)


def test_direct_path_enforces_the_same_signature_rules(stack, worker):
    keys = load_keys(stack.secrets / "hmac.key")
    sig_fail = worker.counters["signature_failures"]

    def tdict(inp, tenant=None, dc="INTERNAL"):
        return Task(task_id=inp.call_id, tenant_id=tenant or inp.tenant_id, type=TaskType.SERVERLESS_EXEC, input=inp.model_dump(mode="json"),
                    data_class=dc, ttl_s=30).model_dump(mode="json")
    good = sign(keys, forged_body(stack), 1)
    r = direct_request(stack, tdict(good))
    assert r["ok"] and r["result"]["output"]["status"] == "ok"
    r = direct_request(stack, tdict(good))                                                             # replay of the same signed call
    assert r["result"]["output"]["error"]["message"] == "replayed nonce"
    bad = sign(keys, forged_body(stack), 1).model_copy(update={"fn_id": "tampered"})
    assert direct_request(stack, tdict(bad))["result"]["output"]["error"]["type"] == "SignatureError"
    other = sign(keys, forged_body(stack), 1)
    assert direct_request(stack, tdict(other, tenant=stack.tenants["beta"]))["result"]["output"]["error"]["type"] == "SignatureError"
    restricted = sign(keys, forged_body(stack), 1)
    assert direct_request(stack, tdict(restricted, dc="RESTRICTED"))["result"]["output"]["error"]["type"] == "DataClassDenied"
    assert worker.counters["signature_failures"] >= sig_fail + 2
    assert direct_request(stack, {"garbage": 1})["ok"] is False


def test_restricted_data_class_never_reaches_a_worker(stack):
    keys = load_keys(stack.secrets / "hmac.key")
    s = sign(keys, forged_body(stack), 1)
    bx = BusExecutor(stack.nats_url, task_timeout=6.0, identity="sl-attacker-dc")
    try:
        t = Task(task_id=s.call_id, tenant_id=s.tenant_id, type=TaskType.SERVERLESS_EXEC, input=s.model_dump(mode="json"), data_class="RESTRICTED", ttl_s=4)
        with pytest.raises(Exception) as e:
            bx.execute(t)
        assert type(e.value).__name__ in ("WorkerLost", "TaskExpiredError")             # no subscriber exists for aicf.sl.SERVERLESS_EXEC.dc.RESTRICTED
    finally:
        bx.close()


def test_legacy_wildcard_worker_never_even_sees_serverless_messages(stack, nats_url):
    """A pre-existing worker subscribed to `aicf.task.>` must not receive SERVERLESS_EXEC (it would nak it forever and steal slots)."""
    from agents.task_bus import BusWorker
    seen = []

    class Spy(BusWorker):
        async def _handle(self, nc, m):
            seen.append(m.subject)
            return await super()._handle(nc, m)

    class Ex:
        def execute(self, task, attempt, deps):
            raise AssertionError("must never run")
    legacy = Spy(nats_url, Ex(), name="aicf-sl-test-legacy")
    th = threading.Thread(target=legacy.run, daemon=True)
    th.start()
    assert legacy.ready.wait(10)
    try:
        for i in range(3):
            _, d = stack.call("alpha", square, i, keep_warm_s=0, fn_id="legacy-check")
            assert result(d) == i * i
        assert seen == [], f"legacy worker was handed serverless messages: {seen}"
    finally:
        legacy.request_drain()
        th.join(10)


def test_generic_workers_never_pull_serverless(nats_url):
    from agents.task_bus import BusWorker, DEFAULT_TASK_TYPES
    assert TaskType.SERVERLESS_EXEC not in DEFAULT_TASK_TYPES
    w = BusWorker(nats_url, executor=None, name="generic-test")
    assert TaskType.SERVERLESS_EXEC.value not in w.allowed_types


# ---- performance (SLO bookkeeping; numbers go to the phase report) --------------------------------------------------------
def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def test_warm_path_latency_slo_and_report(stack, worker):
    N = 150
    st0, d0 = stack.call("alpha", square, 1, keep_warm_s=120, fn_id="slo")        # cold; the following calls are warm
    assert st0 == 200 and d0["status"] == "ok"
    via_gw, direct = [], []
    for i in range(N):
        t = time.perf_counter()
        st, d = stack.call("alpha", square, i, keep_warm_s=120, fn_id="slo")
        via_gw.append((time.perf_counter() - t) * 1000)
        assert st == 200 and d["cold_start"] is False
    import http.client
    body_args = base64.b64encode(cloudpickle.dumps(((3,), {}))).decode()
    bundle = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": square, "enter": [], "exit": []})).decode()
    payload = json.dumps({"app_name": "app", "fn_id": "slo", "bundle_b64": bundle, "args_b64": body_args, "keep_warm_s": 120}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", stack.svc_port)
    hdr = {"X-Aicf-Proxy-Secret": stack.secret, "X-Aicf-Tenant": stack.tenants["alpha"], "X-Aicf-Role": "tenant", "Content-Type": "application/json"}
    for _ in range(N):
        t = time.perf_counter()
        conn.request("POST", "/v1/invoke", payload, hdr)
        r = conn.getresponse()
        r.read()
        direct.append((time.perf_counter() - t) * 1000)
        assert r.status == 200
    def st(xs):
        return {"n": len(xs), "p50": round(pct(xs, 50), 2), "p95": round(pct(xs, 95), 2), "p99": round(pct(xs, 99), 2), "max": round(max(xs), 2)}
    waits = sorted(w * 1000 for w in stack.bx.wait_samples_by_type.get("SERVERLESS_EXEC", []))
    rep = {"label": "[ĐO]", "bus_queue_wait_ms_publish_to_worker_start": {"p50": round(pct(waits, 50), 2), "p95": round(pct(waits, 95), 2)} if waits else None,
           "warm_call_via_gateway_ms": st(via_gw), "warm_call_service_direct_keepalive_ms": st(direct),
           "payload": "~1 KiB request (bundle ~0.5 KiB), result 1 int; includes HTTP + auth + sign + NATS + worker verify + pipe to a warm container",
           "slo_target": {"p50_ms": 20, "p95_ms": 60}}
    out = ROOT / "benchmarks/reports/p_serverless_2_slo.json"
    out.write_text(json.dumps(rep, indent=2) + "\n")
    print(json.dumps(rep, indent=2))
    assert rep["warm_call_via_gateway_ms"]["p50"] <= 20 and rep["warm_call_via_gateway_ms"]["p95"] <= 60, rep

