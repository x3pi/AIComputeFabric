"""LoRA aggregation maths (plan D15, P6.3)."""
from __future__ import annotations

import numpy as np


def naive_merge(As, Bs):
    """FORBIDDEN in production: mean(B) @ mean(A) != mean(B_k @ A_k)."""
    return np.mean(Bs, axis=0) @ np.mean(As, axis=0)


def exact_delta_w(As, Bs, alpha: float, r: int):
    """Exact average update: (alpha/r) * mean_k(B_k @ A_k)."""
    return (alpha / r) * np.mean([b @ a for a, b in zip(As, Bs)], axis=0)


def lora_fa_merge(Bs):
    """Frozen-A: A identical everywhere, so mean(B_k) @ A is exact."""
    return np.mean(Bs, axis=0)


def svd_refactor(delta_w, r: int):
    """Best rank-r factorisation (Eckart-Young). Returns (B, A, frobenius_error)."""
    u, s, vt = np.linalg.svd(delta_w, full_matrices=False)
    b = u[:, :r] * s[:r]
    a = vt[:r, :]
    err = float(np.linalg.norm(delta_w - b @ a))
    return b, a, err
