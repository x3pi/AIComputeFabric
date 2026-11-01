import subprocess, sys, time, statistics as st
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_fix.jsonl"
SRC = "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/sglang-v0519-fix/python"
def cmd():
    c = S1.cmd_sglang(); i = c.index("--disable-prefill-cuda-graph"); c[i:i+2] = ["--cuda-graph-max-bs-decode", "16"]; return c
def run(r):
    rec = {"phase": "P13v0523", "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        P.stop_all_engines(); c = cmd(); rec["cmd"] = " ".join(c); env = S1.base_env(); env["PYTHONPATH"] = SRC
        with open(S0.P12_LOGS / f"p13fix_r{r}.log", "w") as lf:
            pr = subprocess.Popen(c, env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=400, procs=[pr])
            u = "http://127.0.0.1:18090"
            P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
            ms = [P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id=f"v23_{r}_{k}", concurrency=16, prompt_tokens=512, max_tokens=128) for k in range(4)]
            rec["mixed"] = {"tok_s": [m["aggregate_wall_tok_s"] for m in ms], "wall_s": [m["wall_s"] for m in ms], "errors": sum(m["errors"] for m in ms)}
        rec["status"] = "OK"; print("fix", r, rec["mixed"], flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print("fix", r, "ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
for r in (1, 2): run(r)
print("ALLDONE", flush=True)
