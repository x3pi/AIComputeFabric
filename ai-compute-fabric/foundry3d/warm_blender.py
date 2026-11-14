"""Quản lý Warm Headless Blender 4.2 Worker trong DockerSandbox cô lập + Cổng MemAvailable (D-3D-2, D-3D-4)."""
from __future__ import annotations

import json
import os
import select
import shutil
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_FOUNDRY_ROOT = Path(os.environ.get("AICF_FOUNDRY3D_ROOT", "/mnt/Data/aicf-3d-foundry"))
DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_MIN_MEM_AVAILABLE_MB = 2048
MAX_WORK_DIR_BYTES = 512 * 1024 * 1024  # 512 MiB cho mỗi phiên /work


class BlenderSandboxError(RuntimeError):
    """Lỗi thực thi hoặc vi phạm bảo mật của Blender worker."""


class LowMemoryQueuedError(BlenderSandboxError):
    """MemAvailable thấp hơn ngưỡng an toàn (>= 2,048 MiB) -> xếp hàng queued_low_mem."""


def read_mem_available_mb(meminfo_path: str = "/proc/meminfo") -> int:
    """Đọc MemAvailable (MiB) từ /proc/meminfo."""
    try:
        with open(meminfo_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except OSError:
        pass
    return 0


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            p = Path(root) / fn
            try:
                if not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


@dataclass
class BlenderLimits:
    memory_mb: int = 1536
    memory_swap_mb: int = 2048
    cpus: float = 4.0
    pids: int = 256
    cpu_ulimit_s: int = 180
    fsize_ulimit_bytes: int = 512 * 1024 * 1024
    call_timeout_s: float = 90.0
    start_timeout_s: float = 45.0
    max_frame_bytes: int = 32 * 1024 * 1024


class WarmBlenderContainer:
    """Một container Blender 4.2 --background --factory-startup -Y giao tiếp khung JSON 4-byte."""

    def __init__(
        self,
        name: str,
        work_dir: Path,
        runtime_root: Path,
        scripts_dir: Path,
        limits: BlenderLimits,
        image: str = DEFAULT_IMAGE,
        no_new_privileges: bool = False,
        artifacts_mount: Optional[Path] = None,
    ) -> None:
        self.name = name
        self.work_dir = work_dir
        self.runtime_root = runtime_root
        self.scripts_dir = scripts_dir
        self.limits = limits
        self.image = image
        self.no_new_privileges = no_new_privileges
        self.artifacts_mount = artifacts_mount
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.dead = False
        self.created_at = time.monotonic()
        self.last_used = self.created_at
        self.calls = 0
        self._err: deque[str] = deque(maxlen=64)

    def build_command(self) -> list[str]:
        sysroot = "/runtime/sysroot"
        ld_paths = ":".join([
            f"{sysroot}/usr/lib/x86_64-linux-gnu",
            f"{sysroot}/lib/x86_64-linux-gnu",
            f"{sysroot}/usr/lib",
            "/runtime/blender-4.2-lts/lib",
        ])
        cmd = [
            "docker", "run", "--rm", "-i", "--name", self.name,
            "--label", "aicf.foundry3d=1",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--pids-limit", str(int(self.limits.pids)),
            "--memory", f"{int(self.limits.memory_mb)}m",
            "--memory-swap", f"{int(self.limits.memory_swap_mb)}m",
            "--cpus", str(float(self.limits.cpus)),
            "--ulimit", f"cpu={int(self.limits.cpu_ulimit_s)}:{int(self.limits.cpu_ulimit_s)}",
            "--ulimit", f"fsize={int(self.limits.fsize_ulimit_bytes)}:{int(self.limits.fsize_ulimit_bytes)}",
            "--user", "65534:65534",
            *(["--security-opt", "no-new-privileges"] if self.no_new_privileges else []),
            "-v", f"{self.runtime_root.resolve()}:/runtime:ro",
            "-v", f"{self.scripts_dir.resolve()}:/aicf_scripts:ro",
            "-v", f"{self.work_dir.resolve()}:/work:rw",
            "-w", "/work",
            "-e", "HOME=/work/.env_tmp",
            "-e", "TMPDIR=/work/.env_tmp",
            "-e", "TEMP=/work/.env_tmp",
            "-e", "TMP=/work/.env_tmp",
            "-e", "XDG_CONFIG_HOME=/work/.env_tmp/xdg_cfg",
            "-e", "XDG_CACHE_HOME=/work/.env_tmp/xdg_cache",
            "-e", "XDG_DATA_HOME=/work/.env_tmp/xdg_data",
            "-e", "BLENDER_USER_CONFIG=/work/.env_tmp/blender_cfg",
            "-e", "BLENDER_USER_SCRIPTS=/work/.env_tmp/blender_scripts",
            "-e", "MESA_SHADER_CACHE_DIR=/work/.env_tmp/mesa_cache",
            "-e", "PYTHONPYCACHEPREFIX=/work/.env_tmp/pycache",
            "-e", "LIBGL_ALWAYS_SOFTWARE=1",
            "-e", "MESA_LOADER_DRIVER_OVERRIDE=llvmpipe",
            "-e", f"PATH={sysroot}/usr/bin:/usr/local/bin:/usr/bin:/bin",
            "-e", f"LD_LIBRARY_PATH={ld_paths}",
            "-e", f"LIBGL_DRIVERS_PATH={sysroot}/usr/lib/x86_64-linux-gnu/dri",
            "-e", f"__EGL_VENDOR_LIBRARY_DIRS={sysroot}/usr/share/glvnd/egl_vendor.d",
            "-e", f"AICF_MAX_FRAME={int(self.limits.max_frame_bytes)}",
            "-e", "AICF_GLTF_VALIDATOR=/runtime/gltf_validator/gltf_validator",
            self.image,
            "python", "-u", "-I", "-B", "/aicf_scripts/_blender_supervisor.py",
        ]
        return cmd

    def start(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.work_dir.chmod(0o777)
        except OSError:
            pass
        cmd = self.build_command()
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._drain_err, daemon=True).start()
        try:
            ready = self._recv(time.monotonic() + self.limits.start_timeout_s)
        except Exception:
            self.kill()
            raise
        if not ready.get("ready"):
            self.kill()
            raise BlenderSandboxError(f"Blender daemon không sẵn sàng: {self.stderr_tail()}")

    def _drain_err(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        for line in iter(self.proc.stderr.readline, b""):
            self._err.append(line.decode(errors="replace").rstrip()[-300:])

    def stderr_tail(self) -> str:
        return "\n".join(list(self._err)[-10:])[-1200:]

    def _read_exact(self, n: int, deadline: float) -> bytes:
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        buf = bytearray()
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

    def _recv(self, deadline: float) -> dict[str, Any]:
        try:
            (length,) = struct.unpack(">I", self._read_exact(4, deadline))
            if length > self.limits.max_frame_bytes:
                raise BlenderSandboxError(f"Frame phản hồi quá lớn: {length}")
            raw = self._read_exact(length, deadline)
            return json.loads(raw.decode("utf-8"))
        except TimeoutError:
            self.kill()
            raise BlenderSandboxError(f"Blender timeout sau {self.limits.call_timeout_s}s") from None
        except EOFError:
            rc = self.proc.poll() if self.proc is not None else None
            self.dead = True
            why = "OOM (exit 137)" if rc == 137 else f"exit {rc}"
            raise BlenderSandboxError(f"Blender container dừng ({why}): {self.stderr_tail()}") from None

    def call(self, req: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        if self.dead or self.proc is None or self.proc.stdin is None:
            raise BlenderSandboxError("Blender container đã dừng")
        if dir_size_bytes(self.work_dir) > MAX_WORK_DIR_BYTES:
            raise BlenderSandboxError("Thư mục /work vượt trần 512 MiB")
        body = json.dumps(req, separators=(",", ":")).encode("utf-8")
        if len(body) > self.limits.max_frame_bytes:
            raise BlenderSandboxError(f"Request quá lớn: {len(body)} bytes")
        try:
            self.proc.stdin.write(struct.pack(">I", len(body)) + body)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.dead = True
            raise BlenderSandboxError(f"Broken pipe tới Blender daemon: {self.stderr_tail()}") from None
        t_limit = timeout_s if timeout_s is not None else self.limits.call_timeout_s
        resp = self._recv(time.monotonic() + t_limit)
        self.calls += 1
        self.last_used = time.monotonic()
        return resp

    def kill(self) -> None:
        self.dead = True
        try:
            if self.proc is not None and self.proc.stdin is not None and not self.proc.stdin.closed:
                body = b'{"op":"shutdown"}'
                self.proc.stdin.write(struct.pack(">I", len(body)) + body)
                self.proc.stdin.flush()
        except Exception:
            pass
        subprocess.run(["docker", "kill", self.name], capture_output=True)
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.work_dir, ignore_errors=True)


class WarmBlenderPool:
    """Bể giữ ấm tối đa 1 container Blender CPU (Mode M1) + kiểm tra MemAvailable trước mỗi job."""

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        work_base: Path | str | None = None,
        scripts_dir: Path | str | None = None,
        limits: BlenderLimits | None = None,
        max_workers: int = 1,
        min_mem_available_mb: int = DEFAULT_MIN_MEM_AVAILABLE_MB,
        mem_reader: Callable[[], int] = read_mem_available_mb,
        image: str = DEFAULT_IMAGE,
    ) -> None:
        base = DEFAULT_FOUNDRY_ROOT
        self.runtime_root = Path(runtime_root) if runtime_root else base / "runtime"
        self.work_base = Path(work_base) if work_base else base / "tmp"
        self.scripts_dir = Path(scripts_dir) if scripts_dir else Path(__file__).resolve().parent / "blender_scripts"
        self.limits = limits or BlenderLimits()
        self.max_workers = max(1, int(max_workers))
        self.min_mem_available_mb = int(min_mem_available_mb)
        self.mem_reader = mem_reader
        self.image = image
        self._mu = threading.Lock()
        self._workers: list[WarmBlenderContainer] = []
        self._nnp_checked: Optional[bool] = None

    def _check_nnp(self) -> bool:
        if self._nnp_checked is None:
            r = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--user", "65534:65534",
                    self.image, "python", "-c", "pass",
                ],
                capture_output=True,
                timeout=30,
            )
            self._nnp_checked = r.returncode == 0
        return self._nnp_checked

    def check_memory_gate(self, *, reuse_warm: bool = False) -> int:
        avail = int(self.mem_reader())
        threshold = max(1024, self.min_mem_available_mb - 640) if reuse_warm else self.min_mem_available_mb
        if avail < threshold:
            raise LowMemoryQueuedError(
                f"MemAvailable={avail} MiB < ngưỡng an toàn {threshold} MiB (queued_low_mem)"
            )
        return avail

    def _acquire(self) -> tuple[WarmBlenderContainer, bool]:
        with self._mu:
            self._workers = [w for w in self._workers if not w.dead]
            for w in self._workers:
                if w.lock.acquire(blocking=False):
                    try:
                        self.check_memory_gate(reuse_warm=True)
                    except Exception:
                        w.lock.release()
                        raise
                    return w, False
            if len(self._workers) < self.max_workers:
                self.check_memory_gate(reuse_warm=False)
                wid = uuid.uuid4().hex[:10]
                cname = f"aicf-3d-test-b42-{wid}"
                wdir = self.work_base / f"pool-{wid}"
                w = WarmBlenderContainer(
                    name=cname,
                    work_dir=wdir,
                    runtime_root=self.runtime_root,
                    scripts_dir=self.scripts_dir,
                    limits=self.limits,
                    image=self.image,
                    no_new_privileges=self._check_nnp(),
                )
                w.lock.acquire()
                self._workers.append(w)
                try:
                    w.start()
                except Exception:
                    w.lock.release()
                    self._workers = [x for x in self._workers if x is not w]
                    raise
                return w, True
        # Nếu container duy nhất đang bận, chờ nó nhả lock rồi kiểm tra lại mem gate
        while True:
            self.check_memory_gate(reuse_warm=True)
            with self._mu:
                for w in self._workers:
                    if not w.dead and w.lock.acquire(blocking=False):
                        return w, False
            time.sleep(0.05)

    def run_task(
        self,
        op: str,
        payload: dict[str, Any],
        timeout_s: float | None = None,
    ) -> tuple[dict[str, Any], Path, bool]:
        """Thực thi một bước Blender trong container ấm.

        Trả về `(response, call_work_dir_host, cold_start)`.
        Caller tự thu hồi tệp xuất từ `call_work_dir_host` rồi gọi `cleanup_call_dir(call_id)`.
        """
        w, cold = self._acquire()
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        try:
            resp = w.call({"op": op, "call_id": call_id, **payload}, timeout_s=timeout_s)
            return resp, w.work_dir / "calls" / call_id, cold
        except BlenderSandboxError:
            w.kill()
            with self._mu:
                self._workers = [x for x in self._workers if x is not w]
            raise
        finally:
            if w.lock.locked():
                w.lock.release()

    def cleanup_call_dir(self, call_work_dir: Path) -> None:
        """Nhờ daemon (UID 65534) hoặc rmtree xóa sạch thư mục con của call vừa xong."""
        call_id = call_work_dir.name
        with self._mu:
            for w in self._workers:
                if not w.dead and call_work_dir.is_relative_to(w.work_dir):
                    if w.lock.acquire(blocking=False):
                        try:
                            w.call({"op": "cleanup", "call_id": call_id}, timeout_s=10.0)
                        except Exception:
                            pass
                        finally:
                            w.lock.release()
                    break
        shutil.rmtree(call_work_dir, ignore_errors=True)

    def shutdown(self) -> None:
        with self._mu:
            workers = list(self._workers)
            self._workers.clear()
        for w in workers:
            w.kill()

