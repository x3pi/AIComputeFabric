"""vLLM adapter implementing InferenceEngine (plan P3.1): runs `python -m vllm.entrypoints.openai.api_server` on a verified
Hugging Face snapshot directory (safetensors; never `trust_remote_code`).

Differences from the llama adapter that the contract has to absorb:
  * the model is a DIRECTORY (a snapshot's `files/`), not a single GGUF; `ModelSpec.sha256`, when given, is the snapshot's
    manifest digest (core.model_registry.snapshot.manifest_digest) and is checked against `../MANIFEST.json`;
  * vLLM cannot offload part of the layers: `Placement.gpu_layers == 0` (CPU only) is refused, any other value means "all on GPU";
  * the VRAM budget is a fraction of the card (`gpu_memory_utilization`, constructor argument), not a layer count;
  * kv_type "q8_0" maps to vLLM's fp8 KV cache (8-bit, but a different format from ggml q8_0);
  * `Placement.flash_attn` is ignored: vLLM picks its attention backend itself.

Measured on: Qwen3-0.6B, one RTX 3060 12 GB, Linux (benchmarks/reports/p3_vllm_adapter_contract.json). Not covered: Windows
(vLLM has no supported Windows build), multi-GPU tensor parallelism, LoRA serving, quantised checkpoints."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from inference.adapters._server import OpenAIServerAdapter
from inference.engine import EngineError, ModelSpec, Placement, Plan, ProbeResult

FORBIDDEN = {".bin", ".pkl", ".pickle", ".pt", ".pth", ".ckpt"}


class VllmAdapter(OpenAIServerAdapter):
    name = "vllm"
    chat_extras = {"model": "aicf", "chat_template_kwargs": {"enable_thinking": False}}      # "aicf" = --served-model-name below

    def __init__(self, python: str, gpu_memory_utilization: float = 0.5, log_path: Optional[str] = None, env: Optional[dict] = None):
        super().__init__(log_path, env)
        if not 0.05 <= gpu_memory_utilization <= 0.95:
            raise EngineError("gpu_memory_utilization must be between 0.05 and 0.95")
        self.python, self.util = python, gpu_memory_utilization

    def _environment(self) -> dict:
        e = super()._environment()
        e.setdefault("HF_HUB_OFFLINE", "1")           # the snapshot is already local and verified; never fetch anything
        e.setdefault("VLLM_NO_USAGE_STATS", "1")
        e.setdefault("DO_NOT_TRACK", "1")
        e.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")   # the FlashInfer sampler compiles CUDA code at start-up and needs nvcc, which a worker PC may not have
        return e

    # 1 -----------------------------------------------------------------
    def probe(self) -> ProbeResult:
        if not Path(self.python).is_file():
            return ProbeResult(False, reason=f"python not found: {self.python}")
        try:
            r = subprocess.run([self.python, "-c", "import vllm, torch; print(vllm.__version__); print(torch.cuda.is_available())"],
                               capture_output=True, text=True, timeout=180, env=self._environment())
        except (OSError, subprocess.TimeoutExpired) as e:
            return ProbeResult(False, reason=str(e))
        if r.returncode != 0:
            return ProbeResult(False, reason=(r.stderr or r.stdout)[-200:])
        lines = r.stdout.split()
        cuda = len(lines) > 1 and lines[-1] == "True"
        return ProbeResult(cuda, version=f"vllm {lines[0]}", reason="" if cuda else "torch cannot see a CUDA device",
                           gpu_backends=("cuda",) if cuda else ())

    # 2 -----------------------------------------------------------------
    def plan(self, model: ModelSpec, p: Placement) -> Plan:
        if p.kv_type not in ("f16", "q8_0"):
            raise EngineError(f"kv_type must be f16 or q8_0, got {p.kv_type!r}")
        if p.gpu_layers <= 0:
            raise EngineError("vLLM cannot run on the CPU here and cannot offload part of the layers: gpu_layers must be > 0")
        if p.ctx_per_seq <= 0 or p.parallel <= 0:
            raise EngineError("ctx_per_seq > 0 and parallel > 0 are required")
        args = ("--model", model.path, "--host", "127.0.0.1", "--port", str(p.port), "--max-model-len", str(p.ctx_per_seq),
                "--max-num-seqs", str(p.parallel), "--gpu-memory-utilization", f"{self.util:g}",
                "--kv-cache-dtype", "fp8" if p.kv_type == "q8_0" else "auto", "--served-model-name", "aicf")
        notes = ["all layers on the GPU (vLLM has no partial offload)"]
        if p.kv_type == "q8_0":
            notes.append("kv q8_0 is served as vLLM fp8, not ggml q8_0")
        if p.flash_attn != "auto":
            notes.append("flash_attn is ignored: vLLM chooses its attention backend")
        return Plan(args=args, placement=p, notes=tuple(notes))

    # 3 -----------------------------------------------------------------
    def prepare(self, model: ModelSpec) -> None:
        d = Path(model.path)
        if not d.is_dir():
            raise EngineError(f"model directory not found: {model.path}")
        bad = [f.name for f in d.rglob("*") if f.suffix.lower() in FORBIDDEN]
        if bad:
            raise EngineError(f"pickle-capable weight files refused: {bad[:3]}")
        if not list(d.glob("*.safetensors")):
            raise EngineError("no .safetensors weights in the model directory")
        try:
            cfg = json.loads((d / "config.json").read_text())
        except (OSError, ValueError) as e:
            raise EngineError(f"unreadable config.json: {e}") from e
        if "auto_map" in cfg:
            raise EngineError("config.json asks for remote code (auto_map): refused")
        if model.sha256:
            from core.model_registry.snapshot import SnapshotError, load_manifest, manifest_digest, verify_snapshot
            try:
                verify_snapshot(d.parent)
                got = manifest_digest(load_manifest(d.parent))
            except SnapshotError as e:
                raise EngineError(f"model directory is not a verified snapshot: {e}") from e
            if got != model.sha256.lower():
                raise EngineError("model SHA-256 does not match the registry entry")

    def _command(self, plan: Plan) -> list:
        return [self.python, "-m", "vllm.entrypoints.openai.api_server", *plan.args]
