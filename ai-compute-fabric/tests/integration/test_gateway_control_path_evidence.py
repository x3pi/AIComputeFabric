"""Validates the recorded gateway<->worker-guard run (plan P2.2). Single host, one GPU
shared by all workers: this proves the control path, not fleet scale."""
import json
from pathlib import Path

REPORT = Path(__file__).parents[2] / "benchmarks/reports/p2_gateway_control_path_poc.json"


def test_control_path_checks_all_passed():
    r = json.loads(REPORT.read_text())
    c = r["checks"]
    assert r["pass"] is True and r["workers"] >= 3
    assert c["buckets_reported"] and c["others_still_running"] and c["survivors_online"]
    assert c["offline_detected_s"] < 5, "a dead worker must show offline within a few heartbeats"
    assert c["pause_applied_s"] < 5
