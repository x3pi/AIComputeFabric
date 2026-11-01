#!/usr/bin/env python3
"""Single RTX 3060, Qwen3-8B-AWQ: SGLang vs vLLM, CUDA graphs ON in both, C=1 decode + C=16 multiuser. ABAB."""
import argparse, subprocess, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p10_client, p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_single_c16.jsonl"
def cmd_sglang(graph):
    c = S1.cmd_sglang()
    if graph:
        i = c.index("--disable-prefill-cuda-graph"); c[i:i+2] = ["--cuda-graph-max-bs-decode", "16"]
    return c
def cmd_vllm(graph):
    c = S1.cmd_vllm()
    if graph: c.remove("--enforce-eager")
    return c
def run(engine, graph, r):
    tag = f"{engine}_{'graph' if graph else 'eager'}"
    rec = {"phase": "P13", "variant": tag, "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mem_avail_mib_before": S0.mem_avail_mib()}
    try:
        P.stop_all_engines()
        cmd = (cmd_sglang if engine == "sglang" else cmd_vllm)(graph); rec["cmd"] = " ".join(cmd)
        with open(S0.P12_LOGS / f"p13_{tag}_r{r}.log", "w") as lf:
            proc = subprocess.Popen(cmd, env=S1.base_env(), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=300, procs=[proc])
            u = "http://127.0.0.1:18090"
            rec["decode_c1"] = p10_client.run_decode_series(base_url=u, model="aicf", cfg_id=f"p13_{tag}_r{r}", reps=5, burn_in=1, max_tokens=256, seed=142)["decode_tok_s"]
            P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
            rec["c16"] = []
            for k in range(3):
                m = P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id=f"p13_{tag}_r{r}_c16_{k}", concurrency=16, prompt_tokens=512, max_tokens=128)
                rec["c16"].append({kk: m.get(kk) for kk in ("aggregate_wall_tok_s", "ttft_p50_ms", "ttft_p95_ms", "errors", "median_user_tok_s")})
        rec["status"] = "OK"; print(tag, r, rec["decode_c1"]["median"], [x["aggregate_wall_tok_s"] for x in rec["c16"]], flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print(tag, r, "ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--rounds", type=int, default=4); ap.add_argument("--eager", action="store_true"); a = ap.parse_args()
    for r in range(1, a.rounds + 1):
        order = ["sglang", "vllm"] if r % 2 else ["vllm", "sglang"]
        for e in order: run(e, not a.eager, r)
