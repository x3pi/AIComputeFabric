import subprocess, sys, time, statistics as st
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_adm.jsonl"
def sg(extra, mrr="16"):
    c = S1.cmd_sglang(); i = c.index("--disable-prefill-cuda-graph"); c[i:i+2] = ["--cuda-graph-max-bs-decode", "16"]
    j = c.index("--max-running-requests"); c[j+1] = mrr; return c + extra
VAR = {"mrr16": lambda: sg([]), "mrr20": lambda: sg([], "20"), "mrr32": lambda: sg([], "32"),
       "cps4096": lambda: sg(["--chunked-prefill-size", "4096"]), "cps1024": lambda: sg(["--chunked-prefill-size", "1024"])}
def run(name):
    rec = {"phase": "P13adm", "variant": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        P.stop_all_engines(); cmd = VAR[name](); rec["cmd"] = " ".join(cmd)
        with open(S0.P12_LOGS / f"p13adm_{name}.log", "w") as lf:
            pr = subprocess.Popen(cmd, env=S1.base_env(), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=300, procs=[pr])
            u = "http://127.0.0.1:18090"
            P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
            ms = [P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id=f"adm_{name}_{k}", concurrency=16, prompt_tokens=512, max_tokens=128) for k in range(4)]
            rec["mixed"] = {"tok_s_med": st.median(m["aggregate_wall_tok_s"] for m in ms), "wall_s_med": st.median(m["wall_s"] for m in ms), "ttft_p50": st.median(m["ttft_p50_ms"] for m in ms), "errors": sum(m["errors"] for m in ms)}
        rec["status"] = "OK"; print(name, rec["mixed"], flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print(name, "ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
for n in VAR: run(n)
print("ALLDONE", flush=True)
