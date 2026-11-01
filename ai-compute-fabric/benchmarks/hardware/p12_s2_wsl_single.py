#!/usr/bin/env python3
"""P12 / S2b - single RTX 5070 under WSL2 (no distribution): SGLang (triton, no cuda graph) vs vLLM (--enforce-eager), ABAB.
Compares with the 3060 native result to see whether the WSL2 side alone is slower / has a long launch tail."""
import subprocess, sys, time, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0
OUT = P.REPORTS_DIR / "p12_s2_wsl_single.jsonl"
COMMON = "export TMPDIR=/tmp CUDA_HOME=/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13 VLLM_USE_FLASHINFER_SAMPLER=0; export PATH=/opt/venv-sglang/bin:$CUDA_HOME/bin:/usr/local/bin:$PATH\n"
CMD = {
 "sglang": "exec /opt/venv-sglang/bin/python -m sglang.launch_server --model-path /opt/hf-models/Qwen3-8B-AWQ --served-model-name aicf --host 0.0.0.0 --port 18090 --context-length 16384 --max-running-requests 16 --mem-fraction-static 0.82 --attention-backend triton --sampling-backend pytorch --disable-prefill-cuda-graph --disable-decode-cuda-graph",
 "vllm": "exec /opt/venv-vllm/bin/python -m vllm.entrypoints.cli.main serve /opt/hf-models/Qwen3-8B-AWQ --served-model-name aicf --host 0.0.0.0 --port 18090 --max-model-len 16384 --max-num-seqs 16 --gpu-memory-utilization 0.82 --enforce-eager",
}
def run(engine, r):
    rec = {"phase": "S2b", "engine": engine, "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    p = None
    try:
        P.stop_all_engines()
        P.setup_wsl_flat_network()
        lf = open(S0.P12_LOGS / f"p12_wslsingle_{engine}_r{r}.log", "w")
        p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", P.SSH_HOST, f"{P.WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
                             stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        p.stdin.write(COMMON + CMD[engine] + "\n"); p.stdin.close()
        url = f"http://{P.WIN_IP}:18090"
        rec["startup_s"] = P.wait_http_ready(url, timeout_s=300, procs=[p])
        dec = p10_client.run_decode_series(base_url=url, model="aicf", cfg_id=f"p12wsl_{engine}_r{r}", reps=7, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = dec["decode_tok_s"]; rec["status"] = "OK"
        print(f"[{engine} r{r}] {rec['decode_tok_s']}", flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print("ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--rounds", type=int, default=3); a = ap.parse_args()
    for r in range(1, a.rounds + 1):
        for e in (["sglang", "vllm"] if r % 2 else ["vllm", "sglang"]): run(e, r)
