"""Process sandboxes for tools (plan P5.2).

A tool never runs in the agent's process. Backends, strongest first:
  * DockerSandbox - no network, read-only root, all capabilities dropped, non-root, pid/memory/cpu
    limits, only the tool's own file mounted (read-only). Isolates filesystem AND network.
  * BwrapSandbox  - bubblewrap: filesystem isolation only. On hosts where user namespaces are
    restricted (this dev machine: AppArmor) it cannot unshare the network, so it reports
    isolates_network=False and `pick_sandbox` refuses it unless the caller allows that.
  * GpuDockerSandbox + WarmDockerPool - serverless runtime (ADR-003): the same hardening plus an optional GPU
    (`--runtime=nvidia`, NOT `--gpus`, which fails on this host) and a long-lived container per (tenant, env_hash, fn).
  * NullSandbox   - used where nothing is available (e.g. Windows today): it refuses to run
    anything. Fail closed, never fall back to running the tool unsandboxed."""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class SandboxError(RuntimeError):
    """The tool could not be run or was killed (timeout, memory, output size)."""


class SandboxUnavailable(SandboxError):
    pass


@dataclass
class SandboxLimits:
    timeout_s: float = 10.0
    memory_mb: int = 128
    pids: int = 64
    max_output_bytes: int = 64 * 1024


class Sandbox:
    isolates_network = False
    isolates_filesystem = False
    name = "base"

    def run(self, script: Path, payload: dict, limits: SandboxLimits) -> dict:
        raise NotImplementedError


def _collect(proc_out: bytes, rc: int, err: bytes, limits: SandboxLimits) -> dict:
    if len(proc_out) > limits.max_output_bytes:
        raise SandboxError(f"tool output exceeds {limits.max_output_bytes} bytes")
    if rc != 0:
        raise SandboxError(f"tool exited with {rc}: {err[-200:].decode(errors='replace')}")
    try:
        return json.loads(proc_out.decode())
    except ValueError as e:
        raise SandboxError(f"tool did not return JSON: {e}") from e


class DockerSandbox(Sandbox):
    isolates_network = True
    isolates_filesystem = True
    name = "docker"

    def __init__(self, image: str = "python:3.12-alpine"):
        self.image = image
        self.no_new_privileges = self._nnp_works(image)

    @staticmethod
    def _nnp_works(image: str) -> bool:
        """no-new-privileges is defence in depth (the tool is non-root with no capabilities anyway).
        On some hosts (seen here: Docker + AppArmor) it makes every exec fail, so it is used only when it works."""
        r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                            "--user", "65534:65534", image, "python", "-c", "pass"], capture_output=True, timeout=60)
        return r.returncode == 0

    @staticmethod
    def available() -> bool:
        if not shutil.which("docker"):
            return False
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0

    def command(self, name: str, script: Path, limits: SandboxLimits) -> list:
        return ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--pids-limit", str(limits.pids), "--memory", f"{limits.memory_mb}m",
                "--memory-swap", f"{limits.memory_mb}m", "--cpus", "1", "--user", "65534:65534",
                *(["--security-opt", "no-new-privileges"] if self.no_new_privileges else []),
                "--tmpfs", "/tmp:size=8m,noexec,nosuid", "-v", f"{script.resolve()}:/tool/tool.py:ro", "-w", "/tool",
                self.image, "python", "-I", "/tool/tool.py"]

    def run(self, script: Path, payload: dict, limits: SandboxLimits) -> dict:
        name = "aicf-tool-" + uuid.uuid4().hex[:12]
        cmd = self.command(name, script, limits)
        try:
            p = subprocess.run(cmd, input=json.dumps(payload).encode(), capture_output=True, timeout=limits.timeout_s)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            raise SandboxError(f"tool timed out after {limits.timeout_s}s") from None
        return _collect(p.stdout, p.returncode, p.stderr, limits)


class BwrapSandbox(Sandbox):
    isolates_filesystem = True
    name = "bwrap"

    def __init__(self):
        self._net = False                      # network isolation is decided by probing, not by hope
        base = self._base()
        if shutil.which("bwrap") and subprocess.run(base + ["--unshare-net", "true"], capture_output=True).returncode == 0:
            self._net = True
        self.isolates_network = self._net

    @staticmethod
    def _base() -> list:
        return ["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--unshare-pid", "--unshare-ipc", "--new-session", "--die-with-parent", "--clearenv"]

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("bwrap")) and subprocess.run(BwrapSandbox._base() + ["true"], capture_output=True).returncode == 0

    def run(self, script: Path, payload: dict, limits: SandboxLimits) -> dict:
        cmd = self._base() + (["--unshare-net"] if self._net else []) + ["--ro-bind", str(script.resolve()), "/tool/tool.py",
                                                                          "/usr/bin/python3", "-I", "/tool/tool.py"]
        try:
            p = subprocess.run(cmd, input=json.dumps(payload).encode(), capture_output=True, timeout=limits.timeout_s)
        except subprocess.TimeoutExpired:
            raise SandboxError(f"tool timed out after {limits.timeout_s}s") from None
        return _collect(p.stdout, p.returncode, p.stderr, limits)


