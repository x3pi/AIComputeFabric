"""GPU class gate (feasibility review, D5): which role a GPU may take in the fleet.

Layer-split groups run at the pace of their slowest member and every decode token crosses the
network once per hop, so a card without tensor cores or with little VRAM must not join one.
Rules are conservative: anything unknown is classed as the weakest tier. Thresholds are design
choices, not measurements: they come from the fleet being RTX 3060 12 GB and have not been
tested on any other card.

Tensor cores: compute capability >= 7.0 AND not a GTX-branded part (GTX 16xx is Turing, 7.5, but has
none). The name test is a heuristic; the compute capability alone cannot tell GTX 16xx from RTX 20xx."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

MIN_GROUP_VRAM_MIB = 12 * 1024 - 512     # a 12 GB card reports ~12288 MiB; allow for rounding by the driver
MIN_SERVE_VRAM_MIB = 6 * 1024

TIERS = ("group", "standalone", "light", "unusable")


@dataclass(frozen=True)
class GpuInfo:
    name: str
    compute_cap: tuple[int, int] | None
    vram_mib: int


def has_tensor_cores(g: GpuInfo) -> bool:
    if g.compute_cap is None or g.compute_cap < (7, 0):
        return False
    return "GTX" not in g.name.upper()


def classify(g: GpuInfo) -> str:
    """group      - may be a member of a distributed (layer-split) group and serve large models
    standalone - may serve a whole model on its own, never in a group
    light      - embeddings, rerank, small models only
    unusable   - below any useful size, or unknown"""
    if g.vram_mib < MIN_SERVE_VRAM_MIB or g.compute_cap is None:
        return "unusable" if g.vram_mib < 2048 else "light"
    if not has_tensor_cores(g):
        return "light"
    if g.vram_mib >= MIN_GROUP_VRAM_MIB:
        return "group"
    return "standalone"


def parse_smi(csv_text: str) -> list[GpuInfo]:
    out = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        name, cc, mem = parts
        try:
            major, minor = cc.split(".")
            cap = (int(major), int(minor))
        except ValueError:
            cap = None
        try:
            vram = int(mem.split()[0])
        except (ValueError, IndexError):
            vram = 0
        out.append(GpuInfo(name, cap, vram))
    return out


def detect() -> list[GpuInfo]:
    r = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader"],
                       capture_output=True, text=True, timeout=10)
    return parse_smi(r.stdout) if r.returncode == 0 else []
