#!/usr/bin/env python3
"""B07: speculative decoding on one GPU (plan P4.3).

Same target model, same prompts, greedy decoding. For each configuration (no speculation, draft
models of different size, n-gram drafting) it records decode tok/s, draft acceptance and whether
the output text is identical to the no-speculation output. The gate: speculation is enabled by
default only if acceptance >= 0.65, speedup >= 1.25x and greedy output matches 100 %."""
import argparse, json, os, statistics, subprocess, time, urllib.request
from pathlib import Path

PROMPTS = [
    "Liệt kê 5 lợi ích của việc đọc sách.", "Giải thích ngắn gọn sự khác nhau giữa TCP và UDP.",
    "Viết một hàm Python kiểm tra số nguyên tố và giải thích.", "Tóm tắt lịch sử Việt Nam thế kỷ 20 trong một đoạn.",
    "Write a SQL query that returns the top 3 customers by total order value.", "Explain what a mutex is, with a short C++ example.",
    "Dịch sang tiếng Anh: 'Hôm nay trời đẹp, chúng ta đi dạo công viên nhé.'", "Cho ví dụ về hàng đợi công bằng giữa nhiều phòng ban dùng chung GPU.",
    "Viết email xin nghỉ phép 2 ngày gửi quản lý.", "List the steps to rotate an API key safely.",
    "Sửa lỗi chính tả: 'Tôi đang hok bài ở trương đại họt bách khoa.'", "Write a JSON object describing a GPU worker with id, vram and state fields.",
]


def get(url, data=None, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def run_config(a, name, extra, port, logdir):
    cmd = [a.server, "-m", a.target, "-ngl", "99", "-ngld", "99", "-c", "4096", "-np", "1", "--port", str(port), "--host", "127.0.0.1",
           "-fa", "on", "--no-warmup"] + extra
    log = open(Path(logdir) / f"b07_{name}.log", "w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "LD_LIBRARY_PATH": str(Path(a.server).parent)})
    try:
        t0 = time.time()
        while time.time() - t0 < 180:
            try:
                if get(f"http://127.0.0.1:{port}/health").get("status") == "ok":
                    break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit(f"{name}: server not ready")
        body = lambda q: {"messages": [{"role": "user", "content": q}], "max_tokens": a.tokens, "temperature": 0, "seed": 1,
                          "chat_template_kwargs": {"enable_thinking": False}}
        get(f"http://127.0.0.1:{port}/v1/chat/completions", body("Xin chào"))  # warm CUDA graphs / caches, not measured
        rows = []
        for q in PROMPTS:
            r = get(f"http://127.0.0.1:{port}/v1/chat/completions", body(q))
            tm = r["timings"]
            rows.append({"text": r["choices"][0]["message"]["content"], "tok_s": tm["predicted_per_second"], "n": tm["predicted_n"],
                         "draft_n": tm.get("draft_n", 0), "draft_ok": tm.get("draft_n_accepted", 0)})
        return rows
    finally:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    for k in ("server", "target", "out", "logdir"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--draft", action="append", default=[], help="name=path of a draft GGUF (repeatable)")
    ap.add_argument("--tokens", type=int, default=160)
    ap.add_argument("--repeats", type=int, default=2)
    a = ap.parse_args()
    Path(a.logdir).mkdir(parents=True, exist_ok=True)
    configs = [("baseline", [])]
    for d in a.draft:
        n, path = d.split("=", 1)
        for mx in (4, 8):
            configs.append((f"draft_{n}_max{mx}", ["-md", path, "--spec-type", "draft-simple", "--spec-draft-n-max", str(mx)]))
    configs.append(("ngram_simple", ["--spec-type", "ngram-simple"]))
    results = {}
    for rep in range(a.repeats):
        for name, extra in configs:
            rows = run_config(a, name, extra, 18990 + rep, a.logdir)
            results.setdefault(name, []).append(rows)
            print(name, "rep", rep, "median tok/s", round(statistics.median(r["tok_s"] for r in rows), 1), flush=True)
    base_rows = results["baseline"][0]
    summary = {}
    for name, reps in results.items():
        toks = [statistics.median(r["tok_s"] for r in rows) for rows in reps]
        flat = [r for rows in reps for r in rows]
        dn, dk = sum(r["draft_n"] for r in flat), sum(r["draft_ok"] for r in flat)
        same = sum(1 for rows in reps for r, b in zip(rows, base_rows) if r["text"] == b["text"]) / (len(reps) * len(base_rows))
        summary[name] = {"tok_s_median_of_prompts": round(statistics.median(toks), 2), "tok_s_per_repeat": [round(t, 2) for t in toks],
                         "acceptance": round(dk / dn, 3) if dn else None, "greedy_output_identical_to_baseline_pct": round(100 * same, 1)}
    b = summary["baseline"]["tok_s_median_of_prompts"]
    for s in summary.values():
        s["speedup_vs_baseline"] = round(s["tok_s_median_of_prompts"] / b, 3)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "benchmark": "B07", "date": time.strftime("%Y-%m-%d"),
                                        "target": Path(a.target).name, "prompts": len(PROMPTS), "max_tokens": a.tokens, "repeats": a.repeats,
                                        "summary": summary}, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
