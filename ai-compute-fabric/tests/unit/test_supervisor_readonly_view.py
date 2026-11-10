"""A read-only evaluate (commit=False) must not start the cooldown, so GET polling is stable (no flicker in the UI)."""
from __future__ import annotations

from agents.task_bus import BusMetricsSnapshot, ElasticSupervisor, TaskType

IDLE = BusMetricsSnapshot(
    queue_depth_by_type={"TEXT_GENERATE": 0},
    inflight_by_type={"TEXT_GENERATE": 0},
    redelivered_by_type={"TEXT_GENERATE": 0},
    live_agents_by_type={"TEXT_GENERATE": 3},
    live_agents_total=3,
    median_wait_s_by_type={"TEXT_GENERATE": 0.01},
    rejected_counts={"queue_full": 0},
    completed_total=20,
)


def _sup() -> ElasticSupervisor:
    return ElasticSupervisor(propose_only=True, scale_down_queue_depth=0, scale_down_wait_s=0.1, cooldown_s=3.0)


def test_readonly_view_is_stable_across_polls():
    sup = _sup()
    actions = [sup.evaluate(IDLE, [TaskType.TEXT_GENERATE], now=100.0 + 2 * i, commit=False)[0].action for i in range(5)]
    assert actions == ["scale_down"] * 5


def test_committing_evaluate_still_starts_cooldown():
    sup = _sup()
    assert sup.evaluate(IDLE, [TaskType.TEXT_GENERATE], now=100.0)[0].action == "scale_down"
    assert sup.evaluate(IDLE, [TaskType.TEXT_GENERATE], now=102.0)[0].action == "noop"
