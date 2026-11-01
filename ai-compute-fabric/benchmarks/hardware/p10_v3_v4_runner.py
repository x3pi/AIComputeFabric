#!/usr/bin/env python3
"""Pha V2.5, V3, V4 (va đối chứng gộp 2 card llama.cpp) Runner.

Thực hiện:
1. Pha V2.5: Đo đơn card RTX 3060 (pc-001) với mô hình MoE 30B-A3B:
   - llama.cpp: Qwen3-30B-A3B-Q4_K_M.gguf với `-ngl 99 -ncmoe 20`
   - vLLM: thử nghiệm `--cpu-offload-gb` trên 12 GiB VRAM (ghi nhận hành vi thực tế / lỗi / tốc độ)
2. Pha V4 (Spike gộp 2 card Linux pc-001 <-> Windows 10 WSL2 pc-win-01 qua NAT):
   - V4.1: Đo băng thông và độ trễ mạng thực tế giữa Linux (10.42.0.1) và WSL2 (qua 10.42.0.225 portproxy) bằng ping & TCP stream.
   - V4.2: Thử nghiệm 1 — torch.distributed (gloo & nccl) 2 nút qua TCPStore + portproxy.
   - V4.3: Thử nghiệm 2 — Ray cluster 2 nút (ép dải cổng cố định + portproxy).
3. Đo đối chứng gộp 2 card llama.cpp (Mode C: Linux RTX 3060 + Windows RTX 5070 qua ggml-rpc-server 10.42.0.225:50052)
   bằng cùng `p10_client.py` (decode >= 7 lần, prefill 2k/8k/24k >= 3 lần, multi-user 1/4/8/16) cho Qwen3-30B-A3B-Q4_K_M.gguf.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import p10_client

REPO_ROOT = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric")
WORK_DIR = REPO_ROOT / ".aicf-work"
REPORTS_DIR = REPO_ROOT / "ai-compute-fabric" / "benchmarks" / "reports"
LOGS_DIR = REPORTS_DIR / "logs"

LLAMA_SERVER_BIN = WORK_DIR / "bin" / "llama-server"
LLAMA_LIB_DIR = WORK_DIR / "bin"
MOE_GGUF_MODEL = WORK_DIR / "Qwen3-30B-A3B-Q4_K_M.gguf"
QWEN8B_AWQ_DIR = WORK_DIR / "hf-models" / "Qwen3-8B-AWQ"
QWEN06B_DIR = WORK_DIR / "hf-train" / "Qwen3-0.6B"
VLLM_PYTHON = WORK_DIR / "venv-vllm" / "bin" / "python"

WIN_HOST = "fios02@10.42.0.225"
WIN_IP = "10.42.0.225"
LINUX_IP = "10.42.0.1"
WSL_EXE = r'"C:\Program Files\WSL\wsl.exe"'
PORT = 18080


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def ssh_win(cmd: str, timeout: float = 120.0, check: bool = False) -> subprocess.CompletedProcess:
    full = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        WIN_HOST,
        cmd,
    ]
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout, check=check)


def get_linux_gpu_metrics() -> dict[str, Any]:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        ).strip()
        parts = [x.strip() for x in out.split(",")]
        return {
            "gpu_name": parts[0],
            "vram_used_mib": float(parts[1]),
            "vram_total_mib": float(parts[2]),
            "gpu_temp_c": float(parts[3]),
            "gpu_power_w": float(parts[4]) if parts[4] != "[N/A]" else None,
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_win_gpu_metrics() -> dict[str, Any]:
    try:
        cp = ssh_win(
            "nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits",
            timeout=10,
        )
        out = cp.stdout.strip().splitlines()[0]
        parts = [x.strip() for x in out.split(",")]
        return {
            "gpu_name": parts[0],
            "vram_used_mib": float(parts[1]),
            "vram_total_mib": float(parts[2]),
            "gpu_temp_c": float(parts[3]),
            "gpu_power_w": float(parts[4]) if parts[4] != "[N/A]" else None,
        }
    except Exception as exc:
        return {"error": str(exc)}


def wait_for_server(base_url: str, timeout_s: float = 240.0, proc: subprocess.Popen | None = None) -> float:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"Server process exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{base_url}/v1/models", timeout=3) as resp:
                if resp.status == 200:
                    return round(time.perf_counter() - t0, 2)
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"Server at {base_url} did not become ready within {timeout_s}s")


def stop_local_servers() -> None:
    subprocess.run(["pkill", "-f", f"port {PORT}"], check=False)
    subprocess.run(["pkill", "-f", "[v]llm.entrypoints.openai.api_server"], check=False)
    subprocess.run(["pkill", "-f", "[l]lama-server"], check=False)
    time.sleep(2.0)


def run_pha_v25_moe_single_card() -> None:
    """Pha V2.5: MoE 30B-A3B trên 1 card RTX 3060 12GB (-ncmoe 20 vs vLLM --cpu-offload-gb)."""
    print("\n=== PHA V2.5: MoE 30B-A3B TRÊN 1 CARD RTX 3060 (pc-001) ===", flush=True)
    out_jsonl = REPORTS_DIR / "p10_v25_moe_single_card.jsonl"
    if out_jsonl.exists():
        out_jsonl.unlink()

    stop_local_servers()
    base_url = f"http://127.0.0.1:{PORT}"

    # 1. Đo llama.cpp Qwen3-30B-A3B-Q4_K_M.gguf với -ngl 99 -ncmoe 20
    log_llama = LOGS_DIR / "p10_v25_pc001_llama_ncmoe20.log"
    cmd_llama = [
        str(LLAMA_SERVER_BIN),
        "-m", str(MOE_GGUF_MODEL),
        "--alias", "aicf",
        "--host", "127.0.0.1",
        "--port", str(PORT),
        "-ngl", "99",
        "-ncmoe", "20",
        "-fa", "on",
        "-ctk", "f16",
        "-ctv", "f16",
        "-c", "8192",
        "-np", "1",
        "-t", "8",
    ]
    env_llama = os.environ.copy()
    env_llama["LD_LIBRARY_PATH"] = f"{LLAMA_LIB_DIR}:{env_llama.get('LD_LIBRARY_PATH', '')}"
    print(f"[V2.5][llama.cpp] CMD: {' '.join(cmd_llama)}", flush=True)
    with log_llama.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd_llama, stdout=lf, stderr=subprocess.STDOUT, env=env_llama)
        try:
            startup_s = wait_for_server(base_url, timeout_s=180.0, proc=proc)
            dec = p10_client.run_decode_series(
                base_url=base_url, model="aicf", cfg_id="v25_ncmoe20", reps=7, burn_in=1, max_tokens=256, seed=42
            )
            append_jsonl(out_jsonl, {
                "phase": "V2.5",
                "host": "pc-001",
                "engine": "llama.cpp (-ncmoe 20)",
                "workload": "decode_1user",
                "model_file": str(MOE_GGUF_MODEL),
                "startup_s": startup_s,
                "cmd": " ".join(cmd_llama),
                "gpu_after": get_linux_gpu_metrics(),
                "result": dec,
            })
            print(f"[V2.5][llama.cpp] decode_1user: {dec['decode_tok_s']} tok/s", flush=True)

            pf2k = p10_client.run_prefill_series(
                base_url=base_url, model="aicf", cfg_id="v25_ncmoe20", target_prompt_tokens=2048, reps=3, burn_in=1, max_tokens=16, seed=42
            )
            append_jsonl(out_jsonl, {
                "phase": "V2.5",
                "host": "pc-001",
                "engine": "llama.cpp (-ncmoe 20)",
                "workload": "prefill_2k",
                "model_file": str(MOE_GGUF_MODEL),
                "cmd": " ".join(cmd_llama),
                "gpu_after": get_linux_gpu_metrics(),
                "result": pf2k,
            })
            print(f"[V2.5][llama.cpp] prefill_2k: TTFT={pf2k['ttft_ms']} ms | tok/s={pf2k['prefill_tok_s']}", flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
            stop_local_servers()

    # Multi-user C=4 với llama.cpp -ncmoe 20
    cmd_llama_c4 = [
        str(LLAMA_SERVER_BIN),
        "-m", str(MOE_GGUF_MODEL),
        "--alias", "aicf",
        "--host", "127.0.0.1",
        "--port", str(PORT),
        "-ngl", "99",
        "-ncmoe", "20",
        "-fa", "on",
        "-ctk", "f16",
        "-ctv", "f16",
        "-c", "8192",
        "-np", "4",
        "-t", "8",
    ]
    with log_llama.open("a", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd_llama_c4, stdout=lf, stderr=subprocess.STDOUT, env=env_llama)
        try:
            startup_s = wait_for_server(base_url, timeout_s=180.0, proc=proc)
            mu4 = p10_client.run_multiuser_batch(
                base_url=base_url, model="aicf", cfg_id="v25_ncmoe20", concurrency=4, prompt_tokens=512, max_tokens=128, seed=42
            )
            append_jsonl(out_jsonl, {
                "phase": "V2.5",
                "host": "pc-001",
                "engine": "llama.cpp (-ncmoe 20)",
                "workload": "multi_user_c4",
                "model_file": str(MOE_GGUF_MODEL),
                "cmd": " ".join(cmd_llama_c4),
                "gpu_after": get_linux_gpu_metrics(),
                "result": mu4,
            })
            print(f"[V2.5][llama.cpp] multi_user_c4: agg={mu4['aggregate_wall_tok_s']} tok/s", flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
            stop_local_servers()

    # 2. Thử nghiệm vLLM --cpu-offload-gb trên RTX 3060 12GB:
    for tag, model_dir, offload_gb in [
        ("vllm_qwen8b_awq_cpu_offload_4gb", QWEN8B_AWQ_DIR, "4"),
        ("vllm_qwen06b_bf16_cpu_offload_1gb", QWEN06B_DIR, "1"),
    ]:
        log_vllm = LOGS_DIR / f"p10_v25_{tag}.log"
        cmd_vllm = [
            str(VLLM_PYTHON),
            "-m", "vllm.entrypoints.openai.api_server",
            "--model", str(model_dir),
            "--served-model-name", "aicf",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--max-model-len", "8192",
            "--gpu-memory-utilization", "0.85",
            "--cpu-offload-gb", offload_gb,
            "--no-enable-prefix-caching",
        ]
        env_vllm = os.environ.copy()
        env_vllm["TMPDIR"] = "/tmp"
        env_vllm["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
        print(f"[V2.5][vLLM] Testing {tag}: {' '.join(cmd_vllm)}", flush=True)
        with log_vllm.open("w", encoding="utf-8") as lf:
            proc = subprocess.Popen(cmd_vllm, stdout=lf, stderr=subprocess.STDOUT, env=env_vllm)
            try:
                startup_s = wait_for_server(base_url, timeout_s=150.0, proc=proc)
                dec = p10_client.run_decode_series(
                    base_url=base_url, model="aicf", cfg_id=tag, reps=5, burn_in=1, max_tokens=128, seed=42
                )
                append_jsonl(out_jsonl, {
                    "phase": "V2.5",
                    "host": "pc-001",
                    "engine": "vllm (--cpu-offload-gb)",
                    "config_id": tag,
                    "status": "OK",
                    "startup_s": startup_s,
                    "cmd": " ".join(cmd_vllm),
                    "gpu_after": get_linux_gpu_metrics(),
                    "result": dec,
                })
                print(f"[V2.5][vLLM] {tag}: decode={dec['decode_tok_s']} tok/s", flush=True)
            except Exception as exc:
                err_tail = ""
                try:
                    err_tail = "\n".join(log_vllm.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
                except Exception:
                    pass
                rec = {
                    "phase": "V2.5",
                    "host": "pc-001",
                    "engine": "vllm (--cpu-offload-gb)",
                    "config_id": tag,
                    "status": "FAILED",
                    "error": str(exc),
                    "log_tail": err_tail,
                    "cmd": " ".join(cmd_vllm),
                }
                append_jsonl(out_jsonl, rec)
                print(f"[V2.5][vLLM] {tag} FAILED: {exc}", flush=True)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except Exception:
                    proc.kill()
                stop_local_servers()


def run_pha_v4_spike() -> bool:
    """Pha V4: Spike gộp 2 card giữa Linux (10.42.0.1) và WSL2 (qua 10.42.0.225 NAT)."""
    print("\n=== PHA V4: SPIKE GỘP 2 CARD LINUX (10.42.0.1) <-> WSL2 (10.42.0.225 NAT) ===", flush=True)
    out_jsonl = REPORTS_DIR / "p10_v4_spike.jsonl"
    spike_log = LOGS_DIR / "p10_v4_spike.log"
    if out_jsonl.exists():
        out_jsonl.unlink()
    if spike_log.exists():
        spike_log.unlink()

    def log_msg(msg: str) -> None:
        print(msg, flush=True)
        with spike_log.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    keepalive_proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", WIN_HOST, f'{WSL_EXE} -d Ubuntu-24.04 -u root -- sleep 1800'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)

    # Bước 0: Lấy IP nội bộ của WSL2 (Hyper-V NAT switch) và kiểm tra chế độ mạng
    cp_ip = ssh_win(f'{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -c "hostname -I && ip -4 addr show eth0"')
    wsl_ip = cp_ip.stdout.strip().splitlines()[0].split()[0]
    log_msg(f"[V4.0] WSL2 eth0 internal IP: {wsl_ip}")
    log_msg(f"[V4.0] WSL2 ip addr:\n{cp_ip.stdout.strip()}")

    # Bước 1: Đo độ trễ (ping) và băng thông (TCP stream) giữa Linux (10.42.0.1) và WSL2
    cp_ping = subprocess.run(["ping", "-c", "5", WIN_IP], capture_output=True, text=True)
    log_msg(f"[V4.1] Ping Linux ({LINUX_IP}) -> Windows ({WIN_IP}):\n{cp_ping.stdout.strip()}")

    cp_wsl_ping = ssh_win(f'{WSL_EXE} -d Ubuntu-24.04 -u root -- ping -c 5 {LINUX_IP}')
    log_msg(f"[V4.1] Ping WSL2 ({wsl_ip}) -> Linux ({LINUX_IP}):\n{cp_wsl_ping.stdout.strip()}")

    import base64

    bw_script_server = (
        "import socket, time, sys\n"
        "port = int(sys.argv[1])\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('0.0.0.0', port))\n"
        "s.listen(1)\n"
        "s.settimeout(20.0)\n"
        "conn, addr = s.accept()\n"
        "t0 = time.perf_counter()\n"
        "total = 0\n"
        "while True:\n"
        "    d = conn.recv(1048576)\n"
        "    if not d: break\n"
        "    total += len(d)\n"
        "dt = time.perf_counter() - t0\n"
        "conn.close(); s.close()\n"
        "print(f'RECV_BYTES={total} SECONDS={dt:.4f} GBPS={(total*8)/(dt*1e9):.3f}')\n"
    )
    bw_script_client = (
        "import socket, time, sys\n"
        "host, port = sys.argv[1], int(sys.argv[2])\n"
        "buf = b'x' * 1048576\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(15.0)\n"
        "s.connect((host, port))\n"
        "t0 = time.perf_counter()\n"
        "for _ in range(512):\n"
        "    s.sendall(buf)\n"
        "s.shutdown(socket.SHUT_WR)\n"
        "s.close()\n"
        "dt = time.perf_counter() - t0\n"
        "print(f'SENT_512MIB SECONDS={dt:.4f} GBPS={(512*1048576*8)/(dt*1e9):.3f}')\n"
    )
    b64_srv = base64.b64encode(bw_script_server.encode()).decode()
    b64_cli = base64.b64encode(bw_script_client.encode()).decode()

    srv_proc = subprocess.Popen(
        [sys.executable, "-c", bw_script_server, "15201"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(1.0)
    cp_cli = ssh_win(
        f'{WSL_EXE} -d Ubuntu-24.04 -u root -- /opt/venv-vllm/bin/python -c "import base64; exec(base64.b64decode(\'{b64_cli}\'))" {LINUX_IP} 15201',
        timeout=30,
    )
    try:
        srv_out, _ = srv_proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        srv_proc.kill()
        srv_out, _ = srv_proc.communicate()
    log_msg(f"[V4.1] TCP Throughput WSL2 -> Linux (512 MiB): client={cp_cli.stdout.strip()} {cp_cli.stderr.strip()} | server={srv_out.strip()}")

    # Chiều 2: Linux -> WSL2 qua netsh interface portproxy trên cổng 15202
    ssh_win(f"netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=15202 connectaddress={wsl_ip} connectport=15202")
    ssh_win('netsh advfirewall firewall add rule name="AICF_P10_V4_TEST" dir=in action=allow protocol=TCP localport=15202,29500-29510,6379,16380-16420')
    wsl_srv_proc = subprocess.Popen(
        [
            "ssh", "-o", "BatchMode=yes", WIN_HOST,
            f'{WSL_EXE} -d Ubuntu-24.04 -u root -- /opt/venv-vllm/bin/python -c "import base64; exec(base64.b64decode(\'{b64_srv}\'))" 15202',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(1.5)
    cli_local = subprocess.run([sys.executable, "-c", bw_script_client, WIN_IP, "15202"], capture_output=True, text=True, timeout=30)
    try:
        wsl_srv_out, _ = wsl_srv_proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        wsl_srv_proc.kill()
        wsl_srv_out, _ = wsl_srv_proc.communicate()
    log_msg(f"[V4.1] TCP Throughput Linux -> WSL2 via portproxy (512 MiB): client={cli_local.stdout.strip()} {cli_local.stderr.strip()} | server={wsl_srv_out.strip()}")

    append_jsonl(out_jsonl, {
        "phase": "V4.1",
        "test": "network_bandwidth_latency",
        "wsl_internal_ip": wsl_ip,
        "wsl_to_linux_512mib": cp_cli.stdout.strip(),
        "linux_to_wsl_portproxy_512mib": cli_local.stdout.strip(),
        "ping_linux_to_win": cp_ping.stdout.strip().splitlines()[-1] if cp_ping.stdout.strip() else "",
        "ping_wsl_to_linux": cp_wsl_ping.stdout.strip().splitlines()[-1] if cp_wsl_ping.stdout.strip() else "",
    })

    # Bước 2: Thử nghiệm torch.distributed 2 nút (Rank 0 trên Linux 10.42.0.1, Rank 1 trong WSL2)
    for backend in ["gloo", "nccl"]:
        log_msg(f"\n[V4.2] Testing torch.distributed init_process_group(backend='{backend}') across Linux (Rank 0) + WSL2 (Rank 1)...")
        dist_code = (
            "import os, sys, datetime, torch, torch.distributed as dist\n"
            "rank = int(sys.argv[1])\n"
            "backend = sys.argv[2]\n"
            "os.environ['MASTER_ADDR'] = '10.42.0.1'\n"
            "os.environ['MASTER_PORT'] = '29500'\n"
            "os.environ['NCCL_IB_DISABLE'] = '1'\n"
            "os.environ['NCCL_DEBUG'] = 'INFO'\n"
            "os.environ['NCCL_DEBUG_SUBSYS'] = 'INIT,NET'\n"
            "os.environ['TORCH_NCCL_BLOCKING_WAIT'] = '1'\n"
            "print(f'[Rank {rank}] Initializing {backend}...', flush=True)\n"
            "dist.init_process_group(backend=backend, rank=rank, world_size=2, timeout=datetime.timedelta(seconds=8))\n"
            "t = torch.ones(4, device='cuda' if backend=='nccl' else 'cpu') * (rank + 1)\n"
            "dist.all_reduce(t)\n"
            "print(f'[Rank {rank}] SUCCESS all_reduce result: {t.tolist()}', flush=True)\n"
            "dist.destroy_process_group()\n"
        )
        b64_dist = base64.b64encode(dist_code.encode()).decode()
        r0_env = os.environ.copy()
        r0_env["GLOO_SOCKET_IFNAME"] = "enp133s0"
        r0_env["NCCL_SOCKET_IFNAME"] = "enp133s0"
        r0_proc = subprocess.Popen(
            [str(VLLM_PYTHON), "-c", dist_code, "0", backend],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=r0_env,
        )
        r1_cmd = (
            f'{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -c '
            f'"GLOO_SOCKET_IFNAME=eth0 NCCL_SOCKET_IFNAME=eth0 timeout 12s /opt/venv-vllm/bin/python -c \'import base64; exec(base64.b64decode(\\"{b64_dist}\\"))\' 1 {backend}"'
        )
        try:
            cp_r1 = ssh_win(r1_cmd, timeout=20)
            r1_out = cp_r1.stdout + "\n" + cp_r1.stderr
        except Exception as exc:
            r1_out = f"SSH/Timeout Exception: {exc}"

        try:
            r0_out, _ = r0_proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            r0_proc.kill()
            r0_out, _ = r0_proc.communicate()

        log_msg(f"[V4.2][{backend}] Rank 0 (Linux) output:\n{r0_out.strip()}")
        log_msg(f"[V4.2][{backend}] Rank 1 (WSL2) output:\n{r1_out.strip()}")
        append_jsonl(out_jsonl, {
            "phase": "V4.2",
            "test": f"torch_distributed_{backend}",
            "rank0_output": r0_out.strip()[-2000:],
            "rank1_output": r1_out.strip()[-2000:],
            "success": ("SUCCESS all_reduce" in r0_out and "SUCCESS all_reduce" in r1_out),
        })

    # Bước 3: Thử nghiệm vLLM multi-node (--nnodes 2 --node-rank 0/1 --master-addr 10.42.0.1 --master-port 29505 --pipeline-parallel-size 2)
    # với Qwen3-0.6B để ghi nhận chính xác điểm nghẽn của vLLM 0.32.0 khi chạy qua NAT WSL2 trên Windows 10
    log_msg("\n[V4.3] Testing vLLM --nnodes 2 --pipeline-parallel-size 2 across Linux (node-rank 0) + WSL2 (node-rank 1)...")
    vllm_r0_env = os.environ.copy()
    vllm_r0_env["TMPDIR"] = "/tmp"
    vllm_r0_env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    vllm_r0_env["GLOO_SOCKET_IFNAME"] = "enp133s0"
    vllm_r0_env["NCCL_SOCKET_IFNAME"] = "enp133s0"
    vllm_r0_env["NCCL_IB_DISABLE"] = "1"
    vllm_r0_env["NCCL_DEBUG"] = "INFO"
    vllm_r0_env["NCCL_DEBUG_SUBSYS"] = "INIT,NET"
    vllm_r0_cmd = [
        str(VLLM_PYTHON),
        "-m", "vllm.entrypoints.openai.api_server",
        "--model", str(QWEN06B_DIR),
        "--served-model-name", "aicf",
        "--host", "0.0.0.0",
        "--port", "18085",
        "--pipeline-parallel-size", "2",
        "--nnodes", "2",
        "--node-rank", "0",
        "--master-addr", LINUX_IP,
        "--master-port", "29505",
        "--distributed-executor-backend", "mp",
        "--max-model-len", "2048",
        "--gpu-memory-utilization", "0.50",
        "--enforce-eager",
    ]
    vllm_r0_proc = subprocess.Popen(
        vllm_r0_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=vllm_r0_env,
    )
    vllm_r1_cmd = (
        f'{WSL_EXE} -d Ubuntu-24.04 -u root -- bash -c "'
        f'TMPDIR=/tmp VLLM_USE_FLASHINFER_SAMPLER=0 GLOO_SOCKET_IFNAME=eth0 NCCL_SOCKET_IFNAME=eth0 '
        f'NCCL_IB_DISABLE=1 NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET '
        f'timeout 25s /opt/venv-vllm/bin/python -m vllm.entrypoints.openai.api_server '
        f'--model /opt/hf-models/Qwen3-0.6B --served-model-name aicf '
        f'--pipeline-parallel-size 2 --nnodes 2 --node-rank 1 '
        f'--master-addr {LINUX_IP} --master-port 29505 '
        f'--distributed-executor-backend mp --max-model-len 2048 '
        f'--gpu-memory-utilization 0.50 --enforce-eager --headless"'
    )
    try:
        cp_vllm_r1 = ssh_win(vllm_r1_cmd, timeout=35)
        vllm_r1_out = cp_vllm_r1.stdout + "\n" + cp_vllm_r1.stderr
    except Exception as exc:
        vllm_r1_out = f"SSH/Timeout Exception: {exc}"

    try:
        vllm_r0_out, _ = vllm_r0_proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        vllm_r0_proc.kill()
        vllm_r0_out, _ = vllm_r0_proc.communicate()

    log_msg(f"[V4.3] vLLM Node 0 (Linux) tail:\n{vllm_r0_out.strip()[-1500:]}")
    log_msg(f"[V4.3] vLLM Node 1 (WSL2) tail:\n{vllm_r1_out.strip()[-1500:]}")

    append_jsonl(out_jsonl, {
        "phase": "V4.3",
        "test": "vllm_nnodes2_pp2_mp",
        "node0_output": vllm_r0_out.strip()[-2000:],
        "node1_output": vllm_r1_out.strip()[-2000:],
    })

    # Dọn dẹp các rule portproxy thử nghiệm của V4
    stop_local_servers()
    ssh_win(f'{WSL_EXE} -d Ubuntu-24.04 -u root -- pkill -f "[v]llm.entrypoints.openai.api_server" || true')
    ssh_win("netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=15202")
    ssh_win('netsh advfirewall firewall delete rule name="AICF_P10_V4_TEST"')
    keepalive_proc.terminate()
    return False


def run_pha_v3_v5_llama_merged_baseline() -> None:
    """Đo đối chứng gộp 2 card llama.cpp (Mode C: RTX 3060 + RTX 5070 qua ggml-rpc-server 10.42.0.225:50052)
    bằng cùng p10_client.py để bảng so sánh gộp 2 card có đầy đủ số [ĐO] mới nhất.
    """
    print("\n=== ĐO ĐỐI CHỨNG GỘP 2 CARD LLAMA.CPP (MODE C: RTX 3060 + RTX 5070 RPC) ===", flush=True)
    out_jsonl = REPORTS_DIR / "p10_v3_llama_merged.jsonl"
    if out_jsonl.exists():
        out_jsonl.unlink()

    stop_local_servers()
    ssh_win("taskkill /F /IM llama-server.exe /T")
    ssh_win("taskkill /F /IM ggml-rpc-server.exe /T")
    time.sleep(2.0)
    rpc_log = LOGS_DIR / "p10_v3_win_rpc_server.log"
    rpc_lf = rpc_log.open("w", encoding="utf-8")
    rpc_proc = subprocess.Popen(
        [
            "ssh", "-o", "BatchMode=yes", WIN_HOST,
            r"C:\aicf\llama\ggml-rpc-server.exe -H 0.0.0.0 -p 50052 -d CUDA0 -c",
        ],
        stdout=rpc_lf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"[Merged][RPC] Started ggml-rpc-server.exe on {WIN_IP}:50052 (local ssh pid={rpc_proc.pid})", flush=True)
    time.sleep(4.0)

    base_url = f"http://127.0.0.1:{PORT}"
    log_merged = LOGS_DIR / "p10_v3_llama_merged_qwen30b.log"
    env_llama = os.environ.copy()
    env_llama["LD_LIBRARY_PATH"] = f"{LLAMA_LIB_DIR}:{env_llama.get('LD_LIBRARY_PATH', '')}"

    try:
        # 1. Single-user decode (7 reps) + prefill 2k, 8k, 24k (3 reps) ở ctx 24576 (-ts 52,48)
        cmd_single = [
            str(LLAMA_SERVER_BIN),
            "-m", str(MOE_GGUF_MODEL),
            "--alias", "aicf",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--rpc", f"{WIN_IP}:50052",
            "-ngl", "99",
            "--split-mode", "layer",
            "--tensor-split", "52,48",
            "-fa", "on",
            "-ctk", "f16",
            "-ctv", "f16",
            "--fit", "off",
            "-c", "24576",
            "-np", "1",
            "-t", "8",
            "-Cr", "0-7",
            "--cpu-strict", "1",
        ]
        print(f"[Merged][llama.cpp] CMD: {' '.join(cmd_single)}", flush=True)
        with log_merged.open("w", encoding="utf-8") as lf:
            proc = subprocess.Popen(cmd_single, stdout=lf, stderr=subprocess.STDOUT, env=env_llama)
            try:
                startup_s = wait_for_server(base_url, timeout_s=240.0, proc=proc)
                dec = p10_client.run_decode_series(
                    base_url=base_url, model="aicf", cfg_id="merged_qwen30b_single", reps=7, burn_in=1, max_tokens=256, seed=42
                )
                append_jsonl(out_jsonl, {
                    "phase": "V3_V5_Merged_Baseline",
                    "host": "pc-001 + pc-win-01 (RPC)",
                    "engine": "llama.cpp (2-card RPC Mode C)",
                    "workload": "decode_1user",
                    "model_file": str(MOE_GGUF_MODEL),
                    "startup_s": startup_s,
                    "cmd": " ".join(cmd_single),
                    "gpu_linux_after": get_linux_gpu_metrics(),
                    "gpu_win_after": get_win_gpu_metrics(),
                    "result": dec,
                })
                print(f"[Merged][llama.cpp] decode_1user: {dec['decode_tok_s']} tok/s", flush=True)

                for label, p_tok in [("2k", 2048), ("8k", 8192), ("24k", 23000)]:
                    pf = p10_client.run_prefill_series(
                        base_url=base_url, model="aicf", cfg_id="merged_qwen30b_single", target_prompt_tokens=p_tok, reps=3, burn_in=1, max_tokens=16, seed=42
                    )
                    append_jsonl(out_jsonl, {
                        "phase": "V3_V5_Merged_Baseline",
                        "host": "pc-001 + pc-win-01 (RPC)",
                        "engine": "llama.cpp (2-card RPC Mode C)",
                        "workload": f"prefill_{label}",
                        "model_file": str(MOE_GGUF_MODEL),
                        "cmd": " ".join(cmd_single),
                        "gpu_linux_after": get_linux_gpu_metrics(),
                        "gpu_win_after": get_win_gpu_metrics(),
                        "result": pf,
                    })
                    print(
                        f"[Merged][llama.cpp] prefill_{label}: TTFT={pf['ttft_ms']} ms | prefill_tok_s={pf['prefill_tok_s']}",
                        flush=True,
                    )
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except Exception:
                    proc.kill()
                stop_local_servers()

        # 2. Multi-user C = 1, 4, 8, 16 trên gộp 2 card
        for c in [1, 4, 8, 16]:
            ctx_c = max(4096, 2048 * c)
            cmd_mu = [
                str(LLAMA_SERVER_BIN),
                "-m", str(MOE_GGUF_MODEL),
                "--alias", "aicf",
                "--host", "127.0.0.1",
                "--port", str(PORT),
                "--rpc", f"{WIN_IP}:50052",
                "-ngl", "99",
                "--split-mode", "layer",
                "--tensor-split", "52,48",
                "-fa", "on",
                "-ctk", "f16",
                "-ctv", "f16",
                "--fit", "off",
                "-c", str(ctx_c),
                "-np", str(c),
                "-t", "8",
                "-Cr", "0-7",
                "--cpu-strict", "1",
            ]
            with log_merged.open("a", encoding="utf-8") as lf:
                proc = subprocess.Popen(cmd_mu, stdout=lf, stderr=subprocess.STDOUT, env=env_llama)
                try:
                    startup_s = wait_for_server(base_url, timeout_s=240.0, proc=proc)
                    mu = p10_client.run_multiuser_batch(
                        base_url=base_url, model="aicf", cfg_id=f"merged_qwen30b_c{c}", concurrency=c, prompt_tokens=512, max_tokens=128, seed=42
                    )
                    append_jsonl(out_jsonl, {
                        "phase": "V3_V5_Merged_Baseline",
                        "host": "pc-001 + pc-win-01 (RPC)",
                        "engine": "llama.cpp (2-card RPC Mode C)",
                        "workload": f"multi_user_c{c}",
                        "model_file": str(MOE_GGUF_MODEL),
                        "startup_s": startup_s,
                        "cmd": " ".join(cmd_mu),
                        "gpu_linux_after": get_linux_gpu_metrics(),
                        "gpu_win_after": get_win_gpu_metrics(),
                        "result": mu,
                    })
                    print(
                        f"[Merged][llama.cpp] multi_user_c{c}: agg={mu['aggregate_wall_tok_s']} tok/s "
                        f"| min_user={mu['min_user_tok_s']} tok/s | ttft_p95={mu['ttft_p95_ms']} ms",
                        flush=True,
                    )
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except Exception:
                        proc.kill()
                    stop_local_servers()
    finally:
        rpc_proc.terminate()
        rpc_lf.close()
        ssh_win("taskkill /F /IM ggml-rpc-server.exe /T")


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    v25_file = REPORTS_DIR / "p10_v25_moe_single_card.jsonl"
    if not v25_file.exists() or len(v25_file.read_text(encoding="utf-8").splitlines()) < 4:
        run_pha_v25_moe_single_card()
    else:
        print("\n=== Skipping Pha V2.5 (already recorded in p10_v25_moe_single_card.jsonl) ===", flush=True)
    run_pha_v4_spike()
    run_pha_v3_v5_llama_merged_baseline()
    print("\n=== HOÀN TẤT PHA V2.5, V3, V4 ===", flush=True)


if __name__ == "__main__":
    main()
