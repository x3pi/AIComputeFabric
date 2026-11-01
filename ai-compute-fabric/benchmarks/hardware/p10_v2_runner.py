#!/usr/bin/env python3
"""Pha V2 Runner — Đo đối đầu vLLM vs llama.cpp trên từng card riêng (RTX 3060 Linux & RTX 5070 Windows/WSL2).

Tuân thủ KE_HOACH_THU_NGHIEM_VLLM_WSL2.md:
- Dùng cùng 1 client streaming (`p10_client.py`)
- Xen kẽ ABAB giữa llama.cpp (A) và vLLM (B) trong cùng phiên
- Decode 1 người dùng: >= 7 lần lặp (4 ở A1/B1 + 4 ở A2/B2 = 8 lần lặp, bỏ 1 lần burn-in mỗi vòng)
- Prefill (2k, 8k, 24k): >= 3 lần lặp (2 ở A1/B1 + 2 ở A2/B2 = 4 lần lặp), chống prefix cache bằng nonce + --no-enable-prefix-caching
- Đa người dùng (C = 1, 4, 8, 16): đo tổng tok/s hoàn tất, TTFT P50/P95, ITL P50/P95
- Mutation check: chạy vLLM với --max-num-seqs 1 ở C=8 để xác nhận thông lượng sụt về mức C=1
- Kiểm tra chất lượng code (15 bài HumanEval có unit test)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p10_v2_quality

REPO_ROOT = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric")
WORK_ROOT = REPO_ROOT / ".aicf-work"
REPORTS_DIR = REPO_ROOT / "ai-compute-fabric" / "benchmarks" / "reports"
LOGS_DIR = REPORTS_DIR / "logs"


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_linux_gpu_and_mem() -> dict[str, Any]:
    smi = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,memory.free,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
        text=True,
    ).strip()
    parts = [p.strip() for p in smi.split(",")]
    mem_avail_kb = 0
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
                break
    return {
        "vram_used_mib": int(parts[0]),
        "vram_free_mib": int(parts[1]),
        "vram_total_mib": int(parts[2]),
        "gpu_temp_c": int(parts[3]),
        "ram_avail_mib": round(mem_avail_kb / 1024.0, 1),
    }


def wait_http_ready(base_url: str, timeout_s: int = 240, proc: subprocess.Popen | None = None) -> float:
    t0 = time.monotonic()
    url = base_url.rstrip("/") + "/v1/models"
    for _ in range(timeout_s * 2):
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"Server exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return round(time.monotonic() - t0, 2)
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Server at {url} not ready within {timeout_s}s")


class LinuxEngineManager:
    def __init__(self, max_ctx: int = 24576):
        self.max_ctx = max_ctx
        self.proc: subprocess.Popen | None = None
        self.last_cmd_str: str = ""

    def stop(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            self.proc = None
        subprocess.run(["pkill", "-f", "[.]aicf-work/bin/llama-server"], check=False)
        subprocess.run(["pkill", "-f", "[v]llm.entrypoints.openai.api_server"], check=False)
        time.sleep(2)

    def start_llama(self, np_slots: int, ctx_total: int, log_file: Path) -> float:
        self.stop()
        cmd = [
            str(WORK_ROOT / "bin" / "llama-server"),
            "-m", str(WORK_ROOT / "Qwen3-8B-Q4_K_M.gguf"),
            "--alias", "aicf",
            "--host", "127.0.0.1",
            "--port", "18090",
            "-ngl", "99",
            "-fa", "on",
            "-ctk", "f16",
            "-ctv", "f16",
            "-c", str(ctx_total),
            "-np", str(np_slots),
            "-t", "8",
            "-Cr", "0-7",
            "--cpu-strict", "1",
        ]
        self.last_cmd_str = shlex.join(cmd)
        print(f"[pc-001][llama.cpp] CMD: {self.last_cmd_str}", flush=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_file, "a", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        return wait_http_ready("http://127.0.0.1:18090", timeout_s=60, proc=self.proc)

    def start_vllm(self, max_num_seqs: int, gpu_util: float, log_file: Path, extra_args: list[str] | None = None) -> float:
        self.stop()
        env = os.environ.copy()
        env["TMPDIR"] = "/tmp"
        env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
        env["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
        env["VLLM_CACHE_ROOT"] = str(WORK_ROOT / "cache" / "vllm")
        env["TORCHINDUCTOR_CACHE_DIR"] = str(WORK_ROOT / "cache" / "inductor")
        env["TRITON_CACHE_DIR"] = str(WORK_ROOT / "cache" / "triton")
        cmd = [
            str(WORK_ROOT / "venv-vllm" / "bin" / "python"),
            "-m", "vllm.entrypoints.openai.api_server",
            "--model", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
            "--served-model-name", "aicf",
            "--host", "127.0.0.1",
            "--port", "18090",
            "--max-model-len", str(self.max_ctx),
            "--max-num-seqs", str(max_num_seqs),
            "--gpu-memory-utilization", str(gpu_util),
            "--no-enable-prefix-caching",
        ]
        if extra_args:
            cmd.extend(extra_args)
        self.last_cmd_str = shlex.join(cmd)
        print(f"[pc-001][vLLM] CMD: {self.last_cmd_str}", flush=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_file, "a", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        return wait_http_ready("http://127.0.0.1:18090", timeout_s=240, proc=self.proc)


def run_pc001_benchmark(out_jsonl: Path, max_ctx: int = 24576, vllm_extra_args: list[str] | None = None, skip_a1: bool = False) -> None:
    mgr = LinuxEngineManager(max_ctx=max_ctx)
    base_url = "http://127.0.0.1:18090"
    prefill_tiers = [
        ("2k", 2048),
        ("8k", 8192),
        ("24k", min(23000, max_ctx - 512)),
    ]

    try:
        for round_idx in [1, 2]:
            if not (skip_a1 and round_idx == 1):
                # --- ROUND A (llama.cpp) ---
                print(f"\n=== [pc-001] ROUND A{round_idx}: llama.cpp (Qwen3-8B-Q4_K_M.gguf) ===", flush=True)
                log_a = LOGS_DIR / f"p10_v2_pc001_llama_r{round_idx}.log"
                startup_s = mgr.start_llama(np_slots=1, ctx_total=max_ctx, log_file=log_a)

                # 1-user decode (4 reps per round -> 8 total, plus 1 burn-in)
                dec = p10_client.run_decode_series(
                    base_url=base_url, model="aicf", cfg_id=f"pc001_llama_A{round_idx}",
                    reps=4, burn_in=1, max_tokens=256, seed=42 + round_idx * 100
                )
                hw_after_dec = get_linux_gpu_and_mem()
                print(f"[pc-001][A{round_idx}] decode_1user: {dec['decode_tok_s']} tok/s | hw={hw_after_dec}", flush=True)
                append_jsonl(out_jsonl, {
                    "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "llama.cpp",
                    "quant": "GGUF Q4_K_M", "round": f"A{round_idx}", "workload": "decode_1user",
                    "startup_s": startup_s, "cmd": mgr.last_cmd_str, "hw": hw_after_dec, "result": dec,
                })

                # Prefill (2 reps per round -> 4 total)
                for label, plen in prefill_tiers:
                    pf = p10_client.run_prefill_series(
                        base_url=base_url, model="aicf", cfg_id=f"pc001_llama_A{round_idx}_{label}",
                        target_prompt_tokens=plen, reps=2, burn_in=(1 if round_idx == 1 else 0),
                        max_tokens=16, seed=42 + round_idx * 100
                    )
                    hw_pf = get_linux_gpu_and_mem()
                    print(f"[pc-001][A{round_idx}] prefill_{label}: ttft={pf['ttft_ms']} ms | tok/s={pf['prefill_tok_s']}", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "llama.cpp",
                        "quant": "GGUF Q4_K_M", "round": f"A{round_idx}", "workload": f"prefill_{label}",
                        "cmd": mgr.last_cmd_str, "hw": hw_pf, "result": pf,
                    })

                # Code quality check on Round A1
                if round_idx == 1:
                    qual_a = p10_v2_quality.run_quality_suite(base_url, "aicf", "pc-001:llama.cpp:Q4_K_M")
                    print(f"[pc-001][A1] quality_15: {qual_a['passed']}/{qual_a['total']}", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "llama.cpp",
                        "quant": "GGUF Q4_K_M", "round": "A1", "workload": "quality_15",
                        "result": qual_a,
                    })

                # Multi-user C = 1, 4, 8, 16 with -np C -c (2048 * C)
                for c_val in [1, 4, 8, 16]:
                    mgr.start_llama(np_slots=c_val, ctx_total=max(4096, 2048 * c_val), log_file=log_a)
                    # 1 quick warmup request
                    asyncio.run(p10_client.stream_one_chat_completion(
                        base_url=base_url, model="aicf",
                        prompt=p10_client.build_prompt_with_nonce(f"warm_A{round_idx}_c{c_val}", 0, target_tokens=128),
                        cfg_id="warmup", rep_idx=0, max_tokens=16, seed=42
                    ))
                    mu = p10_client.run_multiuser_batch(
                        base_url=base_url, model="aicf", cfg_id=f"pc001_llama_A{round_idx}",
                        concurrency=c_val, prompt_tokens=512, max_tokens=128, seed=42 + round_idx * 100
                    )
                    hw_mu = get_linux_gpu_and_mem()
                    print(f"[pc-001][A{round_idx}] multi_user_c{c_val}: agg={mu['aggregate_wall_tok_s']} tok/s | ttft_p95={mu['ttft_p95_ms']} ms", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "llama.cpp",
                        "quant": "GGUF Q4_K_M", "round": f"A{round_idx}", "workload": f"multi_user_c{c_val}",
                        "cmd": mgr.last_cmd_str, "hw": hw_mu, "result": mu,
                    })
                mgr.stop()

            # --- ROUND B (vLLM) ---
            print(f"\n=== [pc-001] ROUND B{round_idx}: vLLM (Qwen3-8B-AWQ) ===", flush=True)
            log_b = LOGS_DIR / f"p10_v2_pc001_vllm_r{round_idx}.log"
            startup_b = mgr.start_vllm(max_num_seqs=16, gpu_util=0.92, log_file=log_b, extra_args=vllm_extra_args)

            dec_b = p10_client.run_decode_series(
                base_url=base_url, model="aicf", cfg_id=f"pc001_vllm_B{round_idx}",
                reps=4, burn_in=1, max_tokens=256, seed=42 + round_idx * 100
            )
            hw_dec_b = get_linux_gpu_and_mem()
            print(f"[pc-001][B{round_idx}] decode_1user: {dec_b['decode_tok_s']} tok/s | hw={hw_dec_b}", flush=True)
            append_jsonl(out_jsonl, {
                "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "vLLM",
                "quant": "AWQ 4-bit", "round": f"B{round_idx}", "workload": "decode_1user",
                "startup_s": startup_b, "cmd": mgr.last_cmd_str, "hw": hw_dec_b, "result": dec_b,
            })

            for label, plen in prefill_tiers:
                pf_b = p10_client.run_prefill_series(
                    base_url=base_url, model="aicf", cfg_id=f"pc001_vllm_B{round_idx}_{label}",
                    target_prompt_tokens=plen, reps=2, burn_in=(1 if round_idx == 1 else 0),
                    max_tokens=16, seed=42 + round_idx * 100
                )
                hw_pf_b = get_linux_gpu_and_mem()
                print(f"[pc-001][B{round_idx}] prefill_{label}: ttft={pf_b['ttft_ms']} ms | tok/s={pf_b['prefill_tok_s']}", flush=True)
                append_jsonl(out_jsonl, {
                    "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "vLLM",
                    "quant": "AWQ 4-bit", "round": f"B{round_idx}", "workload": f"prefill_{label}",
                    "cmd": mgr.last_cmd_str, "hw": hw_pf_b, "result": pf_b,
                })

            if round_idx == 1:
                qual_b = p10_v2_quality.run_quality_suite(base_url, "aicf", "pc-001:vLLM:AWQ")
                print(f"[pc-001][B1] quality_15: {qual_b['passed']}/{qual_b['total']}", flush=True)
                append_jsonl(out_jsonl, {
                    "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "vLLM",
                    "quant": "AWQ 4-bit", "round": "B1", "workload": "quality_15",
                    "result": qual_b,
                })

            for c_val in [1, 4, 8, 16]:
                mu_b = p10_client.run_multiuser_batch(
                    base_url=base_url, model="aicf", cfg_id=f"pc001_vllm_B{round_idx}",
                    concurrency=c_val, prompt_tokens=512, max_tokens=128, seed=42 + round_idx * 100
                )
                hw_mu_b = get_linux_gpu_and_mem()
                print(f"[pc-001][B{round_idx}] multi_user_c{c_val}: agg={mu_b['aggregate_wall_tok_s']} tok/s | ttft_p95={mu_b['ttft_p95_ms']} ms", flush=True)
                append_jsonl(out_jsonl, {
                    "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "vLLM",
                    "quant": "AWQ 4-bit", "round": f"B{round_idx}", "workload": f"multi_user_c{c_val}",
                    "cmd": mgr.last_cmd_str, "hw": hw_mu_b, "result": mu_b,
                })
            mgr.stop()

        # --- MUTATION CHECK ON vLLM (--max-num-seqs 1 at C=8) ---
        print("\n=== [pc-001] MUTATION CHECK: vLLM --max-num-seqs 1 at C=8 ===", flush=True)
        log_mut = LOGS_DIR / "p10_v2_pc001_vllm_mutation.log"
        mgr.start_vllm(max_num_seqs=1, gpu_util=0.92, log_file=log_mut, extra_args=vllm_extra_args)
        asyncio.run(p10_client.stream_one_chat_completion(
            base_url=base_url, model="aicf",
            prompt=p10_client.build_prompt_with_nonce("warm_mut", 0, target_tokens=128),
            cfg_id="warmup", rep_idx=0, max_tokens=16, seed=42
        ))
        mut_res = p10_client.run_multiuser_batch(
            base_url=base_url, model="aicf", cfg_id="pc001_vllm_MUT",
            concurrency=8, prompt_tokens=512, max_tokens=128, seed=999
        )
        print(f"[pc-001][MUTATION] c=8 with max_num_seqs=1: agg={mut_res['aggregate_wall_tok_s']} tok/s | ttft_p95={mut_res['ttft_p95_ms']} ms", flush=True)
        append_jsonl(out_jsonl, {
            "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "vLLM",
            "quant": "AWQ 4-bit", "round": "MUTATION", "workload": "mutation_max_num_seqs_1_c8",
            "cmd": mgr.last_cmd_str, "result": mut_res,
        })
    finally:
        mgr.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["pc-001"], default="pc-001")
    ap.add_argument("--max-ctx", type=int, default=24576)
    ap.add_argument("--enforce-eager", action="store_true")
    ap.add_argument("--skip-a1", action="store_true")
    ap.add_argument("--out-jsonl", default=str(REPORTS_DIR / "p10_v2_pc001.jsonl"))
    args = ap.parse_args()

    extra = ["--enforce-eager"] if args.enforce_eager else []
    run_pc001_benchmark(Path(args.out_jsonl), max_ctx=args.max_ctx, vllm_extra_args=extra, skip_a1=args.skip_a1)
