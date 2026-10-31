#!/usr/bin/env python3
"""Pha T4.2 (Quality Verification: Perplexity + 15 Code Eval + 2 Long-Context 24k Needle Eval)
and Pha T5 (7x ABAB Interleaved Confirmation + Sustained Thermal/Swap Stability Run).

Strictly implements KE_HOACH_TOI_UU_CPU_KV_NCMOE.md Sections 3 (Pha T4.2 & Pha T5).
"""
import argparse
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p9_cpu_kv_runner import (
    GPUMonitor,
    SEED_BASE,
    check_kernel_oom,
    inspect_process_threads,
    parse_server_log_info,
    post_completion,
    read_cpu_telemetry,
    read_meminfo,
    run_single_config,
    stop_server,
    wait_server_ready,
)


CODING_PROBLEMS = [
    {
        "id": "p01_two_sum",
        "prompt": "Write ONLY a Python function `def two_sum(nums: list[int], target: int) -> tuple[int, int]:` that returns indices `(i, j)` with `i < j` such that `nums[i] + nums[j] == target`. Do not include markdown fences or explanations, only valid Python code.",
        "test": "assert two_sum([2, 7, 11, 15], 9) == (0, 1)\nassert two_sum([3, 2, 4], 6) == (1, 2)\nassert two_sum([-3, 4, 3, 90], 0) == (0, 2)",
    },
    {
        "id": "p02_valid_parens",
        "prompt": "Write ONLY a Python function `def is_valid_parens(s: str) -> bool:` that returns True iff `s` containing only `'()[]{}'` is properly balanced and nested. Output only valid Python code.",
        "test": "assert is_valid_parens('()[]{}') is True\nassert is_valid_parens('([{}])') is True\nassert is_valid_parens('(]') is False\nassert is_valid_parens('([)]') is False",
    },
    {
        "id": "p03_merge_intervals",
        "prompt": "Write ONLY a Python function `def merge_intervals(intervals: list[list[int]]) -> list[list[int]]:` that merges all overlapping intervals and returns sorted non-overlapping intervals. Output only valid Python code.",
        "test": "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]\nassert merge_intervals([[1,4],[4,5]]) == [[1,5]]",
    },
    {
        "id": "p04_lru_cache",
        "prompt": "Write ONLY a Python class `LRUCache` with `__init__(self, capacity: int)`, `get(self, key: int) -> int` (returns -1 if missing), and `put(self, key: int, value: int) -> None` in O(1) time. Output only valid Python code.",
        "test": "c = LRUCache(2)\nc.put(1, 1)\nc.put(2, 2)\nassert c.get(1) == 1\nc.put(3, 3)\nassert c.get(2) == -1\nc.put(4, 4)\nassert c.get(1) == -1\nassert c.get(3) == 3\nassert c.get(4) == 4",
    },
    {
        "id": "p05_longest_unique_substr",
        "prompt": "Write ONLY a Python function `def length_of_longest_substring(s: str) -> int:` returning the length of the longest substring without repeating characters. Output only valid Python code.",
        "test": "assert length_of_longest_substring('abcabcbb') == 3\nassert length_of_longest_substring('bbbbb') == 1\nassert length_of_longest_substring('pwwkew') == 3\nassert length_of_longest_substring('') == 0",
    },
    {
        "id": "p06_coin_change",
        "prompt": "Write ONLY a Python function `def coin_change(coins: list[int], amount: int) -> int:` returning the fewest number of coins needed to make up `amount`, or -1 if impossible. Output only valid Python code.",
        "test": "assert coin_change([1, 2, 5], 11) == 3\nassert coin_change([2], 3) == -1\nassert coin_change([1], 0) == 0",
    },
    {
        "id": "p07_top_k_frequent",
        "prompt": "Write ONLY a Python function `def top_k_frequent(nums: list[int], k: int) -> list[int]:` returning the `k` most frequent elements sorted in descending order of frequency. Output only valid Python code.",
        "test": "assert top_k_frequent([1,1,1,2,2,3], 2) == [1, 2]\nassert top_k_frequent([4,4,4,6,6,1,2,2,2,2], 2) == [2, 4]",
    },
    {
        "id": "p08_edit_distance",
        "prompt": "Write ONLY a Python function `def min_distance(word1: str, word2: str) -> int:` returning the Levenshtein edit distance between `word1` and `word2`. Output only valid Python code.",
        "test": "assert min_distance('horse', 'ros') == 3\nassert min_distance('intention', 'execution') == 5\nassert min_distance('', 'abc') == 3",
    },
    {
        "id": "p09_kth_largest",
        "prompt": "Write ONLY a Python function `def find_kth_largest(nums: list[int], k: int) -> int:` returning the k-th largest element in the array (1-indexed). Output only valid Python code.",
        "test": "assert find_kth_largest([3,2,1,5,6,4], 2) == 5\nassert find_kth_largest([3,2,3,1,2,4,5,5,6], 4) == 4",
    },
    {
        "id": "p10_topo_sort_can_finish",
        "prompt": "Write ONLY a Python function `def can_finish(num_courses: int, prerequisites: list[list[int]]) -> bool:` returning True iff all courses can be finished given pairs `[a, b]` meaning `b` must be taken before `a`. Output only valid Python code.",
        "test": "assert can_finish(2, [[1,0]]) is True\nassert can_finish(2, [[1,0],[0,1]]) is False\nassert can_finish(4, [[1,0],[2,1],[3,2]]) is True",
    },
    {
        "id": "p11_trie",
        "prompt": "Write ONLY a Python class `Trie` with `__init__(self)`, `insert(self, word: str) -> None`, `search(self, word: str) -> bool`, and `starts_with(self, prefix: str) -> bool`. Output only valid Python code.",
        "test": "t = Trie()\nt.insert('apple')\nassert t.search('apple') is True\nassert t.search('app') is False\nassert t.starts_with('app') is True\nt.insert('app')\nassert t.search('app') is True",
    },
    {
        "id": "p12_binary_search_rotated",
        "prompt": "Write ONLY a Python function `def search_rotated(nums: list[int], target: int) -> int:` returning the index of `target` in a rotated sorted array of distinct integers, or -1 if not found, in O(log n) time. Output only valid Python code.",
        "test": "assert search_rotated([4,5,6,7,0,1,2], 0) == 4\nassert search_rotated([4,5,6,7,0,1,2], 3) == -1\nassert search_rotated([1], 0) == -1",
    },
    {
        "id": "p13_subarray_sum_k",
        "prompt": "Write ONLY a Python function `def subarray_sum(nums: list[int], k: int) -> int:` returning the total number of contiguous subarrays whose sum equals `k`. Output only valid Python code.",
        "test": "assert subarray_sum([1,1,1], 2) == 2\nassert subarray_sum([1,2,3], 3) == 2\nassert subarray_sum([1,-1,0], 0) == 3",
    },
    {
        "id": "p14_max_path_sum_grid",
        "prompt": "Write ONLY a Python function `def min_path_sum(grid: list[list[int]]) -> int:` returning the minimum sum of a path from top-left to bottom-right moving only right or down. Output only valid Python code.",
        "test": "assert min_path_sum([[1,3,1],[1,5,1],[4,2,1]]) == 7\nassert min_path_sum([[1,2,3],[4,5,6]]) == 12",
    },
    {
        "id": "p15_dijkstra_shortest_path",
        "prompt": "Write ONLY a Python function `def shortest_path(n: int, edges: list[tuple[int, int, int]], src: int, dst: int) -> int:` returning the shortest path weight from `src` to `dst` in a directed graph with `n` nodes (0..n-1) and non-negative `(u, v, w)` edges, or -1 if unreachable. Output only valid Python code.",
        "test": "assert shortest_path(4, [(0,1,1),(1,2,2),(0,2,4),(2,3,1)], 0, 3) == 4\nassert shortest_path(3, [(0,1,5)], 0, 2) == -1",
    },
]


