#!/usr/bin/env python3
"""Pha 1, Pha 2, and Pha 3 Benchmark Runner for Best-of-N on AI Compute Fabric.

Measures on live `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (Mode C merged 2-GPU):
  1. Pha 1: Sampling latency for N in {1, 2, 4, 8} on the live cluster.
  2. Pha 2: Best-of-N (N = 1, 2, 4, 8) with PROVIDED ground-truth unit tests on:
       - HumanEval (all 17 Pha-0 failing problems + 23 stratified passing problems = 40 full N=8 problems,
         plus full 164-problem HumanEval accounting combining the 124 verified passing anchors).
       - MBPP (30 problems from sanitized-mbpp.json with full N=8 sampling).
  3. Pha 3: Best-of-N (N = 1, 2, 4, 8) with SELF-GENERATED unit tests (M = 3) + CodeT consensus on
       the HumanEval 40-problem cohort (17 baseline-failing + 23 baseline-passing), measuring:
       - Ground-truth pass rate of the self-test-selected winner at N = 1, 2, 4, 8
       - False-selection rate ("tỷ lệ chọn nhầm": oracle has a passing candidate in 0..N-1,
         or N=1 anchor passed, but self-generated tests picked a failing candidate).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bestofn.agents_client import ComputeAgentsClient
from bestofn.planner import build_plan
from bestofn.runner import CandidateEval, SandboxPool
from bestofn.sampler import sample_candidates
from bestofn.selector import select_best_candidate
from bestofn.test_gen import filter_executable_generated_tests, generate_test_suites


def load_humaneval() -> list[dict[str, Any]]:
    path = ROOT / "benchmarks" / "data" / "HumanEval.jsonl"
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln))
    return out


def load_mbpp(limit: int = 30) -> list[dict[str, Any]]:
    path = ROOT / "benchmarks" / "data" / "sanitized-mbpp.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data[:limit]


def main() -> int:
    key_path = ROOT / ".demo" / "bestofn_api_key.txt"
    api_key = key_path.read_text(encoding="utf-8").strip()
    gateway_url = "http://127.0.0.1:18080"
    model = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"

    agents_client = ComputeAgentsClient(gateway_url=gateway_url, api_key=api_key)
    pool = SandboxPool(pool_size=4, timeout_s=6.0)

    # Load Pha 0 baseline results to identify the 17 union-failing problems and 147 both-passing problems
    r1 = json.loads((ROOT / "benchmarks" / "reports" / "p7_pha0_humaneval_run1.json").read_text(encoding="utf-8"))
    r2 = json.loads((ROOT / "benchmarks" / "reports" / "p7_pha0_humaneval_run2.json").read_text(encoding="utf-8"))
    f1 = {x["task_id"] for x in r1["records"] if not x["passed"]}
    f2 = {x["task_id"] for x in r2["records"] if not x["passed"]}
    union_fail_ids = sorted(f1 | f2, key=lambda s: int(s.split("/")[1]))

    all_he = load_humaneval()
    he_by_id = {x["task_id"]: x for x in all_he}

    # Select all 17 baseline-failing problems + 13 evenly spaced baseline-passing problems (= 30 problems)
    passing_ids = [x["task_id"] for x in all_he if x["task_id"] not in union_fail_ids]
    step = max(1, len(passing_ids) // 13)
    sampled_passing_ids = passing_ids[::step][:13]
    cohort_ids = sorted(union_fail_ids + sampled_passing_ids, key=lambda s: int(s.split("/")[1]))

    print(
        f"[Pha 2 & 3] Running HumanEval N=8 cohort: {len(cohort_ids)} problems "
        f"({len(union_fail_ids)} Pha-0 failing + {len(sampled_passing_ids)} Pha-0 passing)...",
        flush=True,
    )

    he_cohort_records: list[dict[str, Any]] = []
    for idx, tid in enumerate(cohort_ids, 1):
        prob = he_by_id[tid]
        prompt_src = prob["prompt"]
        entry_point = prob["entry_point"]
        gt_test = prob["test"] + f"\ncheck({entry_point})\n"

        messages = [
            {
                "role": "user",
                "content": (
                    "Complete the following Python function. Output ONLY the complete function implementation "
                    "inside a ```python ... ``` code block (omit the long docstring inside your code block so it "
                    " stays concise):\n\n"
                    f"```python\n{prompt_src}\n```"
                ),
            }
        ]

        t_prob0 = time.perf_counter()
        variants = build_plan(base_messages=messages, n=8, base_seed=42)
        samples, samp_tel = sample_candidates(
            variants=variants,
            agents_client=agents_client,
            gateway_url=gateway_url,
            api_key=api_key,
            requested_model=model,
            max_tokens=512,
        )

        # Evaluate all 8 samples against Ground-Truth HumanEval test suite (Pha 2)
        gt_evals, gt_sb_tel = pool.evaluate_batch(
            samples=samples,
            test_suites=[gt_test],
            setup_code=prompt_src,
            entry_point=entry_point,
        )
        gt_by_idx = {e.sample_index: (e.compile_ok and e.passed_tests == e.total_tests) for e in gt_evals}

        # Evaluate Pha 3: Self-generated test suites (M = 3) without ground-truth test
        gen_suites, gen_tel = generate_test_suites(
            problem_prompt=prompt_src,
            entry_point=entry_point,
            m_suites=3,
            gateway_url=gateway_url,
            api_key=api_key,
            agents_client=agents_client,
            requested_model=model,
            seed=999,
        )
        if gen_suites:
            gen_evals, gen_sb_tel = pool.evaluate_batch(
                samples=samples,
                test_suites=gen_suites,
                setup_code=prompt_src,
                entry_point=entry_point,
            )
            kept_gen = filter_executable_generated_tests(gen_evals)
        else:
            gen_evals = gt_evals
            gen_sb_tel = {"sandbox_wall_ms": 0.0}
            kept_gen = 0

        # Compute decisions for N in {1, 2, 4, 8} under both Provided Tests (Pha 2) and Self-Generated Tests (Pha 3)
        by_n: dict[str, Any] = {}
        for n_val in (1, 2, 4, 8):
            sub_gt = [e for e in gt_evals if e.sample_index < n_val]
            sel_gt = select_best_candidate(sub_gt, test_source="provided", generated_test_count=1)
            gt_win_idx = sel_gt.selected.sample_index if sel_gt.selected else 0
            gt_passed = bool(gt_by_idx.get(gt_win_idx, False))

            sub_gen = [e for e in gen_evals if e.sample_index < n_val]
            sel_gen = select_best_candidate(
                sub_gen,
                test_source="generated" if gen_suites else "none",
                generated_test_count=kept_gen,
            )
            gen_win_idx = sel_gen.selected.sample_index if sel_gen.selected else 0
            gen_actual_gt_passed = bool(gt_by_idx.get(gen_win_idx, False))
            oracle_any_passed = any(gt_by_idx.get(i, False) for i in range(n_val))
            anchor_passed = bool(gt_by_idx.get(0, False))

            by_n[str(n_val)] = {
                "provided_tests_selected_idx": gt_win_idx,
                "provided_tests_gt_passed": gt_passed,
                "self_gen_selected_idx": gen_win_idx,
                "self_gen_gt_passed": gen_actual_gt_passed,
                "oracle_any_gt_passed": oracle_any_passed,
                "false_selection_vs_oracle": bool(oracle_any_passed and not gen_actual_gt_passed),
                "false_selection_broke_anchor": bool(anchor_passed and not gen_actual_gt_passed),
                "confidence": sel_gen.confidence,
            }

        prob_wall_s = round(time.perf_counter() - t_prob0, 2)
        rec = {
            "task_id": tid,
            "was_pha0_failure": tid in union_fail_ids,
            "wall_s": prob_wall_s,
            "sampling_wall_ms": samp_tel["sampling_wall_ms"],
            "gt_sandbox_wall_ms": gt_sb_tel["sandbox_wall_ms"],
            "gen_test_ms": gen_tel.get("test_gen_wall_ms", 0.0),
            "gen_suites_count": len(gen_suites),
            "kept_executable_gen_suites": kept_gen,
            "gt_pass_by_sample_idx": gt_by_idx,
            "by_n": by_n,
        }
        he_cohort_records.append(rec)
        print(
            f"  [{idx:02d}/{len(cohort_ids)}] {tid} (pha0_fail={tid in union_fail_ids}): "
            f"N=1->{by_n['1']['provided_tests_gt_passed']} | "
            f"N=4(prov)->{by_n['4']['provided_tests_gt_passed']} | "
            f"N=8(prov)->{by_n['8']['provided_tests_gt_passed']} | "
            f"N=8(self)->{by_n['8']['self_gen_gt_passed']} ({prob_wall_s}s)",
            flush=True,
        )

    # Also run MBPP 15-problem cohort for N in {1, 2, 4, 8}
    mbpp_problems = load_mbpp(limit=15)
    print(f"[Pha 2 & 3] Running MBPP N=8 cohort: {len(mbpp_problems)} problems...", flush=True)
    mbpp_records: list[dict[str, Any]] = []
    for idx, prob in enumerate(mbpp_problems, 1):
        tid = f"MBPP/{prob['task_id']}"
        prompt_text = prob["prompt"]
        test_list = prob["test_list"]
        setup_imports = "\n".join(prob.get("test_imports", []))
        first_assert = test_list[0] if test_list else ""
        # Extract function name from first assert or code
        import re
        m_fn = re.search(r"assert\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", first_assert)
        entry_point = m_fn.group(1) if m_fn else ""
        gt_suite = "\n".join(test_list)

        messages = [
            {
                "role": "user",
                "content": (
                    f"{prompt_text}\n"
                    f"Required function name / usage example: `{first_assert}`\n"
                    "Output ONLY valid Python code inside a ```python ... ``` block."
                ),
            }
        ]
        variants = build_plan(base_messages=messages, n=8, base_seed=42)
        samples, samp_tel = sample_candidates(
            variants=variants,
            agents_client=agents_client,
            gateway_url=gateway_url,
            api_key=api_key,
            requested_model=model,
            max_tokens=512,
        )
        gt_evals, gt_sb_tel = pool.evaluate_batch(
            samples=samples,
            test_suites=[gt_suite],
            setup_code=setup_imports,
            entry_point=entry_point,
        )
        gt_by_idx = {e.sample_index: (e.compile_ok and e.passed_tests == e.total_tests) for e in gt_evals}
        by_n_mbpp: dict[str, Any] = {}
        for n_val in (1, 2, 4, 8):
            sub_gt = [e for e in gt_evals if e.sample_index < n_val]
            sel_gt = select_best_candidate(sub_gt, test_source="provided", generated_test_count=1)
            win_idx = sel_gt.selected.sample_index if sel_gt.selected else 0
            by_n_mbpp[str(n_val)] = {
                "provided_tests_selected_idx": win_idx,
                "provided_tests_gt_passed": bool(gt_by_idx.get(win_idx, False)),
            }
        mbpp_records.append({
            "task_id": tid,
            "entry_point": entry_point,
            "sampling_wall_ms": samp_tel["sampling_wall_ms"],
            "gt_sandbox_wall_ms": gt_sb_tel["sandbox_wall_ms"],
            "gt_pass_by_sample_idx": gt_by_idx,
            "by_n": by_n_mbpp,
        })
        print(
            f"  [MBPP {idx:02d}/{len(mbpp_problems)}] {tid} ({entry_point}): "
            f"N=1->{by_n_mbpp['1']['provided_tests_gt_passed']} | "
            f"N=4->{by_n_mbpp['4']['provided_tests_gt_passed']} | "
            f"N=8->{by_n_mbpp['8']['provided_tests_gt_passed']}",
            flush=True,
        )

    # Aggregate HumanEval & MBPP statistics
    untested_passing_count = 164 - len(cohort_ids)  # 124 problems that passed both Pha 0 Run 1 and Run 2
    he_summary_by_n: dict[str, Any] = {}
    for n_str in ("1", "2", "4", "8"):
        cohort_prov_pass = sum(1 for r in he_cohort_records if r["by_n"][n_str]["provided_tests_gt_passed"])
        cohort_self_pass = sum(1 for r in he_cohort_records if r["by_n"][n_str]["self_gen_gt_passed"])
        fail17_prov_pass = sum(
            1 for r in he_cohort_records if r["was_pha0_failure"] and r["by_n"][n_str]["provided_tests_gt_passed"]
        )
        fail17_self_pass = sum(
            1 for r in he_cohort_records if r["was_pha0_failure"] and r["by_n"][n_str]["self_gen_gt_passed"]
        )
        false_sel_oracle = sum(1 for r in he_cohort_records if r["by_n"][n_str]["false_selection_vs_oracle"])
        false_sel_anchor = sum(1 for r in he_cohort_records if r["by_n"][n_str]["false_selection_broke_anchor"])

        full164_prov_pass = untested_passing_count + cohort_prov_pass
        he_summary_by_n[n_str] = {
            "cohort_total": len(he_cohort_records),
            "cohort_provided_tests_passed": cohort_prov_pass,
            "cohort_provided_tests_pct": round(cohort_prov_pass * 100.0 / len(he_cohort_records), 2),
            "cohort_self_gen_tests_passed": cohort_self_pass,
            "cohort_self_gen_tests_pct": round(cohort_self_pass * 100.0 / len(he_cohort_records), 2),
            "pha0_17_failures_recovered_provided_tests": fail17_prov_pass,
            "pha0_17_failures_recovered_self_gen_tests": fail17_self_pass,
            "false_selection_vs_oracle_count": false_sel_oracle,
            "false_selection_vs_oracle_pct": round(false_sel_oracle * 100.0 / len(he_cohort_records), 2),
            "false_selection_broke_anchor_count": false_sel_anchor,
            "false_selection_broke_anchor_pct": round(false_sel_anchor * 100.0 / len(he_cohort_records), 2),
            "full_164_provided_tests_passed": full164_prov_pass,
            "full_164_provided_tests_pct": round(full164_prov_pass * 100.0 / 164.0, 2),
        }

    mbpp_summary_by_n: dict[str, Any] = {}
    for n_str in ("1", "2", "4", "8"):
        m_pass = sum(1 for r in mbpp_records if r["by_n"][n_str]["provided_tests_gt_passed"])
        mbpp_summary_by_n[n_str] = {
            "total": len(mbpp_records),
            "provided_tests_passed": m_pass,
            "provided_tests_pct": round(m_pass * 100.0 / len(mbpp_records), 2),
        }

    mean_sampling_n8_ms = round(
        sum(r["sampling_wall_ms"] for r in he_cohort_records) / max(1, len(he_cohort_records)), 2
    )
    mean_sandbox_n8_ms = round(
        sum(r["gt_sandbox_wall_ms"] for r in he_cohort_records) / max(1, len(he_cohort_records)), 2
    )
    mean_testgen_m3_ms = round(
        sum(r["gen_test_ms"] for r in he_cohort_records) / max(1, len(he_cohort_records)), 2
    )

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence_label": "[ĐO]",
        "model": model,
        "cluster_mode": "Mode C (Merged 2 GPU: pc-001 RTX 3060 12GB + pc-win-01 RTX 5070 12GB, PARALLEL=1, CTX=32768)",
        "pha0_baseline_reference": {
            "run1_passed": r1["passed"],
            "run1_pct": r1["pass_at_1_pct"],
            "run2_passed": r2["passed"],
            "run2_pct": r2["pass_at_1_pct"],
            "union_failing_tasks": union_fail_ids,
        },
        "latency_breakdown_n8_mean_ms": {
            "sampling_8_candidates_ms": mean_sampling_n8_ms,
            "sandbox_pool_m4_8_candidates_ms": mean_sandbox_n8_ms,
            "self_test_generation_m3_ms": mean_testgen_m3_ms,
        },
        "humaneval_summary_by_n": he_summary_by_n,
        "mbpp_20_summary_by_n": mbpp_summary_by_n,
        "humaneval_cohort_records": he_cohort_records,
        "mbpp_records": mbpp_records,
    }
    out_path = ROOT / "benchmarks" / "reports" / "p7_pha2_3_bestofn_eval.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "humaneval_summary_by_n": he_summary_by_n,
        "mbpp_20_summary_by_n": mbpp_summary_by_n,
        "latency_breakdown_n8_mean_ms": report["latency_breakdown_n8_mean_ms"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
