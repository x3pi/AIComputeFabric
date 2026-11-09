#!/usr/bin/env python3
"""Why is llama-server aggregate throughput at 2 concurrent streams below 1 stream? Sweep server flags on one GPU (Qwen3-4B Q4_K_M)
and measure decode tok/s at 1/2/3/4 concurrent streams with the same streaming client as openai_stream_bench.py.
Usage: llamacpp_c2_probe.py [variant ...]   (production must be paused first)"""
import json, os, signal, subprocess, sys, threading, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openai_stream_bench import prompt, stream  # noqa: E402

ROOT = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric")
LLAMA = ROOT / ".aicf-work" / "bin" / "llama-server"
GGUF = ROOT / ".aicf-work" / "Qwen3-4B-Q4_K_M.gguf"
OUT = ROOT / "ai-compute-fabric" / "benchmarks" / "reports" / "llamacpp_c2_probe.json"
PORT = 18190
BASE = ["-c", "16384", "-ngl", "99", "-fa", "on", "-ctk", "f16", "-ctv", "f16", "--no-warmup", "--fit", "off"]
# name -> (extra args after -np N, env)
VARIANTS = {
    "np4": (["-np", "4"], {}),                                   # production-like (the anomaly)
    "np2": (["-np", "2"], {}),
    "np1": (["-np", "1"], {}),
    "np4_nograph": (["-np", "4"], {"GGML_CUDA_DISABLE_GRAPHS": "1"}),
    "np4_kvsplit": (["-np", "4", "--no-kv-unified"], {}),
    "np4_nocb": (["-np", "4", "--no-cont-batching"], {}),
    "np4_fa_off": (["-np", "4", "-fa", "off"], {}),
    "np4_ub512": (["-np", "4", "-b", "512", "-ub", "512"], {}),
}


def wait(proc: subprocess.Popen) -> float:
    t = time.time()
    while time.time() - t < 300:
        if proc.poll() is not None:
            raise RuntimeError("server exited")
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
                return time.time() - t
        except Exception:
            time.sleep(1)
    raise TimeoutError


def conc(c: int, max_tokens: int = 192) -> dict:
    out = [None] * c

    def w(i):
        out[i] = stream(f"http://127.0.0.1:{PORT}", "x", prompt(600, 100 + i + c * 10), max_tokens)
    th = [threading.Thread(target=w, args=(i,)) for i in range(c)]
    t0 = time.perf_counter(); [t.start() for t in th]; [t.join() for t in th]; el = time.perf_counter() - t0
    return {"agg_tok_s": round(sum(o["out_tok"] for o in out) / el, 1),
            "per_stream_decode_tok_s": round(sum(o["decode_tok_s"] for o in out) / c, 1), "ttft_ms_max": round(max(o["ttft_ms"] for o in out))}


def run(name: str) -> dict:
    args, extra_env = VARIANTS[name]
    env = os.environ.copy(); env.update(extra_env); env["LD_LIBRARY_PATH"] = f"{LLAMA.parent}:{env.get('LD_LIBRARY_PATH', '')}"
    base = list(BASE)
    if "-fa" in args:                       # override the default flash-attention flag instead of duplicating it
        i = base.index("-fa"); del base[i:i + 2]
    for _ in range(90):
        if subprocess.run(["ss", "-ltn", f"sport = :{PORT}"], capture_output=True, text=True).stdout.count("LISTEN") == 0:
            break
        time.sleep(2)
    log = open(f"/mnt/Data/aicf-sglang-cmp/probe_{name}.log", "w")
    pr = subprocess.Popen([str(LLAMA), "-m", str(GGUF), "--host", "127.0.0.1", "--port", str(PORT)] + base + args, env=env,
                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait(pr)
        stream(f"http://127.0.0.1:{PORT}", "x", prompt(300, 1), 16)
        res = {"variant": name, "args": args, "env": extra_env}
        for c in (1, 2, 3, 4):
            conc(c)                              # warm-up pass for this concurrency
            res[f"c{c}"] = conc(c)
        print(json.dumps(res), flush=True)
        return res
    finally:
        os.killpg(pr.pid, signal.SIGTERM); time.sleep(5)


if __name__ == "__main__":
    names = sys.argv[1:] or list(VARIANTS)
    results = []
    for n in names:
        try:
            results.append(run(n))
        except Exception as e:  # keep going; record the failure
            print(json.dumps({"variant": n, "error": repr(e)}), flush=True); results.append({"variant": n, "error": repr(e)})
    OUT.write_text(json.dumps(results, indent=1))
