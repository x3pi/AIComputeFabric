#!/usr/bin/env python3
"""Build markdown tables for the full-164 report from benchmarks/reports/full164/*.json (no numbers typed by hand)."""
from __future__ import annotations

import json
import math
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "reports" / "full164"
ORDER = ["Qwen3-Coder-30B", "Qwen3-30B-A3B", "GLM-4.7-Flash", "Qwen3.8-27B", "GLM-4-32B", "DeepSeek-R1", "Qwen3.5-9B",
         "GLM-4-9B", "Qwen3-8B", "deepseek-coder", "Qwen3-4B", "DeepSeek-V2"]


def short(name: str) -> str:
    return name.replace(".gguf", "").replace("-Instruct", "").replace("-Q4_K_M", "").replace(".Q4_K_M", "").replace(".Q3_K_M", " Q3")


def wilson(p: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    ph = p / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return 100 * (c - h), 100 * (c + h)


def load():
    rows = []
    for f in sorted(D.glob("*__*.json")):
        if f.name.startswith("_") or ".answers" in f.name or ".INCOMPLETE" in f.name or "_v2check" in f.name:
            continue
        d = json.loads(f.read_text())
        d["_file"] = f.name
        rows.append(d)

    def key(d):
        for i, k in enumerate(ORDER):
            if d["model"].startswith(k) or k in d["model"]:
                return (i, d["mode"])
        return (99, d["mode"])

    return sorted(rows, key=key)


def vram_nodes(d):
    b = d["deployment"]["llama_log_buffers"]
    loc = sum(b.get(k, 0) for k in ("cuda0_model_mib", "cuda0_kv_mib", "cuda0_rs_mib", "cuda0_compute_mib"))
    rem = sum(b.get(k, 0) for k in ("rpc0_model_mib", "rpc0_kv_mib", "rpc0_rs_mib", "rpc0_compute_mib"))
    return loc, rem, b.get("cpu_mapped_model_mib", 0)


def main_table(rows):
    out = ["| Model | Chế độ | Pass@1 /164 | CI 95% | Bài bị cắt (chạm trần) | Decode p50 (tok/s) | TPOT p50 (ms) | TTFT p50 (ms) | Prefill prompt dài (tok/s) | J/token | J/bài đúng | Giây/bài đúng |",
           "|:--|:--|--:|:--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for d in rows:
        if "summary" not in d:
            out.append(f"| {short(d['model'])} | {d['mode']} | **KHÔNG NẠP ĐƯỢC** | — | — | — | — | — | — | — | — | — |")
            continue
        s = d["summary"]
        lo, hi = wilson(s["pass"], s["total"])
        out.append(
            f"| {short(d['model'])} | `{d['mode']}` | **{s['pass']}** ({s['pass_rate_pct']}%) | {lo:.0f}–{hi:.0f}% | {sum(1 for t in d['tasks'] if t.get('finish') == 'length')} | "
            f"{s['decode_tok_s']['p50']} | {s['tpot_ms']['p50']} | {s['ttft_ms']['p50']} | {s['long_prompt_prefill']['prefill_tok_s']['p50']} | "
            f"{s['joule_per_output_token']} | {s['joule_per_correct_task']} | {s['seconds_per_correct_task']} |")
    return "\n".join(out)


def resource_table(rows):
    out = ["| Model | Chế độ | Nạp (s) | VRAM 3060 (MiB) | VRAM 5070 (MiB) | RAM host mmap (MiB) | Công suất 3060 / 5070 (W) | Mạng khi đang sinh (Mbit/s) | Byte mạng / token | CPU host (%) | C=1 → C=2 (tok/s gộp) |",
           "|:--|:--|--:|--:|--:|--:|:--:|--:|--:|--:|:--:|"]
    for d in rows:
        if "summary" not in d:
            continue
        s, dep = d["summary"], d["deployment"]
        loc, rem, cpu = vram_nodes(d)
        p = s["power"]
        c = s["concurrency"]
        out.append(
            f"| {short(d['model'])} | `{d['mode']}` | {dep['cold_load_seconds']} | {loc:,.0f} | {rem:,.0f} | {cpu:,.0f} | "
            f"{p['local_3060'].get('mean_power_w', 0)} / {p['win_5070'].get('mean_power_w', 0)} | "
            f"{s['net_bytes_total'] * 8 / 1e6 / max(1e-9, s['gen_seconds_per_task'] * s['total']):.0f} | {s['net_bytes_per_output_token']:,.0f} | {s['cpu_util_pct_host']} | {c['C1_aggregate_tok_s']} → {c['C2_aggregate_tok_s']} ({c['C2_aggregate_tok_s'] / c['C1_aggregate_tok_s']:.2f}×) |")
    return "\n".join(out)


def pairs_table(rows):
    by = {}
    for d in rows:
        if "summary" in d:
            by.setdefault(d["model"], {})[d["mode"]] = d
    out = ["| Model | So sánh | Tốc độ giải mã | TTFT p50 | J/token | Prefill dài | Pass@1 | Số bài khác kết quả | Tăng tốc theo C=2 |",
           "|:--|:--|--:|--:|--:|--:|--:|--:|--:|"]
    for model, m in by.items():
        one = m.get("split") or m.get("smart_1gpu")
        mg = m.get("merged")
        if not (one and mg):
            continue
        a, b = one["summary"], mg["summary"]
        ta = {t["task_id"]: t["passed"] for t in one["tasks"]}
        tb = {t["task_id"]: t["passed"] for t in mg["tasks"]}
        diff = sum(1 for k in ta if k in tb and ta[k] != tb[k])
        ra = lambda x, y: f"{x / y:.2f}×" if y else "—"  # noqa: E731
        out.append(
            f"| {short(model)} | merged vs `{one['mode']}` | **{ra(b['decode_tok_s']['p50'], a['decode_tok_s']['p50'])}** ({a['decode_tok_s']['p50']} → {b['decode_tok_s']['p50']}) | "
            f"{ra(b['ttft_ms']['p50'], a['ttft_ms']['p50'])} ({a['ttft_ms']['p50']} → {b['ttft_ms']['p50']}) | "
            f"{ra(b['joule_per_output_token'], a['joule_per_output_token'])} ({a['joule_per_output_token']} → {b['joule_per_output_token']}) | "
            f"{ra(b['long_prompt_prefill']['prefill_tok_s']['p50'], a['long_prompt_prefill']['prefill_tok_s']['p50'])} | "
            f"{a['pass']} → {b['pass']} | {diff}/164 | "
            f"{ra(b['concurrency']['C2_aggregate_tok_s'], a['concurrency']['C2_aggregate_tok_s'])} |")
    return "\n".join(out)


def causes_table(rows):
    out = ["| Model | Chế độ | Pass | Sai logic (AssertionError) | Lỗi cú pháp/chạy | Bị cắt khi sinh | Token sinh/bài | Prompt token/bài |", "|:--|:--|--:|--:|--:|--:|--:|--:|"]
    for d in rows:
        if "summary" not in d:
            continue
        s = d["summary"]
        c = s["fail_causes"]
        wrong = c.get("AssertionError", 0)
        other = sum(v for k, v in c.items() if k not in ("pass", "truncated", "AssertionError"))
        out.append(f"| {short(d['model'])} | `{d['mode']}` | {s['pass']} | {wrong} | {other} | {c.get('truncated', 0)} | {s['out_tokens_per_task']} | {s['prompt_tokens_per_task']} |")
    return "\n".join(out)


if __name__ == "__main__":
    rows = load()
    txt = ["## TABLE_MAIN", main_table(rows), "", "## TABLE_RESOURCE", resource_table(rows), "", "## TABLE_PAIRS", pairs_table(rows), "",
           "## TABLE_CAUSES", causes_table(rows)]
    (D / "_tables.md").write_text("\n".join(txt), encoding="utf-8")
    print("\n".join(txt))