class NullSandbox(Sandbox):
    name = "none"

    def run(self, script, payload, limits):
        raise SandboxUnavailable(f"no sandbox backend on {sys.platform}: refusing to run a tool unsandboxed")


# ---------------------------------------------------------------------------------------------------------------------
# Serverless: GPU sandbox + warm container pool (plan Pha 1)
# ---------------------------------------------------------------------------------------------------------------------
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
ENV_DENY_PREFIXES = ("NVIDIA_", "AICF_", "LD_", "PYTHON", "DOCKER_", "HOME", "PATH", "TMP", "CUDA_VISIBLE")
RUNNER_PATH = Path(__file__).with_name("_warm_runner.py")


class SandboxBusy(SandboxError):
    """No warm container is free and the pool is at capacity (caller should queue or evict)."""


@dataclass
class ServerlessLimits:
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    tmpfs_mb: int = 256
    timeout_s: float = 30.0
    keep_warm_s: float = 60.0
    start_timeout_s: float = 60.0
    max_output_bytes: int = 256 * 1024
    max_frame_bytes: int = 16 * 1024 * 1024
    vram_mb: int = 0
    gpu_index: Optional[int] = None

    def shape(self) -> tuple:
        """Everything baked into the container at start: a warm container is reused only for an identical shape."""
        return (self.memory_mb, self.cpus, self.pids, self.tmpfs_mb, self.vram_mb, self.gpu_index, self.max_frame_bytes)


