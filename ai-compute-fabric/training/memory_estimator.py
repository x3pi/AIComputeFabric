"""Peak-VRAM estimator for LoRA fine-tuning (plan P6.3).

peak = ctx + 2*P_model (bf16 weights) + P_lora*(4 weights + 4 grads + optimizer state)
       + tokens * (vocab*C_LOGITS + [no checkpointing] layers*(hidden+inter)*C_ACT + [checkpointing] layers*hidden*2)

The three constants were fitted on measured peaks (benchmarks/reports/p6_lora_memory_raw.jsonl, set "calib":
Qwen3-0.6B on an RTX 3060 12 GB, torch 2.14 / bf16 autocast / fp32 adapters / PyTorch caching allocator). They are
properties of that stack, not of physics: a different torch version, attention kernel or dtype moves them.
Validity: dense Qwen3-style decoder, LoRA on all 7 projections, full-vocabulary logits (labels given). Not
validated: other architectures, 4-bit base weights (QLoRA), multi-GPU, sequence packing, flash-attention variants."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MIB = 2 ** 20
# fitted by benchmarks/training/fit_estimator.py; rewritten there
FIT = {"ctx_mib": 0.0, "c_logits": 0.0, "c_act": 0.0}
_FIT_FILE = Path(__file__).with_name("memory_estimator_fit.json")
if _FIT_FILE.exists():
    FIT = json.loads(_FIT_FILE.read_text())
OPT_BYTES_PER_PARAM = {"adamw": 8.0, "adamw8bit": 2.0}


@dataclass(frozen=True)
class ModelSpec:
    hidden: int
    inter: int
    layers: int
    heads: int
    kv_heads: int
    head_dim: int
    vocab: int
    tied: bool = True

    @classmethod
    def from_config(cls, path) -> "ModelSpec":
        c = json.loads(Path(path).read_text())
        return cls(c["hidden_size"], c["intermediate_size"], c["num_hidden_layers"], c["num_attention_heads"], c["num_key_value_heads"],
                   c.get("head_dim") or c["hidden_size"] // c["num_attention_heads"], c["vocab_size"], c.get("tie_word_embeddings", True))

    def lora_params(self, rank: int) -> int:
        q, kv = self.heads * self.head_dim, self.kv_heads * self.head_dim
        per_layer = (self.hidden + q) + 2 * (self.hidden + kv) + (q + self.hidden) + 2 * (self.hidden + self.inter) + (self.inter + self.hidden)
        return rank * per_layer * self.layers

    def model_params(self) -> int:
        q, kv = self.heads * self.head_dim, self.kv_heads * self.head_dim
        attn = self.hidden * q + 2 * self.hidden * kv + q * self.hidden
        mlp = 3 * self.hidden * self.inter
        emb = self.vocab * self.hidden * (1 if self.tied else 2)
        return self.layers * (attn + mlp + 2 * self.hidden + 2 * self.head_dim) + emb + self.hidden


def estimate_mib(spec: ModelSpec, batch: int, seq: int, rank: int, checkpointing: bool, optimizer: str, fit: dict | None = None) -> float:
    f = fit or FIT
    if optimizer not in OPT_BYTES_PER_PARAM:
        raise ValueError(f"unknown optimizer {optimizer!r}")
    if min(batch, seq, rank) < 1:
        raise ValueError("batch, seq and rank must be >= 1")
    weights = 2 * spec.model_params()
    lora = spec.lora_params(rank) * (4 + 4 + OPT_BYTES_PER_PARAM[optimizer])
    per_token = spec.vocab * f["c_logits"]
    per_token += spec.layers * spec.hidden * 2 if checkpointing else spec.layers * (spec.hidden + spec.inter) * f["c_act"]
    return f["ctx_mib"] + (weights + lora + batch * seq * per_token) / MIB


def fits_in(spec: ModelSpec, total_mib: float, batch, seq, rank, checkpointing, optimizer, margin: float = 0.08) -> bool:
    """True when the estimate plus a safety margin fits. The margin is a design choice, sized from the held-out error."""
    return estimate_mib(spec, batch, seq, rank, checkpointing, optimizer) * (1 + margin) <= total_mib
