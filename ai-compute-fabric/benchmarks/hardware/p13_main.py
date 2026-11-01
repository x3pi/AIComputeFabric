import subprocess, sys, time, statistics as st
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p10_client, p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_main.jsonl"
W = "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work"
SRC = "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/sglang-main/python"
def base(extra):
    c = S1.cmd_sglang(); c[0] = f"{W}/venv-sglang-main/bin/python"
    i = c.index("--disable-prefill-cuda-graph"); c[i:i+2] = ["--cuda-graph-max-bs-decode", "16"]
    return c + extra
def default_backends():
    c = base([])
    for f in ("--attention-backend", "--sampling-backend"):
        i = c.index(f); del c[i:i+2]
    return c
VAR = {"main_triton": base, "main_default": default_backends}
def run(name):
    rec = {"phase": "P13main", "variant": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        P.stop_all_engines(); c = VAR[name]([]) if name == "main_triton" else VAR[name]()
        rec["cmd"] = " ".join(c); env = S1.base_env(); env["PYTHONPATH"] = SRC
        env["CUDA_HOME"] = f"{W}/venv-sglang-main/lib/python3.12/site-packages/nvidia/cu13"; env["PATH"] = env["CUDA_HOME"] + "/bin:" + env["PATH"]
        with open(S0.P12_LOGS / f"p13main_{name}.log", "w") as lf:
            pr = subprocess.Popen(c, env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=1200, procs=[pr])
            u = "http://127.0.0.1:18090"
            rec["decode_c1"] = p10_client.run_decode_series(base_url=u, model="aicf", cfg_id=f"p13main_{name}", reps=5, burn_in=1, max_tokens=256, seed=142)["decode_tok_s"]
            P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
            for conc in (4, 8, 16):
                ms = [P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id=f"m_{name}_{conc}_{k}", concurrency=conc, prompt_tokens=512, max_tokens=128) for k in range(4)]
                rec[f"c{conc}"] = {"tok_s_med": st.median(m["aggregate_wall_tok_s"] for m in ms), "ttft_p50": st.median(m["ttft_p50_ms"] for m in ms), "errors": sum(m["errors"] for m in ms)}
        rec["status"] = "OK"; print(name, rec["decode_c1"]["median"], {k: rec[k]["tok_s_med"] for k in ("c4", "c8", "c16")}, flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print(name, "ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
for n in VAR: run(n)
print("ALLDONE", flush=True)