def extract_python_code(text: str) -> str:
    # Strip <think>...</think> blocks if present
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"```(?:python)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return cleaned


def run_code_test(code_str: str, test_str: str) -> tuple[bool, str]:
    full_script = f"from typing import *\nimport heapq, collections, math, bisect, functools\n\n{code_str}\n\n{test_str}\n"
    try:
        res = subprocess.run(
            [sys.executable, "-I", "-c", full_script],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            return True, "PASS"
        return False, (res.stderr.strip().splitlines()[-1] if res.stderr.strip() else f"rc={res.returncode}")
    except Exception as e:
        return False, str(e)


def build_24k_needle_prompt(needle_idx: int, seed: int) -> tuple[str, dict]:
    rng = random.Random(seed + needle_idx * 777)
    nonce = uuid.uuid4().hex[:10]
    words = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "xray",
    ]
    if needle_idx == 1:
        needles = {
            "early": ("AICF_SECRET_KEY_EARLY", "K9X-7419-ALPHA"),
            "mid": ("AICF_SECRET_KEY_MID", "M4Z-8823-BRAVO"),
            "late": ("AICF_SECRET_KEY_LATE", "Q7W-3105-OMEGA"),
        }
    else:
        needles = {
            "early": ("MODULE_CHECKSUM_01", "SHA-994120"),
            "mid": ("MODULE_CHECKSUM_02", "SHA-553817"),
            "late": ("MODULE_CHECKSUM_03", "SHA-208469"),
        }

    # Build ~23,800 tokens (~4,320 word-number pairs at ~5.51 tokens/pair) and inject the 3 needles at ~2k, ~12k, and ~22k tokens
    n_pairs = 4320
    pos_early = 380    # ~2,100 tokens
    pos_mid = 2160     # ~11,900 tokens
    pos_late = 3950    # ~21,800 tokens

    chunks = [f"{nonce} [RunID=needle24k_{needle_idx}]\nBelow is a system log stream containing 3 configuration records.\n"]
    for i in range(n_pairs):
        if i == pos_early:
            k, v = needles["early"]
            chunks.append(f"\n[CONFIG_RECORD] {k} = \"{v}\" [/CONFIG_RECORD]\n")
        elif i == pos_mid:
            k, v = needles["mid"]
            chunks.append(f"\n[CONFIG_RECORD] {k} = \"{v}\" [/CONFIG_RECORD]\n")
        elif i == pos_late:
            k, v = needles["late"]
            chunks.append(f"\n[CONFIG_RECORD] {k} = \"{v}\" [/CONFIG_RECORD]\n")
        w = rng.choice(words)
        n = rng.randint(100, 999)
        chunks.append(f"{w} {n} ")
        if (i + 1) % 32 == 0:
            chunks.append("\n")

    k1, v1 = needles["early"]
    k2, v2 = needles["mid"]
    k3, v3 = needles["late"]
    chunks.append(
        f"\nBased strictly on the [CONFIG_RECORD] entries above, output ONLY the exact values for "
        f"{k1}, {k2}, and {k3} in the format:\n"
        f"{k1}=<value>\n{k2}=<value>\n{k3}=<value>\nDo not include any other explanation."
    )
    return "".join(chunks), {k1: v1, k2: v2, k3: v3}


