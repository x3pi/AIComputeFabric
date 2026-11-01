#!/usr/bin/env python3
"""P10 Mở rộng — Đo và so sánh thực nghiệm SGLang (0.5.19) & LMDeploy (0.18.0)
cho cả 2 trường hợp:
1. Chạy riêng từng card (pc-001 RTX 3060 12GB Linux & pc-win-01 RTX 5070 12GB WSL2)
   + Kiểm tra RadixAttention (Best-of-N shared prefix C=8) và CPU Offload (--cpu-offload-gb 4)
2. Gộp 2 card (pc-001 Linux 10.42.0.1 <-> pc-win-01 WSL2 10.42.0.225 qua portproxy)

Dùng cùng bộ đo `p10_client.py` và `p10_v2_quality.py` để so sánh trực tiếp với
`llama.cpp` và `vLLM`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shlex
import signal
import statistics
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
OUT_JSONL = REPORTS_DIR / "p10_sglang_lmdeploy.jsonl"

SSH_HOST = "fios02@10.42.0.225"
WSL_EXE = r'"C:\Program Files\WSL\wsl.exe"'
LINUX_IP = "10.42.0.1"
WIN_IP = "10.42.0.225"


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
            raise RuntimeError(f"Server exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return round(time.monotonic() - t0, 2)
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Server at {url} not ready within {timeout_s}s")


def stop_local_servers() -> None:
    subprocess.run(["fuser", "-k", "-9", "18090/tcp"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "-f", "[v]env-lmdeploy/bin"], check=False)
    subprocess.run(["pkill", "-9", "-f", "[v]env-sglang/bin"], check=False)
    subprocess.run(["pkill", "-9", "-f", "sglang::"], check=False)
    for _ in range(20):
        time.sleep(0.5)
        try:
            if get_linux_gpu_and_mem()["vram_used_mib"] < 200:
                break
        except Exception:
            pass
    time.sleep(1.0)


def start_local_lmdeploy(max_ctx: int, log_file: Path, backend: str = "turbomind", extra_args: list[str] | None = None) -> tuple[subprocess.Popen, str, float]:
    stop_local_servers()
    env = os.environ.copy()
    env["TMPDIR"] = str(WORK_ROOT / "tmp")
    env["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
    cmd = [
        str(WORK_ROOT / "venv-lmdeploy" / "bin" / "lmdeploy"),
        "serve", "api_server",
        str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
        "--backend", backend,
        "--model-format", "awq",
        "--model-name", "aicf",
        "--server-name", "127.0.0.1",
        "--server-port", "18090",
        "--session-len", str(max_ctx),
        "--max-batch-size", "16",
        "--cache-max-entry-count", "0.78",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd_str = shlex.join(cmd)
    print(f"[pc-001][LMDeploy-{backend}] CMD: {cmd_str}", flush=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    lf = open(log_file, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    startup_s = wait_http_ready("http://127.0.0.1:18090", timeout_s=180, proc=proc)
    return proc, cmd_str, startup_s


def start_local_sglang(max_ctx: int, log_file: Path, disable_radix: bool = True, extra_args: list[str] | None = None) -> tuple[subprocess.Popen, str, float]:
    stop_local_servers()
    env = os.environ.copy()
    env["TMPDIR"] = str(WORK_ROOT / "tmp")
    env["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
    env["SGLANG_CACHE_DIR"] = str(WORK_ROOT / "cache" / "sglang")
    env["FLASHINFER_WORKSPACE_BASE"] = str(WORK_ROOT / "cache" / "flashinfer")
    cuda_home = str(WORK_ROOT / "venv-sglang" / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13")
    env["CUDA_HOME"] = cuda_home
    env["PATH"] = f"{cuda_home}/bin:" + env.get("PATH", "")

    cmd = [
        str(WORK_ROOT / "venv-sglang" / "bin" / "python"),
        "-m", "sglang.launch_server",
        "--model-path", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
        "--served-model-name", "aicf",
        "--host", "127.0.0.1",
        "--port", "18090",
        "--context-length", str(max_ctx),
        "--max-running-requests", "16",
        "--mem-fraction-static", "0.85",
        "--attention-backend", "triton",
        "--sampling-backend", "pytorch",
        "--disable-prefill-cuda-graph",
        "--cuda-graph-max-bs-decode", "16",
    ]
    if disable_radix:
        cmd.append("--disable-radix-cache")
    if extra_args:
        cmd.extend(extra_args)
    cmd_str = shlex.join(cmd)
    print(f"[pc-001][SGLang] CMD: {cmd_str}", flush=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    lf = open(log_file, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    startup_s = wait_http_ready("http://127.0.0.1:18090", timeout_s=180, proc=proc)
    return proc, cmd_str, startup_s


def run_shared_prefix_batch(base_url: str, model: str, cfg_id: str, concurrency: int = 8, shared_tokens: int = 2048, max_tokens: int = 128) -> dict[str, Any]:
    """Đo kịch bản Best-of-N: C request dùng CHUNG 1 prefix dài 2048 token (chỉ khác 1 dòng hậu tố nhỏ ở cuối)."""
    shared_prefix = p10_client.build_prompt_with_nonce(f"SHARED_BESTOFN_{cfg_id}", 0, target_tokens=shared_tokens)

    async def _run() -> dict[str, Any]:
        tasks = []
        t0 = time.monotonic()
        for slot in range(concurrency):
            prompt = shared_prefix + f"\n# Candidate solution variant #{slot + 1}: Implement now.\n"
            tasks.append(
                p10_client.stream_one_chat_completion(
                    base_url=base_url,
                    model=model,
                    prompt=prompt,
                    cfg_id=f"{cfg_id}_bestofn_c{concurrency}_s{slot}",
                    rep_idx=slot,
                    max_tokens=max_tokens,
                    seed=100 + slot,
                )
            )
        results = await asyncio.gather(*tasks)
        wall_s = time.monotonic() - t0
        total_tok = sum(r.completion_tokens for r in results)
        ttfts = [r.ttft_ms for r in results]
        return {
            "concurrency": concurrency,
            "shared_prompt_tokens": shared_tokens,
            "wall_s": round(wall_s, 3),
            "total_completion_tokens": total_tok,
            "aggregate_wall_tok_s": round(total_tok / wall_s, 2),
            "ttft_p50_ms": round(statistics.median(ttfts), 2),
            "ttft_p95_ms": round(sorted(ttfts)[int(0.95 * (len(ttfts) - 1))], 2),
        }

    return asyncio.run(_run())


def run_single_card_suite_on_endpoint(
    base_url: str,
    host_label: str,
    gpu_label: str,
    engine_label: str,
    quant_label: str,
    startup_s: float,
    cmd_str: str,
    hw_fn,
) -> None:
    # 1. Decode 1-user (7 reps + 1 burn-in)
    dec = p10_client.run_decode_series(
        base_url=base_url, model="aicf", cfg_id=f"{host_label}_{engine_label}_dec",
        reps=7, burn_in=1, max_tokens=256, seed=142,
    )
    hw_dec = hw_fn()
    print(f"[{host_label}][{engine_label}] decode_1user: {dec['decode_tok_s']} tok/s | hw={hw_dec}", flush=True)
    append_jsonl(OUT_JSONL, {
        "host": host_label, "gpu": gpu_label, "engine": engine_label,
        "quant": quant_label, "workload": "decode_1user",
        "startup_s": startup_s, "cmd": cmd_str, "hw": hw_dec, "result": dec,
    })

    # 2. Prefill 2k & 8k (3 reps + 1 burn-in)
    for label, plen in [("2k", 2048), ("8k", 8192)]:
        pf = p10_client.run_prefill_series(
            base_url=base_url, model="aicf", cfg_id=f"{host_label}_{engine_label}_pf_{label}",
            target_prompt_tokens=plen, reps=3, burn_in=1, max_tokens=16, seed=142,
        )
        hw_pf = hw_fn()
        print(f"[{host_label}][{engine_label}] prefill_{label}: ttft={pf['ttft_ms']} ms | tok/s={pf['prefill_tok_s']}", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": host_label, "gpu": gpu_label, "engine": engine_label,
            "quant": quant_label, "workload": f"prefill_{label}",
            "cmd": cmd_str, "hw": hw_pf, "result": pf,
        })

    # 3. Code quality (15 problems with unit tests)
    qual = p10_v2_quality.run_quality_suite(base_url, "aicf", f"{host_label}:{engine_label}:{quant_label}")
    print(f"[{host_label}][{engine_label}] quality_15: {qual['passed']}/{qual['total']}", flush=True)
    append_jsonl(OUT_JSONL, {
        "host": host_label, "gpu": gpu_label, "engine": engine_label,
        "quant": quant_label, "workload": "quality_15",
        "result": qual,
    })

    # 4. Multi-user C = 1, 4, 8, 16
    for c_val in [1, 4, 8, 16]:
        mu = p10_client.run_multiuser_batch(
            base_url=base_url, model="aicf", cfg_id=f"{host_label}_{engine_label}_mu",
            concurrency=c_val, prompt_tokens=512, max_tokens=128, seed=142,
        )
        hw_mu = hw_fn()
        print(f"[{host_label}][{engine_label}] multi_user_c{c_val}: agg={mu['aggregate_wall_tok_s']} tok/s | ttft_p95={mu['ttft_p95_ms']} ms", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": host_label, "gpu": gpu_label, "engine": engine_label,
            "quant": quant_label, "workload": f"multi_user_c{c_val}",
            "cmd": cmd_str, "hw": hw_mu, "result": mu,
        })


def run_pc001_experiments(max_ctx: int = 16384) -> None:
    base_url = "http://127.0.0.1:18090"

    # --- 1. LMDeploy (TurboMind) on pc-001 (RTX 3060) ---
    print("\n=== [pc-001] 1. LMDeploy 0.18.0 (TurboMind) on RTX 3060 ===", flush=True)
    log_lmd = LOGS_DIR / "p10_pc001_lmdeploy_turbomind.log"
    try:
        proc, cmd_str, startup_s = start_local_lmdeploy(max_ctx=max_ctx, log_file=log_lmd, backend="turbomind")
        run_single_card_suite_on_endpoint(
            base_url=base_url,
            host_label="pc-001",
            gpu_label="RTX 3060 12GB",
            engine_label="LMDeploy (TurboMind)",
            quant_label="AWQ 4-bit",
            startup_s=startup_s,
            cmd_str=cmd_str,
            hw_fn=get_linux_gpu_and_mem,
        )
        # Also measure shared prefix C=8 on LMDeploy (with prefix caching enabled)
        stop_local_servers()
        proc, cmd_str, startup_s = start_local_lmdeploy(
            max_ctx=max_ctx, log_file=log_lmd, backend="turbomind", extra_args=["--enable-prefix-caching"]
        )
        # Warmup shared prefix once so it's in cache, then measure C=8
        sp_warm = run_shared_prefix_batch(base_url, "aicf", "pc001_lmd_prefix", concurrency=1, shared_tokens=2048, max_tokens=16)
        sp_res = run_shared_prefix_batch(base_url, "aicf", "pc001_lmd_prefix", concurrency=8, shared_tokens=2048, max_tokens=128)
        print(f"[pc-001][LMDeploy] bestofn_shared_prefix_c8: agg={sp_res['aggregate_wall_tok_s']} tok/s | ttft_p50={sp_res['ttft_p50_ms']} ms", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "LMDeploy (TurboMind + PrefixCache)",
            "quant": "AWQ 4-bit", "workload": "bestofn_shared_prefix_c8",
            "cmd": cmd_str, "result": sp_res,
        })
    finally:
        stop_local_servers()

    # --- 2. SGLang 0.5.19 on pc-001 (RTX 3060) ---
    print("\n=== [pc-001] 2. SGLang 0.5.19 on RTX 3060 ===", flush=True)
    log_sgl = LOGS_DIR / "p10_pc001_sglang.log"
    try:
        proc, cmd_str, startup_s = start_local_sglang(max_ctx=max_ctx, log_file=log_sgl, disable_radix=True)
        run_single_card_suite_on_endpoint(
            base_url=base_url,
            host_label="pc-001",
            gpu_label="RTX 3060 12GB",
            engine_label="SGLang",
            quant_label="AWQ 4-bit (awq_marlin)",
            startup_s=startup_s,
            cmd_str=cmd_str,
            hw_fn=get_linux_gpu_and_mem,
        )
        # Measure shared prefix C=8 WITHOUT RadixCache first
        sp_no_radix = run_shared_prefix_batch(base_url, "aicf", "pc001_sgl_noradix", concurrency=8, shared_tokens=2048, max_tokens=128)
        print(f"[pc-001][SGLang no-Radix] bestofn_shared_prefix_c8: agg={sp_no_radix['aggregate_wall_tok_s']} tok/s | ttft_p50={sp_no_radix['ttft_p50_ms']} ms", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "SGLang (no-RadixCache)",
            "quant": "AWQ 4-bit", "workload": "bestofn_shared_prefix_c8",
            "cmd": cmd_str, "result": sp_no_radix,
        })

        # Now restart SGLang WITH RadixAttention enabled (default) and test shared prefix C=8!
        stop_local_servers()
        proc, cmd_str, startup_s = start_local_sglang(max_ctx=max_ctx, log_file=log_sgl, disable_radix=False)
        _ = run_shared_prefix_batch(base_url, "aicf", "pc001_sgl_radix", concurrency=1, shared_tokens=2048, max_tokens=16)
        sp_radix = run_shared_prefix_batch(base_url, "aicf", "pc001_sgl_radix", concurrency=8, shared_tokens=2048, max_tokens=128)
        print(f"[pc-001][SGLang RadixAttention] bestofn_shared_prefix_c8: agg={sp_radix['aggregate_wall_tok_s']} tok/s | ttft_p50={sp_radix['ttft_p50_ms']} ms", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "SGLang (RadixAttention ON)",
            "quant": "AWQ 4-bit", "workload": "bestofn_shared_prefix_c8",
            "cmd": cmd_str, "result": sp_radix,
        })
    finally:
        stop_local_servers()

    # --- 3. SGLang CPU Offload (--cpu-offload-gb 4) on pc-001 ---
    print("\n=== [pc-001] 3. SGLang --cpu-offload-gb 4 on RTX 3060 ===", flush=True)
    log_sgl_off = LOGS_DIR / "p10_pc001_sglang_cpu_offload.log"
    try:
        proc, cmd_str, startup_s = start_local_sglang(
            max_ctx=8192, log_file=log_sgl_off, disable_radix=True, extra_args=["--cpu-offload-gb", "4"]
        )
        dec_off = p10_client.run_decode_series(
            base_url=base_url, model="aicf", cfg_id="pc001_sglang_offload4gb",
            reps=5, burn_in=1, max_tokens=64, seed=142,
        )
        hw_off = get_linux_gpu_and_mem()
        print(f"[pc-001][SGLang --cpu-offload-gb 4] decode_1user: {dec_off['decode_tok_s']} tok/s | hw={hw_off}", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "SGLang (--cpu-offload-gb 4)",
            "quant": "AWQ 4-bit", "workload": "cpu_offload_decode_1user",
            "startup_s": startup_s, "cmd": cmd_str, "hw": hw_off, "result": dec_off,
        })
    except Exception as e:
        print(f"[pc-001][SGLang --cpu-offload-gb 4] FAILED: {e}", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001", "gpu": "RTX 3060 12GB", "engine": "SGLang (--cpu-offload-gb 4)",
            "workload": "cpu_offload_decode_1user", "status": "FAILED", "error": str(e),
        })
    finally:
        stop_local_servers()


def stop_wsl_servers(tunnel_proc: subprocess.Popen | None = None, server_proc: subprocess.Popen | None = None) -> None:
    if tunnel_proc is not None:
        try:
            os.killpg(os.getpgid(tunnel_proc.pid), signal.SIGTERM)
        except Exception:
            pass
    if server_proc is not None:
        try:
            os.killpg(os.getpgid(server_proc.pid), signal.SIGTERM)
        except Exception:
            pass
    subprocess.run(["pkill", "-f", "127.0.0.1:18091:"], check=False)
    try:
        run_wsl_bash("fuser -k -9 18090/tcp 2>/dev/null || true; pkill -9 -f '[/]opt/venv-lmdeploy' || true; pkill -9 -f '[/]opt/venv-sglang' || true; pkill -9 -f 'sglang::' || true")
    except Exception:
        pass
    for _ in range(20):
        time.sleep(0.5)
        try:
            if get_win_gpu_and_mem()["vram_used_mib"] < 250:
                break
        except Exception:
            pass
    time.sleep(1.0)


def run_pcwin01_experiments(max_ctx: int = 16384) -> None:
    local_port = 18091
    base_url = f"http://127.0.0.1:{local_port}"

    # Start keepalive for WSL2
    keepalive = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- sleep infinity"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(2)
    ips = run_wsl_bash("hostname -I").split()
    wsl_ip = ips[0] if ips else "127.0.0.1"
    print(f"\n[pc-win-01] WSL2 active, IP = {wsl_ip}", flush=True)

    tunnel_proc: subprocess.Popen | None = None
    server_proc: subprocess.Popen | None = None

    def start_tunnel() -> subprocess.Popen:
        subprocess.run(["pkill", "-f", f"127.0.0.1:{local_port}:"], check=False)
        tp = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-N", "-L", f"127.0.0.1:{local_port}:{wsl_ip}:18090", SSH_HOST],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        time.sleep(1.0)
        return tp

    try:
        # --- 1. LMDeploy (pytorch backend) on pc-win-01 (RTX 5070 WSL2) ---
        # Already measured in task-1719 (decode 90.37 tok/s, quality 15/15, C=16 441.01 tok/s)
        for backend in []:
            print(f"\n=== [pc-win-01] LMDeploy 0.18.0 ({backend}) on RTX 5070 WSL2 ===", flush=True)
            stop_wsl_servers(tunnel_proc, server_proc)
            log_lmd_win = LOGS_DIR / f"p10_pcwin01_lmdeploy_{backend}.log"
            lmd_cmd = (
                f"export TMPDIR=/tmp LD_LIBRARY_PATH=/opt/venv-lmdeploy/lib/python3.12/site-packages/nvidia/cublas/lib:"
                f"/opt/venv-lmdeploy/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/usr/lib/wsl/lib:$LD_LIBRARY_PATH; "
                f"exec /opt/venv-lmdeploy/bin/lmdeploy serve api_server /opt/hf-models/Qwen3-8B-AWQ "
                f"--backend {backend} --model-format awq --model-name aicf "
                f"--server-name 0.0.0.0 --server-port 18090 --session-len {max_ctx} "
                f"--max-batch-size 16 --cache-max-entry-count 0.78"
            )
            lf = open(log_lmd_win, "a", encoding="utf-8")
            server_proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
                stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            assert server_proc.stdin is not None
            server_proc.stdin.write(lmd_cmd + "\n")
            server_proc.stdin.flush()
            tunnel_proc = start_tunnel()
            try:
                startup_s = wait_http_ready(base_url, timeout_s=180, proc=server_proc)
                run_single_card_suite_on_endpoint(
                    base_url=base_url,
                    host_label="pc-win-01",
                    gpu_label="RTX 5070 12GB (WSL2)",
                    engine_label=f"LMDeploy ({backend})",
                    quant_label="AWQ 4-bit",
                    startup_s=startup_s,
                    cmd_str=lmd_cmd,
                    hw_fn=get_win_gpu_and_mem,
                )
            except Exception as e:
                print(f"[pc-win-01][LMDeploy-{backend}] FAILED on RTX 5070 (sm_120): {e}", flush=True)
                append_jsonl(OUT_JSONL, {
                    "host": "pc-win-01", "gpu": "RTX 5070 12GB (WSL2)",
                    "engine": f"LMDeploy ({backend})", "quant": "AWQ 4-bit",
                    "workload": "sm120_compatibility", "status": "FAILED",
                    "cmd": lmd_cmd, "error": str(e),
                })

        # --- 2. SGLang 0.5.19 on pc-win-01 (RTX 5070 WSL2) ---
        print("\n=== [pc-win-01] SGLang 0.5.19 on RTX 5070 WSL2 ===", flush=True)
        stop_wsl_servers(tunnel_proc, server_proc)
        log_sgl_win = LOGS_DIR / "p10_pcwin01_sglang.log"
        sgl_cmd = (
            f"export TMPDIR=/tmp CUDA_HOME=/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13 "
            f"PATH=/opt/venv-sglang/bin:/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13/bin:/usr/local/bin:$PATH "
            f"LD_LIBRARY_PATH=/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13/lib:/usr/lib/wsl/lib:$LD_LIBRARY_PATH; "
            f"exec /opt/venv-sglang/bin/python -m sglang.launch_server "
            f"--model-path /opt/hf-models/Qwen3-8B-AWQ --served-model-name aicf "
            f"--host 0.0.0.0 --port 18090 --context-length {max_ctx} "
            f"--max-running-requests 16 --mem-fraction-static 0.85 "
            f"--attention-backend triton --sampling-backend pytorch "
            f"--disable-prefill-cuda-graph --cuda-graph-max-bs-decode 16 --disable-radix-cache"
        )
        lf = open(log_sgl_win, "a", encoding="utf-8")
        server_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert server_proc.stdin is not None
        server_proc.stdin.write(sgl_cmd + "\n")
        server_proc.stdin.flush()
        tunnel_proc = start_tunnel()
        try:
            startup_s = wait_http_ready(base_url, timeout_s=240, proc=server_proc)
            run_single_card_suite_on_endpoint(
                base_url=base_url,
                host_label="pc-win-01",
                gpu_label="RTX 5070 12GB (WSL2)",
                engine_label="SGLang (WSL2)",
                quant_label="AWQ 4-bit",
                startup_s=startup_s,
                cmd_str=sgl_cmd,
                hw_fn=get_win_gpu_and_mem,
            )
            # Also test Best-of-N shared prefix C=8 with RadixAttention ON on RTX 5070 WSL2
            stop_wsl_servers(tunnel_proc, server_proc)
            sgl_radix_cmd = sgl_cmd.replace(" --disable-radix-cache", "")
            server_proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
                stdin=subprocess.PIPE, stdout=lf, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            assert server_proc.stdin is not None
            server_proc.stdin.write(sgl_radix_cmd + "\n")
            server_proc.stdin.flush()
            tunnel_proc = start_tunnel()
            wait_http_ready(base_url, timeout_s=240, proc=server_proc)
            _ = run_shared_prefix_batch(base_url, "aicf", "pcwin01_sgl_radix", concurrency=1, shared_tokens=2048, max_tokens=16)
            sp_radix_win = run_shared_prefix_batch(base_url, "aicf", "pcwin01_sgl_radix", concurrency=8, shared_tokens=2048, max_tokens=128)
            print(f"[pc-win-01][SGLang RadixAttention] bestofn_shared_prefix_c8: agg={sp_radix_win['aggregate_wall_tok_s']} tok/s | ttft_p50={sp_radix_win['ttft_p50_ms']} ms", flush=True)
            append_jsonl(OUT_JSONL, {
                "host": "pc-win-01", "gpu": "RTX 5070 12GB (WSL2)", "engine": "SGLang (RadixAttention ON)",
                "quant": "AWQ 4-bit", "workload": "bestofn_shared_prefix_c8",
                "cmd": sgl_radix_cmd, "result": sp_radix_win,
            })
        except Exception as e:
            print(f"[pc-win-01][SGLang] FAILED on RTX 5070 (sm_120): {e}", flush=True)
            append_jsonl(OUT_JSONL, {
                "host": "pc-win-01", "gpu": "RTX 5070 12GB (WSL2)",
                "engine": "SGLang (WSL2)", "quant": "AWQ 4-bit",
                "workload": "sm120_compatibility", "status": "FAILED",
                "cmd": sgl_cmd, "error": str(e),
            })
    finally:
        stop_wsl_servers(tunnel_proc, server_proc)
        try:
            os.killpg(os.getpgid(keepalive.pid), signal.SIGTERM)
        except Exception:
            pass


def run_2card_merged_experiments() -> None:
    """Thử nghiệm gộp 2 card (pc-001 Linux 10.42.0.1 <-> pc-win-01 WSL2 10.42.0.225) cho SGLang và LMDeploy."""
    print("\n=== [2-CARD MERGED] Thử nghiệm gộp 2 card cho SGLang & LMDeploy ===", flush=True)
    stop_local_servers()
    keepalive = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- sleep infinity"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(2)
    wsl_ip = run_wsl_bash("hostname -I").split()[0]

    # Set up portproxy on Windows for 29500..29510
    ps_setup = [f"netsh interface portproxy reset | Out-Null"]
    for port in range(29500, 29511):
        ps_setup.append(
            f"netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport={port} connectaddress={wsl_ip} connectport={port} | Out-Null"
        )
    ps_setup.append(
        'New-NetFirewallRule -DisplayName "AICF_P10_Spike" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 29500-29510 -ErrorAction SilentlyContinue | Out-Null'
    )
    run_win_ps("; ".join(ps_setup))

    try:
        # --- 1. SGLang Multi-Node (--nnodes 2 --pp-size 2) ---
        print("\n--- [2-CARD] 1. SGLang --nnodes 2 --pp-size 2 (pc-001 + pc-win-01 WSL2) ---", flush=True)
        log_sgl_r0 = LOGS_DIR / "p10_2card_sglang_rank0.log"
        log_sgl_r1 = LOGS_DIR / "p10_2card_sglang_rank1.log"
        env = os.environ.copy()
        env["TMPDIR"] = "/tmp"
        env["GLOO_SOCKET_IFNAME"] = "enp133s0"
        env["NCCL_SOCKET_IFNAME"] = "enp133s0"
        env["NCCL_IB_DISABLE"] = "1"
        cuda_home = str(WORK_ROOT / "venv-sglang" / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13")
        env["CUDA_HOME"] = cuda_home
        env["PATH"] = f"{cuda_home}/bin:" + env.get("PATH", "")

        cmd_r0 = [
            str(WORK_ROOT / "venv-sglang" / "bin" / "python"),
            "-m", "sglang.launch_server",
            "--model-path", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
            "--served-model-name", "aicf",
            "--host", "0.0.0.0", "--port", "18090",
            "--context-length", "8192",
            "--nnodes", "2", "--node-rank", "0",
            "--dist-init-addr", f"{LINUX_IP}:29500",
            "--pp-size", "2",
            "--attention-backend", "triton", "--sampling-backend", "pytorch",
            "--disable-prefill-cuda-graph", "--disable-decode-cuda-graph",
        ]
        cmd_r1_str = (
            f"export TMPDIR=/tmp GLOO_SOCKET_IFNAME=eth0 NCCL_SOCKET_IFNAME=eth0 NCCL_IB_DISABLE=1 "
            f"CUDA_HOME=/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13 "
            f"PATH=/opt/venv-sglang/bin:/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13/bin:/usr/local/bin:$PATH; "
            f"exec /opt/venv-sglang/bin/python -m sglang.launch_server "
            f"--model-path /opt/hf-models/Qwen3-8B-AWQ --served-model-name aicf "
            f"--host 0.0.0.0 --port 18090 --context-length 8192 "
            f"--nnodes 2 --node-rank 1 --dist-init-addr {LINUX_IP}:29500 --pp-size 2 "
            f"--attention-backend triton --sampling-backend pytorch "
            f"--disable-prefill-cuda-graph --disable-decode-cuda-graph"
        )
        lf_r0 = open(log_sgl_r0, "w", encoding="utf-8")
        lf_r1 = open(log_sgl_r1, "w", encoding="utf-8")
        p_r0 = subprocess.Popen(cmd_r0, env=env, stdout=lf_r0, stderr=subprocess.STDOUT, start_new_session=True)
        p_r1 = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf_r1, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert p_r1.stdin is not None
        p_r1.stdin.write(cmd_r1_str + "\n")
        p_r1.stdin.flush()

        sgl_2card_status = "FAILED_OR_TIMEOUT"
        t0 = time.monotonic()
        for _ in range(45):
            if p_r0.poll() is not None or p_r1.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:18090/v1/models", timeout=1) as resp:
                    if resp.status == 200:
                        sgl_2card_status = "PASS"
                        break
            except Exception:
                pass
            time.sleep(1.0)
        elapsed_sgl = round(time.monotonic() - t0, 2)
        stop_local_servers()
        stop_wsl_servers(None, p_r1)
        r0_tail = log_sgl_r0.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        r1_tail = log_sgl_r1.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        print(f"[2-CARD][SGLang --nnodes 2 --pp-size 2] status={sgl_2card_status} ({elapsed_sgl}s)", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001 + pc-win-01 (WSL2 NAT)",
            "engine": "SGLang (--nnodes 2 --pp-size 2)",
            "workload": "2card_merged_multi_node",
            "status": sgl_2card_status,
            "elapsed_s": elapsed_sgl,
            "rank0_tail": r0_tail,
            "rank1_tail": r1_tail,
        })

        # --- 2. LMDeploy Multi-Node (--nnodes 2 --tp 2) ---
        print("\n--- [2-CARD] 2. LMDeploy --nnodes 2 --tp 2 (pc-001 + pc-win-01 WSL2) ---", flush=True)
        log_lmd_r0 = LOGS_DIR / "p10_2card_lmdeploy_rank0.log"
        log_lmd_r1 = LOGS_DIR / "p10_2card_lmdeploy_rank1.log"
        cmd_lmd_r0 = [
            str(WORK_ROOT / "venv-lmdeploy" / "bin" / "lmdeploy"),
            "serve", "api_server",
            str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
            "--backend", "pytorch",
            "--model-format", "awq",
            "--model-name", "aicf",
            "--server-name", "0.0.0.0", "--server-port", "18090",
            "--session-len", "8192",
            "--nnodes", "2", "--node-rank", "0",
            "--dist-init-addr", f"{LINUX_IP}:29500",
            "--tp", "2",
        ]
        cmd_lmd_r1_str = (
            f"export TMPDIR=/opt/tmp GLOO_SOCKET_IFNAME=eth0 NCCL_SOCKET_IFNAME=eth0 NCCL_IB_DISABLE=1; "
            f"exec /opt/venv-lmdeploy/bin/lmdeploy serve api_server /opt/hf-models/Qwen3-8B-AWQ "
            f"--backend pytorch --model-format awq --model-name aicf "
            f"--server-name 0.0.0.0 --server-port 18090 --session-len 8192 "
            f"--nnodes 2 --node-rank 1 --dist-init-addr {LINUX_IP}:29500 --tp 2"
        )
        lf_lmd_r0 = open(log_lmd_r0, "w", encoding="utf-8")
        lf_lmd_r1 = open(log_lmd_r1, "w", encoding="utf-8")
        p_lmd_r0 = subprocess.Popen(cmd_lmd_r0, env=env, stdout=lf_lmd_r0, stderr=subprocess.STDOUT, start_new_session=True)
        p_lmd_r1 = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf_lmd_r1, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert p_lmd_r1.stdin is not None
        p_lmd_r1.stdin.write(cmd_lmd_r1_str + "\n")
        p_lmd_r1.stdin.flush()

        lmd_2card_status = "FAILED_OR_TIMEOUT"
        t0 = time.monotonic()
        for _ in range(45):
            if p_lmd_r0.poll() is not None or p_lmd_r1.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:18090/v1/models", timeout=1) as resp:
                    if resp.status == 200:
                        lmd_2card_status = "PASS"
                        break
            except Exception:
                pass
            time.sleep(1.0)
        elapsed_lmd = round(time.monotonic() - t0, 2)
        stop_local_servers()
        stop_wsl_servers(None, p_lmd_r1)
        lmd_r0_tail = log_lmd_r0.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        lmd_r1_tail = log_lmd_r1.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        print(f"[2-CARD][LMDeploy --nnodes 2 --tp 2] status={lmd_2card_status} ({elapsed_lmd}s)", flush=True)
        append_jsonl(OUT_JSONL, {
            "host": "pc-001 + pc-win-01 (WSL2 NAT)",
            "engine": "LMDeploy (--nnodes 2 --tp 2)",
            "workload": "2card_merged_multi_node",
            "status": lmd_2card_status,
            "elapsed_s": elapsed_lmd,
            "rank0_tail": lmd_r0_tail,
            "rank1_tail": lmd_r1_tail,
        })
    finally:
        run_win_ps(
            'netsh interface portproxy reset | Out-Null; Remove-NetFirewallRule -DisplayName "AICF_P10_Spike" -ErrorAction SilentlyContinue | Out-Null'
        )
        try:
            os.killpg(os.getpgid(keepalive.pid), signal.SIGTERM)
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["pc001", "pcwin01", "2card", "all"], default="all")
    ap.add_argument("--max-ctx", type=int, default=16384)
    args = ap.parse_args()

    if args.phase in ("pc001", "all"):
        run_pc001_experiments(max_ctx=args.max_ctx)
    if args.phase in ("pcwin01", "all"):
        run_pcwin01_experiments(max_ctx=args.max_ctx)
    if args.phase in ("2card", "all"):
        run_2card_merged_experiments()

