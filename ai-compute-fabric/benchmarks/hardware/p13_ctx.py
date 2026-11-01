import subprocess, sys, time, statistics as st
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import p11_2gpu_runner as P, p12_s0_repro as S0, p12_s1_single as S1
OUT = P.REPORTS_DIR / "p13_ctx.jsonl"
def sg(extra):
    c = S1.cmd_sglang(); i = c.index("--disable-prefill-cuda-graph"); c[i:i+2] = ["--cuda-graph-max-bs-decode", "16"]; return c + extra
VAR = {"sg_kv8": lambda: sg([]), "sg_kv2": lambda: sg(["--triton-attention-num-kv-splits", "2"]),
       "sg_kv16": lambda: sg(["--triton-attention-num-kv-splits", "16"]), "sg_kv32": lambda: sg(["--triton-attention-num-kv-splits", "32"]),
       "vllm": lambda: (lambda c: (c.remove("--enforce-eager"), c)[1])(S1.cmd_vllm())}
def wall(u, pt, mt):
    xs = [P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="c", concurrency=16, prompt_tokens=pt, max_tokens=mt)["wall_s"] for _ in range(3)]
    return st.median(xs)
def run(name):
    rec = {"phase": "P13ctx", "variant": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        P.stop_all_engines(); cmd = VAR[name](); rec["cmd"] = " ".join(cmd)
        with open(S0.P12_LOGS / f"p13ctx_{name}.log", "w") as lf:
            pr = subprocess.Popen(cmd, env=S1.base_env(), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=300, procs=[pr])
            u = "http://127.0.0.1:18090"
            P.run_weighted_multiuser_batch(base_urls=[u], weights=[1.0], model="aicf", cfg_id="warm", concurrency=16, prompt_tokens=512, max_tokens=64)
            rec["step_ms"] = {}
            for pt in (64, 512, 2048):
                w8, w256 = wall(u, pt, 8), wall(u, pt, 256)
                rec["step_ms"][pt] = {"prefill_s": round(w8, 3), "wall256_s": round(w256, 3), "step_ms": round((w256 - w8) / 248 * 1000, 2)}
        rec["status"] = "OK"; print(name, {k: v["step_ms"] for k, v in rec["step_ms"].items()}, flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print(name, "ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
for n in VAR: run(n)
print("ALLDONE", flush=True)
