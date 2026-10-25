"""Validates the recorded control-plane capacity run (plan §B.7 / I.7). Simulated workers on
the same host as the gateway: it proves the control plane only, not the inference data plane."""
import json
from pathlib import Path

R = Path(__file__).parents[2] / "benchmarks/reports/p2_gateway_capacity.json"


def test_control_plane_handles_5000_workers_without_errors_and_with_compact_frames():
    rows = {r["workers"]: r for r in json.loads(R.read_text())["scenarios"]}
    assert {200, 1000, 5000} <= rows.keys()
    for r in rows.values():
        assert r["errors"] == 0
        assert r["hb_p99_ms"] < 150, "W4 gate: scheduling latency p95 < 150 ms"
    # shared pre-serialised frame: must stay far below the 482 KB / 2.4 MB per-viewer marshal of v1
    assert rows[5000]["sse_avg_msg_bytes"] < 200_000
    assert rows[1000]["sse_avg_msg_bytes"] < 50_000
