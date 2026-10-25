"""Adaptive Scheduler v1 (plan P2.3): hard filters, then argmin expected time.

Pure logic with no I/O so every decision is unit-testable. Hard filters never
trade off against score: memory, model identity, data residency, SLA.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Worker:
    worker_id: str
    site_id: str
    allocatable_bytes: int
    user_is_active: bool = False
    loaded: dict = field(default_factory=dict)      # model_id -> (weight_sha256, tokenizer_sha256, revision)
    decode_tps: float = 40.0                        # measured tokens/s for the target engine
    cold_load_s: float = 25.0
    queue_s: float = 0.0
    rtt_ms: float = 1.0
    engines: tuple = ("llama.cpp",)


@dataclass(frozen=True)
class Request:
    model_id: str
    weight_sha256: str
    tokenizer_sha256: str
    revision: str
    required_bytes: int
    output_tokens: int = 256
    prompt_tokens: int = 512
    prefill_tps: float = 800.0
    site_allowlist: tuple = ()          # empty = any site
    data_class: str = "DATA_INTERNAL"
    sla_s: float = 60.0
    engine: str = "llama.cpp"


def expected_time(w: Worker, r: Request) -> float:
    loaded = w.loaded.get(r.model_id) == (r.weight_sha256, r.tokenizer_sha256, r.revision)
    t_cold = 0.0 if loaded else w.cold_load_s
    t_prefill = r.prompt_tokens / r.prefill_tps
    t_decode = r.output_tokens / max(w.decode_tps, 1e-9)
    return w.queue_s + t_cold + t_prefill + t_decode + w.rtt_ms / 1000.0


def reject_reason(w: Worker, r: Request) -> str | None:
    if w.user_is_active:
        return "USER_ACTIVE"
    if r.engine not in w.engines:
        return "ENGINE_UNSUPPORTED"
    if w.allocatable_bytes < r.required_bytes:
        return "INSUFFICIENT_VRAM"
    have = w.loaded.get(r.model_id)
    if have is not None and have != (r.weight_sha256, r.tokenizer_sha256, r.revision):
        return "MODEL_IDENTITY_MISMATCH"
    if r.site_allowlist and w.site_id not in r.site_allowlist:
        return "DATA_RESIDENCY"
    if r.data_class == "DATA_RESTRICTED" and not r.site_allowlist:
        return "RESTRICTED_NEEDS_ALLOWLIST"
    if expected_time(w, r) > r.sla_s:
        return "SLA"
    return None


def select(workers: list[Worker], r: Request) -> tuple[Worker | None, dict]:
    """Return (best worker or None, {worker_id: reject_reason | None})."""
    reasons = {w.worker_id: reject_reason(w, r) for w in workers}
    ok = [w for w in workers if reasons[w.worker_id] is None]
    if not ok:
        return None, reasons
    return min(ok, key=lambda w: (expected_time(w, r), w.worker_id)), reasons
