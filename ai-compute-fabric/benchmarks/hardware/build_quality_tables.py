#!/usr/bin/env python3
"""Quality-comparison tables from benchmarks/reports/full164/*.json (per-task pass vectors of every model/config).

Representative config per model = `merged` when it exists, otherwise the only valid config. Mode barely changes quality
(see the mode-flip table), so this keeps one fixed, non-cherry-picked rule.
"""
from __future__ import annotations

import itertools
import json
import math
from collections import defaultdict
from pathlib import Path

from scipy.stats import binomtest

HW = Path(__file__).resolve().parent
D = HW.parents[0] / "reports" / "full164"
HE = HW.parents[0] / "data" / "HumanEval.jsonl"
MODELS_DIR = HW.parents[1] / ".demo" / "models"

# (display name, type, params label). Params are vendor/earlier-report figures, NOT measured here.
META = {
    "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf": ("Qwen3-Coder-30B-A3B", "MoE", "30.5B / 3.3B active"),
    "Qwen3-30B-A3B-Q4_K_M.gguf": ("Qwen3-30B-A3B", "MoE", "30.5B / 3.3B active"),
    "GLM-4.7-Flash-Q4_K_M.gguf": ("GLM-4.7-Flash", "MoE", "~30B / ~3B active"),
    "Qwen3.8-27B-UD-Q4_K_M.gguf": ("Qwen3.8-27B", "dense", "27B"),
    "GLM-4-32B-0414-Q4_K_M.gguf": ("GLM-4-32B-0414", "dense", "32B"),
    "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf": ("DeepSeek-R1-14B", "dense, reasoning", "14.8B"),
    "Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf": ("Kimi-k3-9B", "hybrid, reasoning", "9.2B"),
    "GLM-4-9B-0414-Q4_K_M.gguf": ("GLM-4-9B-0414", "dense", "9B"),
    "Qwen3-8B-Q4_K_M.gguf": ("Qwen3-8B", "dense", "8B"),
    "deepseek-coder-6.7b-instruct.Q4_K_M.gguf": ("deepseek-coder-6.7B", "dense", "6.7B"),
    "Qwen3-4B-Q4_K_M.gguf": ("Qwen3-4B", "dense", "4B"),
    "DeepSeek-V2-Lite-Chat.Q3_K_M.gguf": ("DeepSeek-V2-Lite (Q3)", "MoE", "15.7B / 2.4B active"),
    "DeepSeek-V2-Lite-Chat.Q4_K_M.gguf": ("DeepSeek-V2-Lite (Q4)", "MoE", "15.7B / 2.4B active"),
    "DeepSeek-R1-Distill-Qwen-32B-IQ4_XS.gguf": ("DeepSeek-R1-32B (IQ4_XS)", "dense, reasoning", "32.8B"),
}


def wilson(p, n, z=1.96):
    ph = p / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return 100 * (c - h), 100 * (c + h)


def load():
    by = defaultdict(dict)
    for f in sorted(D.glob("*__*.json")):
        if "_v2check" in f.name or ".INCOMPLETE" in f.name or ".answers" in f.name or f.name.startswith("_"):
            continue
        d = json.loads(f.read_text())
        if "summary" in d:
            by[d["model"]][d["mode"]] = d
    return by


def vec(d):
    return {t["task_id"]: bool(t["passed"]) for t in d["tasks"]}


