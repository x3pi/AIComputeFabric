import subprocess, sys, time, statistics as st
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p10_client, p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_vllm_c.jsonl"
rec = {"phase": "P13vllm", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
try:
    P.stop_all_engines(); c = S1.cmd_vllm(); c.remove("--enforce-eager"); rec["cmd"] = " ".join(c)
    with open(S0.P12_LOGS / "p13vllm_c.log", "w") as lf:
        pr = subprocess.Popen(c, env=S1.base_env(), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=300, procs=[pr]); u = "http://127.0.0.1:18090"
        rec["decode_c1"] = p10_client.run_decode_series(base_url=u, model="aicf", cfg_id="p13vllm", reps=5, burn_in=1, max_tokens=256, seed=142)["decode_tok_s"]
        P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
        for conc in (4, 8, 16):
            ms = [P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id=f"v_{conc}_{k}", concurrency=conc, prompt_tokens=512, max_tokens=128) for k in range(4)]
            rec[f"c{conc}"] = {"tok_s_med": st.median(m["aggregate_wall_tok_s"] for m in ms), "ttft_p50": st.median(m["ttft_p50_ms"] for m in ms), "errors": sum(m["errors"] for m in ms)}
    rec["status"] = "OK"; print("vllm", rec["decode_c1"]["median"], {k: rec[k]["tok_s_med"] for k in ("c4", "c8", "c16")}, flush=True)
except Exception as e:
    rec["status"] = "FAILED"; rec["error"] = repr(e); print("vllm ERR", e, flush=True)
finally:
    P.append_jsonl(OUT, rec); P.stop_all_engines()
print("ALLDONE", flush=True)