def ensure_perplexity_corpus(corpus_path: Path) -> dict:
    """Create a deterministic source-code + markdown corpus (~140 KB, >= 32k tokens) for llama-perplexity."""
    repo_root = Path(__file__).resolve().parents[3]
    source_files = [
        repo_root / "ai-compute-fabric/apps/site-gateway/cluster_topology.go",
        repo_root / "ai-compute-fabric/apps/worker-guard/engine.go",
        repo_root / "ai-compute-fabric/apps/worker-guard/guard.go",
        repo_root / "ai-compute-fabric/apps/worker-guard/server.go",
        repo_root / "KE_HOACH_BEST_OF_N_CODE.md",
    ]
    pieces = []
    for sf in source_files:
        if sf.exists():
            pieces.append(f"// === FILE: {sf.name} ===\n" + sf.read_text(encoding="utf-8", errors="ignore"))
    full_text = "\n\n".join(pieces)
    # Repeat deterministically if needed so size is ~160 KB (~40,000 tokens > 32,768)
    while len(full_text.encode("utf-8")) < 160000:
        full_text = full_text + "\n\n" + full_text
    full_text = full_text[:165000]
    corpus_path.write_text(full_text, encoding="utf-8")
    sha = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    return {"path": str(corpus_path), "bytes": corpus_path.stat().st_size, "sha256": sha}


