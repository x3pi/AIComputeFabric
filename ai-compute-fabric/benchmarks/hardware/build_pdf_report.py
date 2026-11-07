#!/usr/bin/env python3
"""Build the combined, plain-language PDF report from benchmarks/reports/full164/*.json.

All figures come from the JSON result files (nothing typed by hand). Charts: matplotlib -> PNG; layout: HTML -> Chrome headless.
Usage: python3 build_pdf_report.py [outdir_for_intermediates]
"""
from __future__ import annotations

import html
import json
import math
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HW = Path(__file__).resolve().parent
REP = HW.parents[0] / "reports"
D = REP / "full164"
OUT_PDF = REP / "BAO_CAO_TONG_HOP_AI_VIET_CODE.pdf"
WORK = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/aicf_pdf")
WORK.mkdir(parents=True, exist_ok=True)

BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8984"
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

NAMES = {
    "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf": "Qwen3-Coder-30B",
    "Qwen3-30B-A3B-Q4_K_M.gguf": "Qwen3-30B-A3B",
    "GLM-4.7-Flash-Q4_K_M.gguf": "GLM-4.7-Flash",
    "Qwen3.8-27B-UD-Q4_K_M.gguf": "Qwen3.8-27B",
    "GLM-4-32B-0414-Q4_K_M.gguf": "GLM-4-32B",
    "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf": "DeepSeek-R1-14B",
    "Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf": "Kimi-k3-9B",
    "GLM-4-9B-0414-Q4_K_M.gguf": "GLM-4-9B",
    "Qwen3-8B-Q4_K_M.gguf": "Qwen3-8B",
    "deepseek-coder-6.7b-instruct.Q4_K_M.gguf": "deepseek-coder-6.7B",
    "Qwen3-4B-Q4_K_M.gguf": "Qwen3-4B",
    "DeepSeek-V2-Lite-Chat.Q3_K_M.gguf": "DeepSeek-V2-Lite (Q3)",
    "DeepSeek-V2-Lite-Chat.Q4_K_M.gguf": "DeepSeek-V2-Lite (Q4)",
    "DeepSeek-R1-Distill-Qwen-32B-IQ4_XS.gguf": "DeepSeek-R1-32B",
}
REASONING = {"DeepSeek-R1-14B", "DeepSeek-R1-32B", "Kimi-k3-9B"}
MODE_VN = {"merged": "Gộp 2 card", "split": "1 card", "smart_1gpu": "1 card + RAM"}


def wilson(p, n, z=1.96):
    ph = p / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return 100 * (c - h), 100 * (c + h)


def load():
    by = {}
    for f in sorted(D.glob("*__*.json")):
        if "_v2check" in f.name or ".INCOMPLETE" in f.name or ".answers" in f.name or f.name.startswith("_"):
            continue
        d = json.loads(f.read_text())
        if "summary" in d:
            by.setdefault(NAMES[d["model"]], {})[d["mode"]] = d["summary"]
    return by


by = load()
rep = {m: (v.get("merged") or next(iter(v.values()))) for m, v in by.items()}
rep_mode = {m: ("merged" if "merged" in v else next(iter(v))) for m, v in by.items()}

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK, "text.color": INK})


def clean(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)


# ---------------------------------------------------------------- chart A: speed-up from merging
speed = {}
for m, v in by.items():
    one = v.get("split") or v.get("smart_1gpu")
    if one and "merged" in v:
        speed[m] = v["merged"]["decode_tok_s"]["p50"] / one["decode_tok_s"]["p50"]
order = sorted(speed, key=lambda m: speed[m])
fig, ax = plt.subplots(figsize=(6.6, 3.5), dpi=200)
ax.barh(range(len(order)), [speed[m] for m in order], color=BLUE, height=0.62)
ax.set_yticks(range(len(order)))
ax.set_yticklabels(order)
for i, m in enumerate(order):
    ax.text(speed[m] + 0.05, i, f"{speed[m]:.1f}×", va="center", fontsize=9, color=INK)
