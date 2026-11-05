"""GPU admission / monitor / control endpoint with a fake nvidia-smi and a fake pool: the safety invariants, no Docker, no GPU."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from agents.sandbox import NoCapacity
from agents.sl_gpu import ControlServer, GpuAdmission, GpuMonitor, GpuProbe

CID = "a" * 64


class FakeSmi:
    """Plays nvidia-smi for one 12288 MiB GPU. `used` is what is already allocated by anybody."""

    def __init__(self, used=0, total=12288, procs=None):
        self.used, self.total, self.procs = used, total, procs or {}

    def __call__(self, args):
        if "--query-gpu=memory.free,memory.total" in args:
            return f"{self.total - self.used}, {self.total}\n"
        if "--query-gpu=uuid" in args:
            return "GPU-uuid-0\n"
        if "--query-compute-apps=gpu_uuid,pid,used_memory" in args:
            return "".join(f"GPU-uuid-0, {pid}, {mib}\n" for pid, mib in self.procs.items()) + "GPU-other, 999, 5000\n"
        raise AssertionError(args)


def probe(smi):
    return GpuProbe(run=smi)


def test_probe_parses_memory_and_filters_processes_to_the_gpu():
    smi = FakeSmi(used=2000, procs={11: 300, 12: 700})
    p = probe(smi)
    assert p.mem(0) == (10288, 12288)
    assert p.procs(0) == {11: 300, 12: 700}                  # the process of another GPU (uuid GPU-other) is not ours


def test_admits_exactly_up_to_the_headroom_and_not_one_mib_more():
    adm = GpuAdmission(probe(FakeSmi(used=0)), headroom_mib=1536)
    adm.reserve("c1", 0, 12288 - 1536)                       # leaves exactly the headroom: allowed
    adm2 = GpuAdmission(probe(FakeSmi(used=0)), headroom_mib=1536)
    with pytest.raises(NoCapacity):
        adm2.reserve("c1", 0, 12288 - 1536 + 1)              # one MiB into the headroom: refused


def test_requested_8192_with_only_4096_free_is_refused_before_any_container_exists():
    adm = GpuAdmission(probe(FakeSmi(used=12288 - 4096)), headroom_mib=1536)
    with pytest.raises(NoCapacity, match="8192 MiB requested"):
        adm.reserve("c1", 0, 8192)
    assert adm.limit_of("c1") is None and adm.rejected == 1
    adm.reserve("c2", 0, 2560)                               # 4096 - 1536 fits


def test_two_cold_starts_cannot_both_pass_on_the_same_free_memory():
    adm = GpuAdmission(probe(FakeSmi(used=0)), headroom_mib=1536)
    adm.reserve("c1", 0, 6000)                               # nothing allocated yet (still starting)
    with pytest.raises(NoCapacity):
        adm.reserve("c2", 0, 6000)                           # 12288 - 6000 (promised) - 6000 = 288 < 1536
    adm.reserve("c3", 0, 4000)


def test_reservation_is_not_counted_twice_once_the_container_has_allocated():
    smi = FakeSmi(used=0)
    adm = GpuAdmission(probe(smi), headroom_mib=1536)
    adm.reserve("c1", 0, 6000)
    smi.used = 6000                                          # c1 really allocated it
    adm.observe({"c1": 6000})
    assert adm.uncovered_mib(0) == 0
    adm.reserve("c2", 0, 4000)                               # free 6288 - 4000 = 2288 >= 1536


def test_partial_allocation_keeps_the_rest_reserved():
    smi = FakeSmi(used=2000)
    adm = GpuAdmission(probe(smi), headroom_mib=1536)
    adm.reserve("c1", 0, 6000)
    adm.observe({"c1": 2000})
    assert adm.uncovered_mib(0) == 4000


def test_release_returns_the_reservation():
    adm = GpuAdmission(probe(FakeSmi()), headroom_mib=1536)
    adm.reserve("c1", 0, 9000)
    with pytest.raises(NoCapacity):
        adm.reserve("c2", 0, 5000)
    adm.release("c1")
    adm.reserve("c2", 0, 5000)


def test_concurrent_reservations_never_over_commit():
    adm = GpuAdmission(probe(FakeSmi()), headroom_mib=1536)
    ok, bad = [], []

    def go(i):
        try:
            adm.reserve(f"c{i}", 0, 2000)
            ok.append(i)
        except NoCapacity:
            bad.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) * 2000 <= 12288 - 1536 and len(ok) == (12288 - 1536) // 2000 and len(bad) == 40 - len(ok)


def test_zero_or_missing_vram_declaration_is_refused():
    adm = GpuAdmission(probe(FakeSmi()), headroom_mib=1536)
    with pytest.raises(NoCapacity):
        adm.reserve("c1", 0, 0)


class FakePool:
    def __init__(self, names):
        self.names, self.killed, self.kill_all_calls = names, [], []

    def snapshot(self):
        return [{"name": n, "gpu_index": 0} for n in self.names]

    def container_id(self, name):
        return CID if name == "c1" else None

    def kill_container(self, name, reason):
        self.killed.append((name, reason))
        return True

    def kill_all(self, tenant_id=None, reason=None, gpu_only=False, wait=True):
        self.kill_all_calls.append((reason, gpu_only))
        return list(self.names)


class CgProbe(GpuProbe):
    @staticmethod
    def container_of(pid):
        return CID if pid == 11 else None


def monitor(smi, limit, tol=64):
    adm = GpuAdmission(CgProbe(run=smi), 1536, tolerance_mib=tol)
    adm.reserve("c1", 0, limit)
    pool = FakePool(["c1"])
    return GpuMonitor(pool, CgProbe(run=smi), adm, 0), pool, adm


def test_container_over_its_declared_vram_is_killed_with_a_reason():
    smi = FakeSmi(used=3000, procs={11: 3000})
    m, pool, _ = monitor(smi, limit=2048)
    m.tick()
    assert len(pool.killed) == 1 and "vram_limit" in pool.killed[0][1] and "3000" in pool.killed[0][1]


def test_tolerance_is_respected_and_boundary_is_exclusive():
    m, pool, _ = monitor(FakeSmi(used=2112, procs={11: 2112}), limit=2048)           # exactly limit + tol
    m.tick()
    assert pool.killed == []
    m2, pool2, _ = monitor(FakeSmi(used=2113, procs={11: 2113}), limit=2048)
    m2.tick()
    assert len(pool2.killed) == 1


def test_processes_of_other_containers_are_not_attributed():
    m, pool, _ = monitor(FakeSmi(used=0, procs={77: 9000}), limit=2048)           # pid 77 is the LLM engine / a human
    m.tick()
    assert pool.killed == [] and m.last == {}


def test_monitor_survives_nvidia_smi_failures():
    def boom(args):
        raise RuntimeError("nvidia-smi hung")
    m, pool, _ = monitor(FakeSmi(), limit=1000)
    m.probe = GpuProbe(run=boom)
    m.tick()
    assert m.errors == 1 and pool.killed == []


SECRET = "control-secret-0123456789"


def http(srv, method, path, secret=SECRET):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method, data=b"" if method == "POST" else None, headers={"X-Aicf-Evict-Secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_control_endpoint_needs_the_secret_and_evicts_only_gpu_containers():
    pool = FakePool(["c1", "c2"])
    m, _, _ = monitor(FakeSmi(procs={11: 500}), limit=2048)
    m.last = {"c1": 500, "c2": 700}
    srv = ControlServer(pool, m, SECRET, port=0).start()
    try:
        assert http(srv, "POST", "/v1/evict", secret="wrong")[0] == 403
        assert http(srv, "POST", "/v1/evict", secret="")[0] == 403
        assert http(srv, "GET", "/v1/status", secret="wrong")[0] == 403
        assert pool.kill_all_calls == []
        assert http(srv, "GET", "/v1/status") == (200, {"vram_mib": 1200, "containers": 2})
        st, doc = http(srv, "POST", "/v1/evict")
        assert st == 200 and doc == {"evicted": 2} and pool.kill_all_calls == [("evicted: the GPU owner needs its VRAM back", True)]
        assert http(srv, "POST", "/v1/nope")[0] == 404
    finally:
        srv.close()


def test_control_server_refuses_a_weak_secret():
    with pytest.raises(ValueError):
        ControlServer(FakePool([]), None, "short", port=0)