def run_perplexity_eval(ppl_bin: str, model_path: str, corpus_path: Path, kv_type: str, cpu_args: list[str], log_dir: Path) -> dict:
    # We evaluate perplexity with -c 8192 --chunks 4 (4 chunks * 8192 = 32,768 tokens total, per KE_HOACH line 118)
    cmd = [
        ppl_bin,
        "-m", model_path,
        "-f", str(corpus_path),
        "-c", "8192",
        "--chunks", "4",
        "-ngl", "99",
        "-ncmoe", "30",
        "-fa", "on",
        "-ctk", kv_type,
        "-ctv", kv_type,
    ] + cpu_args
    env = os.environ.copy()
    bin_dir = str(Path(ppl_bin).resolve().parent)
    env["LD_LIBRARY_PATH"] = f"{bin_dir}:{env.get('LD_LIBRARY_PATH', '')}"
    log_path = log_dir / f"p9_t4_perplexity_{kv_type}.log"
    print(f"\n=== Running llama-perplexity ({kv_type}): {' '.join(cmd)} ===", flush=True)
    t0 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n")
        lf.flush()
        res = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, timeout=1800)
    elapsed_s = round(time.perf_counter() - t0, 2)
    txt = log_path.read_text(errors="ignore")
    final_ppl = None
    ppl_std = None
    m = re.search(r"Final estimate:\s*PPL\s*=\s*([0-9.]+)\s*\+/-\s*([0-9.]+)", txt)
    if m:
        final_ppl = float(m.group(1))
        ppl_std = float(m.group(2))
    print(f"    Perplexity ({kv_type}): PPL = {final_ppl} +/- {ppl_std} (elapsed={elapsed_s}s, rc={res.returncode})", flush=True)
    return {
        "kv_type": kv_type,
        "cmd": cmd,
        "rc": res.returncode,
        "ppl": final_ppl,
        "ppl_std": ppl_std,
        "elapsed_s": elapsed_s,
        "log_file": str(log_path),
    }