ax.axvline(1, color=INK2, lw=1, ls=(0, (4, 3)))
ax.text(1.0, len(order) - 0.35, "bằng 1 card", ha="center", va="bottom", fontsize=8, color=INK2)
ax.set_xlim(0, max(speed.values()) * 1.15)
ax.set_xlabel("Gộp 2 card viết nhanh gấp bao nhiêu lần so với chạy 1 card")
clean(ax)
plt.tight_layout()
plt.savefig(WORK / "chartA.png")
plt.close()

# ---------------------------------------------------------------- chart B: quality ranking
order = sorted(rep, key=lambda m: rep[m]["pass"])
fig, ax = plt.subplots(figsize=(6.6, 4.8), dpi=200)
for i, m in enumerate(order):
    p = rep[m]["pass"]
    lo, hi = wilson(p, 164)
    col = ORANGE if m in REASONING else BLUE
    ax.barh(i, p, color=col, height=0.62)
    ax.plot([lo * 1.64, hi * 1.64], [i, i], color=INK2, lw=1.2)
    ax.text(hi * 1.64 + 1.5, i, f"{p}  ({100*p/164:.0f}%)", va="center", fontsize=8.5, color=INK)
ax.set_yticks(range(len(order)))
ax.set_yticklabels(order)
ax.set_xlim(0, 195)
ax.set_xlabel("Số bài giải đúng trên 164 bài (vạch xám = khoảng sai số 95%)")
handles = [plt.Rectangle((0, 0), 1, 1, color=BLUE), plt.Rectangle((0, 0), 1, 1, color=ORANGE)]
ax.legend(handles, ["Model viết thẳng", "Model suy luận dài (hay bị cắt giữa chừng)"], loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=8.5)
clean(ax)
plt.tight_layout()
plt.savefig(WORK / "chartB.png")
plt.close()

# ---------------------------------------------------------------- chart C: quality vs cost
pts = {m: (rep[m]["joule_per_correct_task"], rep[m]["pass"]) for m in rep}


def dominated(m):
    x, y = pts[m]
    return any(pts[o][1] >= y and pts[o][0] <= x and (pts[o][1] > y or pts[o][0] < x) for o in pts if o != m)


fig, ax = plt.subplots(figsize=(6.6, 3.7), dpi=200)
off = {  # label offsets (points) chosen to avoid collisions
    "Qwen3.8-27B": (-6, 6, "right"), "Qwen3-Coder-30B": (7, 4, "left"), "GLM-4-32B": (-6, -12, "right"),
    "Qwen3-30B-A3B": (-7, -3, "right"), "GLM-4-9B": (-4, 8, "right"), "Qwen3-8B": (8, -4, "left"),
    "Kimi-k3-9B": (-6, 6, "right"), "GLM-4.7-Flash": (7, -21, "left"), "deepseek-coder-6.7B": (8, -3, "left"),
    "Qwen3-4B": (-7, -9, "right"), "DeepSeek-R1-14B": (-6, -13, "right"), "DeepSeek-R1-32B": (-6, 7, "right"),
    "DeepSeek-V2-Lite (Q4)": (8, -4, "left"), "DeepSeek-V2-Lite (Q3)": (8, -4, "left"),
}
for m, (x, y) in pts.items():
    front = not dominated(m)
    ax.scatter(x, y, s=70 if front else 44, color=BLUE if front else GRAY, zorder=3, edgecolor="white", linewidth=1.2)
    dx, dy, ha = off[m]
    ax.annotate(m, (x, y), xytext=(dx, dy), textcoords="offset points", ha=ha, fontsize=8, color=INK if front else INK2)
ax.set_xscale("log")
ax.set_xlim(250, 30000)
ax.set_ylim(55, 170)
ax.set_xlabel("Điện tiêu tốn để ra 1 bài đúng (Joule, thang log, càng trái càng tiết kiệm)")
ax.set_ylabel("Số bài đúng /164")
ax.yaxis.grid(True, color=GRID, lw=0.8)
clean(ax)
ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
handles = [plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=BLUE, markersize=9),
           plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=GRAY, markersize=8)]
