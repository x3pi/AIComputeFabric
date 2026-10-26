"""Validates the recorded browser run of screen 4a (tests/e2e/agents_dag_e2e.mjs): real Chrome, real
gateway proxy, real dag_api and a half-finished workflow. Linux only."""
import json
from pathlib import Path

R = Path(__file__).parents[2] / "benchmarks/reports/p5_agent_dag_e2e.json"


def test_every_browser_check_passed():
    r = json.loads(R.read_text())
    failed = [c["name"] for c in r["checks"] if not c["ok"]]
    assert r["pass"] and not failed, failed
    assert len(r["checks"]) >= 12