def run_code_and_needle_eval_for_kv(
    bin_path: str,
    model_path: str,
    port: int,
    kv_type: str,
    ncmoe: int,
    cpu_args: list[str],
    log_dir: Path,
) -> dict:
    cmd = [
        bin_path,
        "-m", model_path,
        "-c", "32768",
        "-np", "1",
        "-ngl", "99",
        "--port", str(port),
        "--host", "127.0.0.1",
        "-fa", "on",
        "-ctk", kv_type,
        "-ctv", kv_type,
        "--no-warmup",
        "--fit", "off",
        "-lv", "3",
        "-ncmoe", str(ncmoe),
    ] + cpu_args

    env = os.environ.copy()
    bin_dir = str(Path(bin_path).resolve().parent)
    env["LD_LIBRARY_PATH"] = f"{bin_dir}:{env.get('LD_LIBRARY_PATH', '')}"
    log_path = log_dir / f"p9_t4_code_needle_{kv_type}_N{ncmoe}.log"

    print(f"\n=== Running 15 Code Eval + 2x 24k Needle Eval ({kv_type}, -ncmoe {ncmoe}) ===", flush=True)
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
        try:
            ready, reason = wait_server_ready(proc, port=port, timeout_s=180)
            if not ready:
                stop_server(proc)
                raise RuntimeError(f"Server failed to start for {kv_type} code eval: {reason}")

            code_results = []
            passed_count = 0
            for idx, prob in enumerate(CODING_PROBLEMS):
                p_str = (
                    f"<|im_start|>system\nYou are a precise Python coding assistant. Return only valid Python code without markdown explanation.<|im_end|>\n"
                    f"<|im_start|>user\n[EvalID={prob['id']}]\n{prob['prompt']} /no_think<|im_end|>\n"
                    f"<|im_start|>assistant\n"
                )
                res = post_completion(port, p_str, n_predict=512, seed=42)
                raw_out = res.get("content", "")
                code_only = extract_python_code(raw_out)
                ok, msg = run_code_test(code_only, prob["test"])
                if ok:
                    passed_count += 1
                code_results.append({
                    "id": prob["id"],
                    "passed": ok,
                    "detail": msg,
                    "output_sha256": hashlib.sha256(raw_out.encode("utf-8")).hexdigest()[:16],
                    "code_snippet": code_only[:240],
                })
                print(f"    [{idx+1:2d}/15] {prob['id']}: {'PASS' if ok else 'FAIL (' + msg + ')'}", flush=True)

            needle_results = []
            needle_passed = 0
            for n_idx in [1, 2]:
                nprompt, expected_map = build_24k_needle_prompt(n_idx, seed=SEED_BASE)
                Full_prompt = (
                    f"<|im_start|>user\n{nprompt} /no_think<|im_end|>\n"
                    f"<|im_start|>assistant\n"
                )
                res = post_completion(port, Full_prompt, n_predict=128, seed=42, timeout_s=600)
                raw_out = re.sub(r"<think>.*?</think>", "", res.get("content", ""), flags=re.DOTALL).strip()
                t_obj = res.get("timings", {})
                hits = {k: (v in raw_out) for k, v in expected_map.items()}
                all_hit = all(hits.values())
                if all_hit:
                    needle_passed += 1
                needle_results.append({
                    "needle_test_id": n_idx,
                    "prompt_n": t_obj.get("prompt_n"),
                    "prefill_tok_s": round(t_obj.get("prompt_per_second", 0.0), 2),
                    "expected": expected_map,
                    "hits": hits,
                    "all_passed": all_hit,
                    "raw_response": raw_out[:300],
                })
                print(f"    [Needle 24k #{n_idx}] prompt_n={t_obj.get('prompt_n')}, hits={hits}, all_passed={all_hit}", flush=True)
        finally:
            stop_server(proc)

    return {
        "kv_type": kv_type,
        "ncmoe": ncmoe,
        "code_pass_count": passed_count,
        "code_total": len(CODING_PROBLEMS),
        "code_pass_rate": round(passed_count / len(CODING_PROBLEMS), 4),
        "code_results": code_results,
        "needle_24k_pass_count": needle_passed,
        "needle_24k_total": 2,
        "needle_results": needle_results,
    }