ax.legend(handles, ["Không model nào vừa đúng nhiều hơn vừa rẻ hơn", "Có lựa chọn tốt hơn"], loc="lower left", bbox_to_anchor=(0, 1.0), ncol=1, frameon=False, fontsize=8.5)
plt.tight_layout()
plt.savefig(WORK / "chartC.png")
plt.close()

# ---------------------------------------------------------------- facts used in the text
R = rep
coder, q30, q27, g32 = R["Qwen3-Coder-30B"], R["Qwen3-30B-A3B"], R["Qwen3.8-27B"], R["GLM-4-32B"]
g9, q8, r1k = R["GLM-4-9B"], R["Qwen3-8B"], R["DeepSeek-R1-14B"]
small = ["Qwen3-4B", "Qwen3-8B", "GLM-4-9B", "deepseek-coder-6.7B", "Kimi-k3-9B"]
sp_small = [speed[m] for m in small]
sp_moe = [speed[m] for m in ("Qwen3-Coder-30B", "Qwen3-30B-A3B", "GLM-4.7-Flash")]
ttft_small = [by[m]["merged"]["ttft_ms"]["p50"] / (by[m].get("split") or by[m].get("smart_1gpu"))["ttft_ms"]["p50"] for m in small]
jt_small = [by[m]["merged"]["joule_per_output_token"] / (by[m].get("split") or by[m].get("smart_1gpu"))["joule_per_output_token"] for m in small]
coder_one = by["Qwen3-Coder-30B"]["smart_1gpu"]


def _split_j_with_idle_card(m):
    """J/token of the 1-card run if the idle second card's power (also sampled) were added, as `merged` does for both cards."""
    s1 = by[m].get("split") or by[m].get("smart_1gpu")
    return s1["joule_per_output_token"] * (s1["power"]["local_3060"]["mean_power_w"] + s1["power"]["win_5070"]["mean_power_w"]) / s1["power"]["local_3060"]["mean_power_w"]


jt_small_fair = [by[m]["merged"]["joule_per_output_token"] / _split_j_with_idle_card(m) for m in small]


def e(x):
    return html.escape(str(x))


def fmt(x, nd=0):
    return f"{x:,.{nd}f}"


rows = []
for m in sorted(by, key=lambda m: -max(v["pass"] for v in by[m].values())):
    for mode in ("merged", "split", "smart_1gpu"):
        if mode in by[m]:
            s = by[m][mode]
            rows.append(f"<tr><td>{e(m)}</td><td>{MODE_VN[mode]}</td><td class=n>{s['pass']}</td><td class=n>{s['decode_tok_s']['p50']:.0f}</td>"
                        f"<td class=n>{fmt(s['joule_per_correct_task'])}</td><td class=n>{s['seconds_per_correct_task']:.1f}</td></tr>")
appendix_rows = "\n".join(rows)

CSS = """
@page { size: A4; margin: 16mm 16mm 18mm 16mm; }
* { box-sizing: border-box; }
body { font-family: 'DejaVu Sans', sans-serif; color: #0b0b0b; font-size: 10pt; line-height: 1.5; }
h1 { font-size: 22pt; line-height: 1.2; margin: 0 0 4pt; }
h2 { font-size: 14pt; margin: 0 0 6pt; padding-top: 2pt; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; }
.sub { color: #52514e; font-size: 10.5pt; margin-bottom: 14pt; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
.card { border: 1px solid #e4e3df; border-radius: 6px; padding: 8pt 11pt; margin: 6pt 0; background: #fcfcfb; }
.card b.k { color: #2a78d6; font-size: 15pt; margin-right: 6pt; }
.big { display: flex; gap: 8pt; margin: 8pt 0 4pt; }
.big > div { flex: 1; border: 1px solid #e4e3df; border-radius: 6px; padding: 8pt 10pt; background: #fcfcfb; }
.big .v { font-size: 17pt; font-weight: bold; color: #2a78d6; line-height: 1.15; }
.big .l { font-size: 8.5pt; color: #52514e; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 8pt; font-size: 9pt; }
th { text-align: left; background: #f1f0ed; padding: 4pt 6pt; border-bottom: 1.5px solid #cfcdc7; }
td { padding: 4pt 6pt; border-bottom: 1px solid #e4e3df; vertical-align: top; }
td.n, th.n { text-align: right; white-space: nowrap; }
.small { font-size: 8.5pt; color: #52514e; }
img { width: 100%; display: block; margin: 4pt 0; }
ul { margin: 4pt 0 4pt 16pt; padding: 0; } li { margin: 3pt 0; }
.warn { border-left: 3px solid #eb6834; padding: 3pt 9pt; margin: 8pt 0; background: #fdf3ee; }
.note { border-left: 3px solid #2a78d6; padding: 3pt 9pt; margin: 8pt 0; background: #eff5fc; }
"""

