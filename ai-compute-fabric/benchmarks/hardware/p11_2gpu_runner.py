#!/usr/bin/env python3
"""P11 Runner — Đo và đánh giá cải thiện Windows 10 WSL2 cho chế độ gộp 2 GPU
với SGLang (0.5.19) và vLLM (0.32.0):
1. Pha N1: Kiểm chứng mạng phẳng L3/NAT (nccl0) + PyTorch Distributed (Gloo & NCCL 2-node).
2. Pha N2: Gộp 2 card kiểu Chia tầng (Pipeline Parallel PP=2):
   - SGLang PP=2 (Chia đều 18,18 vs Chia lệch 13,23 theo sức mạnh RTX 3060 / RTX 5070 + Mutation 24,12).
   - vLLM PP=2 (Chia đều 18,18 vs Chia lệch 13,23).
3. Pha N3: Gộp 2 card kiểu Chia tải (Data Parallel / Weighted Router DP=2, tỷ lệ 35% : 65%)
   ở các mức đồng thời C = 1, 4, 8, 16, 32 và Best-of-N (C = 8, 16).
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
OUT_JSONL = REPORTS_DIR / "p11_2gpu_sglang_vllm.jsonl"

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


def run_win_ps(ps_script: str, timeout: int = 45) -> str:
    b64 = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
    out = subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"powershell -NoProfile -EncodedCommand {b64}"],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return out.strip()


def run_wsl_bash(bash_script: str, timeout: int = 90) -> str:
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


def get_both_gpus_hw() -> dict[str, Any]:
    return {
        "pc001_rtx3060": get_linux_gpu_and_mem(),
        "pcwin01_rtx5070": get_win_gpu_and_mem(),
    }


def stop_all_engines() -> None:
    """Dừng các tiến trình inference trên cả pc-001 và pc-win-01 để giải phóng VRAM."""
    for pat in ("[s]glang.launch_server", "[s]glang::", "[v]llm/bin/vllm", "[v]llm.entrypoints", "[V]LLM::", "[l]mdeploy serve", "[b]in/llama-server"):
        subprocess.run(["pkill", "-9", "-f", pat], check=False)
    try:
        run_wsl_bash(
            "pkill -9 -f '[s]glang.launch_server' || true; pkill -9 -f '[s]glang::' || true; "
            "pkill -9 -f '[v]llm/bin/vllm' || true; pkill -9 -f '[v]llm.entrypoints' || true; pkill -9 -f '[V]LLM::' || true; "
            "pkill -9 -f '[l]mdeploy serve' || true"
        )
    except Exception:
        pass
    for _ in range(15):
        time.sleep(0.5)
        try:
            if get_linux_gpu_and_mem()["vram_used_mib"] < 250:
                break
        except Exception:
            pass
    time.sleep(1.0)


WSL_NET_INIT_SNIPPET = """
ip link show nccl0 >/dev/null 2>&1 || (ip link add nccl0 type dummy && ip addr add 10.42.0.225/32 dev nccl0 && ip link set nccl0 up)
sysctl -w net.ipv4.ip_local_port_range="40000 40060" >/dev/null
sysctl -w net.ipv4.conf.all.route_localnet=1 >/dev/null
WSL_IP=$(ip -4 addr show eth0 | awk '/inet / {split($2,a,"/"); print a[1]}')
iptables -t nat -F
iptables -t nat -A PREROUTING -i eth0 -p tcp -d "$WSL_IP" -j DNAT --to-destination 10.42.0.225
iptables -t nat -A POSTROUTING -o eth0 -s 10.42.0.225 -j SNAT --to-source "$WSL_IP"
"""


def setup_wsl_flat_network() -> str:
    """Thiết lập giao diện nccl0 (10.42.0.225/32), dải cổng 40000-40060 và portproxy trên Windows 10."""
    wsl_setup = f"set -e\n{WSL_NET_INIT_SNIPPET}\necho \"$WSL_IP\"\n"
    wsl_ip = run_wsl_bash(wsl_setup).splitlines()[-1].strip()
    ps_script = f"""
