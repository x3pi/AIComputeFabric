"""llama.cpp `llama-server` adapter implementing InferenceEngine (plan P3.1)."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

from inference.engine import (Chunk, EngineError, Metrics, ModelSpec, Placement, Plan, ProbeResult)


def _new_group() -> dict:
    """Own process group so stop() takes the whole tree. Windows branch compiled/read only: not run on Windows yet."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate(p: subprocess.Popen, grace_s: float) -> None:
    if sys.platform == "win32":  # no graceful signal for a console-less server: end the tree at once
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        p.wait(timeout=10)
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=10)
    except ProcessLookupError:
        pass


class LlamaServerAdapter:
    def __init__(self, binary: str, log_path: Optional[str] = None, env: Optional[dict] = None):
        self.binary = binary
        self.log_path = log_path
        self.env = env
        self._proc: Optional[subprocess.Popen] = None
        self._port = 0
        self._log = None
        self._requests = 0
        self._tokens = 0
        self._ttft: Optional[float] = None
        self._tok_s: Optional[float] = None

    def _environment(self):
        e = dict(os.environ)
        e.update(self.env or {})
        d = str(Path(self.binary).resolve().parent)  # llama.cpp ships its shared libraries next to the binary
        key = "PATH" if sys.platform == "win32" else "LD_LIBRARY_PATH"
        e[key] = d + os.pathsep + e.get(key, "")
        return e

    # 1 -----------------------------------------------------------------
    def probe(self) -> ProbeResult:
        if not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
            return ProbeResult(False, reason=f"binary not found or not executable: {self.binary}")
        try:
            r = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=30, env=self._environment())
        except (OSError, subprocess.TimeoutExpired) as e:
            return ProbeResult(False, reason=str(e))
        text = (r.stdout + r.stderr)
        version = next((ln.strip() for ln in text.splitlines() if ln.lower().startswith("version")), "")
        backends = ("cuda",) if "CUDA" in text else ()
        return ProbeResult(r.returncode == 0, version=version, reason="" if r.returncode == 0 else text[-200:], gpu_backends=backends)

    # 2 -----------------------------------------------------------------
    def plan(self, model: ModelSpec, p: Placement) -> Plan:
        if p.flash_attn not in ("on", "off", "auto"):
            raise EngineError(f"flash_attn must be on/off/auto, got {p.flash_attn!r}")
        if p.kv_type not in ("f16", "q8_0"):
            raise EngineError(f"kv_type must be f16 or q8_0, got {p.kv_type!r}")
        if p.kv_type != "f16" and p.flash_attn == "off":
            raise EngineError("a quantised KV cache needs flash attention")
        if p.gpu_layers < 0 or p.ctx_per_seq <= 0 or p.parallel <= 0:
            raise EngineError("gpu_layers >= 0, ctx_per_seq > 0 and parallel > 0 are required")
        args = ("-m", model.path, "-c", str(p.ctx_per_seq * p.parallel), "-np", str(p.parallel),
                "-ngl", str(p.gpu_layers), "--port", str(p.port), "--host", "127.0.0.1",
                "-fa", p.flash_attn, "-ctk", p.kv_type, "-ctv", p.kv_type, "--no-warmup", "--fit", "off")
        notes = ()
        if p.gpu_layers == 0:
            notes = ("CPU only: no layer is offloaded to the GPU",)
        return Plan(args=args, placement=p, notes=notes)

    # 3 -----------------------------------------------------------------
    def prepare(self, model: ModelSpec) -> None:
        path = Path(model.path)
        if not path.is_file():
            raise EngineError(f"model file not found: {model.path}")
        with path.open("rb") as f:
            if f.read(4) != b"GGUF":
                raise EngineError("not a GGUF file")
        if model.sha256:
            h = hashlib.sha256()
            with path.open("rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            if h.hexdigest() != model.sha256.lower():
                raise EngineError("model SHA-256 does not match the registry entry")

    # 4 -----------------------------------------------------------------
    def start(self, plan: Plan, model: ModelSpec, timeout_s: float = 180.0) -> None:
        if self._proc and self._proc.poll() is None:
            raise EngineError("engine already running")
        self._port = plan.placement.port
        out = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        self._log = out if self.log_path else None
        self._proc = subprocess.Popen([self.binary, *plan.args], stdout=out, stderr=subprocess.STDOUT,
                                      env=self._environment(), **_new_group())
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self._cleanup()
                raise EngineError("llama-server exited during start-up")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        self.stop(grace_s=1)
        raise EngineError(f"llama-server not healthy after {timeout_s:.0f} s")

    # 5 -----------------------------------------------------------------
    def infer(self, messages: list, max_tokens: int = 64, temperature: float = 0.0) -> Iterator[Chunk]:
        if not (self._proc and self._proc.poll() is None):
            raise EngineError("engine is not running")
        body = json.dumps({"stream": True, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                           "chat_template_kwargs": {"enable_thinking": False}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self._port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        t0, first, n = time.time(), None, 0
        self._requests += 1
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                for raw in r:
                    line = raw.decode().strip()
                    if not line.startswith("data: ") or line.endswith("[DONE]"):
                        continue
                    ch = json.loads(line[6:])["choices"][0]
                    d = ch.get("delta", {})
                    text, reasoning = d.get("content") or "", d.get("reasoning_content") or ""
                    if text or reasoning:
                        n += 1
                        if first is None:
                            first = time.time()
                    yield Chunk(text=text, reasoning=reasoning, finish_reason=ch.get("finish_reason"))
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"inference failed: {e}") from e
        finally:
            self._tokens += n
            if first is not None:
                self._ttft = (first - t0) * 1000
                dt = time.time() - first
                self._tok_s = (n - 1) / dt if dt > 0 and n > 1 else None

    # 6 -----------------------------------------------------------------
    def metrics(self) -> Metrics:
        running = bool(self._proc and self._proc.poll() is None)
        return Metrics(running=running, pid=self._proc.pid if running else 0, requests=self._requests,
                       completion_tokens=self._tokens, last_ttft_ms=self._ttft, last_tok_s=self._tok_s)

    # 7 -----------------------------------------------------------------
    def stop(self, grace_s: float = 5.0) -> None:
        p = self._proc
        if p is None:
            return
        if p.poll() is None:
            _terminate(p, grace_s)
        self._cleanup()

    def _cleanup(self):
        self._proc = None
        if self._log:
            self._log.close()
            self._log = None