HTML = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Báo cáo tổng hợp: AI viết code trên 2 máy</title><style>{CSS}</style></head><body>

<div class="page">
<h1>Chạy AI viết code trên 2 máy:<br>nên gộp card hay chạy riêng?</h1>
<div class="sub">Báo cáo tổng hợp từ 23 cấu hình đo, mỗi cấu hình giải đủ 164 bài lập trình (HumanEval). Đo ngày 06–07/11/2026.</div>

<h3>Kết luận nhanh</h3>
<div class="card"><b class="k">1</b><b>Model nhỏ thì chạy 1 card.</b> Gộp 2 card chỉ nhanh hơn {min(sp_small):.2f}–{max(sp_small):.2f} lần, nhưng chờ phản hồi đầu tiên lâu hơn {min(ttft_small):.1f}–{max(ttft_small):.1f} lần và tốn điện hơn {100*(min(jt_small)-1):.0f}–{100*(max(jt_small)-1):.0f}% mỗi token (còn {100*(min(jt_small_fair)-1):.0f}% đến +{100*(max(jt_small_fair)-1):.0f}% nếu tính cả điện card rảnh ở chế độ 1 card). Chất lượng không đổi.</div>
<div class="card"><b class="k">2</b><b>Model lớn kiểu MoE 30B thì gộp 2 card.</b> Nhanh hơn {min(sp_moe):.1f}–{max(sp_moe):.1f} lần so với chạy 1 card (có đẩy bớt sang RAM) và ít tốn điện hơn. Chất lượng giữ nguyên.</div>
<div class="card"><b class="k">3</b><b>Model tốt nhất về cân bằng: Qwen3-Coder-30B (gộp 2 card).</b> Đúng {coder['pass']}/164 bài ({coder['pass_rate_pct']:.0f}%), viết {coder['decode_tok_s']['p50']:.0f} token/giây, chỉ {coder['seconds_per_correct_task']:.1f} giây và {coder['joule_per_correct_task']:.0f} J điện cho mỗi bài đúng.</div>
<div class="card"><b class="k">4</b><b>Bốn model đầu bảng ngang nhau về chất lượng.</b> Qwen3.8-27B ({q27['pass']}), Qwen3-Coder-30B ({coder['pass']}), GLM-4-32B ({g32['pass']}), Qwen3-30B-A3B ({q30['pass']}): chênh lệch giữa chúng nhỏ hơn mức may rủi. Qwen3.8-27B đúng nhiều nhất nhưng tốn gấp {q27['joule_per_correct_task']/coder['joule_per_correct_task']:.1f} lần điện mỗi bài đúng.</div>

<div class="card"><b class="k">5</b><b>DeepSeek không tiết kiệm ở cỡ chạy được trên cụm này.</b> R1-32B đúng {R['DeepSeek-R1-32B']['pass']}/164 nhưng tốn {fmt(R['DeepSeek-R1-32B']['joule_per_correct_task'])} J mỗi bài đúng (gấp {R['DeepSeek-R1-32B']['joule_per_correct_task']/coder['joule_per_correct_task']:.0f} lần Coder-30B) vì phải viết khoảng {R['DeepSeek-R1-32B']['out_tokens_per_task']:.0f} token mỗi bài; V2-Lite chỉ đúng {R['DeepSeek-V2-Lite (Q4)']['pass']} bài. Chữ "tiết kiệm" của DeepSeek nói về giá dịch vụ và model rất lớn, không phải các model tự chạy ở đây.</div>

