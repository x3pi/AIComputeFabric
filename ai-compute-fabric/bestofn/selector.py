"""Candidate selector and CodeT consensus scorer for Best-of-N (Pha 2.2 & Pha 3.1).

Selection order (KE_HOACH_BEST_OF_N_CODE.md §1 & §4 Pha 2.2):
  0. Discard any candidate that failed to compile / had a SyntaxError or SandboxError timeout.
  1. Highest number of passed tests (or CodeT consensus score when self-generated tests are used).
  2. Largest consensus cluster among candidates (agreement on pass_vector + normalized code / stdout).
  3. Shorter execution time / shorter clean code length (len(code)).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from bestofn.runner import CandidateEval


@dataclass
class SelectionResult:
    selected: CandidateEval | None
    confidence: str
    consensus_ratio: float
    bon_report: dict[str, Any]


def select_best_candidate(
    evaluations: list[CandidateEval],
    sampling_telemetry: dict[str, Any] | None = None,
    runner_telemetry: dict[str, Any] | None = None,
    test_source: str = "provided",
    generated_test_count: int = 0,
) -> SelectionResult:
    compilable = [e for e in evaluations if e.compile_ok]
    if not compilable:
        fallback = evaluations[0] if evaluations else None
        report = _build_report(
            evaluations=evaluations,
            selected=fallback,
            confidence="none",
            consensus_ratio=0.0,
            codet_scores={},
            sampling_telemetry=sampling_telemetry,
            runner_telemetry=runner_telemetry,
            test_source=test_source,
            generated_test_count=generated_test_count,
        )
        return SelectionResult(
            selected=fallback,
            confidence="none",
            consensus_ratio=0.0,
            bon_report=report,
        )

    # Count structural / pass-vector consensus among compilable candidates
    code_hash_counts = Counter(e.code_hash for e in compilable)
    vector_counts = Counter(e.pass_vector for e in compilable if e.pass_vector and "1" in e.pass_vector)

    # CodeT dual-agreement score: for each candidate, sum of (other candidates passing suite j) across suites j it passes
    codet_scores: dict[int, float] = {}
    num_suites = max((e.total_tests for e in compilable), default=0)
    suite_pass_counts = [0] * num_suites
    for e in compilable:
        for j, ch in enumerate(e.pass_vector):
            if j < num_suites and ch == "1":
                suite_pass_counts[j] += 1

    for e in compilable:
        if num_suites > 0 and test_source == "generated":
            # CodeT consensus: passing tests that more candidates agree on carries higher weight
            agree_sum = sum(
                suite_pass_counts[j]
                for j, ch in enumerate(e.pass_vector)
                if j < num_suites and ch == "1"
            )
            codet_scores[e.sample_index] = round(
                e.passed_tests * 10.0 + agree_sum + code_hash_counts[e.code_hash] * 0.5,
                3,
            )
        else:
            codet_scores[e.sample_index] = float(e.passed_tests)

    def sort_key(e: CandidateEval) -> tuple[Any, ...]:
        primary_test_score = codet_scores.get(e.sample_index, float(e.passed_tests))
        vec_consensus = vector_counts.get(e.pass_vector, 0) if "1" in e.pass_vector else 0
        struct_consensus = code_hash_counts.get(e.code_hash, 0)
        code_len = len(e.code.strip()) if e.code.strip() else 999999
        return (
            primary_test_score,          # (1) Most tests passed / CodeT score (higher is better)
            e.passed_tests,              # Exact test pass count
            vec_consensus,               # (2a) Behavioral pass-vector consensus (higher is better)
            struct_consensus,            # (2b) Normalized code consensus (higher is better)
            -code_len,                   # (3a) Shorter clean implementation (smaller len is better)
            -round(e.exec_ms, 1),        # (3b) Faster execution time
            -e.sample_index,             # Deterministic tie-breaker (prefer lower-temp anchor)
        )

    ranked = sorted(compilable, key=sort_key, reverse=True)
    winner = ranked[0]

    passing_any = [e for e in compilable if e.passed_tests > 0]
    if winner.total_tests > 0:
        agree_peers = sum(
            1 for e in compilable
            if e.passed_tests == winner.passed_tests and e.pass_vector == winner.pass_vector and winner.passed_tests > 0
        )
        consensus_ratio = round(agree_peers / max(1, len(compilable)), 4)
    else:
        consensus_ratio = round(code_hash_counts[winner.code_hash] / max(1, len(compilable)), 4)

    if winner.total_tests > 0 and winner.passed_tests == winner.total_tests and consensus_ratio >= 0.5:
        confidence = "high"
    elif winner.passed_tests > 0 and (winner.passed_tests == winner.total_tests or len(passing_any) >= 2):
        confidence = "medium"
    else:
        confidence = "low"

    report = _build_report(
        evaluations=evaluations,
        selected=winner,
        confidence=confidence,
        consensus_ratio=consensus_ratio,
        codet_scores=codet_scores,
        sampling_telemetry=sampling_telemetry,
        runner_telemetry=runner_telemetry,
        test_source=test_source,
        generated_test_count=generated_test_count,
    )
    return SelectionResult(
        selected=winner,
        confidence=confidence,
        consensus_ratio=consensus_ratio,
        bon_report=report,
    )


def _build_report(
    evaluations: list[CandidateEval],
    selected: CandidateEval | None,
    confidence: str,
    consensus_ratio: float,
    codet_scores: dict[int, float],
    sampling_telemetry: dict[str, Any] | None,
    runner_telemetry: dict[str, Any] | None,
    test_source: str,
    generated_test_count: int,
) -> dict[str, Any]:
    candidates_summary = []
    for e in evaluations:
        candidates_summary.append({
            "sample_index": e.sample_index,
            "agent_id": e.agent_id,
            "model": e.model,
            "strategy_id": e.strategy_id,
            "temperature": e.temperature,
            "compile_ok": e.compile_ok,
            "passed_tests": e.passed_tests,
            "total_tests": e.total_tests,
            "pass_rate": e.pass_rate,
            "pass_vector": e.pass_vector,
            "codet_score": codet_scores.get(e.sample_index, 0.0),
            "code_len": len(e.code),
            "code_hash": e.code_hash,
            "gen_ms": e.gen_ms,
            "exec_ms": e.exec_ms,
            "sandbox_ms": e.sandbox_ms,
            "error_type": e.error_type,
            "error": e.error[:240] if e.error else "",
            "output_truncated": e.output_truncated,
        })
    return {
        "n_candidates": len(evaluations),
        "n_compilable": sum(1 for e in evaluations if e.compile_ok),
        "n_all_tests_passed": sum(
            1 for e in evaluations if e.compile_ok and e.total_tests > 0 and e.passed_tests == e.total_tests
        ),
        "selected_sample_index": selected.sample_index if selected else -1,
        "selected_agent_id": selected.agent_id if selected else "",
        "selected_model": selected.model if selected else "",
        "selected_passed_tests": selected.passed_tests if selected else 0,
        "selected_total_tests": selected.total_tests if selected else 0,
        "confidence": confidence,
        "consensus_ratio": consensus_ratio,
        "test_source": test_source,
        "generated_test_count": generated_test_count,
        "sampling": sampling_telemetry or {},
        "sandbox_pool": runner_telemetry or {},
        "candidates": candidates_summary,
    }

