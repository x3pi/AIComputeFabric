"""Re-checks the COMMITTED measurement report (no GPU needed).

This verifies recorded evidence, it does not re-measure. To re-measure run
benchmarks/hardware/calibrate_vram.py on a machine with an NVIDIA GPU.
"""
import json
from pathlib import Path

import pytest

REPORT = Path(__file__).resolve().parents[2] / "benchmarks/reports/p1_vram_calibration_rtx3060.json"
MIB = 1 << 20


@pytest.fixture(scope="module")
def rep():
    return json.loads(REPORT.read_text())


def tol(est, meas):
    return abs(est - meas) <= max(meas // 20, 300_000_000)


def test_report_states_its_limited_scope(rep):
    assert "NOT the target Windows fleet" in rep["scope"]


def test_calibrated_meets_gate_g_vram_on_recorded_data(rep):
    a = rep["all"]
    assert a["cal_pass"] / a["started"] >= 0.95        # gate: >= 95 % of configs
    assert rep["heldout_batch"]["cal_pass"] == rep["heldout_batch"]["started"]


def test_raw_parser_alone_would_not_meet_the_gate(rep):
    a = rep["all"]
    assert a["raw_pass"] / a["started"] < 0.95         # why the calibration rule exists


def test_pass_flags_match_recomputation(rep):
    for r in rep["rows_fit"] + rep["rows_heldout"]:
        if r["started"]:
            assert tol(r["parser"]["calibrated_vram"], r["measured_delta_bytes"]) == r["cal_within_tolerance"]


def test_estimates_never_below_measured(rep):
    assert rep["all"]["never_underestimates_cal"] is True


def test_configs_that_did_not_start_were_predicted_to_overflow(rep):
    assert rep["all"]["not_started_all_predicted_over_free_vram"] is True


def test_kv_matches_formula(rep):
    """KV buffer in llama.cpp log == 2*layers*kv_heads*head_dim*ctx*2 bytes (Qwen3: 36x8x128)."""
    for r in rep["rows_fit"] + rep["rows_heldout"]:
        if r["started"]:
            total_ctx = r["ctx_per_seq"] * r["parallel"]
            want_mib = 2 * 36 * 8 * 128 * total_ctx * 2 / MIB
            assert r["log_buffers_mib"]["KV"] == pytest.approx(want_mib, rel=1e-3)


def test_go_test_inputs_match_report(rep):
    """The three measured cases hard-coded in estimator_test.go must equal the report."""
    want = {(8192, 4): (6417, 1152, 11457, 9768), (4096, 3): (5577, 576, 7257, 6624), (2048, 4): (5157, 288, 6417, 5916)}
    got = {}
    for r in rep["rows_fit"] + rep["rows_heldout"]:
        k = (r["ctx_per_seq"], r["parallel"])
        if r["model"].startswith("Qwen3-8B") and not r["flash_attn"] and r["started"] and k in want:
            p = r["parser"]
            got[k] = (p["one_slot_vram"] // MIB, p["one_slot_kv"] // MIB, p["vram_nonuma"] // MIB, r["measured_delta_bytes"] // MIB)
    assert got == want
