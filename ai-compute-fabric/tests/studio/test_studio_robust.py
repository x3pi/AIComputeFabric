"""Concurrency and hostile input against the Studio service (real PostgreSQL, fake worker and LLM)."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.studio.test_studio_service import call, needs_pg, new_job, svc, tenant, wait_status  # noqa: F401


def raw_http(service, request_bytes):
    host, port = service.url[len("http://"):].split(":")
    import socket
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(request_bytes)
    s.shutdown(socket.SHUT_WR)
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    s.close()
    return data


@needs_pg
def test_parallel_saves_of_one_template_get_distinct_versions(svc, tenant):  # noqa: F811
    svc.settings  # quota max_prompt_versions=3 in this fixture is too small; use several names instead
    names = [f"n{i}" for i in range(3)]
    def save(i):
        return call(svc, "POST", f"/v1/tenants/{tenant}/prompts", {"name": names[i % 3], "system": f"s{i}"}, role="operator")
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(save, range(9)))
    versions = {}
    for st, r in res:
        assert st in (201, 429, 409), (st, r)           # the per-name quota is 3 in this fixture
        if st == 201:
            versions.setdefault(r["name"], []).append(r["version"])
    for n, v in versions.items():
        assert len(v) == len(set(v)), f"duplicate version numbers for {n}: {v}"
        assert sorted(v) == list(range(1, len(v) + 1)), f"gap in versions of {n}: {sorted(v)}"


@needs_pg
def test_many_parallel_reads_and_jobs_do_not_run_two_gpu_jobs_at_once(svc, tenant):  # noqa: F811
    seen = []
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            _, d = call(svc, "GET", f"/v1/tenants/{tenant}/lora/jobs", role="viewer")
            seen.append(sum(j["status"] == "running" for j in d["jobs"]))
    t = threading.Thread(target=watch)
    t.start()
    ids = [new_job(svc, tenant)[1]["id"] for _ in range(2)]
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: call(svc, "GET", "/v1/methods", role="viewer")[0], range(200)))
    for i in ids:
        wait_status(svc, tenant, i, ("succeeded",))
    stop.set()
    t.join()
    assert set(codes) == {200} and max(seen) <= 1, f"running at once: {max(seen)}"


@needs_pg
@pytest.mark.parametrize("name", ["x'; DROP TABLE studio_jobs;--", "a" * 5000, "../../etc/passwd", "ünï😀code", "x\x00y", " ", "%00"])
def test_hostile_names_never_cause_a_500(svc, tenant, name):  # noqa: F811
    for path in (f"/v1/tenants/{tenant}/prompts/{name}", f"/v1/tenants/{name}/prompts", f"/v1/tenants/{tenant}/lora/jobs/{name}"):
        try:
            st, _ = call(svc, "GET", path.replace(" ", "%20"), role="viewer")
        except Exception:
            continue                                   # urllib refusing to even send it is fine
        assert st in (400, 404), (path[:60], st)
    st, _ = call(svc, "POST", f"/v1/tenants/{tenant}/prompts", {"name": name, "system": "s"}, role="operator")
    assert st in (201, 400), st
    assert call(svc, "GET", "/v1/health", role="viewer")[0] == 200, "service still alive and tables intact"


@needs_pg
def test_malformed_requests(svc):  # noqa: F811
    r = raw_http(svc, b"POST /v1/tenants/t1/prompts HTTP/1.1\r\nHost: x\r\nX-Aicf-Role: operator\r\nContent-Type: application/json\r\nContent-Length: 5\r\n\r\n{bad}")
    assert b" 400 " in r.split(b"\r\n")[0]
    r = raw_http(svc, b"POST /v1/tenants/t1/prompts HTTP/1.1\r\nHost: x\r\nX-Aicf-Role: operator\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n[]")
    assert b" 400 " in r.split(b"\r\n")[0]
    big = svc.settings.max_body + 1
    r = raw_http(svc, f"POST /v1/tenants/t1/prompts HTTP/1.1\r\nHost: x\r\nX-Aicf-Role: operator\r\nContent-Length: {big}\r\n\r\n".encode())
    assert b" 413 " in r.split(b"\r\n")[0]
    r = raw_http(svc, b"POST /v1/tenants/t1/prompts HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n")
    assert b" 400 " in r.split(b"\r\n")[0]
    r = raw_http(svc, b"FOO /v1/methods HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b" 500 " not in r.split(b"\r\n")[0]
    assert call(svc, "POST", "/v1/tenants/t1/prompts", {"name": "a", "system": "x\u0000y"}, role="operator")[0] == 400, "NUL in a body string"
    assert call(svc, "GET", "/v1/health", role="viewer")[0] == 200


@needs_pg
def test_unknown_role_header_is_treated_as_viewer(svc, tenant):  # noqa: F811
    for role in ("root", "ADMINISTRATOR", "admin; operator", "0", "operator\r"):
        try:
            st, _ = call(svc, "POST", f"/v1/tenants/{tenant}/prompts", {"name": "a" + uuid.uuid4().hex[:4], "system": "s"}, role=role)
        except Exception:
            continue
        assert st == 403, (role, st)