netsh interface portproxy reset | Out-Null
$wslIp = '{wsl_ip}'
foreach ($p in @(5201, 18090) + (29500..29520) + (40000..40060)) {{
    netsh interface portproxy add v4tov4 listenaddress=10.42.0.225 listenport=$p connectaddress=$wslIp connectport=$p | Out-Null
}}
Remove-NetFirewallRule -DisplayName 'AICF-P11-WSL2' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'AICF-P11-WSL2' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5201,18090,29500-29520,40000-40060 | Out-Null
Write-Output "OK:$wslIp"
"""
    run_win_ps(ps_script, timeout=60)
    return wsl_ip


def wait_http_ready(base_url: str, timeout_s: int = 240, procs: list[subprocess.Popen] | None = None) -> float:
    t0 = time.monotonic()
    url = base_url.rstrip("/") + "/v1/models"
    for _ in range(timeout_s * 2):
        if procs:
            for p in procs:
                if p is not None and p.poll() is not None:
                    raise RuntimeError(f"Process exited early with code {p.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return round(time.monotonic() - t0, 2)
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Server at {url} not ready within {timeout_s}s")


def run_shared_prefix_batch(
    base_urls: list[str],
    weights: list[float],
    model: str,
    cfg_id: str,
    concurrency: int = 8,
    shared_tokens: int = 2048,
    max_tokens: int = 128,
) -> dict[str, Any]:
    """Đo Best-of-N với prefix chung 2048 token (hỗ trợ 1 endpoint hoặc Weighted Router nhiều endpoint)."""
    shared_prefix = p10_client.build_prompt_with_nonce(f"SHARED_BESTOFN_{cfg_id}", 0, target_tokens=shared_tokens)

    # Chia slot theo trọng số weights
    url_assignments: list[str] = []
    if len(base_urls) == 1:
        url_assignments = [base_urls[0]] * concurrency
    else:
        n0 = max(1, round(concurrency * weights[0]))
        for i in range(concurrency):
            url_assignments.append(base_urls[0] if i < n0 else base_urls[1])

    async def _run() -> dict[str, Any]:
        tasks = []
        t0 = time.monotonic()
        for slot in range(concurrency):
            prompt = shared_prefix + f"\n# Candidate solution variant #{slot + 1}: Implement now.\n"
            tasks.append(
                p10_client.stream_one_chat_completion(
                    base_url=url_assignments[slot],
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
            "ttft_max_ms": round(max(ttfts), 2),
            "errors": sum(1 for r in results if r.error),
        }

    return asyncio.run(_run())


def run_weighted_multiuser_batch(
    base_urls: list[str],
    weights: list[float],
    model: str,
    cfg_id: str,
    concurrency: int,
    prompt_tokens: int = 1024,
    max_tokens: int = 128,
) -> dict[str, Any]:
    """Đo đa người dùng C=1..32 phân tải theo trọng số giữa các endpoint."""
    url_assignments: list[str] = []
    if len(base_urls) == 1:
        url_assignments = [base_urls[0]] * concurrency
    else:
        n0 = round(concurrency * weights[0])
        if concurrency > 1:
            n0 = max(1, min(concurrency - 1, n0))
        for i in range(concurrency):
            url_assignments.append(base_urls[0] if i < n0 else base_urls[1])

    async def _run() -> dict[str, Any]:
        tasks = []
        t0 = time.monotonic()
        for slot in range(concurrency):
            prompt = p10_client.build_prompt_with_nonce(
                f"{cfg_id}_c{concurrency}_s{slot}", slot, target_tokens=prompt_tokens
            )
            tasks.append(
                p10_client.stream_one_chat_completion(
                    base_url=url_assignments[slot],
                    model=model,
                    prompt=prompt,
                    cfg_id=f"{cfg_id}_c{concurrency}_s{slot}",
                    rep_idx=slot,
                    max_tokens=max_tokens,
                    seed=42 + slot,
                )
            )
        results = await asyncio.gather(*tasks)
        wall_s = time.monotonic() - t0
        ok_res = [r for r in results if not r.error]
        total_tok = sum(r.completion_tokens for r in ok_res)
        ttfts = sorted(r.ttft_ms for r in ok_res) if ok_res else [0.0]
        user_tok_s = sorted(r.decode_tok_s for r in ok_res) if ok_res else [0.0]
        p95_idx = min(len(ttfts) - 1, int(len(ttfts) * 0.95))
        return {
            "concurrency": concurrency,
            "wall_s": round(wall_s, 3),
            "total_completion_tokens": total_tok,
            "aggregate_wall_tok_s": round(total_tok / wall_s, 2) if wall_s > 0 else 0.0,
            "min_user_tok_s": round(user_tok_s[0], 2),
            "median_user_tok_s": round(statistics.median(user_tok_s), 2),
            "ttft_p50_ms": round(statistics.median(ttfts), 2),
            "ttft_p95_ms": round(ttfts[p95_idx], 2),
            "errors": sum(1 for r in results if r.error),
            "node_split": {
                "pc001_reqs": sum(1 for u in url_assignments if u == base_urls[0]),
                "pcwin01_reqs": sum(1 for u in url_assignments if len(base_urls) > 1 and u == base_urls[1]),
            },
        }

    return asyncio.run(_run())


def evaluate_2gpu_endpoint(
    mode_label: str,
    engine_label: str,
    base_urls: list[str],
    weights: list[float],
    model: str,
    cfg_prefix: str,
    startup_s: float,
    cmd_info: dict[str, Any],
    concurrencies: tuple[int, ...] = (1, 4, 8, 16),
    run_quality: bool = True,
) -> dict[str, Any]:
    """Đo đầy đủ decode 1-user, prefill 2k/8k, multi-user, Best-of-N và quality 15 bài."""
    primary_url = base_urls[0]
    summary: dict[str, Any] = {}

    # 1. Decode 1-user (7 reps)
    dec = p10_client.run_decode_series(
        base_url=primary_url,
        model=model,
        cfg_id=f"{cfg_prefix}_decode",
        reps=7,
        burn_in=1,
        max_tokens=256,
        seed=142,
    )
    hw = get_both_gpus_hw()
    dec_summary = dec["decode_tok_s"]
    print(
        f"[{mode_label}][{engine_label}] Decode 1u: {dec_summary} tok/s | "
        f"VRAM 3060={hw['pc001_rtx3060']['vram_used_mib']}MiB, 5070={hw['pcwin01_rtx5070']['vram_used_mib']}MiB",
        flush=True,
    )
    append_jsonl(
        OUT_JSONL,
        {
            "mode": mode_label,
            "engine": engine_label,
            "workload": "decode_1user",
            "startup_s": startup_s,
            "cmd_info": cmd_info,
            "summary": dec,
            "hw": hw,
        },
    )
    summary["decode_1u"] = dec_summary

    # 2. Prefill 2k & 8k (3 reps each)
    for pf_label, pf_tok in (("2k", 2048), ("8k", 8192)):
        pf = p10_client.run_prefill_series(
            base_url=primary_url,
            model=model,
            cfg_id=f"{cfg_prefix}_pf_{pf_label}",
            target_prompt_tokens=pf_tok,
            reps=3,
            burn_in=1,
            max_tokens=16,
            seed=142,
        )
        print(
            f"[{mode_label}][{engine_label}] Prefill {pf_label}: TTFT={pf['ttft_ms']} ms "
            f"| {pf['prefill_tok_s']} tok/s",
            flush=True,
        )
        append_jsonl(
            OUT_JSONL,
            {
                "mode": mode_label,
                "engine": engine_label,
                "workload": f"prefill_{pf_label}",
                "summary": pf,
                "hw": get_both_gpus_hw(),
            },
        )
        summary[f"prefill_{pf_label}"] = pf

    # 3. Multi-user C = concurrencies
    for c in concurrencies:
        mu = run_weighted_multiuser_batch(
            base_urls=base_urls,
            weights=weights,
            model=model,
            cfg_id=f"{cfg_prefix}_mu",
            concurrency=c,
            prompt_tokens=512,
            max_tokens=128,
        )
        print(
            f"[{mode_label}][{engine_label}] Multi-user C={c}: agg={mu['aggregate_wall_tok_s']} tok/s "
            f"| min_user={mu['min_user_tok_s']} | ttft_p50={mu['ttft_p50_ms']} ms | ttft_p95={mu['ttft_p95_ms']} ms | err={mu['errors']}",
            flush=True,
        )
        append_jsonl(
            OUT_JSONL,
            {
                "mode": mode_label,
                "engine": engine_label,
                "workload": f"multiuser_c{c}",
                "summary": mu,
                "hw": get_both_gpus_hw(),
            },
        )
        summary[f"multiuser_c{c}"] = mu

    # 4. Best-of-N shared prefix (C=8 and C=16)
    for u in base_urls:
        _ = run_shared_prefix_batch([u], [1.0], model, f"{cfg_prefix}_sp", concurrency=1, shared_tokens=2048, max_tokens=16)
    for sp_c in (8, 16):
        sp = run_shared_prefix_batch(
            base_urls=base_urls,
            weights=weights,
            model=model,
            cfg_id=f"{cfg_prefix}_sp",
            concurrency=sp_c,
            shared_tokens=2048,
            max_tokens=128,
        )
        print(
            f"[{mode_label}][{engine_label}] Best-of-N SharedPrefix C={sp_c}: agg={sp['aggregate_wall_tok_s']} tok/s "
            f"| ttft_p50={sp['ttft_p50_ms']} ms",
            flush=True,
        )
        append_jsonl(
            OUT_JSONL,
            {
                "mode": mode_label,
                "engine": engine_label,
                "workload": f"bestofn_shared_prefix_c{sp_c}",
                "summary": sp,
                "hw": get_both_gpus_hw(),
            },
        )
        summary[f"bestofn_c{sp_c}"] = sp

    # 5. Code Quality 15 problems
    if run_quality:
        q_res = p10_v2_quality.run_quality_suite(primary_url, model, f"{mode_label}:{engine_label}")
        print(
            f"[{mode_label}][{engine_label}] Quality 15: {q_res['passed']}/{q_res['total']}",
            flush=True,
        )
        append_jsonl(
            OUT_JSONL,
            {
                "mode": mode_label,
                "engine": engine_label,
                "workload": "quality_15",
                "summary": {
                    "passed": q_res["passed"],
                    "total": q_res["total"],
                    "pass_rate": q_res.get("pass_rate", round(q_res["passed"] / q_res["total"], 4)),
                },
            },
        )
        summary["quality_15"] = q_res["passed"]

    return summary


def run_phase_n1_net_nccl() -> None:
    """Pha N1: Thiết lập nccl0 + portproxy và chạy kiểm chứng Gloo & NCCL 2-node."""
    print("\n=== [PHA N1] Kiểm chứng mạng phẳng L3/NAT (nccl0) & PyTorch Distributed (Gloo + NCCL) ===", flush=True)
    stop_all_engines()
    wsl_ip = setup_wsl_flat_network()
    print(f"[PHA N1] WSL2 eth0 IP={wsl_ip}, nccl0=10.42.0.225/32, port_range=40000..40060", flush=True)

    smoke_py = REPO_ROOT / "ai-compute-fabric" / "benchmarks" / "hardware" / "p11_nccl_smoke.py"
    for backend in ("gloo", "nccl"):
        env0 = os.environ.copy()
        env0["GLOO_SOCKET_IFNAME"] = "enp133s0"
        env0["NCCL_SOCKET_IFNAME"] = "enp133s0"
        env0["NCCL_IB_DISABLE"] = "1"
        env0["NCCL_P2P_DISABLE"] = "1"
        cmd0 = [
            str(WORK_ROOT / "venv-sglang" / "bin" / "python"),
            str(smoke_py),
            "--rank", "0", "--world-size", "2",
            "--master-addr", LINUX_IP, "--master-port", "29500",
            "--backend", backend,
        ]
        cmd1_bash = f"""
