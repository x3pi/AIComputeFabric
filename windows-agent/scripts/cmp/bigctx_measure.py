"""usage: bigctx_measure.py <fill_tokens> -- decode tok/s (prompt ngan) + prefill/decode khi prompt dai <fill_tokens>"""
import sys, json, time, requests
B = "http://10.42.0.1:18090"
def comp(prompt, n):
    r = requests.post(B + "/completion", json={"prompt": prompt, "n_predict": n, "temperature": 0, "cache_prompt": False}, timeout=1800)
    r.raise_for_status(); t = r.json()["timings"]
    return {k: round(t[k], 1) for k in ("prompt_n", "prompt_per_second", "predicted_n", "predicted_per_second")}
fill = int(sys.argv[1])
print("short", [comp("Write a long story about a robot.", 128) for _ in range(3)])
words = " ".join(f"item{i} alpha beta gamma delta {i*7%97}" for i in range(fill))  # ~ >= fill tokens
n = len(requests.post(B + "/tokenize", json={"content": words}).json()["tokens"])
cut = words[: int(len(words) * min(1.0, fill / n))]
print("long", comp(cut + "\nSummarize the text above in one sentence:", 64))