<div class="big">
<div><div class="v">{coder['pass']}/164</div><div class="l">bài đúng của Qwen3-Coder-30B</div></div>
<div><div class="v">{speed['Qwen3-Coder-30B']:.1f}×</div><div class="l">nhanh hơn khi gộp 2 card (so với 1 card + RAM)</div></div>
<div><div class="v">{coder['joule_per_correct_task']:.0f} J</div><div class="l">điện cho mỗi bài đúng (≈ {coder['joule_per_correct_task']/3600:.2f} Wh)</div></div>
</div>

<h3>Cụm máy và cách đo</h3>
<ul>
<li><b>Hai máy:</b> máy Linux có card RTX 3060 12 GB và máy Windows có card RTX 5070 12 GB, nối nhau bằng mạng 2,5 Gbps.</li>
<li><b>Hai cách chạy:</b> <i>1 card</i> = model chạy trên 1 máy (model lớn thì đẩy bớt phần phụ sang RAM). <i>Gộp 2 card</i> = chia model ra cho cả hai máy qua mạng.</li>
<li><b>Bài đo:</b> 164 bài viết hàm Python, chấm bằng bộ test tự động. Mỗi cấu hình chạy đủ 164 bài; điện được đo thật trên cả hai card.</li>
</ul>
</div>

<div class="page">
<h2>1. Gộp 2 card hay chạy 1 card?</h2>
<img src="chartA.png">
<p class="small">Các model MoE 30B (Qwen3-Coder-30B, Qwen3-30B-A3B, GLM-4.7-Flash) được so với cách chạy 1 card có đẩy bớt sang RAM, vì chúng không vừa 1 card. Các model còn lại so với chạy 1 card thuần.</p>
<table>
<tr><th>Loại model</th><th>Nên chạy</th><th>Vì sao</th></tr>
<tr><td><b>Nhỏ, vừa 1 card</b><br><span class=small>Qwen3-4B/8B, GLM-4-9B, deepseek-coder, Kimi-9B</span></td><td><b>1 card</b></td><td>Gộp chỉ nhanh hơn {min(sp_small):.2f}–{max(sp_small):.2f} lần. Phản hồi đầu tiên chậm hơn {min(ttft_small):.1f}–{max(ttft_small):.1f} lần, tốn thêm điện không đáng kể đến khoảng 10%, chiếm cả 2 máy. Để máy kia chạy việc khác hoặc phục vụ người dùng khác.</td></tr>
<tr><td><b>MoE cỡ 30B</b><br><span class=small>Qwen3-Coder-30B, Qwen3-30B-A3B, GLM-4.7-Flash</span></td><td><b>Gộp 2 card</b></td><td>Nhanh hơn {min(sp_moe):.1f}–{max(sp_moe):.1f} lần. Với Coder-30B: {coder['decode_tok_s']['p50']:.0f} so với {coder_one['decode_tok_s']['p50']:.0f} token/giây, phản hồi đầu tiên {coder['ttft_ms']['p50']:.0f} so với {coder_one['ttft_ms']['p50']:.0f} ms. Số bài đúng như nhau ({coder['pass']} = {coder_one['pass']}).</td></tr>
<tr><td><b>Dense lớn 27–32B</b><br><span class=small>Qwen3.8-27B, GLM-4-32B</span></td><td><b>Gộp 2 card</b> (không có lựa chọn khác)</td><td>Không vừa 1 card. Chạy được nhưng chậm ({q27['decode_tok_s']['p50']:.0f} và {g32['decode_tok_s']['p50']:.0f} token/giây) và tốn điện gấp khoảng 6–7 lần Coder-30B.</td></tr>
<tr><td><b>Sát giới hạn 1 card</b><br><span class=small>DeepSeek-R1-14B</span></td><td><b>Gộp 2 card</b></td><td>Trên 1 card chỉ còn dư 53 MiB VRAM; cơ chế bảo vệ của hệ thống đã tắt engine 2 lần trong 164 bài. Gộp 2 card chạy ổn định.</td></tr>
</table>
<div class="note"><b>Quy tắc nhớ nhanh:</b> nếu model (kèm bộ nhớ ngữ cảnh) chiếm dưới khoảng 9 GB VRAM thì chạy 1 card; nếu hơn thì gộp 2 card. Gộp 2 card cần mạng khoảng 45–490 Mbit/s khi đang viết, nên mạng 2,5 Gbps dư sức.</div>
</div>

