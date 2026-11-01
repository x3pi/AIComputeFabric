import subprocess, sys, time, statistics as st
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_cause.jsonl"
def sg(extra):
    c = S1.cmd_sglang(); i = c.index("--disable-prefill-cuda-graph"); c[i:i+2] = ["--cuda-graph-max-bs-decode", "16"]; return c + extra
VAR = {
 "sg_base": lambda: sg([]),
 "sg_cps8192": lambda: sg(["--chunked-prefill-size", "8192", "--max-prefill-tokens", "16384"]),
 "sg_ncds4": lambda: sg(["--num-continuous-decode-steps", "4"]),
 "vllm": lambda: (lambda c: (c.remove("--enforce-eager"), c)[1])(S1.cmd_vllm()),
}
PROF = {"decode": (64, 256), "prefill": (512, 8), "mixed": (512, 128)}
def run(name, r):
    rec = {"phase": "P13c", "variant": name, "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        P.stop_all_engines(); cmd = VAR[name](); rec["cmd"] = " ".join(cmd)
        with open(S0.P12_LOGS / f"p13c_{name}_r{r}.log", "w") as lf:
            pr = subprocess.Popen(cmd, env=S1.base_env(), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=300, procs=[pr])
            u = "http://127.0.0.1:18090"
            P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
            for pn, (pt, mt) in PROF.items():
                xs = []
                for k in range(3):
                    m = P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id=f"p13c_{name}_{pn}_{r}_{k}", concurrency=16, prompt_tokens=pt, max_tokens=mt)
                    xs.append((m.get("aggregate_wall_tok_s"), m.get("wall_s"), m.get("ttft_p50_ms"), m.get("errors")))
                rec[pn] = {"tok_s_med": st.median(x[0] for x in xs), "wall_s_med": st.median(x[1] for x in xs), "ttft_p50_med": st.median(x[2] for x in xs), "errors": sum(x[3] or 0 for x in xs)}
        rec["status"] = "OK"; print(name, r, {k: (v["tok_s_med"], v["wall_s_med"]) for k, v in rec.items() if k in PROF}, flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print(name, r, "ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
names = list(VAR)
for r in (1, 2):
    o = names[r % 4:] + names[:r % 4]
    for n in o: run(n, r)
print("ALLDONE", flush=True)
