"""Runtime settings of the AI Studio service. Everything environment-specific lives here and is passed in, never read
from module globals, so tests build a Settings with temporary directories and fake backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Quotas:
    max_documents: int = 500                   # RAG documents per tenant
    max_total_chars: int = 20_000_000          # RAG text per tenant
    max_upload_bytes: int = 20 * 1024 * 1024   # one uploaded file / dataset
    max_prompt_versions: int = 200             # per template name
    max_prompt_templates: int = 100            # per tenant
    max_active_jobs: int = 2                   # queued + running jobs per tenant
    max_examples: int = 50_000                 # Q&A pairs per dataset
    max_example_chars: int = 4_000             # one question or answer


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    app_dsn: str                                # unprivileged role (row-level security applies)
    embed_url: str = ""
    rerank_url: str = ""
    llm_url: str = ""
    base_models: dict = field(default_factory=dict)   # name -> local HF directory usable for LoRA
    train_python: str = ""                      # interpreter with torch + peft + transformers
    llama_cpp_dir: str = ""                    # llama.cpp checkout with convert_lora_to_gguf.py (enables adapter export for llama-server)
    worker_url: str = ""                       # worker-guard loopback API (http://127.0.0.1:8099): enables choosing the served model/adapter
    adapters_dir: str = ""                     # where exported adapters are placed for worker-guard (--adapters-dir)
    lora_runner: tuple = ()                     # argv prefix replacing `train_python -m studio.lora_run` (tests use a fake worker)
    gpu_total_mib: int = 12288
    gpu_reserved_mib: int = 1536                # kept free for the owner of the PC (worker-guard headroom)
    max_body: int = 64 * 1024 * 1024
    quotas: Quotas = field(default_factory=Quotas)
