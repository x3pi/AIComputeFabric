#!/usr/bin/env python3
"""P12 / Pha S1b — single-card control for the PP=2 gap.

Same model, same flags as the P11 PP=2 runs, but on ONE card (RTX 3060, no network, no pipeline):
  sglang : --attention-backend triton --sampling-backend pytorch, CUDA graphs disabled
  vllm   : --enforce-eager (CUDA graphs disabled), default attention backend
If SGLang is already slower than vLLM here, the PP=2 C=1 gap is (at least partly) an engine/kernel effect, not a network effect.
If they are equal here, the extra PP=2 gap comes from the distributed path.
ABAB, C=1 decode only. Results: reports/p12_s1_single.jsonl, logs: reports/logs/p12/.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0

OUT = P.REPORTS_DIR / "p12_s1_single.jsonl"
MODEL_DIR = P.WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["TMPDIR"] = "/tmp"
    env["XDG_CACHE_HOME"] = str(P.WORK_ROOT / "cache")
    env["SGLANG_CACHE_DIR"] = str(P.WORK_ROOT / "cache" / "sglang")
    env["FLASHINFER_WORKSPACE_BASE"] = str(P.WORK_ROOT / "cache" / "flashinfer")
    cuda_home = str(P.WORK_ROOT / "venv-sglang" / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13")
    env["CUDA_HOME"] = cuda_home
    env["PATH"] = f"{cuda_home}/bin:" + env.get("PATH", "")
    env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    return env


def cmd_sglang() -> list[str]:
    return [str(P.WORK_ROOT / "venv-sglang" / "bin" / "python"), "-m", "sglang.launch_server",
            "--model-path", str(MODEL_DIR), "--served-model-name", "aicf", "--host", "127.0.0.1", "--port", "18090",
            "--context-length", "16384", "--max-running-requests", "16", "--mem-fraction-static", "0.82",
            "--attention-backend", "triton", "--sampling-backend", "pytorch",
            "--disable-prefill-cuda-graph", "--disable-decode-cuda-graph"]


def cmd_vllm() -> list[str]:
    return [str(P.WORK_ROOT / "venv-vllm" / "bin" / "vllm"), "serve", str(MODEL_DIR), "--served-model-name", "aicf",
            "--host", "127.0.0.1", "--port", "18090", "--max-model-len", "16384", "--max-num-seqs", "16",
            "--gpu-memory-utilization", "0.82", "--enforce-eager"]


def run(engine: str, r: int) -> None:
    rec = {"phase": "S1b", "engine": engine, "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "mem_avail_mib_before": S0.mem_avail_mib()}
    proc = None
    try:
        P.stop_all_engines()
        cmd = cmd_sglang() if engine == "sglang" else cmd_vllm()
        rec["cmd"] = " ".join(cmd)
        with open(S0.P12_LOGS / f"p12_single_{engine}_r{r}.log", "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(cmd, env=base_env(), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            rec["startup_s"] = P.wait_http_ready("http://127.0.0.1:18090", timeout_s=240, procs=[proc])
            dec = p10_client.run_decode_series(base_url="http://127.0.0.1:18090", model="aicf",
                                               cfg_id=f"p12single_{engine}_r{r}", reps=7, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = dec["decode_tok_s"]
        rec["status"] = "OK"
        print(f"[{engine} r{r}] {rec['decode_tok_s']}", flush=True)
    except Exception as e:  # noqa: BLE001
        rec["status"] = "FAILED"
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"[{engine} r{r}] ERROR {e}", flush=True)
    finally:
        P.append_jsonl(OUT, rec)
        P.stop_all_engines()
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    a = ap.parse_args()
    for r in range(1, a.rounds + 1):
        order = ["sglang", "vllm"] if r % 2 else ["vllm", "sglang"]
        for e in order:
            run(e, r)
