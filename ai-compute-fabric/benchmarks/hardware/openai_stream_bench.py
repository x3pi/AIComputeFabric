#!/usr/bin/env python3
"""Same client for any OpenAI-compatible server (llama-server, Foundry Local web service): streaming chat completions.
Measures TTFT / prefill tok/s for ~700 and ~2000 token prompts, single-stream decode (tok/s, TPOT, p50/p95/p99 token gap),
concurrency 2/4 aggregate decode throughput, GPU VRAM + power (nvidia-smi, local). Stdlib only (runs on the Windows host).
Usage: openai_stream_bench.py --base http://127.0.0.1:18190 --model NAME --label X --out result.json [--max-tokens 192]"""
import argparse, json, statistics, subprocess, threading, time, urllib.request

_TEXT = ("Distributed inference splits a neural network across several accelerators. Each device keeps a slice of the "
         "layers and passes small activation tensors to the next one over the network. ")


def prompt(chars, salt):
    return (f"[{salt}] Read the text and then write a long detailed explanation of it.\n" + _TEXT * (chars // len(_TEXT) + 1))[:chars]


def stream(base, model, text, max_tokens):
    body = {"model": model, "messages": [{"role": "user", "content": text}], "max_tokens": max_tokens, "temperature": 0.0,
            "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter(); first = last = None; n = 0; gaps = []; usage = {}
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                ch = json.loads(d)
            except ValueError:
                continue
            if ch.get("usage"):
                usage = ch["usage"]
            for c in ch.get("choices") or []:
                dl = c.get("delta") or {}
                if dl.get("content") or dl.get("reasoning_content"):
                    now = time.perf_counter()
                    if last is not None:
                        gaps.append((now - last) * 1000)
                    first = first or now; last = now; n += 1
    end = time.perf_counter(); first = first or end; last = last or end
    out = int(usage.get("completion_tokens") or n)
    return {"ttft_ms": (first - t0) * 1000, "total_s": end - t0, "out_tok": out, "chunks": n,
            "prompt_tok": int(usage.get("prompt_tokens") or 0),
            "decode_tok_s": (n - 1) / (last - first) if last > first and n > 1 else 0.0, "gaps": gaps}


def pct(v, p):
    s = sorted(v); return round(s[min(len(s) - 1, int(len(s) * p))], 2) if s else None


class Gpu:
    def __init__(self):
        self.p = subprocess.Popen(["nvidia-smi", "--query-gpu=power.draw,memory.used", "--format=csv,noheader,nounits", "-lms", "500"],
                                  stdout=subprocess.PIPE, text=True)
        self.s = []; threading.Thread(target=self._r, daemon=True).start()

    def _r(self):
        for l in self.p.stdout:
            try:
                w, m = l.split(","); self.s.append((time.time(), float(w), float(m)))
            except ValueError:
                pass

    def win(self, t0, t1):
        v = [(w, m) for t, w, m in self.s if t0 <= t <= t1]
        return (round(statistics.mean(w for w, _ in v), 1), max(m for _, m in v)) if v else (0, 0)


def med(rs, k):
    return round(statistics.median(r[k] for r in rs), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True); ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--max-tokens", type=int, default=192); ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    g = Gpu(); res = {"label": a.label, "model": a.model}
    stream(a.base, a.model, prompt(400, 0), 16)                         # warm-up (first request compiles kernels / allocs)
    for name, chars in (("prefill_short", 2600), ("prefill_long", 7600)):
        rs = [stream(a.base, a.model, prompt(chars, 10 + i), 1) for i in range(a.reps)]
        res[name] = {"prompt_tok": rs[0]["prompt_tok"], "ttft_ms_median": med(rs, "ttft_ms"),
                     "prefill_tok_s": round(rs[0]["prompt_tok"] / (med(rs, "ttft_ms") / 1000), 1) if rs[0]["prompt_tok"] else None}
    t0 = time.time(); rs = [stream(a.base, a.model, prompt(600, 20 + i), a.max_tokens) for i in range(a.reps)]; t1 = time.time()
    gaps = [x for r in rs for x in r["gaps"]]
    pw, vram = g.win(t0, t1)
    res["decode_single"] = {"tok_s_median": med(rs, "decode_tok_s"), "out_tok": [r["out_tok"] for r in rs],
                            "ttft_ms_median": med(rs, "ttft_ms"), "token_gap_ms": {"p50": pct(gaps, .5), "p95": pct(gaps, .95), "p99": pct(gaps, .99), "max": round(max(gaps), 2) if gaps else None},
                            "gpu_power_W": pw, "joule_per_out_tok": round(pw / med(rs, "decode_tok_s"), 3) if med(rs, "decode_tok_s") else None}
    for c in (2, 4):
        out = [None] * c
        def w(i):
            out[i] = stream(a.base, a.model, prompt(600, 40 + i), a.max_tokens)
        for _rep in range(2):                                           # first pass warms the extra slots; the second is reported
            ts = time.perf_counter(); th = [threading.Thread(target=w, args=(i,)) for i in range(c)]
            [t.start() for t in th]; [t.join() for t in th]; el = time.perf_counter() - ts
        res[f"throughput_c{c}"] = {"agg_tok_s": round(sum(o["out_tok"] for o in out) / el, 2),
                                   "per_stream_decode_tok_s": round(statistics.mean(o["decode_tok_s"] for o in out), 2),
                                   "ttft_ms_max": round(max(o["ttft_ms"] for o in out), 1)}
    res["vram_peak_MiB"] = max(m for _, _, m in g.s) if g.s else None
    g.p.terminate()
    json.dump(res, open(a.out, "w"), indent=1); print(json.dumps(res))


if __name__ == "__main__":
    main()