def main():
    by = load()
    probs = [json.loads(l) for l in open(HE, encoding="utf-8") if l.strip()]
    ids = [p["task_id"] for p in probs]
    rep = {}
    for m, modes in by.items():
        rep[m] = modes.get("merged") or next(iter(modes.values()))
    names = {m: META[m][0] for m in rep}
    V = {m: vec(rep[m]) for m in rep}
    score = {m: sum(V[m].values()) for m in rep}
    order = sorted(rep, key=lambda m: -score[m])
    out = []

    # ---------------- 1. ranking
    out += ["## Q_RANK",
            "| Hạng | Model | Loại | Tham số | Cỡ file (GiB) | Pass@1 | CI 95% | Cấu hình dùng | Pass@1 mỗi chế độ |",
            "|--:|:--|:--|:--|--:|--:|:--:|:--|:--|"]
    for i, m in enumerate(order, 1):
        lo, hi = wilson(score[m], 164)
        try:
            gib = (MODELS_DIR / m).resolve().stat().st_size / 2**30
        except FileNotFoundError:  # GGUF deleted after its benchmark to free disk; sizes taken from the earlier table
            gib = {"GLM-4-32B-0414-Q4_K_M.gguf": 18.3, "GLM-4.7-Flash-Q4_K_M.gguf": 17.1, "GLM-4-9B-0414-Q4_K_M.gguf": 5.7}[m]
        per_mode = " · ".join(f"`{k}` {sum(vec(v).values())}" for k, v in sorted(by[m].items()))
        out.append(f"| {i} | **{names[m]}** | {META[m][1]} | {META[m][2]} | {gib:.1f} | **{score[m]}** ({100*score[m]/164:.1f}%) | {lo:.0f}–{hi:.0f}% | `{rep[m]['mode']}` | {per_mode} |")

    # ---------------- 2. paired significance
    pairs = []
    for a, b in itertools.combinations(order, 2):
        n01 = sum(1 for t in ids if V[a][t] and not V[b][t])  # a only
        n10 = sum(1 for t in ids if V[b][t] and not V[a][t])  # b only
        p = binomtest(n01, n01 + n10, 0.5).pvalue if n01 + n10 else 1.0
        pairs.append((a, b, n01, n10, p))
    # Holm
    srt = sorted(range(len(pairs)), key=lambda i: pairs[i][4])
    holm = {}
    run = 0.0
    for rank, i in enumerate(srt):
        adj = min(1.0, pairs[i][4] * (len(pairs) - rank))
        run = max(run, adj)
        holm[i] = run
    sig = defaultdict(lambda: {"better": [], "worse": []})
    nsig_raw = nsig_holm = 0
    for i, (a, b, n01, n10, p) in enumerate(pairs):
        if p < 0.05:
            nsig_raw += 1
        if holm[i] < 0.05:
            nsig_holm += 1
            hi_, lo_ = (a, b) if n01 > n10 else (b, a)
            sig[hi_]["better"].append(names[lo_])
            sig[lo_]["worse"].append(names[hi_])
    out += ["", "## Q_SIG", f"Tổng {len(pairs)} cặp; khác biệt có ý nghĩa **trước hiệu chỉnh** (p<0.05): {nsig_raw}; **sau hiệu chỉnh Holm**: {nsig_holm}.", "",
            "| Model | Hơn có ý nghĩa (Holm) so với | Kém có ý nghĩa (Holm) so với |", "|:--|:--|:--|"]
    for m in order:
        out.append(f"| **{names[m]}** ({score[m]}) | {', '.join(sig[m]['better']) or '—'} | {', '.join(sig[m]['worse']) or '—'} |")

    # tiers: greedy, a model joins the current tier if it is not Holm-worse than the tier's first member
    tiers, cur = [], []
    idx = {(a, b): i for i, (a, b, *_r) in enumerate(pairs)}
    for m in order:
        if not cur:
            cur = [m]
            continue
        top = cur[0]
        i = idx.get((top, m), idx.get((m, top)))
        if holm[i] < 0.05:
            tiers.append(cur)
            cur = [m]
        else:
            cur.append(m)
    tiers.append(cur)
    out += ["", "## Q_TIERS", "| Nhóm | Model (điểm) | Ghi chú |", "|:-:|:--|:--|"]
    for k, t in enumerate(tiers, 1):
        out.append(f"| {k} | {', '.join(f'{names[m]} ({score[m]})' for m in t)} | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |")

    # ---------------- 3. task difficulty
    solved = {t: sum(V[m][t] for m in rep) for t in ids}
    dist = defaultdict(int)
    for t in ids:
        dist[solved[t]] += 1
    n = len(rep)
    out += ["", "## Q_DIFF",
            f"- Giải được bởi **cả {n} model**: **{dist[n]}** bài ({100*dist[n]/164:.0f}%)",
            f"- Giải được bởi **không model nào**: **{dist[0]}** bài",
            f"- Chỉ 1–3 model giải được: **{sum(dist[k] for k in (1, 2, 3))}** bài",
            "", "| Số model giải được | " + " | ".join(str(k) for k in range(n, -1, -1)) + " |",
            "|:--|" + "--:|" * (n + 1),
            "| Số bài | " + " | ".join(str(dist[k]) for k in range(n, -1, -1)) + " |"]
    hard = [t for t in ids if solved[t] == 0]
    out += ["", "Bài không model nào giải được: " + (", ".join(f"`{t}`" for t in hard) or "(không có)"),
            "", "Bài chỉ 1 model giải được: " + (", ".join(f"`{t}` ({names[next(m for m in rep if V[m][t])]})" for t in ids if solved[t] == 1) or "(không có)")]

    # by canonical-solution length tercile (objective difficulty proxy)
    def loc(p):
        return len([l for l in p["canonical_solution"].splitlines() if l.strip()])
    ls = sorted(loc(p) for p in probs)
    q1, q2 = ls[len(ls) // 3], ls[2 * len(ls) // 3]
    band = {p["task_id"]: ("ngắn" if loc(p) <= q1 else ("vừa" if loc(p) <= q2 else "dài")) for p in probs}
    cnt = defaultdict(int)
    for t in ids:
        cnt[band[t]] += 1
    out += ["", "## Q_BAND",
            f"Độ khó đo bằng độ dài lời giải chuẩn (số dòng khác rỗng): ngắn ≤ {q1} dòng ({cnt['ngắn']} bài), vừa ≤ {q2} dòng ({cnt['vừa']} bài), dài > {q2} dòng ({cnt['dài']} bài).", "",
            "| Model | Ngắn | Vừa | Dài | Tụt từ ngắn → dài |", "|:--|--:|--:|--:|--:|"]
    for m in order:
        r = {b: 100 * sum(V[m][t] for t in ids if band[t] == b) / cnt[b] for b in ("ngắn", "vừa", "dài")}
        out.append(f"| {names[m]} | {r['ngắn']:.0f}% | {r['vừa']:.0f}% | {r['dài']:.0f}% | −{r['ngắn'] - r['dài']:.0f} điểm |")

    # ---------------- 4. complementarity (upper bound, needs a verifier to realise)
    out += ["", "## Q_UNION", "Độ phủ nếu *có bộ kiểm tra chọn đúng đáp án* (cận trên, như Best-of-N bằng unit test). Chọn tham lam model bổ sung nhiều bài mới nhất:", "",
            "| Số model | Thêm model | Tổng bài phủ | Tỷ lệ |", "|--:|:--|--:|--:|"]
    covered, chosen = set(), []
    pool = list(rep)
    for k in range(1, 6):
        best = max(pool, key=lambda m: len({t for t in ids if V[m][t]} - covered))
        covered |= {t for t in ids if V[best][t]}
        chosen.append(best)
        pool.remove(best)
        out.append(f"| {k} | {names[best]} | {len(covered)} | {100*len(covered)/164:.1f}% |")

    # ---------------- 5. conditional accuracy and token appetite
    out += ["", "## Q_COND",
            "*Chạm trần* = số bài dùng hết ngân sách token (1024; 2048 cho GLM-4.7-Flash; 3072 cho model suy luận). *Đúng khi đã kết thúc* = tỷ lệ đúng trong các bài model tự dừng.", "",
            "| Model | Token sinh/bài | Chạm trần | Đúng khi đã kết thúc | Pass@1 | Đúng trên 1,000 token sinh |", "|:--|--:|--:|--:|--:|--:|"]
    for m in order:
        d = rep[m]
        T = d["tasks"]
        fin = [t for t in T if t.get("finish") != "length"]
        cap = len(T) - len(fin)
        acc = 100 * sum(t["passed"] for t in fin) / max(1, len(fin))
        tok = sum(t.get("out_tok", 0) for t in T)
        out.append(f"| {names[m]} | {d['summary']['out_tokens_per_task']} | {cap} | {acc:.1f}% ({sum(t['passed'] for t in fin)}/{len(fin)}) | {100*score[m]/164:.1f}% | {1000*score[m]/max(1, tok):.2f} bài |")

    # ---------------- 6. mode flips = noise floor
    out += ["", "## Q_FLIP",
            "Cùng trọng số, `temperature=0`, khác chế độ chạy. Mỗi cặp cho biết số bài **đổi kết quả** và hướng đổi; đây là **mức nhiễu nền** để đánh giá chênh lệch điểm giữa các model.", "",
            "| Model | Cặp chế độ | Bài đúng ở cả hai | Chỉ đúng ở chế độ 1 card | Chỉ đúng ở `merged` | Đổi kết quả | Chênh điểm |", "|:--|:--|--:|--:|--:|--:|--:|"]
    flips = []
    for m in order:
        modes = by[m]
        if "merged" not in modes or len(modes) < 2:
            continue
        one = next(k for k in modes if k != "merged")
        a, b = vec(modes[one]), vec(modes["merged"])
        both = sum(1 for t in ids if a[t] and b[t])
        ao = sum(1 for t in ids if a[t] and not b[t])
        bo = sum(1 for t in ids if b[t] and not a[t])
        flips.append((m, ao + bo))
        out.append(f"| {names[m]} | `{one}` ↔ `merged` | {both} | {ao} | {bo} | {ao+bo} | {bo-ao:+d} |")
    nr = [f for m, f in flips if "reasoning" not in META[m][1]]
    rs = [f for m, f in flips if "reasoning" in META[m][1]]
    out += ["", f"Model không suy luận: đổi kết quả {min(nr)}–{max(nr)} bài/164 (trung vị {sorted(nr)[len(nr)//2]}). Model suy luận: {min(rs)}–{max(rs)} bài/164."]

    # ---------------- 7. quality vs cost, Pareto over all configs
    cfgs = []
    for m, modes in by.items():
        for mode, d in modes.items():
            s = d["summary"]
            cfgs.append((names[m], mode, s["pass"], s["joule_per_correct_task"], s["seconds_per_correct_task"]))

    def dominated(c, key):
        return any(o[2] >= c[2] and o[key] <= c[key] and (o[2] > c[2] or o[key] < c[key]) for o in cfgs)

    out += ["", "## Q_PARETO",
            "Một cấu hình *tối ưu Pareto* nếu không có cấu hình nào vừa đúng nhiều hơn (hoặc bằng) vừa rẻ hơn. ★ = tối ưu theo trục đó.", "",
            "| Model | Chế độ | Pass@1 | Điện/bài đúng (J) | Thời gian/bài đúng (s) | Pareto theo điện | Pareto theo thời gian |", "|:--|:--|--:|--:|--:|:-:|:-:|"]
    for c in sorted(cfgs, key=lambda c: -c[2]):
        out.append(f"| {c[0]} | `{c[1]}` | {c[2]} | {c[3]:,.0f} | {c[4]} | {'★' if not dominated(c, 3) else ''} | {'★' if not dominated(c, 4) else ''} |")

    # ---------------- 8. failure anatomy (representative)
    out += ["", "## Q_FAIL", "| Model | Pass | Sai logic | Lỗi cú pháp/chạy | Bị cắt (chạm trần mà sai) | Tổng sai |", "|:--|--:|--:|--:|--:|--:|"]
    for m in order:
        c = rep[m]["summary"]["fail_causes"]
        wrong = c.get("AssertionError", 0)
        trunc = c.get("truncated", 0)
        other = sum(v for k, v in c.items() if k not in ("pass", "truncated", "AssertionError"))
        out.append(f"| {names[m]} | {c.get('pass', 0)} | {wrong} | {other} | {trunc} | {164 - c.get('pass', 0)} |")

    txt = "\n".join(out)
    (D / "_quality_tables.md").write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