def run_t4_quality_suite(
    bin_path: str,
    ppl_bin: str,
    model_path: str,
    port: int,
    ncmoe_q8: int,
    cpu_args: list[str],
    out_dir: Path,
    log_dir: Path,
):
    repo_root = Path(__file__).resolve().parents[3]
    corpus_path = repo_root / ".aicf-work/bench_corpus_p9.txt"
    corpus_meta = ensure_perplexity_corpus(corpus_path)
    print(f"Perplexity corpus ready: {corpus_meta['bytes']} bytes, sha256={corpus_meta['sha256']}", flush=True)

    ppl_f16 = run_perplexity_eval(ppl_bin, model_path, corpus_path, "f16", cpu_args, log_dir)
    ppl_q8 = run_perplexity_eval(ppl_bin, model_path, corpus_path, "q8_0", cpu_args, log_dir)

    eval_f16 = run_code_and_needle_eval_for_kv(bin_path, model_path, port, "f16", 30, cpu_args, log_dir)
    eval_q8 = run_code_and_needle_eval_for_kv(bin_path, model_path, port, "q8_0", ncmoe_q8, cpu_args, log_dir)

    delta_ppl = None
    if ppl_f16["ppl"] is not None and ppl_q8["ppl"] is not None:
        delta_ppl = round(ppl_q8["ppl"] - ppl_f16["ppl"], 5)

    f16_hashes = {r["id"]: r["output_sha256"] for r in eval_f16["code_results"]}
    q8_hashes = {r["id"]: r["output_sha256"] for r in eval_q8["code_results"]}
    exact_match_ids = [pid for pid in f16_hashes if f16_hashes.get(pid) == q8_hashes.get(pid)]

    summary = {
        "phase": "T4.2_quality",
        "corpus": corpus_meta,
        "perplexity": {
            "f16": ppl_f16,
            "q8_0": ppl_q8,
            "delta_q8_minus_f16": delta_ppl,
            "gate_pass_le_0_05": (abs(delta_ppl) <= 0.05) if delta_ppl is not None else False,
        },
        "code_and_needle": {
            "f16_N30": eval_f16,
            f"q8_0_N{ncmoe_q8}": eval_q8,
            "code_delta_pass": eval_q8["code_pass_count"] - eval_f16["code_pass_count"],
            "code_exact_match_count": len(exact_match_ids),
            "code_exact_match_total": len(CODING_PROBLEMS),
            "code_exact_match_ids": exact_match_ids,
            "needle_delta_pass": eval_q8["needle_24k_pass_count"] - eval_f16["needle_24k_pass_count"],
        },
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    out_path = out_dir / "p9_t4_quality.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== T4.2 Quality Evaluation saved to {out_path} ===", flush=True)
    return summary