set -e
{WSL_NET_INIT_SNIPPET}
export GLOO_SOCKET_IFNAME=nccl0 NCCL_SOCKET_IFNAME=nccl0 NCCL_IB_DISABLE=1 NCCL_P2P_DISABLE=1
/opt/venv-sglang/bin/python /root/p11_nccl_smoke.py --rank 1 --world-size 2 --master-addr {LINUX_IP} --master-port 29500 --backend {backend}
"""
        t0 = time.monotonic()
        p0 = subprocess.Popen(cmd0, env=env0, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out1 = run_wsl_bash(cmd1_bash, timeout=45)
        out0, _ = p0.communicate(timeout=45)
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
        status = "PASS" if ("ALL CHECKS PASSED!" in out0 and "ALL CHECKS PASSED!" in out1) else "FAIL"
        print(f"[PHA N1][{backend.upper()}] status={status} elapsed={elapsed_ms} ms", flush=True)
        for ln in out0.splitlines():
            if "SUCCESS" in ln or "RTT" in ln or "all_reduce" in ln:
                print("  R0:", ln, flush=True)
        for ln in out1.splitlines():
            if "SUCCESS" in ln or "RTT" in ln or "all_reduce" in ln:
                print("  R1:", ln, flush=True)
        append_jsonl(
            OUT_JSONL,
            {
                "phase": "N1",
                "workload": f"pytorch_dist_{backend}_2node",
                "status": status,
                "elapsed_ms": elapsed_ms,
                "rank0_output": out0.splitlines(),
                "rank1_output": out1.splitlines(),
            },
        )


def launch_sglang_pp2_cluster(
    layer_partition: str | None,
    max_running_reqs: int = 16,
    disable_radix: bool = False,
) -> tuple[subprocess.Popen, subprocess.Popen, float, dict[str, Any]]:
    """Khởi chạy SGLang --nnodes 2 --pp-size 2 trên pc-001 (Rank 0) và pc-win-01 WSL2 (Rank 1)."""
    stop_all_engines()
    setup_wsl_flat_network()

    part_tag = layer_partition.replace(",", "_") if layer_partition else "18_18"
    log_r0 = LOGS_DIR / f"p11_sglang_pp2_{part_tag}_r0.log"
    log_r1 = LOGS_DIR / f"p11_sglang_pp2_{part_tag}_r1.log"

    env0 = os.environ.copy()
    env0["TMPDIR"] = "/tmp"
    env0["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
    env0["SGLANG_CACHE_DIR"] = str(WORK_ROOT / "cache" / "sglang")
    env0["FLASHINFER_WORKSPACE_BASE"] = str(WORK_ROOT / "cache" / "flashinfer")
    cuda_home = str(WORK_ROOT / "venv-sglang" / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13")
    env0["CUDA_HOME"] = cuda_home
    env0["PATH"] = f"{cuda_home}/bin:" + env0.get("PATH", "")
    env0["GLOO_SOCKET_IFNAME"] = "enp133s0"
    env0["NCCL_SOCKET_IFNAME"] = "enp133s0"
    env0["NCCL_IB_DISABLE"] = "1"
    env0["NCCL_P2P_DISABLE"] = "1"
    if layer_partition:
        env0["SGLANG_PP_LAYER_PARTITION"] = layer_partition
    else:
        env0.pop("SGLANG_PP_LAYER_PARTITION", None)

    cmd0 = [
        str(WORK_ROOT / "venv-sglang" / "bin" / "python"),
        "-m", "sglang.launch_server",
        "--model-path", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
        "--served-model-name", "aicf",
        "--host", "0.0.0.0", "--port", "18090",
        "--context-length", "16384",
        "--max-running-requests", str(max_running_reqs),
        "--mem-fraction-static", "0.82",
        "--nnodes", "2", "--node-rank", "0",
        "--dist-init-addr", f"{LINUX_IP}:29500",
        "--nccl-port", "29505",
        "--pp-size", "2",
        "--attention-backend", "triton",
        "--sampling-backend", "pytorch",
        "--disable-prefill-cuda-graph",
        "--disable-decode-cuda-graph",
    ]
    if disable_radix:
        cmd0.append("--disable-radix-cache")

    part_env_r1 = f"export SGLANG_PP_LAYER_PARTITION={layer_partition};" if layer_partition else "unset SGLANG_PP_LAYER_PARTITION;"
    radix_flag_r1 = "--disable-radix-cache" if disable_radix else ""
    cmd1_bash = f"""