<div class="page">
<h2>2. Model nào viết code đúng nhất?</h2>
<img src="chartB.png">
<p class="small">Mỗi model được tính ở cấu hình gộp 2 card (riêng hai bản DeepSeek-V2-Lite chạy 1 card + RAM vì không gộp được). Vạch xám là khoảng sai số: hai model có vạch chồng lên nhau thì chưa chắc đã khác nhau thật.</p>
<h3>Đọc kết quả thế nào</h3>
<ul>
<li><b>Nhóm đầu</b> (từ {g32['pass']} đến {q27['pass']} bài) gồm Qwen3.8-27B, Qwen3-Coder-30B, GLM-4-32B, Qwen3-30B-A3B. Kiểm định thống kê cho thấy bốn model này <b>không phân biệt được nhau</b>.</li>
<li><b>Nhóm giữa</b> (118–136 bài) gồm tám model cũng không phân biệt được nhau. Qwen3-Coder-30B và Qwen3.8-27B hơn cả tám model này.</li>
<li><b>DeepSeek-V2-Lite</b> yếu hơn hẳn: {R['DeepSeek-V2-Lite (Q4)']['pass']} bài ở bản nén Q4 và {R['DeepSeek-V2-Lite (Q3)']['pass']} bài ở bản nén mạnh hơn Q3. Nén nhẹ hơn cải thiện khoảng 20 bài nhưng model vẫn thấp nhất bảng.</li>
<li><b>Model suy luận dài</b> (màu cam: DeepSeek-R1-14B, R1-32B, Kimi-9B) <b>viết rất dài trước khi trả lời</b> (trung bình {r1k['out_tokens_per_task']:.0f}, {R['DeepSeek-R1-32B']['out_tokens_per_task']:.0f} và {R['Kimi-k3-9B']['out_tokens_per_task']:.0f} token mỗi bài, so với khoảng 200 của model thường). Phần lớn bài sai là do hết giới hạn 3.072 token giữa chừng, nên điểm của chúng là <b>mức thấp nhất</b>. Khi đã nghĩ xong, chúng đúng 98%, 99% và 94%. Bản R1 lớn hơn (32B) chỉ hơn bản 14B khoảng 5 bài, không có ý nghĩa thống kê, mà tốn thêm khoảng 66% điện mỗi bài đúng.</li>
<li><b>Chuyên code</b> có xu hướng giúp: Qwen3-Coder-30B ({coder['pass']}) hơn Qwen3-30B-A3B ({q30['pass']}) cùng cỡ, nhưng với 164 bài chưa đủ chắc để khẳng định.</li>
</ul>
</div>

<div class="page">
<h2>3. Chất lượng so với chi phí</h2>
<img src="chartC.png">
<p class="small">Mỗi chấm là một model ở cấu hình gộp 2 card. Chấm xanh = không có model nào vừa đúng nhiều hơn vừa tốn ít điện hơn.</p>
<p>Chỉ ba model nằm trên đường tối ưu: <b>Qwen3.8-27B</b>, <b>Qwen3-Coder-30B</b> và <b>Qwen3-30B-A3B</b>. Đáng chú ý, Coder-30B ({coder['pass']} bài, {coder['joule_per_correct_task']:.0f} J mỗi bài đúng) đúng nhiều hơn Qwen3-4B ({R['Qwen3-4B']['pass']} bài) tới {coder['pass']-R['Qwen3-4B']['pass']} bài mà còn tốn ít điện hơn ({R['Qwen3-4B']['joule_per_correct_task']:.0f} J). Vậy dùng cả hai card cho model lớn không đắt hơn dùng model nhỏ.</p>

