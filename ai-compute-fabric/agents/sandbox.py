"""Process sandboxes for tools (plan P5.2).

A tool never runs in the agent's process. Backends, strongest first:
  * DockerSandbox - no network, read-only root, all capabilities dropped, non-root, pid/memory/cpu
    limits, only the tool's own file mounted (read-only). Isolates filesystem AND network.
  * BwrapSandbox  - bubblewrap: filesystem isolation only. On hosts where user namespaces are
    restricted (this dev machine: AppArmor) it cannot unshare the network, so it reports
    isolates_network=False and `pick_sandbox` refuses it unless the caller allows that.
  * NullSandbox   - used where nothing is available (e.g. Windows today): it refuses to run
    anything. Fail closed, never fall back to running the tool unsandboxed."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


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

    @property
    def isolates_network(self) -> bool:  # decided by probing, not by hope
        return self._net

    def __init__(self):
        self._net = False
        base = self._base()
        if shutil.which("bwrap") and subprocess.run(base + ["--unshare-net", "true"], capture_output=True).returncode == 0:
            self._net = True

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


def pick_sandbox(require_network_isolation: bool = True) -> Sandbox:
    if DockerSandbox.available():
        return DockerSandbox()
    if BwrapSandbox.available():
        b = BwrapSandbox()
        if b.isolates_network or not require_network_isolation:
            return b
    return NullSandbox()