set -e
{WSL_NET_INIT_SNIPPET}
export TMPDIR=/tmp
export GLOO_SOCKET_IFNAME=nccl0
export NCCL_SOCKET_IFNAME=nccl0
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=1
export CUDA_HOME=/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13
export PATH="/opt/venv-sglang/bin:$CUDA_HOME/bin:/usr/local/bin:$PATH"
{part_env_r1}
exec /opt/venv-sglang/bin/python -m sglang.launch_server \\
  --model-path /opt/hf-models/Qwen3-8B-AWQ \\
  --served-model-name aicf \\
  --host 0.0.0.0 --port 18090 \\
  --context-length 16384 \\
  --max-running-requests {max_running_reqs} \\
  --mem-fraction-static 0.82 \\
  --nnodes 2 --node-rank 1 \\
  --dist-init-addr {LINUX_IP}:29500 \\
  --nccl-port 29505 \\
  --pp-size 2 \\
  --attention-backend triton \\
  --sampling-backend pytorch \\
  --disable-prefill-cuda-graph \\
  --disable-decode-cuda-graph {radix_flag_r1}
"""
    lf0 = open(log_r0, "w", encoding="utf-8")
    lf1 = open(log_r1, "w", encoding="utf-8")
    p0 = subprocess.Popen(cmd0, env=env0, stdout=lf0, stderr=subprocess.STDOUT, start_new_session=True)
    p1 = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
        stdin=subprocess.PIPE, stdout=lf1, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    assert p1.stdin is not None
    p1.stdin.write(cmd1_bash + "\n")
    p1.stdin.flush()

    startup_s = wait_http_ready("http://127.0.0.1:18090", timeout_s=180, procs=[p0, p1])
    return p0, p1, startup_s, {"rank0_cmd": shlex.join(cmd0), "layer_partition": layer_partition or "18,18"}


def run_phase_n2_sglang_pp2() -> None:
    """Pha N2.1 & N2.2: Đo SGLang PP=2 gộp 2 card (Chia đều 18,18 vs Chia lệch 13,23 + Mutation 24,12)."""
    print("\n=== [PHA N2A] SGLang 0.5.19 Gộp 2 Card (--nnodes 2 --pp-size 2) ===", flush=True)
    for part, label, full_eval in (
        (None, "SGLang PP=2 Symmetric (18,18)", True),
        ("13,23", "SGLang PP=2 Asymmetric (13,23)", True),
        ("24,12", "SGLang PP=2 Inverted Mutation (24,12)", False),
    ):
        p0 = p1 = None
        try:
            print(f"\n--- Khởi chạy {label} ---", flush=True)
            p0, p1, startup_s, cmd_info = launch_sglang_pp2_cluster(layer_partition=part, max_running_reqs=16)
            print(f"[{label}] READY in {startup_s}s!", flush=True)
            if full_eval:
                evaluate_2gpu_endpoint(
                    mode_label="G2_PP2_Merged",
                    engine_label=label,
                    base_urls=["http://127.0.0.1:18090"],
                    weights=[1.0],
                    model="aicf",
                    cfg_prefix=f"p11_sgl_pp2_{part or '18_18'}",
                    startup_s=startup_s,
                    cmd_info=cmd_info,
                    concurrencies=(1, 4, 8, 16),
                    run_quality=(part == "13,23"),
                )
            else:
                dec_mut = p10_client.run_decode_series(
                    base_url="http://127.0.0.1:18090",
                    model="aicf",
                    cfg_id="p11_sgl_pp2_mut_24_12_dec",
                    reps=7,
                    burn_in=1,
                    max_tokens=256,
                    seed=142,
                )
                mu8 = run_weighted_multiuser_batch(
                    base_urls=["http://127.0.0.1:18090"],
                    weights=[1.0],
                    model="aicf",
                    cfg_id="p11_sgl_pp2_mut_24_12",
                    concurrency=8,
                    prompt_tokens=512,
                    max_tokens=128,
                )
                print(
                    f"[{label}] Mutation Decode 1u={dec_mut['decode_tok_s']} | C=8: agg={mu8['aggregate_wall_tok_s']} tok/s",
                    flush=True,
                )
                append_jsonl(
                    OUT_JSONL,
                    {
                        "mode": "G2_PP2_Merged",
                        "engine": label,
                        "workload": "mutation_inverted_partition_24_12",
                        "decode_1u": dec_mut,
                        "multiuser_c8": mu8,
                        "hw": get_both_gpus_hw(),
                    },
                )
        except Exception as e:
            print(f"[{label}] ERROR: {e}", flush=True)
            append_jsonl(
                OUT_JSONL,
                {
                    "mode": "G2_PP2_Merged",
                    "engine": label,
                    "status": "FAILED",
                    "error": str(e),
                },
            )
        finally:
            stop_all_engines()


def launch_vllm_pp2_cluster(
    layer_partition: str | None,
) -> tuple[subprocess.Popen, subprocess.Popen, float, dict[str, Any]]:
    """Khởi chạy vLLM 0.32.0 --distributed-executor-backend mp --pipeline-parallel-size 2 --nnodes 2."""
    stop_all_engines()
    setup_wsl_flat_network()

    part_tag = layer_partition.replace(",", "_") if layer_partition else "18_18"
    log_r0 = LOGS_DIR / f"p11_vllm_pp2_{part_tag}_r0.log"
    log_r1 = LOGS_DIR / f"p11_vllm_pp2_{part_tag}_r1.log"

    env0 = os.environ.copy()
    env0["TMPDIR"] = "/tmp"
    env0["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
    env0["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    env0["VLLM_HOST_IP"] = LINUX_IP
    env0["GLOO_SOCKET_IFNAME"] = "enp133s0"
    env0["NCCL_SOCKET_IFNAME"] = "enp133s0"
    env0["NCCL_IB_DISABLE"] = "1"
    env0["NCCL_P2P_DISABLE"] = "1"
    if layer_partition:
        env0["VLLM_PP_LAYER_PARTITION"] = layer_partition
    else:
        env0.pop("VLLM_PP_LAYER_PARTITION", None)

    cmd0 = [
        str(WORK_ROOT / "venv-vllm" / "bin" / "vllm"),
        "serve", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
        "--served-model-name", "aicf",
        "--host", "0.0.0.0", "--port", "18090",
        "--max-model-len", "16384",
        "--max-num-seqs", "16",
        "--gpu-memory-utilization", "0.82",
        "--distributed-executor-backend", "mp",
        "--pipeline-parallel-size", "2",
        "--nnodes", "2", "--node-rank", "0",
        "--master-addr", LINUX_IP, "--master-port", "29500",
        "--enforce-eager",
    ]
    part_env_r1 = f"export VLLM_PP_LAYER_PARTITION={layer_partition};" if layer_partition else "unset VLLM_PP_LAYER_PARTITION;"
    cmd1_bash = f"""
