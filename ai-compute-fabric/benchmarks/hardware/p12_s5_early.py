#!/usr/bin/env python3
"""P12 / S5 - prototype SGLANG_PP_EARLY_PROXY (worktree code on both nodes): flag off vs on (vs vLLM), PP=2 (13,23)."""
import argparse, inspect, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0
OUT = P.REPORTS_DIR / "p12_s5_early.jsonl"
SRC_LINUX = "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/sglang-v0519/python"
SRC_WSL = "/opt/sglang-src/python"
BASE = inspect.getsource(P.launch_sglang_pp2_cluster)
A = 'env0["NCCL_P2P_DISABLE"] = "1"\n'; B = "export NCCL_P2P_DISABLE=1\n"
assert BASE.count(A) == 1 and BASE.count(B) == 1

def make(flag):
    s = BASE.replace(A, A + f'    env0["PYTHONPATH"] = "{SRC_LINUX}"\n    env0["SGLANG_PP_EARLY_PROXY"] = "{flag}"\n', 1)
    s = s.replace(B, B + f"export PYTHONPATH={SRC_WSL}\nexport SGLANG_PP_EARLY_PROXY={flag}\n", 1)
    name = f"launch_sglang_pp2_early{flag}"
    s = s.replace("def launch_sglang_pp2_cluster(", f"def {name}(", 1)
    exec(compile(s, f"<p12_early{flag}>", "exec"), P.__dict__)
    return getattr(P, name)

def greedy(prompt, n=96):
    req = urllib.request.Request("http://127.0.0.1:18090/v1/chat/completions", data=json.dumps(
        {"model": "aicf", "messages": [{"role": "user", "content": prompt}], "max_tokens": n, "temperature": 0, "seed": 1}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]

PROMPTS = ["Write a Python function that returns the n-th Fibonacci number.", "Explain in two sentences what pipeline parallelism is.",
           "List five prime numbers greater than 100.", "Translate 'good morning' into French, Spanish and German."]

def run(name, fn, r, check):
    rec = {"phase": "S5", "variant": name, "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        fn_kwargs = dict(layer_partition="13,23")
        p0, p1, s, info = fn(**fn_kwargs)
        rec["startup_s"] = s
        if check:
            rec["greedy"] = [greedy(p) for p in PROMPTS]
        d = p10_client.run_decode_series(base_url="http://127.0.0.1:18090", model="aicf", cfg_id=f"p12s5_{name}_r{r}", reps=7, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = d["decode_tok_s"]
        if "vllm" not in name:
            rec["multiuser_c4"] = P.run_weighted_multiuser_batch(base_urls=["http://127.0.0.1:18090"], weights=[1.0], model="aicf", cfg_id=f"p12s5_{name}_r{r}_c4", concurrency=4, prompt_tokens=512, max_tokens=128)
        rec["status"] = "OK"; print(f"[{name} r{r}] {d['decode_tok_s']}", flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print("ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--rounds", type=int, default=1); ap.add_argument("--variants", default="off,on"); ap.add_argument("--start-round", type=int, default=1)
    a = ap.parse_args()
    vs = {"off": make(0), "on": make(1), "vllm": P.launch_vllm_pp2_cluster}
    names = a.variants.split(",")
    for r in range(a.start_round, a.start_round + a.rounds):
        k = r % len(names); order = names[k:] + names[:k]
        for n in order: run(n, vs[n], r, check=(n != "vllm"))
