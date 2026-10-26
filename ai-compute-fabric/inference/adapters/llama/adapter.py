"""llama.cpp `llama-server` adapter implementing InferenceEngine (plan P3.1)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from inference.adapters._server import OpenAIServerAdapter
from inference.engine import EngineError, ModelSpec, Placement, Plan, ProbeResult


class LlamaServerAdapter(OpenAIServerAdapter):
    name = "llama-server"
    chat_extras = {"chat_template_kwargs": {"enable_thinking": False}}

    def __init__(self, binary: str, log_path: Optional[str] = None, env: Optional[dict] = None):
        super().__init__(log_path, env)
        self.binary = binary

    def _environment(self):
        e = super()._environment()
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

    # 4-7 (start, infer, metrics, stop) are shared: inference/adapters/_server.py ---------------------
    def _command(self, plan: Plan) -> list:
        return [self.binary, *plan.args]
