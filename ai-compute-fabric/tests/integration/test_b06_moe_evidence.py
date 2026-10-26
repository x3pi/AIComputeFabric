"""Re-checks the COMMITTED B06 measurement (Qwen3-30B-A3B MoE, experts partly on CPU, 1 x RTX 3060).
Verifies recorded evidence only; re-measure with llama-bench -ncmoe (see the report's engine field)."""
import json
from pathlib import Path

import pytest

P = Path(__file__).resolve().parents[2] / "benchmarks/reports/p4_B06_moe_hybrid_rtx3060.json"
B02 = Path(__file__).resolve().parents[2] / "benchmarks/reports/p0_B02_llamabench_Qwen2.5-32B_rtx3060.json"


@pytest.fixture(scope="module")
def rep():
    return json.loads(P.read_text())


def test_caveats_are_recorded(rep):
    text = " ".join(rep["caveats"])
    assert "quality NOT compared" in text and "Windows not measured" in text


def test_more_experts_on_gpu_is_faster_until_it_no_longer_fits(rep):
    fit = sorted((r for r in rep["rows"] if "tg64_tok_s" in r), key=lambda r: -r["n_cpu_moe"])
    speeds = [r["tg64_tok_s"] for r in fit]
    assert speeds == sorted(speeds) and len(fit) >= 4
    oom = [r["n_cpu_moe"] for r in rep["rows"] if r.get("result") == "out_of_memory"]
    assert oom and max(oom) < min(r["n_cpu_moe"] for r in fit)


def test_best_row_and_gate_follow_from_the_data_and_the_b02_baseline(rep):
    best = max((r for r in rep["rows"] if "tg64_tok_s" in r), key=lambda r: r["tg64_tok_s"])
    assert rep["best"] == best
    b02 = [r for r in json.loads(B02.read_text()) if r["n_gen"] and r["n_gpu_layers"] == 32][0]
    ref = 1000 / b02["avg_ts"]
    assert rep["b02_reference"]["tpot_ms"] == pytest.approx(ref, abs=0.1)
    cut = 100 * (1 - best["tpot_ms"] / ref)
    assert rep["tpot_reduction_vs_b02_pct"] == pytest.approx(cut, abs=0.2)
    assert rep["gate_tpot_reduction_ge_40pct"] == (cut >= 40)


def test_noise_is_small_relative_to_the_effect(rep):
    for r in rep["rows"]:
        if "tg64_tok_s" in r and r["n_cpu_moe"] <= 40:
            assert r["tg64_std"] < 0.1 * r["tg64_tok_s"]


OT = Path(__file__).resolve().parents[2] / "benchmarks/reports/p4_B06_override_tensor_rtx3060.json"


def test_override_tensor_matches_n_cpu_moe_within_noise_and_other_splits_are_not_better():
    r = json.loads(OT.read_text())["rows"]
    a, b = r["ncmoe18"], r["ot_layers0_17"]
    assert abs(a["tg64_tok_s"] - b["tg64_tok_s"]) < 2 * max(a["tg64_std"], b["tg64_std"])
    best = max(a["tg64_tok_s"], b["tg64_tok_s"])
    for name in ("ot_down_all", "ot_down_all_plus_up_0_8", "ot_updown_0_17_gate_all"):
        assert r[name]["tg64_tok_s"] <= best + 2 * r[name]["tg64_std"], name
    assert "layer-wise" in " ".join(json.loads(OT.read_text())["findings"])
