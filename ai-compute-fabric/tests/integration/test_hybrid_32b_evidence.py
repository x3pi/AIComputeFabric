"""Re-checks the COMMITTED LOCAL_HYBRID measurements (Qwen2.5-32B on 1 x RTX 3060).

Verifies recorded evidence only; re-measure with benchmarks/hardware/calibrate_vram.py.
"""
import json
from pathlib import Path

import pytest

P = Path(__file__).resolve().parents[2] / "benchmarks/reports/p1_vram_hybrid_32b_rtx3060.json"
MIB = 1 << 20


@pytest.fixture(scope="module")
def rep():
    return json.loads(P.read_text())


def test_scope_is_declared(rep):
    assert "NOT the Windows fleet" in rep["scope"]


def test_vram_estimate_within_tolerance_and_never_under(rep):
    for r in rep["rows"]:
        assert r["started"]
        est, meas = r["parser"]["vram_nonuma"], r["measured_delta_bytes"]
        assert est >= meas
        assert abs(est - meas) <= max(meas // 20, 300_000_000)


def test_default_mmap_ram_figure_is_unusable_for_hybrid(rep):
    """parser's default RAM figure misses the CPU-resident layers by several GB."""
    for r in rep["rows"]:
        assert r["host_mmap_under_logged_mib"] > 5000


def test_no_mmap_ram_figure_is_a_safe_upper_bound(rep):
    for r in rep["rows"]:
        assert r["host_nommap_over_logged_mib"] >= 0          # never under
        assert r["host_nommap_over_logged_mib"] < 2500         # and not wildly loose


def test_more_gpu_layers_means_more_vram_less_host_ram(rep):
    rows = sorted(rep["rows"], key=lambda r: r["ngl"])
    for a, b in zip(rows, rows[1:]):
        assert b["measured_delta_bytes"] > a["measured_delta_bytes"]
        assert b["host_logged_mib"] < a["host_logged_mib"]


def test_plan_example_layer_count_is_consistent(rep):
    """Plan example: ~28-30 layers fit in ~10.2 GB allocatable. Measured: ngl 28 -> 9.0 GB,
    ngl 32 -> 10.2 GB, so ~32 layers fit 10.2 GB (the plan was slightly pessimistic)."""
    by = {r["ngl"]: r["measured_delta_bytes"] / MIB for r in rep["rows"]}
    assert by[28] < 10200 < by[34]
    assert 10000 < by[32] < 10300
