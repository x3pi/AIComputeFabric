"""Speaker-disjoint train/val/test split (plan P6.1)."""
from __future__ import annotations

import hashlib


def _bucket(speaker: str, seed: int) -> float:
    d = hashlib.sha256(f"{seed}:{speaker}".encode()).digest()
    return int.from_bytes(d[:8], "big") / 2**64


def speaker_disjoint_split(records: list[dict], ratios=(0.8, 0.1, 0.1), seed: int = 0) -> dict[str, list[dict]]:
    """Every speaker lands in exactly one split (deterministic by hash)."""
    if abs(sum(ratios) - 1.0) > 1e-9:
        raise ValueError("ratios must sum to 1")
    cut1, cut2 = ratios[0], ratios[0] + ratios[1]
    out: dict[str, list] = {"train": [], "validation": [], "test": []}
    for r in records:
        if not r.get("speaker_id"):
            raise ValueError("record without speaker_id")
        b = _bucket(r["speaker_id"], seed)
        out["train" if b < cut1 else "validation" if b < cut2 else "test"].append(r)
    return out