set -e
{WSL_NET_INIT_SNIPPET}
export TMPDIR=/tmp
export VLLM_USE_FLASHINFER_SAMPLER=0
export VLLM_HOST_IP={WIN_IP}
export GLOO_SOCKET_IFNAME=nccl0
export NCCL_SOCKET_IFNAME=nccl0
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=1
export PATH="/opt/venv-vllm/bin:/usr/local/bin:$PATH"
{part_env_r1}
exec /opt/venv-vllm/bin/python -m vllm.entrypoints.cli.main serve /opt/hf-models/Qwen3-8B-AWQ \\
  --served-model-name aicf \\
  --max-model-len 16384 \\
  --max-num-seqs 16 \\
  --gpu-memory-utilization 0.82 \\
  --distributed-executor-backend mp \\
  --pipeline-parallel-size 2 \\
  --nnodes 2 --node-rank 1 \\
  --master-addr {LINUX_IP} --master-port 29500 \\
  --enforce-eager --headless
"""
    lf0 = open(log_r0, "w", encoding="utf-8")
    lf1 = open(log_r1, "w", encoding="utf-8")
    p0 = subprocess.Popen(cmd0, env=env0, stdout=lf0, stderr=subprocess.STDOUT, start_new_session=True)
    p1 = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
        stdin=subprocess.PIPE, stdout=lf1, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    assert p1.stdin is not None
    p1.stdin.write(cmd1_bash + "\n")
    p1.stdin.flush()

    startup_s = wait_http_ready("http://127.0.0.1:18090", timeout_s=180, procs=[p0, p1])
    return p0, p1, startup_s, {"rank0_cmd": shlex.join(cmd0), "layer_partition": layer_partition or "18,18"}


def run_phase_n2_vllm_pp2() -> None:
    """Pha N2.3: Đo vLLM 0.32.0 PP=2 gộp 2 card (Chia đều 18,18 vs Chia lệch 13,23)."""
    print("\n=== [PHA N2B] vLLM 0.32.0 Gộp 2 Card (--distributed-executor-backend mp --pp 2) ===", flush=True)
    for part, label in (
        (None, "vLLM PP=2 Symmetric (18,18)"),
        ("13,23", "vLLM PP=2 Asymmetric (13,23)"),
    ):
        try:
            print(f"\n--- Khởi chạy {label} ---", flush=True)
            p0, p1, startup_s, cmd_info = launch_vllm_pp2_cluster(layer_partition=part)
            print(f"[{label}] READY in {startup_s}s!", flush=True)
            evaluate_2gpu_endpoint(
                mode_label="G2_PP2_Merged",
                engine_label=label,
                base_urls=["http://127.0.0.1:18090"],
                weights=[1.0],
                model="aicf",
                cfg_prefix=f"p11_vllm_pp2_{part or '18_18'}",
                startup_s=startup_s,
                cmd_info=cmd_info,
                concurrencies=(1, 4, 8, 16),
                run_quality=(part == "13,23"),
            )
        except Exception as e:
            print(f"[{label}] ERROR: {e}", flush=True)
            append_jsonl(
                OUT_JSONL,
                {
                    "mode": "G2_PP2_Merged",
                    "engine": label,
                    "status": "FAILED",
                    "error": str(e),
                },
            )
        finally:
            stop_all_engines()


def run_phase_n3_dp2_weighted() -> None:
    """Pha N3: Đo Chế độ G1 — Gộp Chia tải (DP=2 Weighted Load Balancing 35% : 65%) trên 2 card."""
    print("\n=== [PHA N3] Chế độ G1: Gộp 2 Card Kiểu Chia Tải (DP=2 Weighted Router 35% : 65%) ===", flush=True)
    stop_all_engines()
    setup_wsl_flat_network()

    log_r0 = LOGS_DIR / "p11_dp2_sglang_pc001.log"
    log_r1 = LOGS_DIR / "p11_dp2_sglang_pcwin01.log"

    env0 = os.environ.copy()
    env0["TMPDIR"] = str(WORK_ROOT / "tmp")
    env0["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
    env0["SGLANG_CACHE_DIR"] = str(WORK_ROOT / "cache" / "sglang")
    env0["FLASHINFER_WORKSPACE_BASE"] = str(WORK_ROOT / "cache" / "flashinfer")
    cuda_home = str(WORK_ROOT / "venv-sglang" / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13")
    env0["CUDA_HOME"] = cuda_home
    env0["PATH"] = f"{cuda_home}/bin:" + env0.get("PATH", "")

    cmd0 = [
        str(WORK_ROOT / "venv-sglang" / "bin" / "python"),
        "-m", "sglang.launch_server",
        "--model-path", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
        "--served-model-name", "aicf",
        "--host", "127.0.0.1", "--port", "18090",
        "--context-length", "16384",
        "--max-running-requests", "16",
        "--mem-fraction-static", "0.85",
        "--attention-backend", "triton",
        "--sampling-backend", "pytorch",
        "--disable-prefill-cuda-graph",
        "--cuda-graph-max-bs-decode", "16",
    ]
    cmd1_bash = """
