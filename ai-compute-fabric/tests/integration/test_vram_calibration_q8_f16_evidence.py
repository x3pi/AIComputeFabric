"""Re-checks the COMMITTED Q8_0/FP16 calibration report (no GPU needed); recorded evidence, not a re-measurement."""
import json
from pathlib import Path

import pytest

REPORT = Path(__file__).resolve().parents[2] / "benchmarks/reports/p1_vram_calibration_q8_f16_rtx3060.json"
MIB = 1 << 20


@pytest.fixture(scope="module")
def rep():
    return json.loads(REPORT.read_text())


def tol(est, meas):
    return abs(est - meas) <= max(meas // 20, 300_000_000)


def test_scope_is_stated_and_gaps_are_listed(rep):
    assert "SINGLE dev machine" in rep["scope"] and "NOT refitted" in rep["scope"]
    assert any("8B FP16" in g for g in rep["not_measured"]) and any("MoE" in g for g in rep["not_measured"])


def test_summary_matches_rows(rep):
    st = [r for r in rep["rows"] if r["started"]]
    s = rep["summary"]
    assert s["configs"] == len(rep["rows"]) and s["started"] == len(st)
    assert s["cal_pass"] == sum(tol(r["parser"]["calibrated_vram"], r["measured_delta_bytes"]) for r in st)
    assert s["raw_pass"] == sum(tol(r["parser"]["vram_nonuma"], r["measured_delta_bytes"]) for r in st)


def test_calibrated_rule_holds_on_quants_it_was_not_fitted_on(rep):
    st = [r for r in rep["rows"] if r["started"]]
    assert len(st) >= 10
    assert all(tol(r["parser"]["calibrated_vram"], r["measured_delta_bytes"]) for r in st)
    assert all(r["parser"]["calibrated_vram"] >= r["measured_delta_bytes"] for r in st)      # never under-estimates


def test_raw_parser_alone_still_fails_the_gate_here(rep):
    s = rep["summary"]
    assert s["raw_pass"] / s["started"] < 0.95


def test_every_config_that_did_not_start_was_predicted_not_to_fit(rep):
    ns = [r for r in rep["rows"] if not r["started"]]
    assert ns
    assert all(r["parser"]["calibrated_vram"] > 12288 * MIB for r in ns)