@dataclass(frozen=True)
class WarmKey:
    tenant_id: str
    env_hash: str
    fn_id: str

    def validate(self) -> None:
        for v in (self.tenant_id, self.fn_id):
            if not NAME_RE.match(v):
                raise SandboxError(f"invalid identifier {v!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.env_hash):
            raise SandboxError("invalid env_hash")


def validate_env(env: Optional[dict]) -> dict:
    out = {}
    for k, v in (env or {}).items():
        if not isinstance(k, str) or not ENV_KEY_RE.match(k) or k.startswith(ENV_DENY_PREFIXES):
            raise SandboxError(f"environment variable {k!r} is not allowed")
        if not isinstance(v, str) or len(v) > 1024 or "\x00" in v or "\n" in v:
            raise SandboxError(f"environment value for {k} is not allowed")
        out[k] = v
    return out


class GpuDockerSandbox(DockerSandbox):
    """argv is ALWAYS built from validated fields; no tenant-controlled value reaches a mount, a flag or the image."""
    name = "docker-gpu"

    def __init__(self, image: str = "python:3.12-slim"):     # glibc: the alpine default of DockerSandbox cannot run CUDA/manylinux wheels
        super().__init__(image)

    def _check_env_image(self, env_dir: Path) -> None:
        spec = Path(env_dir) / "spec.json"
        if spec.exists() and json.loads(spec.read_text()).get("image") != self.image:
            raise SandboxError("environment was built for a different base image than this sandbox runs (ABI mismatch)")

    def warm_command(self, name: str, key: WarmKey, env_dir: Path, limits: ServerlessLimits, env: Optional[dict] = None,
                     worker_id: str = "w0") -> list:
        key.validate()
        if not NAME_RE.match(name) or not NAME_RE.match(worker_id):
            raise SandboxError("invalid container or worker name")
        self._check_env_image(env_dir)
        gpu = []
        if limits.gpu_index is not None:
            gpu = ["--runtime=nvidia", "-e", f"NVIDIA_VISIBLE_DEVICES={int(limits.gpu_index)}", "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"]
        user_env = [x for k, v in validate_env(env).items() for x in ("-e", f"{k}={v}")]
        mem = f"{int(limits.memory_mb)}m"
        return ["docker", "run", "--rm", "-i", "--name", name,
                "--label", "aicf.sl=1", "--label", f"aicf.worker={worker_id}", "--label", f"aicf.tenant={key.tenant_id}",
                "--label", f"aicf.env={key.env_hash[:16]}", "--label", f"aicf.fn={key.fn_id}",
                "--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", str(int(limits.pids)),
                "--memory", mem, "--memory-swap", mem, "--cpus", str(float(limits.cpus)), "--user", "65534:65534",
                *(["--security-opt", "no-new-privileges"] if self.no_new_privileges else []),
                "--tmpfs", f"/tmp:size={int(limits.tmpfs_mb)}m,nosuid", "-w", "/tmp",
                "-v", f"{Path(env_dir).resolve()}:/env:ro", "-v", f"{RUNNER_PATH}:/aicf/runner.py:ro",
                *user_env,
                "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1",
                "-e", f"AICF_MAX_FRAME={int(limits.max_frame_bytes)}", "-e", f"AICF_VRAM_MB={int(limits.vram_mb)}",
                *gpu, self.image, "/env/venv/bin/python", "-I", "-B", "/aicf/runner.py"]


class WarmContainer:
    """One long-lived `docker run -i` process speaking length-prefixed JSON frames. One call at a time (pool holds `lock`)."""

    def __init__(self, name: str, key: WarmKey, limits: ServerlessLimits):
        self.name, self.key, self.limits = name, key, limits
        self.lock = threading.Lock()
        self.created = self.last_used = time.monotonic()
        self.calls = 0
        self.loaded: set = set()
        self.dead = False
        self.killed_by_us = False
        self.proc: Optional[subprocess.Popen] = None
        self._err = deque(maxlen=64)

    # -- lifecycle
    def start(self, cmd: list) -> None:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._drain, daemon=True).start()
        try:
            ready = self._recv(time.monotonic() + self.limits.start_timeout_s)
        except SandboxError:
            self.kill()
            raise
        if not ready.get("ready"):
            self.kill()
            raise SandboxError("runner did not announce readiness")

    def _drain(self) -> None:
        for line in iter(self.proc.stderr.readline, b""):
            self._err.append(line.decode(errors="replace").rstrip()[-300:])

    def stderr_tail(self) -> str:
        return "\n".join(list(self._err)[-8:])[-1000:]

    def kill(self) -> None:
        if not self.dead:
            self.dead = True
            self.killed_by_us = True
        subprocess.run(["docker", "kill", self.name], capture_output=True)
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
            except Exception:
                pass

    # -- framing
    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        fd = self.proc.stdout.fileno()
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            r, _, _ = select.select([fd], [], [], min(left, 0.5))
            if not r:
                if self.proc.poll() is not None:
                    raise EOFError
                continue
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def _recv(self, deadline: float) -> dict:
        try:
            (n,) = struct.unpack(">I", self._read_exact(4, deadline))
            if n > self.limits.max_frame_bytes:
                raise SandboxError(f"response frame of {n} bytes exceeds {self.limits.max_frame_bytes}")
            return json.loads(self._read_exact(n, deadline))
        except TimeoutError:
            self.kill()
            raise SandboxError(f"timed out after {self.limits.timeout_s}s") from None
        except EOFError:
            rc = self.proc.poll()
            self.dead = True
            why = "killed, most likely out of memory (exit 137)" if rc == 137 and not self.killed_by_us else f"exited with {rc}"
            raise SandboxError(f"container {why}: {self.stderr_tail()}") from None

    def call(self, req: dict) -> dict:
        """The caller must hold self.lock (the pool does): one call at a time per container."""
        body = json.dumps(req, separators=(",", ":")).encode()
        if len(body) > self.limits.max_frame_bytes:
            raise SandboxError(f"request of {len(body)} bytes exceeds {self.limits.max_frame_bytes}")
        if self.dead:
            raise SandboxError("container is not running")
        try:
            self.proc.stdin.write(struct.pack(">I", len(body)) + body)
        except (BrokenPipeError, OSError):
            self.dead = True
            raise SandboxError(f"container exited: {self.stderr_tail()}") from None
        resp = self._recv(time.monotonic() + self.limits.timeout_s)
        self.calls += 1
        self.last_used = time.monotonic()
        return resp


