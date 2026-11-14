from __future__ import annotations

from foundry3d.gallery_server import is_interrupted


def test_cut_off_run_is_interrupted_but_real_failures_and_passes_are_not():
    assert is_interrupted({"status": "needs_human_review", "qa_report": None, "error": ""})
    # a real failed judgement carries a gate error or a QA report
    assert not is_interrupted({"status": "needs_human_review", "qa_report": None, "error": "[G6_ArticulatedSweep] xuyên"})
    assert not is_interrupted({"status": "needs_human_review", "qa_report": {"geometry": {}}, "error": ""})
    assert not is_interrupted({"status": "completed", "qa_report": None, "error": ""})
    assert not is_interrupted({"status": "running", "qa_report": None, "error": ""})
