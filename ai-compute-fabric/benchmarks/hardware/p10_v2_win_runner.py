#!/usr/bin/env python3
"""Pha V2.2, V2.3, V2.4 Runner trên pc-win-01 (RTX 5070 12GB — Windows 10 Native llama.cpp vs WSL2 vLLM & WSL2 llama.cpp).

Tuân thủ KE_HOACH_THU_NGHIEM_VLLM_WSL2.md:
- Dùng cùng 1 client streaming (`p10_client.py`) kết nối qua SSH local tunnel (`127.0.0.1:18091`) tới cả Windows native lẫn WSL2
- Xen kẽ ABAB giữa llama.cpp Windows Native (A) và vLLM WSL2 (B) trong cùng phiên
- Decode 1 người dùng: 8 lần lặp (4 ở A1/B1 + 4 ở A2/B2, bỏ 1 lần burn-in mỗi vòng)
- Prefill (2k, 8k, 24k): 4 lần lặp (2 ở A1/B1 + 2 ở A2/B2), chống prefix cache bằng nonce + --no-enable-prefix-caching
- Đa người dùng (C = 1, 4, 8, 16)
- Mutation check: chạy vLLM với --max-num-seqs 1 ở C=8
- V2.3: Chạy llama.cpp bản Linux/CUDA bên trong WSL2 (7 lần lặp decode + prefill 2k/8k) để tách riêng chi phí ảo hóa WSL2
- V2.4: Kiểm tra chất lượng code (15 bài HumanEval có unit test)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
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
REPORTS_DIR = REPO_ROOT / "ai-compute-fabric" / "benchmarks" / "reports"
LOGS_DIR = REPORTS_DIR / "logs"
SSH_HOST = "fios02@10.42.0.225"
WSL_EXE = r'"C:\Program Files\WSL\wsl.exe"'


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_win_ps(ps_script: str, timeout: int = 30) -> str:
    b64 = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
    out = subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"powershell -NoProfile -EncodedCommand {b64}"],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return out.strip()


def run_wsl_bash(bash_script: str, timeout: int = 60) -> str:
    out = subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
        input=bash_script,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return out.strip()


def get_win_gpu_and_mem() -> dict[str, Any]:
    out = run_wsl_bash(
        "/usr/lib/wsl/lib/nvidia-smi --query-gpu=memory.used,memory.free,memory.total,temperature.gpu "
        "--format=csv,noheader,nounits && grep MemAvailable /proc/meminfo"
    ).splitlines()
    smi_line = [ln for ln in out if "," in ln][-1]
    mem_line = [ln for ln in out if "MemAvailable" in ln][-1]
    parts = [p.strip() for p in smi_line.split(",")]
    mem_avail_kb = int(mem_line.split()[1])
    return {
        "vram_used_mib": int(parts[0]),
        "vram_free_mib": int(parts[1]),
        "vram_total_mib": int(parts[2]),
        "gpu_temp_c": int(parts[3]),
        "wsl_ram_avail_mib": round(mem_avail_kb / 1024.0, 1),
    }


def wait_http_ready(base_url: str, timeout_s: int = 240, proc: subprocess.Popen | None = None) -> float:
    t0 = time.monotonic()
    url = base_url.rstrip("/") + "/v1/models"
    for _ in range(timeout_s * 2):
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"Remote server exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return round(time.monotonic() - t0, 2)
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Server at {url} not ready within {timeout_s}s")


class WinWSLEngineManager:
    def __init__(self, max_ctx: int = 24576, local_port: int = 18091):
        self.max_ctx = max_ctx
        self.local_port = local_port
        self.keepalive_proc: subprocess.Popen | None = None
        self.tunnel_proc: subprocess.Popen | None = None
        self.server_proc: subprocess.Popen | None = None
        self.last_cmd_str: str = ""
        self.wsl_ip: str = "127.0.0.1"

    def start_keepalive(self) -> None:
        self.keepalive_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- sleep infinity"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(2)
        ips = run_wsl_bash("hostname -I").split()
        self.wsl_ip = ips[0] if ips else "127.0.0.1"
        print(f"[pc-win-01] WSL2 keepalive active, WSL2 IP = {self.wsl_ip}", flush=True)

    def stop_keepalive(self) -> None:
        self.stop_engines()
        if self.keepalive_proc is not None:
            try:
                os.killpg(os.getpgid(self.keepalive_proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.keepalive_proc = None

    def _start_tunnel(self, target_host: str, target_port: int = 18090) -> None:
        if self.tunnel_proc is not None:
            try:
                os.killpg(os.getpgid(self.tunnel_proc.pid), signal.SIGTERM)
                self.tunnel_proc.wait(timeout=5)
            except Exception:
                pass
            self.tunnel_proc = None
        subprocess.run(["pkill", "-f", f"127.0.0.1:{self.local_port}:"], check=False)
        self.tunnel_proc = subprocess.Popen(
            [
                "ssh", "-o", "BatchMode=yes", "-N",
                "-L", f"127.0.0.1:{self.local_port}:{target_host}:{target_port}",
                SSH_HOST,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(1.0)

    def stop_engines(self) -> None:
        if self.tunnel_proc is not None:
            try:
                os.killpg(os.getpgid(self.tunnel_proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.tunnel_proc = None
        if self.server_proc is not None:
            try:
                os.killpg(os.getpgid(self.server_proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.server_proc = None
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, "taskkill /F /IM llama-server.exe /T 2>nul"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            run_wsl_bash("pkill -f '[v]llm.entrypoints.openai.api_server' || true; pkill -f '[/]opt/llama/llama-server' || true")
        except Exception:
            pass
        time.sleep(2.5)

    def start_win_llama(self, np_slots: int, ctx_total: int, log_file: Path) -> float:
        self.stop_engines()
        win_cmd = (
            f"C:\\aicf\\llama\\llama-server.exe -m C:\\aicf\\models\\Qwen3-8B-Q4_K_M.gguf "
            f"--alias aicf --host 127.0.0.1 --port 18090 -ngl 99 -fa on -ctk f16 -ctv f16 "
            f"-c {ctx_total} -np {np_slots} -t 8"
        )
        self.last_cmd_str = win_cmd
        print(f"[pc-win-01][llama.cpp-win] CMD: {win_cmd}", flush=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_file, "a", encoding="utf-8")
        self.server_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, win_cmd],
            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self._start_tunnel("127.0.0.1", 18090)
        return wait_http_ready(f"http://127.0.0.1:{self.local_port}", timeout_s=60, proc=self.server_proc)

    def start_wsl_vllm(self, max_num_seqs: int, gpu_util: float, log_file: Path) -> float:
        self.stop_engines()
        ips = run_wsl_bash("hostname -I").split()
        self.wsl_ip = ips[0] if ips else "127.0.0.1"
        vllm_cmd = (
            f"export VLLM_USE_FLASHINFER_SAMPLER=0 TMPDIR=/tmp; "
            f"exec /opt/venv-vllm/bin/python -m vllm.entrypoints.openai.api_server "
            f"--model /opt/hf-models/Qwen3-8B-AWQ --served-model-name aicf "
            f"--host 0.0.0.0 --port 18090 --max-model-len {self.max_ctx} "
            f"--max-num-seqs {max_num_seqs} --gpu-memory-utilization {gpu_util} "
            f"--max-cudagraph-capture-size 16 --no-enable-prefix-caching"
        )
        self.last_cmd_str = vllm_cmd
        print(f"[pc-win-01][vLLM-wsl2] CMD: {vllm_cmd}", flush=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_file, "a", encoding="utf-8")
        self.server_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert self.server_proc.stdin is not None
        self.server_proc.stdin.write(vllm_cmd + "\n")
        self.server_proc.stdin.flush()
        self._start_tunnel(self.wsl_ip, 18090)
        return wait_http_ready(f"http://127.0.0.1:{self.local_port}", timeout_s=240, proc=self.server_proc)

    def start_wsl_llama(self, np_slots: int, ctx_total: int, log_file: Path) -> float:
        self.stop_engines()
        ips = run_wsl_bash("hostname -I").split()
        self.wsl_ip = ips[0] if ips else "127.0.0.1"
        wsl_llama_cmd = (
            f"export LD_LIBRARY_PATH=/opt/llama:/usr/lib/wsl/lib; "
            f"exec /opt/llama/llama-server -m /opt/models/Qwen3-8B-Q4_K_M.gguf "
            f"--alias aicf --host 0.0.0.0 --port 18090 -ngl 99 -fa on -ctk f16 -ctv f16 "
            f"-c {ctx_total} -np {np_slots} -t 8"
        )
        self.last_cmd_str = wsl_llama_cmd
        print(f"[pc-win-01][llama.cpp-wsl2] CMD: {wsl_llama_cmd}", flush=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_file, "a", encoding="utf-8")
        self.server_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert self.server_proc.stdin is not None
        self.server_proc.stdin.write(wsl_llama_cmd + "\n")
        self.server_proc.stdin.flush()
        self._start_tunnel(self.wsl_ip, 18090)
        return wait_http_ready(f"http://127.0.0.1:{self.local_port}", timeout_s=60, proc=self.server_proc)


def run_pcwin01_benchmark(out_jsonl: Path, max_ctx: int = 24576, gpu_util: float = 0.91) -> None:
    mgr = WinWSLEngineManager(max_ctx=max_ctx, local_port=18091)
    base_url = "http://127.0.0.1:18091"
    prefill_tiers = [
        ("2k", 2048),
        ("8k", 8192),
        ("24k", min(23000, max_ctx - 512)),
    ]

    done_rounds: set[str] = set()
    if out_jsonl.exists():
        round_counts: dict[str, int] = {}
        for line in out_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r_name = json.loads(line).get("round", "")
                round_counts[r_name] = round_counts.get(r_name, 0) + 1
        if round_counts.get("A1", 0) >= 9:
            done_rounds.add("A1")
        if round_counts.get("B1", 0) >= 9:
            done_rounds.add("B1")
        if round_counts.get("A2", 0) >= 8:
            done_rounds.add("A2")
        if round_counts.get("B2", 0) >= 8:
            done_rounds.add("B2")

    mgr.start_keepalive()
    try:
        for round_idx in [1, 2]:
            # --- ROUND A (llama.cpp Windows Native) ---
            if f"A{round_idx}" not in done_rounds:
                print(f"\n=== [pc-win-01] ROUND A{round_idx}: llama.cpp Windows Native ===", flush=True)
                log_a = LOGS_DIR / f"p10_v2_pcwin01_llama_r{round_idx}.log"
                startup_a = mgr.start_win_llama(np_slots=1, ctx_total=max_ctx, log_file=log_a)

                dec_a = p10_client.run_decode_series(
                    base_url=base_url, model="aicf", cfg_id=f"pcwin01_llama_A{round_idx}",
                    reps=4, burn_in=1, max_tokens=256, seed=42 + round_idx * 100
                )
                hw_dec_a = get_win_gpu_and_mem()
                print(f"[pc-win-01][A{round_idx}] decode_1user: {dec_a['decode_tok_s']} tok/s | hw={hw_dec_a}", flush=True)
                append_jsonl(out_jsonl, {
                    "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "llama.cpp (Windows native)",
                    "quant": "GGUF Q4_K_M", "round": f"A{round_idx}", "workload": "decode_1user",
                    "startup_s": startup_a, "cmd": mgr.last_cmd_str, "hw": hw_dec_a, "result": dec_a,
                })

                for label, plen in prefill_tiers:
                    pf_a = p10_client.run_prefill_series(
                        base_url=base_url, model="aicf", cfg_id=f"pcwin01_llama_A{round_idx}_{label}",
                        target_prompt_tokens=plen, reps=2, burn_in=(1 if round_idx == 1 else 0),
                        max_tokens=16, seed=42 + round_idx * 100
                    )
                    hw_pf_a = get_win_gpu_and_mem()
                    print(f"[pc-win-01][A{round_idx}] prefill_{label}: ttft={pf_a['ttft_ms']} ms | tok/s={pf_a['prefill_tok_s']}", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "llama.cpp (Windows native)",
                        "quant": "GGUF Q4_K_M", "round": f"A{round_idx}", "workload": f"prefill_{label}",
                        "cmd": mgr.last_cmd_str, "hw": hw_pf_a, "result": pf_a,
                    })

                if round_idx == 1:
                    qual_a = p10_v2_quality.run_quality_suite(base_url, "aicf", "pc-win-01:llama.cpp-win:Q4_K_M")
                    print(f"[pc-win-01][A1] quality_15: {qual_a['passed']}/{qual_a['total']}", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "llama.cpp (Windows native)",
                        "quant": "GGUF Q4_K_M", "round": "A1", "workload": "quality_15",
                        "result": qual_a,
                    })

                for c_val in [1, 4, 8, 16]:
                    mgr.start_win_llama(np_slots=c_val, ctx_total=max(4096, 2048 * c_val), log_file=log_a)
                    asyncio.run(p10_client.stream_one_chat_completion(
                        base_url=base_url, model="aicf",
                        prompt=p10_client.build_prompt_with_nonce(f"warm_winA{round_idx}_c{c_val}", 0, target_tokens=128),
                        cfg_id="warmup", rep_idx=0, max_tokens=16, seed=42
                    ))
                    mu_a = p10_client.run_multiuser_batch(
                        base_url=base_url, model="aicf", cfg_id=f"pcwin01_llama_A{round_idx}",
                        concurrency=c_val, prompt_tokens=512, max_tokens=128, seed=42 + round_idx * 100
                    )
                    hw_mu_a = get_win_gpu_and_mem()
                    print(f"[pc-win-01][A{round_idx}] multi_user_c{c_val}: agg={mu_a['aggregate_wall_tok_s']} tok/s | ttft_p95={mu_a['ttft_p95_ms']} ms", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "llama.cpp (Windows native)",
                        "quant": "GGUF Q4_K_M", "round": f"A{round_idx}", "workload": f"multi_user_c{c_val}",
                        "cmd": mgr.last_cmd_str, "hw": hw_mu_a, "result": mu_a,
                    })
                mgr.stop_engines()
            else:
                print(f"\n=== [pc-win-01] Skipping ROUND A{round_idx} (already recorded in {out_jsonl.name}) ===", flush=True)

            # --- ROUND B (vLLM inside WSL2) ---
            if f"B{round_idx}" not in done_rounds:
                print(f"\n=== [pc-win-01] ROUND B{round_idx}: vLLM inside WSL2 ===", flush=True)
                log_b = LOGS_DIR / f"p10_v2_pcwin01_vllm_r{round_idx}.log"
                startup_b = mgr.start_wsl_vllm(max_num_seqs=16, gpu_util=gpu_util, log_file=log_b)

                dec_b = p10_client.run_decode_series(
                    base_url=base_url, model="aicf", cfg_id=f"pcwin01_vllm_B{round_idx}",
                    reps=4, burn_in=1, max_tokens=256, seed=42 + round_idx * 100
                )
                hw_dec_b = get_win_gpu_and_mem()
                print(f"[pc-win-01][B{round_idx}] decode_1user: {dec_b['decode_tok_s']} tok/s | hw={hw_dec_b}", flush=True)
                append_jsonl(out_jsonl, {
                    "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "vLLM (WSL2)",
                    "quant": "AWQ 4-bit", "round": f"B{round_idx}", "workload": "decode_1user",
                    "startup_s": startup_b, "cmd": mgr.last_cmd_str, "hw": hw_dec_b, "result": dec_b,
                })

                for label, plen in prefill_tiers:
                    pf_b = p10_client.run_prefill_series(
                        base_url=base_url, model="aicf", cfg_id=f"pcwin01_vllm_B{round_idx}_{label}",
                        target_prompt_tokens=plen, reps=2, burn_in=(1 if round_idx == 1 else 0),
                        max_tokens=16, seed=42 + round_idx * 100
                    )
                    hw_pf_b = get_win_gpu_and_mem()
                    print(f"[pc-win-01][B{round_idx}] prefill_{label}: ttft={pf_b['ttft_ms']} ms | tok/s={pf_b['prefill_tok_s']}", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "vLLM (WSL2)",
                        "quant": "AWQ 4-bit", "round": f"B{round_idx}", "workload": f"prefill_{label}",
                        "cmd": mgr.last_cmd_str, "hw": hw_pf_b, "result": pf_b,
                    })

                if round_idx == 1:
                    qual_b = p10_v2_quality.run_quality_suite(base_url, "aicf", "pc-win-01:vLLM-wsl2:AWQ")
                    print(f"[pc-win-01][B1] quality_15: {qual_b['passed']}/{qual_b['total']}", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "vLLM (WSL2)",
                        "quant": "AWQ 4-bit", "round": "B1", "workload": "quality_15",
                        "result": qual_b,
                    })

                for c_val in [1, 4, 8, 16]:
                    mu_b = p10_client.run_multiuser_batch(
                        base_url=base_url, model="aicf", cfg_id=f"pcwin01_vllm_B{round_idx}",
                        concurrency=c_val, prompt_tokens=512, max_tokens=128, seed=42 + round_idx * 100
                    )
                    hw_mu_b = get_win_gpu_and_mem()
                    print(f"[pc-win-01][B{round_idx}] multi_user_c{c_val}: agg={mu_b['aggregate_wall_tok_s']} tok/s | ttft_p95={mu_b['ttft_p95_ms']} ms", flush=True)
                    append_jsonl(out_jsonl, {
                        "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "vLLM (WSL2)",
                        "quant": "AWQ 4-bit", "round": f"B{round_idx}", "workload": f"multi_user_c{c_val}",
                        "cmd": mgr.last_cmd_str, "hw": hw_mu_b, "result": mu_b,
                    })
                mgr.stop_engines()
            else:
                print(f"\n=== [pc-win-01] Skipping ROUND B{round_idx} (already recorded in {out_jsonl.name}) ===", flush=True)

        # --- MUTATION CHECK ON vLLM WSL2 (--max-num-seqs 1 at C=8) ---
        print("\n=== [pc-win-01] MUTATION CHECK: vLLM --max-num-seqs 1 at C=8 ===", flush=True)
        log_mut = LOGS_DIR / "p10_v2_pcwin01_vllm_mutation.log"
        mgr.start_wsl_vllm(max_num_seqs=1, gpu_util=gpu_util, log_file=log_mut)
        asyncio.run(p10_client.stream_one_chat_completion(
            base_url=base_url, model="aicf",
            prompt=p10_client.build_prompt_with_nonce("warm_win_mut", 0, target_tokens=128),
            cfg_id="warmup", rep_idx=0, max_tokens=16, seed=42
        ))
        mut_res = p10_client.run_multiuser_batch(
            base_url=base_url, model="aicf", cfg_id="pcwin01_vllm_MUT",
            concurrency=8, prompt_tokens=512, max_tokens=128, seed=999
        )
        print(f"[pc-win-01][MUTATION] c=8 with max_num_seqs=1: agg={mut_res['aggregate_wall_tok_s']} tok/s | ttft_p95={mut_res['ttft_p95_ms']} ms", flush=True)
        append_jsonl(out_jsonl, {
            "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "vLLM (WSL2)",
            "quant": "AWQ 4-bit", "round": "MUTATION", "workload": "mutation_max_num_seqs_1_c8",
            "cmd": mgr.last_cmd_str, "result": mut_res,
        })
        mgr.stop_engines()

        # --- V2.3: TÁCH RIÊNG CHI PHÍ ẢO HÓA WSL2 (llama.cpp Linux/CUDA chạy bên trong WSL2) ---
        print("\n=== [pc-win-01] V2.3: llama.cpp Linux/CUDA inside WSL2 ===", flush=True)
        log_v23 = LOGS_DIR / "p10_v23_pcwin01_wsl2_llama.log"
        startup_v23 = mgr.start_wsl_llama(np_slots=1, ctx_total=max_ctx, log_file=log_v23)
        dec_v23 = p10_client.run_decode_series(
            base_url=base_url, model="aicf", cfg_id="pcwin01_llama_wsl2_V23",
            reps=7, burn_in=1, max_tokens=256, seed=777
        )
        hw_v23 = get_win_gpu_and_mem()
        print(f"[pc-win-01][V2.3-WSL2-llama] decode_1user: {dec_v23['decode_tok_s']} tok/s | hw={hw_v23}", flush=True)
        append_jsonl(out_jsonl, {
            "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "llama.cpp (WSL2 Linux/CUDA)",
            "quant": "GGUF Q4_K_M", "round": "V2.3", "workload": "decode_1user",
            "startup_s": startup_v23, "cmd": mgr.last_cmd_str, "hw": hw_v23, "result": dec_v23,
        })
        for label, plen in [("2k", 2048), ("8k", 8192)]:
            pf_v23 = p10_client.run_prefill_series(
                base_url=base_url, model="aicf", cfg_id=f"pcwin01_llama_wsl2_V23_{label}",
                target_prompt_tokens=plen, reps=3, burn_in=1, max_tokens=16, seed=777
            )
            print(f"[pc-win-01][V2.3-WSL2-llama] prefill_{label}: ttft={pf_v23['ttft_ms']} ms | tok/s={pf_v23['prefill_tok_s']}", flush=True)
            append_jsonl(out_jsonl, {
                "host": "pc-win-01", "gpu": "RTX 5070 12GB", "engine": "llama.cpp (WSL2 Linux/CUDA)",
                "quant": "GGUF Q4_K_M", "round": "V2.3", "workload": f"prefill_{label}",
                "cmd": mgr.last_cmd_str, "hw": hw_v23, "result": pf_v23,
            })
    finally:
        mgr.stop_keepalive()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-ctx", type=int, default=24576)
    ap.add_argument("--gpu-util", type=float, default=0.91)
    ap.add_argument("--out-jsonl", default=str(REPORTS_DIR / "p10_v2_pcwin01.jsonl"))
    args = ap.parse_args()

    run_pcwin01_benchmark(Path(args.out_jsonl), max_ctx=args.max_ctx, gpu_util=args.gpu_util)