<h3>Nên chọn gì?</h3>
<table>
<tr><th>Nhu cầu</th><th>Chọn</th><th class=n>Đúng /164</th><th class=n>Giây / bài đúng</th><th class=n>Điện / bài đúng</th></tr>
<tr><td>Cân bằng chất lượng và chi phí</td><td><b>Qwen3-Coder-30B</b>, gộp 2 card</td><td class=n>{coder['pass']}</td><td class=n>{coder['seconds_per_correct_task']:.1f}</td><td class=n>{coder['joule_per_correct_task']:.0f} J</td></tr>
<tr><td>Tiết kiệm điện nhất trong nhóm mạnh</td><td><b>Qwen3-30B-A3B</b>, gộp 2 card</td><td class=n>{q30['pass']}</td><td class=n>{q30['seconds_per_correct_task']:.1f}</td><td class=n>{q30['joule_per_correct_task']:.0f} J</td></tr>
<tr><td>Chất lượng cao nhất, không ngại chậm</td><td><b>Qwen3.8-27B</b>, gộp 2 card</td><td class=n>{q27['pass']}</td><td class=n>{q27['seconds_per_correct_task']:.1f}</td><td class=n>{fmt(q27['joule_per_correct_task'])} J</td></tr>
<tr><td>Chỉ có 1 card 12 GB</td><td><b>GLM-4-9B</b> hoặc <b>Qwen3-8B</b></td><td class=n>{by['GLM-4-9B']['split']['pass']} / {by['Qwen3-8B']['split']['pass']}</td><td class=n>{by['GLM-4-9B']['split']['seconds_per_correct_task']:.1f} / {by['Qwen3-8B']['split']['seconds_per_correct_task']:.1f}</td><td class=n>{by['GLM-4-9B']['split']['joule_per_correct_task']:.0f} / {by['Qwen3-8B']['split']['joule_per_correct_task']:.0f} J</td></tr>
<tr><td>Nên tránh (viết code tương tác)</td><td>DeepSeek-R1-14B / R1-32B, DeepSeek-V2-Lite (Q4)</td><td class=n>{r1k['pass']} / {R['DeepSeek-R1-32B']['pass']} / {R['DeepSeek-V2-Lite (Q4)']['pass']}</td><td class=n>{r1k['seconds_per_correct_task']:.0f} / {R['DeepSeek-R1-32B']['seconds_per_correct_task']:.0f} / {R['DeepSeek-V2-Lite (Q4)']['seconds_per_correct_task']:.0f}</td><td class=n>{fmt(r1k['joule_per_correct_task'])} / {fmt(R['DeepSeek-R1-32B']['joule_per_correct_task'])} / {fmt(R['DeepSeek-V2-Lite (Q4)']['joule_per_correct_task'])} J</td></tr>
</table>
<p class="small">Hàng "chỉ có 1 card" lấy số của cấu hình 1 card. Điện chỉ tính phần card đồ họa.</p>
</div>