def run_sustained_thermal_test(
    bin_path: str,
    model_path: str,
    port: int,
    cfg: dict,
    duration_minutes: float,
    out_dir: Path,
    log_dir: Path,
):
    from p9_cpu_kv_runner import build_long_prompt, build_short_prompt

    out_ts_jsonl = out_dir / "p9_t5_thermal_timeseries.jsonl"
    if out_ts_jsonl.exists():
        out_ts_jsonl.unlink()

    cmd = [
        bin_path,
        "-m", model_path,
        "-c", str(cfg["ctx"]),
        "-np", "1",
        "-ngl", "99",
        "--port", str(port),
        "--host", "127.0.0.1",
        "-fa", "on",
        "-ctk", cfg["kv_type"],
        "-ctv", cfg["kv_type"],
        "--no-warmup",
        "--fit", "off",
        "-lv", "3",
        "-ncmoe", str(cfg["ncmoe"]),
    ] + cfg.get("extra_args", [])

    env = os.environ.copy()
    bin_dir = str(Path(bin_path).resolve().parent)
    env["LD_LIBRARY_PATH"] = f"{bin_dir}:{env.get('LD_LIBRARY_PATH', '')}"
    log_path = log_dir / "p9_t5_sustained_thermal_server.log"

    duration_s = duration_minutes * 60.0
    print(f"\n=== Starting T5 Sustained Thermal/Swap Stability Test ({duration_minutes} min = {duration_s:.0f}s) ===", flush=True)
    print(f"    Command: {' '.join(cmd)}", flush=True)

    gpu_mon = GPUMonitor()
    gpu_mon.start()
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
        try:
            ready, reason = wait_server_ready(proc, port=port, timeout_s=180)
            if not ready:
                raise RuntimeError(f"Server failed to start for thermal test: {reason}")

            t_start = time.perf_counter()
            step = 0
            while (time.perf_counter() - t_start) < duration_s:
                step += 1
                # Every 10th step run an 8k prefill + 64 decode; otherwise run a 256-token decode
                if step % 10 == 0:
                    req_type = "prefill_8k_plus_decode64"
                    prompt = build_long_prompt("T5_thermal", 8000, step, SEED_BASE + step)
                    n_pred = 64
                else:
                    req_type = "decode_256"
                    prompt = build_short_prompt("T5_thermal", step, SEED_BASE + step)
                    n_pred = 256

                res = post_completion(port, prompt, n_predict=n_pred, seed=SEED_BASE + step)
                t_obj = res.get("timings", {})
                cpu_tel = read_cpu_telemetry()
                mem_tel = read_meminfo()
                gpu_snap = gpu_mon.samples[-1] if gpu_mon.samples else {}
                elapsed_s = round(time.perf_counter() - t_start, 2)

                rec = {
                    "step": step,
                    "elapsed_s": elapsed_s,
                    "elapsed_min": round(elapsed_s / 60.0, 2),
                    "req_type": req_type,
                    "prompt_n": t_obj.get("prompt_n"),
                    "predicted_n": t_obj.get("predicted_n"),
                    "prefill_tok_s": round(t_obj.get("prompt_per_second", 0.0), 2),
                    "decode_tok_s": round(t_obj.get("predicted_per_second", 0.0), 2),
                    "cpu_pkg_temp_c": cpu_tel.get("cpu_pkg_temp_c"),
                    "pcore_avg_mhz": cpu_tel.get("p_core_avg_mhz"),
                    "ecore_avg_mhz": cpu_tel.get("e_core_avg_mhz"),
                    "gpu_temp_c": gpu_snap.get("temp_c"),
                    "gpu_power_w": gpu_snap.get("power_w"),
                    "vram_used_mib": gpu_snap.get("used_mib"),
                    "mem_available_mib": mem_tel.get("mem_available_mib"),
                    "swap_used_mib": mem_tel.get("swap_used_mib"),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                with open(out_ts_jsonl, "a", encoding="utf-8") as outf:
                    outf.write(json.dumps(rec, ensure_ascii=False) + "\n")

                if step == 1 or step % 5 == 0 or (time.perf_counter() - t_start) >= duration_s:
                    print(
                        f"    [Thermal step {step:3d} | {rec['elapsed_min']:5.2f}m] "
                        f"type={req_type} dec={rec['decode_tok_s']:5.2f} tok/s | "
                        f"CPU={rec['cpu_pkg_temp_c']}C P={rec['pcore_avg_mhz']}MHz | "
                        f"GPU={rec['gpu_temp_c']}C {rec['gpu_power_w']}W VRAM={rec['vram_used_mib']}MiB | "
                        f"MemAvail={rec['mem_available_mib']}MiB Swap={rec['swap_used_mib']}MiB",
                        flush=True,
                    )
        finally:
            stop_server(proc)
            gpu_mon.stop()

    print(f"=== Sustained Thermal Test saved to {out_ts_jsonl} ===", flush=True)


def run_t5_confirmation(
    bin_path: str,
    model_path: str,
    port: int,
    best_8k_cfg: dict,
    best_32k_cfg: dict,
    thermal_minutes: float,
    out_dir: Path,
    log_dir: Path,
):
    out_jsonl = out_dir / "p9_t5_confirmation.jsonl"
    if out_jsonl.exists():
        out_jsonl.unlink()

    cpu_args = best_8k_cfg.get("extra_args", ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1"])

    # ABAB interleaved confirmation for 8k and 32k (each server run performs 7 decode reps + 3 prefill reps)
    configs = [
        {"tag": "T5_c8k_ABAB_1_base", "phase": "T5", "ctx": 8192, "ncmoe": 20, "kv_type": "f16", "extra_args": [], "prefill_lengths": [8000]},
        {**best_8k_cfg, "tag": "T5_c8k_ABAB_2_opt", "phase": "T5", "prefill_lengths": [8000]},
        {"tag": "T5_c8k_ABAB_3_base", "phase": "T5", "ctx": 8192, "ncmoe": 20, "kv_type": "f16", "extra_args": [], "prefill_lengths": [8000]},
        {**best_8k_cfg, "tag": "T5_c8k_ABAB_4_opt", "phase": "T5", "prefill_lengths": [8000]},
        {"tag": "T5_c32k_ABAB_1_base", "phase": "T5", "ctx": 32768, "ncmoe": 30, "kv_type": "f16", "extra_args": [], "prefill_lengths": [8000, 24000]},
        {**best_32k_cfg, "tag": "T5_c32k_ABAB_2_opt_q8_N24", "phase": "T5", "prefill_lengths": [8000, 24000]},
        {"tag": "T5_c32k_ABAB_3_base", "phase": "T5", "ctx": 32768, "ncmoe": 30, "kv_type": "f16", "extra_args": [], "prefill_lengths": [8000, 24000]},
        {**best_32k_cfg, "tag": "T5_c32k_ABAB_4_opt_q8_N24", "phase": "T5", "prefill_lengths": [8000, 24000]},
        {"tag": "T5_c32k_ABAB_5_opt_f16_N30", "phase": "T5", "ctx": 32768, "ncmoe": 30, "kv_type": "f16", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
    ]

    for idx, cfg in enumerate(configs):
        print(f"\n>>> [T5 {idx+1}/{len(configs)}] Running {cfg['tag']}", flush=True)
        run_single_config(
            cfg=cfg,
            bin_path=bin_path,
            model_path=model_path,
            port=port,
            out_jsonl=out_jsonl,
            log_dir=log_dir,
            decode_reps=7,
            prefill_reps=3,
        )

    if thermal_minutes > 0:
        run_sustained_thermal_test(
            bin_path=bin_path,
            model_path=model_path,
            port=port,
            cfg=best_32k_cfg,
            duration_minutes=thermal_minutes,
            out_dir=out_dir,
            log_dir=log_dir,
        )
    print(f"\n=== PHASE T5 COMPLETED SUCCESSFULLY. Output: {out_jsonl} ===", flush=True)


def main():
    p = argparse.ArgumentParser(description="P9 T4.2 Quality & T5 Confirmation Runner")
    p.add_argument("--mode", required=True, choices=["t4_quality", "t5_confirm"])
    p.add_argument("--bin", default=".aicf-work/bin/llama-server")
    p.add_argument("--ppl-bin", default=".aicf-work/bin/llama-perplexity")
    p.add_argument("--model", default=".aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf")
    p.add_argument("--port", type=int, default=18199)
    p.add_argument("--ncmoe-q8", type=int, default=26)
    p.add_argument("--cpu-args", default="-t 8 -Cr 0-7 --cpu-strict 1")
    p.add_argument("--kv-8k", default="f16")
    p.add_argument("--kv-32k", default="q8_0")
    p.add_argument("--thermal-minutes", type=float, default=30.0)
    p.add_argument("--out-dir", default="ai-compute-fabric/benchmarks/reports")
    args = p.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    cpu_args_list = [x for x in args.cpu_args.split() if x]

    if args.mode == "t4_quality":
        run_t4_quality_suite(
            bin_path=args.bin,
            ppl_bin=args.ppl_bin,
            model_path=args.model,
            port=args.port,
            ncmoe_q8=args.ncmoe_q8,
            cpu_args=cpu_args_list,
            out_dir=out_dir,
            log_dir=log_dir,
        )
    elif args.mode == "t5_confirm":
        best_8k = {
            "ctx": 8192,
            "ncmoe": 20,
            "kv_type": args.kv_8k,
            "extra_args": cpu_args_list,
        }
        best_32k = {
            "ctx": 32768,
            "ncmoe": args.ncmoe_q8,
            "kv_type": args.kv_32k,
            "extra_args": cpu_args_list,
        }
        run_t5_confirmation(
            bin_path=args.bin,
            model_path=args.model,
            port=args.port,
            best_8k_cfg=best_8k,
            best_32k_cfg=best_32k,
            thermal_minutes=args.thermal_minutes,
            out_dir=out_dir,
            log_dir=log_dir,
        )


if __name__ == "__main__":
    main()
