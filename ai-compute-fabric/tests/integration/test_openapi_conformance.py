"""P1.2: contracts/openapi/aicf_v1.yaml against a REAL running site-gateway.

Builds the gateway and the load generator, registers simulated workers over gRPC, then (1) calls every parameterless GET in the
spec and validates the body against its schema, (2) exercises the tenant and pause/resume writes, (3) checks the reverse
direction: every /api/ route the Go sources register appears in the spec. Skipped when the Go toolchain is not installed."""
import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = yaml.safe_load((ROOT / "contracts/openapi/aicf_v1.yaml").read_text())
GATEWAY = ROOT / "apps/site-gateway"

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain not installed")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def validate(name, instance):
    jsonschema.validate(instance, {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]})


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    d = tmp_path_factory.mktemp("gw")
    for out, pkg in (("gw", "."), ("lg", "./cmd/loadgen")):
        subprocess.run(["go", "build", "-o", str(d / out), pkg], cwd=GATEWAY, check=True)
    http_p, grpc_p = free_port(), free_port()
    gw = subprocess.Popen([str(d / "gw"), "-http", f"127.0.0.1:{http_p}", "-grpc", f"127.0.0.1:{grpc_p}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{http_p}"
    for _ in range(50):
        try:
            call(base, "GET", "/api/whoami")
            break
        except OSError:
            time.sleep(0.1)
    lg = subprocess.Popen([str(d / "lg"), "-workers", "3", "-duration", "40s", "-grpc", f"127.0.0.1:{grpc_p}", "-http", f"127.0.0.1:{http_p}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        st, raw, _h = call(base, "GET", "/api/workers")
        if st == 200 and len(json.loads(raw)) >= 3:
            break
        time.sleep(0.2)
    yield base
    for p in (lg, gw):
        p.terminate()
        p.wait(10)


def schema_name(op):
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]
    return ref.get("$ref", "").rsplit("/", 1)[-1] or ref.get("items", {}).get("$ref", "").rsplit("/", 1)[-1]


def test_every_parameterless_get_matches_its_schema(gateway):
    checked = []
    for path, item in SPEC["paths"].items():
        op = item.get("get")
        if not op or "{" in path or path.startswith("/api/agents") or path.endswith("/stream"):
            continue
        st, raw, h = call(gateway, "GET", path)
        assert st == 200, (path, st, raw[:200])
        body = json.loads(raw)
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        if schema.get("type") == "array":
            assert isinstance(body, list), path
            for x in body:
                validate(schema["items"]["$ref"].rsplit("/", 1)[-1], x)
        else:
            validate(schema["$ref"].rsplit("/", 1)[-1], body)
        checked.append(path)
    assert set(checked) >= {"/api/whoami", "/api/stats", "/api/workers", "/api/models/catalog", "/api/vram/estimate", "/api/tenants"}


def test_workers_page_header_and_single_worker(gateway):
    st, raw, h = call(gateway, "GET", "/api/workers?limit=2")
    assert st == 200 and len(json.loads(raw)) == 2 and int(h["X-Total-Count"]) >= 3
    wid = json.loads(raw)[0]["worker_id"]
    st, raw, _ = call(gateway, "GET", f"/api/workers/{wid}")
    validate("Worker", json.loads(raw))
    assert call(gateway, "GET", "/api/workers/no-such")[0] == 404


def test_pause_resume_and_unknown_worker(gateway):
    wid = json.loads(call(gateway, "GET", "/api/workers")[1])[0]["worker_id"]
    assert call(gateway, "POST", f"/api/workers/{wid}/pause")[0] == 204
    assert call(gateway, "POST", f"/api/workers/{wid}/resume")[0] == 204
    assert call(gateway, "POST", "/api/workers/no-such/pause")[0] == 404


def test_tenant_lifecycle_matches_schemas(gateway):
    st, raw, _ = call(gateway, "POST", "/api/tenants", {"name": "conformance"})
    assert st == 201
    created = json.loads(raw)
    validate("TenantCreated", created)
    tid = created["tenant"]["id"]
    assert call(gateway, "POST", "/api/tenants", {"name": "conformance"})[0] == 409
    st, raw, _ = call(gateway, "PATCH", f"/api/tenants/{tid}", {"weight": 3, "disabled": True})
    assert st == 200
    validate("Tenant", json.loads(raw))
    assert json.loads(raw)["weight"] == 3 and json.loads(raw)["disabled"] is True
    st, raw, _ = call(gateway, "POST", f"/api/tenants/{tid}/rotate-key")
    assert st == 200 and json.loads(raw)["api_key"] != created["api_key"]
    assert call(gateway, "PATCH", "/api/tenants/t_nope", {"weight": 1})[0] == 404


def test_fleet_stream_frame_matches_schema(gateway):
    with urllib.request.urlopen(gateway + "/api/fleet/stream", timeout=10) as r:
        line = r.readline().decode()
    assert line.startswith("data: ")
    validate("FleetFrame", json.loads(line[6:]))


def test_every_api_route_in_the_go_code_is_in_the_spec():
    declared = set()
    for f in GATEWAY.glob("*.go"):
        if f.name.endswith("_test.go"):
            continue
        declared |= set(re.findall(r'Handle(?:Func)?\("(/api/[^"]*)"', f.read_text()))
    in_spec = {re.sub(r"/\{[^}]+\}", "", p) for p in SPEC["paths"]}
    full = set(SPEC["paths"])
    # a trailing-slash pattern covers a subtree: it is documented when some spec path lives under it
    missing = sorted(p for p in declared if not (p in in_spec or p.rstrip("/") in in_spec or (p.endswith("/") and any(q.startswith(p) for q in full))))
    assert not missing, f"routes registered in Go but absent from aicf_v1.yaml: {missing}"