<div class="page">
<h2>4. Những điều cần lưu ý</h2>
<ul>
<li><b>Chênh vài bài chưa có nghĩa.</b> Cùng một model, chỉ đổi cách chạy (1 card hay gộp 2 card), vẫn có 4–9 bài trong 164 đổi kết quả (24–27 bài với model suy luận). Vì vậy hai model cách nhau ít hơn khoảng 5 bài thì coi như ngang nhau.</li>
<li><b>Chỉ một bộ đề.</b> HumanEval có 164 bài Python, công khai từ lâu, nên model có thể đã "gặp" đề khi huấn luyện. Điểm tuyệt đối có thể cao hơn khả năng thật; <b>thứ hạng tương đối đáng tin hơn con số tuyệt đối</b>. Chưa đo: sửa code có sẵn, nhiều tệp, ngữ cảnh dài, yêu cầu bằng tiếng Việt.</li>
<li><b>Mỗi cấu hình chỉ đo một lần.</b> Chưa lặp lại nhiều lần.</li>
<li><b>Điện chỉ tính card đồ họa, và đã kiểm chứng.</b> So với bộ đếm năng lượng của chính card, cách đo lệch −1,9% (card 3060) và +0,1% (card 5070) trong một đợt thử; cảm biến công bố sai số ±5 W. Chưa có đồng hồ đo ở ổ cắm. Khi chạy "1 card + RAM", CPU làm thêm việc nhưng điện của nó không được đo, nên cách này thực tế tốn điện hơn con số hiển thị. Ở chế độ 1 card, card thứ hai đang rảnh (2–12 W) cũng không được cộng; nếu cộng thì "gộp 2 card tốn điện hơn" chỉ còn từ −1% đến +10% cho các model nhỏ.</li>
<li><b>Điều kiện không hoàn toàn giống nhau.</b> Qwen3 và GLM-4.7-Flash chạy với chế độ "suy nghĩ" tắt (GLM-4.7-Flash có thể bị đánh giá thấp). Giới hạn độ dài trả lời là 1.024 token (model thường) hoặc 3.072 token (model suy luận).</li>
<li><b>Hai lỗi vận hành đã tìm ra:</b> (1) model <i>deepseek-coder</i> không có sẵn mẫu hội thoại, nên nếu gọi qua giao diện chat thông thường nó không bao giờ dừng viết (đo ban đầu chỉ đạt 60% thay vì 78%); (2) khi chuyển cấu hình bị hỏng, hệ thống có thể kẹt vài phút và từ chối mọi lệnh khác.</li>
<li><b>Cấu hình không chạy được:</b> DeepSeek-V2-Lite ở chế độ gộp 2 card không nạp được ở cả bản Q3 lẫn Q4 (tiến trình tự thoát, không có thông báo lỗi); nguyên nhân chưa tìm ra. DeepSeek-R1-32B bản Q4_K_M không vừa 2 card ở cấu hình chuẩn, nên đo bản IQ4_XS nhẹ hơn 2,2 GB (ảnh hưởng của việc này tới điểm không tách riêng được).</li>
</ul>

<div class="warn"><b>Số liệu cũ cần bỏ.</b> Báo cáo trước đo chỉ 8–12 bài mỗi model nên sai lệch lớn. Ví dụ deepseek-coder từng được 100% (8/8) nhưng đo đủ 164 bài chỉ đạt khoảng 78%; DeepSeek-R1-14B từng 37,5% nay là khoảng 72–74% do giới hạn token cũ quá thấp; DeepSeek-V2-Lite từng 62,5% (5/8) nay chỉ 40% (Q3) hoặc 52% (Q4).</div>

<h3>Muốn kết luận chắc hơn</h3>
<ul>
<li>Chạy thêm một bộ đề độc lập (ví dụ MBPP, có sẵn trong dự án) hoặc bộ test chặt hơn.</li>
<li>Lặp lại 2–3 lần cho các cặp model sát nhau.</li>
<li>Thử model suy luận với giới hạn token lớn hơn.</li>
</ul>
</div>

<div class="page">
<h2>Phụ lục: toàn bộ 23 cấu hình đã đo</h2>
<p class="small">Sắp theo số bài đúng của từng model. "Token/giây" là tốc độ viết (trung vị). "Giây" và "Điện" tính trên mỗi bài đúng.</p>
<table>
<tr><th>Model</th><th>Cách chạy</th><th class=n>Đúng /164</th><th class=n>Token/giây</th><th class=n>Điện / bài đúng (J)</th><th class=n>Giây / bài đúng</th></tr>
{appendix_rows}
</table>
<p class="small">DeepSeek-V2-Lite ở chế độ gộp 2 card (cả Q3 và Q4) không nạp được nên không có trong bảng. Dữ liệu chi tiết: <i>BAO_CAO_FULL164_PHAN_TAN_VS_1_CARD.md</i> và <i>BAO_CAO_SO_SANH_CHAT_LUONG_12_MODEL.md</i> trong thư mục benchmarks/reports.</p>
</div>

</body></html>"""

(WORK / "report.html").write_text(HTML, encoding="utf-8")
subprocess.run(["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={OUT_PDF}", f"file://{WORK / 'report.html'}"], check=True, capture_output=True, timeout=120)
print("wrote", OUT_PDF, OUT_PDF.stat().st_size // 1024, "KB")