set -e
export TMPDIR=/opt/tmp
export XDG_CACHE_HOME=/opt/cache
export SGLANG_CACHE_DIR=/opt/cache/sglang
export FLASHINFER_WORKSPACE_BASE=/opt/cache/flashinfer
export CUDA_HOME=/opt/venv-sglang/lib/python3.12/site-packages/nvidia/cu13
export PATH="/opt/venv-sglang/bin:$CUDA_HOME/bin:/usr/local/bin:$PATH"
exec /opt/venv-sglang/bin/python -m sglang.launch_server \\
  --model-path /opt/hf-models/Qwen3-8B-AWQ \\
  --served-model-name aicf \\
  --host 0.0.0.0 --port 18090 \\
  --context-length 16384 \\
  --max-running-requests 16 \\
  --mem-fraction-static 0.82 \\
  --attention-backend triton \\
  --sampling-backend pytorch \\
  --disable-prefill-cuda-graph \\
  --cuda-graph-max-bs-decode 16
"""
    try:
        t0 = time.monotonic()
        lf0 = open(log_r0, "w", encoding="utf-8")
        lf1 = open(log_r1, "w", encoding="utf-8")
        p0 = subprocess.Popen(cmd0, env=env0, stdout=lf0, stderr=subprocess.STDOUT, start_new_session=True)
        p1 = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf1, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert p1.stdin is not None
        p1.stdin.write(cmd1_bash + "\n")
        p1.stdin.flush()

        wait_http_ready("http://127.0.0.1:18090", timeout_s=180, procs=[p0])
        wait_http_ready(f"http://{WIN_IP}:18090", timeout_s=180, procs=[p1])
        startup_s = round(time.monotonic() - t0, 2)
        print(f"[G1 DP=2 Weighted SGLang] Both nodes READY in {startup_s}s!", flush=True)

        evaluate_2gpu_endpoint(
            mode_label="G1_DP2_Weighted_35_65",
            engine_label="SGLang DP=2 (RTX 3060 35% + RTX 5070 65%)",
            base_urls=["http://127.0.0.1:18090", f"http://{WIN_IP}:18090"],
            weights=[0.35, 0.65],
            model="aicf",
            cfg_prefix="p11_sgl_dp2_35_65",
            startup_s=startup_s,
            cmd_info={"pc001_cmd": shlex.join(cmd0), "weights": [0.35, 0.65]},
            concurrencies=(1, 4, 8, 16, 32),
            run_quality=True,
        )
    finally:
        stop_all_engines()


def run_phase_n3_dp2_vllm() -> None:
    """Pha N3B: Đo Chế độ G1 — Gộp Chia tải vLLM 0.32.0 (DP=2 Weighted Load Balancing 35% : 65%) trên 2 card."""
    print("\n=== [PHA N3B] Chế độ G1: vLLM 0.32.0 Gộp 2 Card Kiểu Chia Tải (DP=2 Weighted Router 35% : 65%) ===", flush=True)
    stop_all_engines()
    setup_wsl_flat_network()

    log_r0 = LOGS_DIR / "p11_dp2_vllm_pc001.log"
    log_r1 = LOGS_DIR / "p11_dp2_vllm_pcwin01.log"

    env0 = os.environ.copy()
    env0["TMPDIR"] = "/tmp"
    env0["XDG_CACHE_HOME"] = str(WORK_ROOT / "cache")
    env0["VLLM_USE_FLASHINFER_SAMPLER"] = "0"

    cmd0 = [
        str(WORK_ROOT / "venv-vllm" / "bin" / "vllm"),
        "serve", str(WORK_ROOT / "hf-models" / "Qwen3-8B-AWQ"),
        "--served-model-name", "aicf",
        "--host", "127.0.0.1", "--port", "18090",
        "--max-model-len", "16384",
        "--max-num-seqs", "16",
        "--gpu-memory-utilization", "0.85",
    ]
    cmd1_bash = """
