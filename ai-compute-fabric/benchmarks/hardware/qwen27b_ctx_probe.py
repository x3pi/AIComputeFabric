#!/usr/bin/env python3
"""One config of the Qwen3.8-27B context/KV sweep, against the engine directly (127.0.0.1:18090). Measures only:
 - short decode/prefill (3 reps), VRAM of both cards
 - needle retrieval: secret code hidden at depth 10/50/90% of a filler of N tokens (thinking off), + server prefill/decode timings
 - optional HumanEval-164 (same prompt/budget/seed/evaluator as full164_distributed_benchmark, thinking off)
Usage: qwen27b_ctx_probe.py --label c65k_q8 --fills 4000,16000,32000,64000 [--he] --out DIR"""
import argparse, json, statistics, subprocess, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import full164_distributed_benchmark as F  # noqa: E402

URL = "http://127.0.0.1:18090"


def post(path, body, timeout=3600):
    r = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def vram():
    out = {}
    try:
        out["linux_MiB"] = int(subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True).split()[0])
    except Exception:
        pass
    try:
        out["win_MiB"] = int(subprocess.check_output(["ssh", "-o", "BatchMode=yes", "fios02@10.42.0.225", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"], text=True, timeout=30).split()[0])
    except Exception:
        pass
    return out


def lines(n, salt):
    return [f"Record {salt}-{i:06d}: sensor {i*7919%1000:03d} reads {(i*31)%97}.{(i*17)%10} units, state {'ok' if i%5 else 'check'}, code {i*104729%99991:05d}." for i in range(n)]


def ntok(text):
    return len(post("/tokenize", {"content": text})["tokens"])


def build(target, depth, k):
    """filler of ~target tokens with the needle at `depth`; returns (text, answer)"""
    per = ntok("\n".join(lines(200, "x"))) / 200.0
    n = max(10, int(target / per))
    vault = f"V{k}-{int(depth*100)}"
    ans = str((k * 7919 + int(depth * 1000) * 104729) % 900000 + 100000)
    for _ in range(3):  # correct the line count once or twice so we land on the target
        ls = lines(n, f"s{k}")
        ls.insert(int(n * depth), f"IMPORTANT NOTE: the access code for vault {vault} is {ans}. Remember it.")
        # a decoy with a different vault name and code
        ls.insert(int(n * ((depth + 0.37) % 1)), f"IMPORTANT NOTE: the access code for vault {vault}9 is {int(ans) + 12345}. Remember it.")
        text = "\n".join(ls)
        t = ntok(text)
        if abs(t - target) < 0.03 * target:
            break
        n = max(10, int(n * target / t))
    return text, ans, vault, t


def ask(text, question, max_tokens=64, think=False):
    body = {"model": "x", "messages": [{"role": "user", "content": text + "\n\n" + question}], "max_tokens": max_tokens,
            "temperature": 0, "seed": 1, "cache_prompt": False}
    if not think:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    t0 = time.time()
    r = post("/v1/chat/completions", body)
    tm = r.get("timings", {})
    return (r["choices"][0]["message"].get("content") or ""), tm, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--fills", default="4000,16000,32000"); ap.add_argument("--he", action="store_true")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    props = post("/props", {}) if False else json.loads(urllib.request.urlopen(URL + "/props").read())
    res = {"label": a.label, "n_ctx": props["default_generation_settings"]["n_ctx"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    print("n_ctx", res["n_ctx"], flush=True)
    # 1. short
    ask("warm up " * 20, "Say hi.", 8)
    reps = []
    for i in range(3):
        _, tm, _ = ask(("Explain distributed inference in detail. " * 12) + str(i), "Write a long explanation.", 128)
        reps.append(tm)
    res["short"] = {"decode_tok_s_median": round(statistics.median(t["predicted_per_second"] for t in reps), 2),
                    "prefill_tok_s_median": round(statistics.median(t["prompt_per_second"] for t in reps), 1)}
    res["vram_idle_after_short"] = vram()
    print("short", res["short"], res["vram_idle_after_short"], flush=True)
    # 2. needle at fills
    needle = []
    for fi, fill in enumerate(int(x) for x in a.fills.split(",")):
        for di, depth in enumerate((0.1, 0.5, 0.9)):
            rec = {"fill_target": fill, "depth": depth}
            try:
                text, ans, vault, t = build(fill, depth, fi * 10 + di)
                got, tm, wall = ask(text, f"What is the access code for vault {vault}? Answer with the number only.")
                rec.update(prompt_tokens=tm.get("prompt_n"), answer=ans, got=got.strip()[:80], correct=ans in got,
                           prefill_tok_s=round(tm.get("prompt_per_second", 0), 1), decode_tok_s=round(tm.get("predicted_per_second", 0), 1),
                           wall_s=round(wall, 1))
                if depth == 0.5:
                    rec["vram"] = vram()
            except Exception as e:
                rec.update(error=str(e)[:200])
            needle.append(rec); print("needle", json.dumps(rec), flush=True)
            (out / f"{a.label}.json").write_text(json.dumps({**res, "needle": needle}, indent=1))
            if rec.get("error"):
                break
    res["needle"] = needle
    # 3. HumanEval
    if a.he:
        probs = F.load_problems(); tasks = []; t0 = time.time()
        for i, pr in enumerate(probs):
            msgs = [{"role": "system", "content": F.SYS_PROMPT}, {"role": "user", "content": f"Complete this Python function:\n\n```python\n{pr['prompt']}\n```"}]
            r = F.chat("Qwen3.8-27B-UD-Q4_K_M.gguf", msgs, 1024, "direct")
            ok, err = F.run_sandbox(F.build_program(pr, r["content"]))
            tasks.append({"task_id": pr["task_id"], "passed": ok, "finish": r["finish"], "out_tok": r["output_tokens"], "decode_tok_s": r["decode_tok_s"]})
            if (i + 1) % 20 == 0:
                print(f"  HE [{i+1}/164] pass={sum(t['passed'] for t in tasks)} elapsed={time.time()-t0:.0f}s", flush=True)
        res["humaneval"] = {"pass": sum(t["passed"] for t in tasks), "total": len(tasks), "seconds": round(time.time() - t0),
                            "decode_tok_s_mean": round(statistics.mean(t["decode_tok_s"] for t in tasks), 2), "tasks": tasks}
    (out / f"{a.label}.json").write_text(json.dumps(res, indent=1))
    print("DONE", a.label, flush=True)


if __name__ == "__main__":
    main()
