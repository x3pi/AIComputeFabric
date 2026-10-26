"""DiLoCo-style outer-step coordinator: stale-update rejection and crash-safe checkpoints (plan P6.3).

Workers pull (version, parameters), run H local steps, and send back a parameter DELTA tagged with the
version they started from. The coordinator applies an outer step only from deltas that are current,
unique per worker, well formed and finite; everything else is rejected with a reason and counted.

What this is: the coordination and bookkeeping logic, on numpy arrays, testable without a GPU.
What it is not: a trainer. Nothing here runs a model; the inner optimiser, LoRA-FA wiring and the
transport between PCs are separate. Not yet exercised on real training or on more than one machine."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Update:
    worker_id: str
    base_version: int
    steps: int
    delta: dict          # name -> ndarray, (theta_after_local_steps - theta_at_base)


@dataclass
class Coordinator:
    params: dict
    outer_lr: float = 0.7
    momentum: float = 0.9
    min_workers: int = 2
    max_delta_norm: float = 1e6
    version: int = 0
    velocity: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)       # worker_id -> Update for the current version
    rejected: dict = field(default_factory=dict)      # reason -> count
    history: list = field(default_factory=list)       # per outer step: version, workers, total local steps

    def __post_init__(self):
        if not self.velocity:
            self.velocity = {k: np.zeros_like(v) for k, v in self.params.items()}

    # ---- worker side ----
    def pull(self) -> tuple:
        return self.version, {k: v.copy() for k, v in self.params.items()}

    # ---- intake ----
    def _reject(self, reason: str):
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        raise Rejected(reason)

    def submit(self, u: Update) -> None:
        if not isinstance(u.base_version, int) or isinstance(u.base_version, bool):
            self._reject("bad_version")
        if u.base_version < self.version:
            self._reject("stale")
        if u.base_version > self.version:
            self._reject("from_the_future")
        if u.worker_id in self.pending:
            self._reject("duplicate")
        if u.steps <= 0:
            self._reject("no_local_steps")
        if set(u.delta) != set(self.params):
            self._reject("wrong_tensors")
        total = 0.0
        for k, d in u.delta.items():
            d = np.asarray(d)
            if d.shape != self.params[k].shape:
                self._reject("wrong_shape")
            if not np.all(np.isfinite(d)):
                self._reject("not_finite")
            total += float(np.sum(d.astype(np.float64) ** 2))
        if total ** 0.5 > self.max_delta_norm:
            self._reject("norm_too_large")
        self.pending[u.worker_id] = u

    def ready(self) -> bool:
        return len(self.pending) >= self.min_workers

    def outer_step(self) -> bool:
        """Average the accepted deltas, Nesterov outer update, new version. False if quorum is missing."""
        if not self.ready():
            return False
        ups = list(self.pending.values())
        for k in self.params:
            avg = np.mean([u.delta[k] for u in ups], axis=0)
            g = -avg                                   # pseudo-gradient, as in DiLoCo
            self.velocity[k] = self.momentum * self.velocity[k] + g
            self.params[k] = self.params[k] - self.outer_lr * (g + self.momentum * self.velocity[k])
        self.history.append({"version": self.version, "workers": sorted(self.pending), "local_steps": sum(u.steps for u in ups)})
        self.version += 1
        self.pending = {}
        return True

    # ---- checkpoints: atomic, checksummed, two generations ----
    def save(self, directory: str | Path) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        names = sorted(self.params)
        arrays = {f"p::{k}": self.params[k] for k in names} | {f"v::{k}": self.velocity[k] for k in names}
        meta = {"version": self.version, "names": names, "outer_lr": self.outer_lr, "momentum": self.momentum,
                "min_workers": self.min_workers, "history": self.history, "rejected": self.rejected}
        final = d / f"ckpt-{self.version:08d}.npz"
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            np.savez(f, __meta__=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8), **arrays)
            f.flush()
            os.fsync(f.fileno())
        digest = hashlib.sha256(Path(tmp).read_bytes()).hexdigest()
        os.replace(tmp, final)                          # atomic: a crash leaves the old file or the new one, never half
        (d / (final.name + ".sha256")).write_text(digest)
        for old in sorted(d.glob("ckpt-*.npz"))[:-2]:   # keep the newest two
            old.unlink()
            (d / (old.name + ".sha256")).unlink(missing_ok=True)
        return final

    @classmethod
    def load_latest(cls, directory: str | Path) -> "Coordinator":
        """Newest checkpoint whose checksum matches; a corrupt newest one falls back to the previous."""
        d = Path(directory)
        for p in sorted(d.glob("ckpt-*.npz"), reverse=True):
            sha = d / (p.name + ".sha256")
            if not sha.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != sha.read_text().strip():
                continue
            z = np.load(p)
            meta = json.loads(bytes(z["__meta__"]).decode())
            names = meta["names"]
            c = cls({k: z[f"p::{k}"] for k in names}, outer_lr=meta["outer_lr"], momentum=meta["momentum"], min_workers=meta["min_workers"],
                    version=meta["version"], velocity={k: z[f"v::{k}"] for k in names})
            c.history, c.rejected = meta["history"], meta["rejected"]
            return c
        raise FileNotFoundError(f"no intact checkpoint in {d}")


def comm_bytes_per_worker(n_params: int, total_steps: int, workers: int, h: int | None, bytes_per_param: int = 4) -> int:
    """Bytes one worker moves over the network. DDP (h=None): ring all-reduce of the gradient every step,
    2*(n-1)/n * P per step. DiLoCo: once every h steps send the delta and receive the new parameters, 2*P.
    An analytic count, not a measurement; assumes ring all-reduce and no compression."""
    if h is None:
        return int(total_steps * 2 * (workers - 1) / workers * n_params * bytes_per_param)
    rounds = -(-total_steps // h)
    return int(rounds * 2 * n_params * bytes_per_param)
