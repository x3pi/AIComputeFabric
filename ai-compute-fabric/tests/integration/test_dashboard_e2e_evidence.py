"""Validates the recorded browser end-to-end run (tests/e2e/dashboard_e2e.mjs): real Chrome, real
gateway, real worker-guard and llama-server on one machine. Linux only; Windows pages not tried."""
import json
from pathlib import Path

R = Path(__file__).parents[2] / "benchmarks/reports/p2_dashboard_e2e.json"


def test_every_browser_check_passed():
    r = json.loads(R.read_text())
    failed = [c["name"] for c in r["checks"] if not c["ok"]]
    assert r["pass"] and not failed, failed
    assert len(r["checks"]) >= 14
