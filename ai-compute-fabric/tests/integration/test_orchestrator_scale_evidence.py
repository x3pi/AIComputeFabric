"""Re-checks the COMMITTED orchestrator-overhead measurements (SIMULATED workers, not B08)."""
import json
from pathlib import Path

R = Path(__file__).resolve().parents[2] / "benchmarks/reports"


def load(n):
    return json.loads((R / n).read_text())


def test_reports_say_they_are_simulated():
    for n in ("p5_orchestrator_scale_simulated.json", "p5_orchestrator_scale_simulated_2s.json"):
        assert "SIMULATED" in load(n)["kind"]


def test_efficiency_is_computed_from_the_other_fields_and_all_tasks_completed():
    for n in ("p5_orchestrator_scale_simulated.json", "p5_orchestrator_scale_simulated_2s.json"):
        d = load(n)
        for r in d["rows"]:
            assert r["complete"]
            ideal = r["workers"] / (d["task_ms"] / 1000)
            assert r["ideal_tasks_per_s"] == round(ideal, 1)
            assert abs(r["throughput_efficiency"] - r["tasks"] / r["seconds"] / ideal) < 0.005   # seconds are rounded in the report


def test_short_tasks_hit_the_orchestrator_ceiling_but_long_tasks_do_not():
    short = {r["workers"]: r for r in load("p5_orchestrator_scale_simulated.json")["rows"]}
    effs = [short[n]["throughput_efficiency"] for n in (8, 20, 40)]
    assert effs == sorted(effs, reverse=True)           # more workers -> more of the ceiling is used
    assert short[40]["tasks_per_s"] < 140                # the ceiling is ~115 tasks/s
    assert load("p5_orchestrator_scale_simulated_2s.json")["rows"][0]["throughput_efficiency"] >= 0.9
