"""InferenceEngine: the 7-function contract every engine adapter implements (plan P1.2, P3.1).

probe -> plan -> prepare -> start -> infer / metrics -> stop. Adapters are interchangeable:
the gateway and tests only use this protocol. Pure standard library on purpose."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol, runtime_checkable


class EngineError(RuntimeError):
    """Raised for every adapter failure so callers handle one exception type."""


@dataclass(frozen=True)
class ProbeResult:
    available: bool
    version: str = ""
    reason: str = ""            # why not available
    gpu_backends: tuple = ()    # e.g. ("cuda",)


@dataclass(frozen=True)
class Placement:
    """What the VRAM estimator decided (plan B.2); the adapter turns it into engine flags."""
    gpu_layers: int
    ctx_per_seq: int
    parallel: int = 1
    flash_attn: str = "on"      # on | off | auto
    kv_type: str = "f16"        # f16 | q8_0
    port: int = 18090


@dataclass(frozen=True)
class Plan:
    args: tuple                 # full command line (without the binary)
    placement: Placement
    notes: tuple = ()


@dataclass(frozen=True)
class ModelSpec:
    path: str
    sha256: Optional[str] = None   # when given, prepare() refuses a file that does not match


@dataclass
class Metrics:
    running: bool = False
    pid: int = 0
    requests: int = 0
    completion_tokens: int = 0
    last_ttft_ms: Optional[float] = None
    last_tok_s: Optional[float] = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    text: str = ""
    reasoning: str = ""
    finish_reason: Optional[str] = None


@runtime_checkable
class InferenceEngine(Protocol):
    def probe(self) -> ProbeResult: ...
    def plan(self, model: ModelSpec, placement: Placement) -> Plan: ...
    def prepare(self, model: ModelSpec) -> None: ...
    def start(self, plan: Plan, model: ModelSpec, timeout_s: float = 180.0) -> None: ...
    def infer(self, messages: list, max_tokens: int = 64, temperature: float = 0.0) -> Iterator[Chunk]: ...
    def metrics(self) -> Metrics: ...
    def stop(self, grace_s: float = 5.0) -> None: ...
