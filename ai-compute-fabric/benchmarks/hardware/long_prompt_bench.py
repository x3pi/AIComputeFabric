#!/usr/bin/env python3
"""Long-prompt probe for one running llama-server: for each target prompt length it builds a distinct prompt (so the prompt
cache cannot help), measures prefill speed (prompt_per_second) and decode speed (predicted_per_second) from the server's own
timings, and the wall time. Token counts come from the server's /tokenize, not from a guess. Measures only."""
import argparse, json, time, urllib.request


def post(url, body, timeout=1800):
    r = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def make_text(base, target, salt):
    def lines(n):
        return "\n".join(f"Record {salt}-{i:06d}: sensor {i*7919%1000:03d} reads {(i*31)%97}.{(i*17)%10} units, state {'ok' if i%5 else 'check'}, code {i*104729%99991:05d}." for i in range(n))
    lo, hi = 1, max(8, target // 12)
    while len(post(base + "/tokenize", {"content": lines(hi)})["tokens"]) < target:
        hi *= 2
    while lo < hi:  # smallest n with >= target tokens
        mid = (lo + hi) // 2
        if len(post(base + "/tokenize", {"content": lines(mid)})["tokens"]) >= target:
            hi = mid
        else:
            lo = mid + 1
    return lines(lo)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True); p.add_argument("--lengths", default="1000,4000,8000,16000,30000")
    p.add_argument("--label", required=True); p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    for k, target in enumerate(int(x) for x in a.lengths.split(",")):
        text = make_text(a.url, target, f"{a.label}{k}")
        body = {"model": "x", "messages": [{"role": "user", "content": text + "\n\nHow many records above have state check? Answer briefly."}],
                "max_tokens": a.max_tokens, "temperature": 0, "seed": 1, "cache_prompt": False}
        t0 = time.time()
        rec = {"label": a.label, "target_tokens": target}
        try:
            r = post(a.url + "/v1/chat/completions", body)
            tm = r.get("timings", {})
            rec.update(ok=True, prompt_n=tm.get("prompt_n"), prefill_tok_s=round(tm.get("prompt_per_second", 0), 1),
                       decode_tok_s=round(tm.get("predicted_per_second", 0), 1), wall_s=round(time.time() - t0, 1))
        except Exception as e:
            rec.update(ok=False, error=str(e)[:200], wall_s=round(time.time() - t0, 1))
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        open(a.out, "a").write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)
        if not rec["ok"]:
            break


if __name__ == "__main__":
    main()
