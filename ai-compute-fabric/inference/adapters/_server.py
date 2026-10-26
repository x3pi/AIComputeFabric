"""Shared plumbing for adapters that run an OpenAI-compatible HTTP server as a child process (llama-server, vLLM):
process group handling, health wait, streaming chat, metrics and stop. Engine-specific parts (probe, plan, prepare,
the command line and the request extras) stay in each adapter."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Iterator, Optional

from inference.engine import Chunk, EngineError, Metrics, Plan


def new_group() -> dict:
    """Own process group so stop() takes the whole tree. Windows branch compiled/read only: not run on Windows yet."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate(p: subprocess.Popen, grace_s: float) -> None:
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


class OpenAIServerAdapter:
    name = "server"                       # used in error messages
    chat_extras: dict = {}                # merged into every chat request body

    def __init__(self, log_path: Optional[str] = None, env: Optional[dict] = None):
        self.log_path = log_path
        self.env = env
        self._proc: Optional[subprocess.Popen] = None
        self._port = 0
        self._log = None
        self._requests = 0
        self._tokens = 0
        self._ttft: Optional[float] = None
        self._tok_s: Optional[float] = None

    def _environment(self) -> dict:
        e = dict(os.environ)
        e.update(self.env or {})
        return e

    def _command(self, plan: Plan) -> list:
        raise NotImplementedError

    def start(self, plan: Plan, model, timeout_s: float = 180.0) -> None:
        if self._proc and self._proc.poll() is None:
            raise EngineError("engine already running")
        self._port = plan.placement.port
        out = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        self._log = out if self.log_path else None
        self._proc = subprocess.Popen(self._command(plan), stdout=out, stderr=subprocess.STDOUT, env=self._environment(), **new_group())
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self._cleanup()
                raise EngineError(f"{self.name} exited during start-up")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        self.stop(grace_s=1)
        raise EngineError(f"{self.name} not healthy after {timeout_s:.0f} s")

    def infer(self, messages: list, max_tokens: int = 64, temperature: float = 0.0) -> Iterator[Chunk]:
        if not (self._proc and self._proc.poll() is None):
            raise EngineError("engine is not running")
        body = json.dumps({"stream": True, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, **self.chat_extras}).encode()
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
                    choices = json.loads(line[6:]).get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    d = ch.get("delta", {})
                    text, reasoning = d.get("content") or "", d.get("reasoning_content") or d.get("reasoning") or ""
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

    def metrics(self) -> Metrics:
        running = bool(self._proc and self._proc.poll() is None)
        return Metrics(running=running, pid=self._proc.pid if running and self._proc else 0, requests=self._requests,
                       completion_tokens=self._tokens, last_ttft_ms=self._ttft, last_tok_s=self._tok_s)

    def stop(self, grace_s: float = 5.0) -> None:
        p = self._proc
        if p is None:
            return
        if p.poll() is None:
            terminate(p, grace_s)
        self._cleanup()

    def _cleanup(self):
        self._proc = None
        if self._log:
            self._log.close()
            self._log = None
