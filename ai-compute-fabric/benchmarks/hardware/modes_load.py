#!/usr/bin/env python3
"""Concurrent chat load against one or more llama-server URLs, for the 3-mode report (p3_three_modes_report).

Each URL gets --concurrency simultaneous streams for --rounds rounds; all URLs run at the same time (that is how
"2 independent workers in parallel" is measured). Reports per-URL and aggregate throughput:
  aggregate_tok_s = sum(completion tokens of all requests) / wall time of the whole run
  per_stream_tok_s = llama-server's own predicted_per_second per request (decode speed of one stream)
Greedy decoding, fixed seed, Vietnamese prompts. It measures only; it asserts nothing about what the numbers should be.
"""
import argparse
import json
import statistics as st
import threading
import time
import urllib.request

PROMPTS = [
    "Hãy giải thích ngắn gọn bằng tiếng Việt vì sao bầu trời có màu xanh.",
    "Viết một đoạn văn ngắn về lợi ích của việc đọc sách mỗi ngày.",
    "Tóm tắt sự khác nhau giữa TCP và UDP bằng tiếng Việt.",
    "Hãy kể ngắn gọn về lịch sử của thành phố Hà Nội.",
    "Giải thích khái niệm bộ nhớ đệm (cache) trong máy tính cho người mới học.",
    "Nêu ba mẹo giúp ngủ ngon hơn, mỗi mẹo một câu.",
]


def one(url, i, max_tokens, out, lock):
    body = {"model": "x", "messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}],
            "max_tokens": max_tokens, "temperature": 0, "seed": 1}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=600).read())
        tm = r.get("timings", {})
        rec = {"ok": True, "completion_tokens": r["usage"]["completion_tokens"], "wall_s": time.time() - t0,
               "predicted_per_second": tm.get("predicted_per_second"), "prompt_ms": tm.get("prompt_ms")}
    except Exception as e:  # recorded, never hidden
        rec = {"ok": False, "error": str(e), "wall_s": time.time() - t0}
    with lock:
        out.append(rec)


def run_url(url, conc, rounds, max_tokens, results, walls):
    t_start = time.time()
    recs, lock = [], threading.Lock()
    for _ in range(rounds):
        ts = [threading.Thread(target=one, args=(url, i, max_tokens, recs, lock)) for i in range(conc)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    results[url] = recs
    walls[url] = time.time() - t_start


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", required=True)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--warmup", type=int, default=1, help="warm-up requests per URL (not counted)")
    p.add_argument("--label", default="")
    a = p.parse_args()
    urls = a.urls.split(",")
    for u in urls:  # warm-up: first request pays prompt-cache / kernel setup
        for _ in range(a.warmup):
            one(u, 0, 16, [], threading.Lock())
    results, walls = {}, {}
    t0 = time.time()
    ts = [threading.Thread(target=run_url, args=(u, a.concurrency, a.rounds, a.max_tokens, results, walls)) for u in urls]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = time.time() - t0
    out = {"label": a.label, "concurrency_per_url": a.concurrency, "rounds": a.rounds, "max_tokens": a.max_tokens,
           "wall_s": round(wall, 2), "urls": {}}
    total = 0
    for u, recs in results.items():
        ok = [r for r in recs if r["ok"]]
        toks = sum(r["completion_tokens"] for r in ok)
        total += toks
        ps = [r["predicted_per_second"] for r in ok if r.get("predicted_per_second")]
        out["urls"][u] = {"requests": len(recs), "failed": len(recs) - len(ok), "completion_tokens": toks,
                          "own_wall_s": round(walls[u], 2), "own_aggregate_tok_s": round(toks / walls[u], 1),
                          "per_stream_tok_s_median": round(st.median(ps), 1) if ps else None,
                          "per_stream_tok_s_min_max": [round(min(ps), 1), round(max(ps), 1)] if ps else None,
                          "errors": [r["error"] for r in recs if not r["ok"]][:3]}
    out["aggregate_tok_s"] = round(total / wall, 1)
    out["note"] = "aggregate = tokens of all requests / wall time of the whole concurrent run (all URLs together)"
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
