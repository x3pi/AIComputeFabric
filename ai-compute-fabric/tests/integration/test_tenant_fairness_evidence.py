"""Validates the recorded two-tenant run (plan I.9): one GPU, one worker, 8 slots."""
import json
from pathlib import Path

R = Path(__file__).parents[2] / "benchmarks/reports/p2_tenant_fairness_rtx3060.json"


def modes():
    return {m["fair"]: m for m in json.loads(R.read_text())["modes"]}


def test_fifo_starves_the_quiet_tenant_and_fair_queuing_does_not():
    m = modes()
    assert m[False]["quiet"]["ok"] == 0, "baseline must show the noisy-neighbour problem"
    assert m[True]["quiet"]["ok"] >= 20 and set(m[True]["quiet"]["status_counts"]) == {"200"}
    assert m[True]["quiet"]["ttft_ms_p50"] < 1000


def test_fairness_costs_no_total_throughput():
    m = modes()
    tot = {f: x["quiet"]["aggregate_tok_s"] + x["noisy"]["aggregate_tok_s"] for f, x in m.items()}
    assert tot[True] > 0.9 * tot[False]
