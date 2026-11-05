"""GPU side of the serverless worker (ADR-003 D3/D10, threats T6/T7).

* GpuProbe     - free/total VRAM and per-container VRAM from nvidia-smi (pid -> container through /proc/<pid>/cgroup, measured 10/10 in phase 0).
* GpuAdmission - the invariant: a container may start only if  free - uncovered_reservations - vram_mb >= headroom.  `uncovered` is what
                 already-admitted containers have reserved but not yet allocated, so two cold starts racing cannot both pass.
* GpuMonitor   - kills a container whose measured VRAM exceeds its reservation (+ tolerance); the in-flight call gets status `oom`.
* ControlServer- loopback endpoint the worker-guard calls: POST /v1/evict (stop all GPU guests) and GET /v1/status (their VRAM).
All of it is inert unless the worker is started with --gpu-enabled."""
from __future__ import annotations

import hmac
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from agents.sandbox import NoCapacity

_CG = re.compile(r"(?:docker-|/docker/)([0-9a-f]{64})")


class GpuProbe:
    """nvidia-smi based. `run` is injectable for tests."""

    def __init__(self, run: Optional[Callable] = None, timeout_s: float = 3.0):
        self._run, self.timeout = run or self._exec, timeout_s

    def _exec(self, args: list) -> str:
        return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=self.timeout, check=True).stdout

    def mem(self, idx: int) -> tuple:
        """(free_mib, total_mib)"""
        out = self._run(["-i", str(int(idx)), "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"])
        f, t = [int(x.strip()) for x in out.strip().splitlines()[0].split(",")]
        return f, t

    def procs(self, idx: int) -> dict:
        """{pid: used_mib} for compute processes on GPU idx."""
        uuid = self._run(["-i", str(int(idx)), "--query-gpu=uuid", "--format=csv,noheader"]).strip()
        out = self._run(["--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"])
        res = {}
        for ln in out.strip().splitlines():
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) == 3 and parts[0] == uuid and parts[1].isdigit() and parts[2].isdigit():
                res[int(parts[1])] = int(parts[2])
        return res

    @staticmethod
    def container_of(pid: int) -> Optional[str]:
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                m = _CG.search(f.read())
            return m.group(1) if m else None
        except OSError:
            return None


class GpuAdmission:
    def __init__(self, probe: GpuProbe, headroom_mib: int, tolerance_mib: int = 64):
        self.probe, self.headroom, self.tol = probe, int(headroom_mib), int(tolerance_mib)
        self._res: dict = {}            # container name -> (gpu, reserved_mib)
        self._usage: dict = {}          # container name -> last measured MiB (fed by GpuMonitor)
        self._mu = threading.Lock()
        self.rejected = 0

    def uncovered_mib(self, gpu: int) -> int:
        with self._mu:
            return sum(max(0, r - self._usage.get(n, 0)) for n, (g, r) in self._res.items() if g == gpu)

    def reserve(self, name: str, gpu: int, vram_mib: int) -> None:
        if vram_mib <= 0:
            raise NoCapacity("GPU functions must declare vram_mb > 0")
        with self._mu:                                     # serialised: the check and the reservation are one step
            free, total = self.probe.mem(gpu)
            uncovered = sum(max(0, r - self._usage.get(n, 0)) for n, (g, r) in self._res.items() if g == gpu)
            left = free - uncovered - vram_mib
            if left < self.headroom:
                self.rejected += 1
                raise NoCapacity(f"{vram_mib} MiB requested but only {max(0, free - uncovered - self.headroom)} MiB can be given without taking the "
                                 f"{self.headroom} MiB headroom (free {free}, already promised to starting containers {uncovered})")
            self._res[name] = (gpu, vram_mib)

    def release(self, name: str) -> None:
        with self._mu:
            self._res.pop(name, None)
            self._usage.pop(name, None)

    def observe(self, usage: dict) -> None:
        with self._mu:
            self._usage = {n: m for n, m in usage.items() if n in self._res}

    def limit_of(self, name: str) -> Optional[int]:
        with self._mu:
            r = self._res.get(name)
            return r[1] if r else None


class GpuMonitor(threading.Thread):
    """Samples per-container VRAM; a container over its reservation (+ tolerance) is killed with reason `vram_limit` (T7)."""

    def __init__(self, pool, probe: GpuProbe, admission: GpuAdmission, gpu: int, interval_s: float = 0.25):
        super().__init__(daemon=True, name="sl-gpu-monitor")
        self.pool, self.probe, self.adm, self.gpu, self.interval = pool, probe, admission, gpu, interval_s
        self._stop_ev = threading.Event()
        self.last: dict = {}
        self.killed: list = []
        self.errors = 0

    def sample(self) -> dict:
        names = {c["name"] for c in self.pool.snapshot()}
        by_id: dict = {}
        for pid, mib in self.probe.procs(self.gpu).items():
            cid = self.probe.container_of(pid)
            if cid:
                by_id[cid] = by_id.get(cid, 0) + mib
        usage = {}
        for name in names:
            cid = self.pool.container_id(name)
            if cid and cid in by_id:
                usage[name] = by_id[cid]
        return usage

    def tick(self) -> None:
        try:
            usage = self.sample()
        except Exception:
            self.errors += 1
            return
        self.last = usage
        self.adm.observe(usage)
        for name, mib in usage.items():
            limit = self.adm.limit_of(name)
            if limit is not None and mib > limit + self.adm.tol:
                self.killed.append((name, mib, limit))                       # recorded first: the caller wakes up the instant the kill lands
                if not self.pool.kill_container(name, f"vram_limit: used {mib} MiB > declared vram_mb={limit}"):
                    self.killed.pop()

    def run(self) -> None:
        while not self._stop_ev.wait(self.interval):
            self.tick()

    def stop(self) -> None:
        self._stop_ev.set()


class ControlServer:
    """POST /v1/evict, GET /v1/status. Loopback + shared secret. Used by worker-guard (Go) only."""

    def __init__(self, pool, monitor: Optional[GpuMonitor], secret: str, host: str = "127.0.0.1", port: int = 18111):
        if len(secret) < 16:
            raise ValueError("control secret must be at least 16 characters")
        outer = self
        self.pool, self.monitor, self.secret = pool, monitor, secret.encode()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self) -> bool:
                if hmac.compare_digest(self.headers.get("X-Aicf-Evict-Secret", "").encode(), outer.secret):
                    return True
                self._json(403, {"error": "forbidden"})
                return False

            def do_POST(self):
                if not self._auth():
                    return
                if self.path != "/v1/evict":
                    return self._json(404, {"error": "not found"})
                killed = outer.pool.kill_all(reason="evicted: the GPU owner needs its VRAM back", gpu_only=True, wait=False)
                self._json(200, {"evicted": len(killed)})

            def do_GET(self):
                if not self._auth():
                    return
                if self.path != "/v1/status":
                    return self._json(404, {"error": "not found"})
                gpu = [c for c in outer.pool.snapshot() if c["gpu_index"] is not None]
                used = sum((outer.monitor.last if outer.monitor else {}).get(c["name"], 0) for c in gpu)
                self._json(200, {"vram_mib": used, "containers": len(gpu)})

        self.srv = ThreadingHTTPServer((host, port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True, name="sl-control")

    def start(self) -> "ControlServer":
        self._t.start()
        return self

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
