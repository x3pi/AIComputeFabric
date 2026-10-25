"""Validates the recorded data-plane load run (plan I.8): one GPU, one worker, Qwen3-8B Q4_K_M,
8 slots. It proves admission control and proxy cost on this setup, nothing about fleet scale."""
import json
from pathlib import Path

R = Path(__file__).parents[2] / "benchmarks/reports/p2_inference_load_rtx3060.json"


def rows():
    d = json.loads(R.read_text())["rows"]
    return {(k, r["concurrency"]): r for k, v in d.items() for r in v}


def test_proxy_adds_no_measurable_cost_and_no_errors():
    r = rows()
    for c in (1, 8):
        direct, gw = r[("direct_engine", c)], r[("via_gateway", c)]
        assert gw["aggregate_tok_s"] > 0.9 * direct["aggregate_tok_s"], "proxy must cost < 10% throughput"
        assert not gw["client_errors"] and set(gw["status_counts"]) == {"200"}


def test_overload_is_shed_with_429_not_collapse():
    r = rows()
    saturated = r[("via_gateway", 8)]["aggregate_tok_s"]
    for c in (32, 64):
        row = r[("via_gateway", c)]
        assert row["status_counts"].get("429", 0) > 0, "excess load must be rejected"
        assert set(row["status_counts"]) <= {"200", "429"}, "no 5xx / transport errors under overload"
        assert row["aggregate_tok_s"] > 0.9 * saturated, "throughput must hold at saturation"
