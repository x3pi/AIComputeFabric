"""Evidence check for the Linux GPU-reclaim PoC (plan I.5). It validates the recorded
measurement; it does NOT re-run the GPU job. Windows/WDDM remains unmeasured."""
import json
from pathlib import Path

REPORT = Path(__file__).parents[2] / "benchmarks/reports/p2_gpu_reclaim_poc_rtx3060_v2.json"


def test_reclaim_report_meets_2s_gate_on_linux():
    r = json.loads(REPORT.read_text())
    s = r["summary"]
    assert r["platform"] == "linux-single-gpu"
    assert s["n"] >= 10 and s["n_ok"] == s["n"], "every trial must observe a reclaim"
    assert s["max_ms"] < 2000 and s["gate_under_2000ms"]
    for t in r["trials"]:
        assert t["user_job_alive"], "the user's job must survive: that is the point of reclaiming"