class WarmDockerPool:
    """Warm containers keyed by (tenant_id, env_hash, fn_id). A container never serves two tenants (T4)."""

    def __init__(self, sandbox: GpuDockerSandbox, max_containers: int = 8, max_per_key: int = 4, worker_id: str = "w0",
                 clock=time.monotonic):
        self.sb, self.max_containers, self.max_per_key, self.worker_id, self.clock = sandbox, max_containers, max_per_key, worker_id, clock
        self._mu = threading.Lock()
        self._by_key: dict = {}
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- call path
    def call(self, key: WarmKey, env_dir: Path, limits: ServerlessLimits, req: dict, bundle_b64: Optional[str] = None,
             env: Optional[dict] = None) -> tuple:
        """Returns (response, cold_start). `req` is the runner "call" frame without bundle_b64 (added only when the container lacks it)."""
        key.validate()
        c, cold = self._acquire(key, env_dir, limits, env)
        try:
            frame = dict(req, op="call", max_output_bytes=limits.max_output_bytes)
            if frame["sha"] not in c.loaded:
                if bundle_b64 is None:
                    raise SandboxError("bundle is not loaded in the container and was not supplied")
                frame["bundle_b64"] = bundle_b64
            resp = c.call(frame)
            if resp.get("loaded") or resp.get("ok"):
                c.loaded.add(frame["sha"])
            resp["container"] = c.name
            return resp, cold
        finally:
            if not c.dead:
                c.last_used = self.clock()
            c.lock.release()
            if c.dead:
                self._forget(c)

    def _acquire(self, key: WarmKey, env_dir: Path, limits: ServerlessLimits, env: Optional[dict]) -> tuple:
        with self._mu:
            lst = self._by_key.setdefault(key, [])
            for c in lst:
                if not c.dead and c.limits.shape() == limits.shape() and c.lock.acquire(blocking=False):
                    return c, False
            total = sum(len(v) for v in self._by_key.values())
            if sum(1 for c in lst if c.limits.shape() == limits.shape()) >= self.max_per_key or total >= self.max_containers:
                raise SandboxBusy(f"warm pool is full ({total}/{self.max_containers} containers, {len(lst)}/{self.max_per_key} for this function)")
            name = "aicf-sl-" + uuid.uuid4().hex[:12]
            c = WarmContainer(name, key, limits)
            c.lock.acquire()
            lst.append(c)
        try:
            c.start(self.sb.warm_command(name, key, env_dir, limits, env, self.worker_id))
        except BaseException:
            c.lock.release()
            self._forget(c)
            raise
        return c, True

    def _forget(self, c: WarmContainer) -> None:
        with self._mu:
            lst = self._by_key.get(c.key, [])
            if c in lst:
                lst.remove(c)
            if not lst:
                self._by_key.pop(c.key, None)

    # -- scale to zero / eviction
    def reap_idle(self, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        victims = []
        with self._mu:
            for lst in self._by_key.values():
                for c in lst:
                    if c.dead or (not c.lock.locked() and now - c.last_used >= c.limits.keep_warm_s):
                        if c.lock.acquire(blocking=False):
                            victims.append(c)
        for c in victims:
            c.kill()
            c.lock.release()
            self._forget(c)
        return [c.name for c in victims]

    def start_reaper(self, interval_s: float = 1.0) -> None:
        def loop():
            while not self._stop.wait(interval_s):
                self.reap_idle()
        self._reaper = threading.Thread(target=loop, daemon=True)
        self._reaper.start()

    def kill_all(self, tenant_id: Optional[str] = None) -> list:
        """Evict (G-RECLAIM path): one `docker kill` for every container so the whole set dies together."""
        with self._mu:
            cs = [c for lst in self._by_key.values() for c in lst if tenant_id is None or c.key.tenant_id == tenant_id]
        for c in cs:
            c.dead = c.killed_by_us = True
        if cs:
            subprocess.run(["docker", "kill", *[c.name for c in cs]], capture_output=True, timeout=30)
        for c in cs:
            if c.proc is not None:
                try:
                    c.proc.wait(timeout=10)
                except Exception:
                    c.proc.kill()
            self._forget(c)
        return [c.name for c in cs]

    def snapshot(self) -> list:
        now = self.clock()
        with self._mu:
            return [{"name": c.name, "tenant_id": c.key.tenant_id, "env_hash": c.key.env_hash, "fn_id": c.key.fn_id, "calls": c.calls,
                     "busy": c.lock.locked(), "idle_s": round(now - c.last_used, 3), "keep_warm_s": c.limits.keep_warm_s,
                     "vram_mb": c.limits.vram_mb, "gpu_index": c.limits.gpu_index}
                    for lst in self._by_key.values() for c in lst]

    def docker_names(self) -> list:
        """Source of truth for 'no orphans': every container of this worker according to Docker."""
        r = subprocess.run(["docker", "ps", "-a", "--filter", "label=aicf.sl=1", "--filter", f"label=aicf.worker={self.worker_id}",
                            "--format", "{{.Names}}"], capture_output=True, text=True)
        return r.stdout.split()

    def kill_orphans(self) -> list:
        known = {c["name"] for c in self.snapshot()}
        orphans = [n for n in self.docker_names() if n not in known]
        if orphans:
            subprocess.run(["docker", "kill", *orphans], capture_output=True)
        return orphans

    def close(self) -> None:
        self._stop.set()
        self.kill_all()


def pick_sandbox(require_network_isolation: bool = True) -> Sandbox:
    if DockerSandbox.available():
        return DockerSandbox()
    if BwrapSandbox.available():
        b = BwrapSandbox()
        if b.isolates_network or not require_network_isolation:
            return b
    return NullSandbox()
