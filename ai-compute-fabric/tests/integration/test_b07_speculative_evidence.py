"""Re-checks the COMMITTED B07 measurement and that the 'enable by default' decision follows the gate."""
import json
from pathlib import Path

P = Path(__file__).resolve().parents[2] / "benchmarks/reports/p4_B07_speculative_rtx3060.json"


def test_baseline_is_reproducible_and_normalised():
    s = json.loads(P.read_text())["summary"]
    b = s["baseline"]
    assert b["speedup_vs_baseline"] == 1.0 and b["greedy_output_identical_to_baseline_pct"] == 100.0
    lo, hi = min(b["tok_s_per_repeat"]), max(b["tok_s_per_repeat"])
    assert (hi - lo) / hi < 0.03


def test_default_on_decision_follows_the_gate():
    s = json.loads(P.read_text())["summary"]
    passing = [n for n, r in s.items() if n != "baseline" and (r["acceptance"] or 0) >= 0.65
               and r["speedup_vs_baseline"] >= 1.25 and r["greedy_output_identical_to_baseline_pct"] == 100.0]
    assert passing == [], f"{passing} would justify enabling speculation by default; update the plan"
    assert max(r["speedup_vs_baseline"] for n, r in s.items() if n != "baseline") < 1.25


def test_speculation_is_not_enabled_by_default_in_the_agent():
    src = (Path(__file__).resolve().parents[2] / "apps/worker-guard/engine.go").read_text()
    assert "--spec-type" not in src and "-md" not in src