set -e
export TMPDIR=/tmp
export XDG_CACHE_HOME=/opt/cache
export VLLM_USE_FLASHINFER_SAMPLER=0
export PATH="/opt/venv-vllm/bin:/usr/local/bin:$PATH"
exec /opt/venv-vllm/bin/python -m vllm.entrypoints.cli.main serve /opt/hf-models/Qwen3-8B-AWQ \\
  --served-model-name aicf \\
  --host 0.0.0.0 --port 18090 \\
  --max-model-len 16384 \\
  --max-num-seqs 16 \\
  --gpu-memory-utilization 0.82
"""
    try:
        t0 = time.monotonic()
        lf0 = open(log_r0, "w", encoding="utf-8")
        lf1 = open(log_r1, "w", encoding="utf-8")
        p0 = subprocess.Popen(cmd0, env=env0, stdout=lf0, stderr=subprocess.STDOUT, start_new_session=True)
        p1 = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST, f"{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
            stdin=subprocess.PIPE, stdout=lf1, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        assert p1.stdin is not None
        p1.stdin.write(cmd1_bash + "\n")
        p1.stdin.flush()

        wait_http_ready("http://127.0.0.1:18090", timeout_s=180, procs=[p0])
        wait_http_ready(f"http://{WIN_IP}:18090", timeout_s=180, procs=[p1])
        startup_s = round(time.monotonic() - t0, 2)
        print(f"[G1 DP=2 Weighted vLLM] Both nodes READY in {startup_s}s!", flush=True)

        evaluate_2gpu_endpoint(
            mode_label="G1_DP2_Weighted_35_65",
            engine_label="vLLM DP=2 (RTX 3060 35% + RTX 5070 65%)",
            base_urls=["http://127.0.0.1:18090", f"http://{WIN_IP}:18090"],
            weights=[0.35, 0.65],
            model="aicf",
            cfg_prefix="p11_vllm_dp2_35_65",
            startup_s=startup_s,
            cmd_info={"pc001_cmd": shlex.join(cmd0), "weights": [0.35, 0.65]},
            concurrencies=(1, 4, 8, 16, 32),
            run_quality=True,
        )
    finally:
        stop_all_engines()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["n1", "n2_sglang", "n2_vllm", "n3_dp2", "n3_dp2_vllm", "all"], default="all")
    args = ap.parse_args()

    if args.phase in ("n1", "all"):
        run_phase_n1_net_nccl()
    if args.phase in ("n2_sglang", "all"):
        run_phase_n2_sglang_pp2()
    if args.phase in ("n2_vllm", "all"):
        run_phase_n2_vllm_pp2()
    if args.phase in ("n3_dp2", "all"):
        run_phase_n3_dp2_weighted()
    if args.phase in ("n3_dp2_vllm", "all"):
        run_phase_n3_dp2_vllm()
