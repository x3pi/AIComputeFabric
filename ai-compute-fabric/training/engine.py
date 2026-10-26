"""TrainingEngine: the contract a trainer implements so training/coordinator.py can drive it (plan P1.2, P6.3).

Mirror of inference/engine.py for training. A round is: pull (version, params) from the Coordinator, set_params,
local_steps(h), get_params, submit the delta. LoraWorker is the implementation that is measured (benchmarks/reports/
p6_diloco_vs_ddp.json); any other trainer (full fine-tune, another PEFT method) must satisfy this protocol and the
contract test in tests/unit/test_training_engine_contract.py. Pure standard library on purpose."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class TrainingEngine(Protocol):
    def get_params(self) -> dict:
        """Name -> numpy array of the TRAINABLE parameters only (copies, never views of live tensors)."""
        ...

    def set_params(self, params: dict) -> None:
        """Overwrite the trainable parameters; every name from get_params() must be accepted, shapes must match."""
        ...

    def local_steps(self, h: int, seed: int) -> list[float]:
        """Run h optimizer steps from the current parameters, batches drawn deterministically from `seed`; return the h losses."""
        ...

    def evaluate(self, texts: list[str], tok_dir: str | Path) -> dict:
        """Held-out quality of the current parameters, at least {'nll', 'perplexity', 'tokens'}."""
        ...
