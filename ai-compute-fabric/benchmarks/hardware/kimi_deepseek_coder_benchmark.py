#!/usr/bin/env python3
"""
Multi-Mode Hardware Benchmark Runner for Kimi & DeepSeek Coding/Reasoning Models
Evaluates across AI Compute Fabric Execution Modes (Section 4 & 5.2 of AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx):
  - Mode A: LOCAL_REPLICA ("split" / 1-GPU 12GB VRAM on pc-001 RTX 3060)
  - Mode B: LOCAL_HYBRID ("smart_1gpu" / 1-GPU + CPU MoE -ncmoe on pc-001) [for MoE models]
  - Mode C: DISTRIBUTED_MODEL ("merged" / 2-GPU 24GB VRAM across pc-001 RTX 3060 + pc-win-01 RTX 5070 via 2.5GbE RPC)

Models Benchmarked:
  1. Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf (5.38 GiB, arch: qwen35 hybrid recurrent+attention, Kimi-k3 reasoning/coding distill)
  2. deepseek-coder-6.7b-instruct.Q4_K_M.gguf (3.80 GiB, arch: llama, DeepSeek specialized coding model)
  3. DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf (8.37 GiB, arch: qwen2, DeepSeek-R1 reasoning/coding distill)
  4. DeepSeek-V2-Lite-Chat.Q3_K_M.gguf (7.58 GiB, arch: deepseek2 MLA + MoE 15.7B total / 2.4B active)
  (+ Reference comparison with Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf from docx_coder_30b_benchmark.json)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric")
MODELS_DIR = ROOT_DIR / ".demo" / "models"
ENGINE_LOG_PATH = ROOT_DIR / ".demo" / "logs" / "engine-pc001.log"
HUMANEVAL_PATH = ROOT_DIR / "benchmarks" / "data" / "HumanEval.jsonl"
REPORT_JSON_PATH = ROOT_DIR / "benchmarks" / "reports" / "kimi_deepseek_modes_benchmark.json"

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:18080")
ENGINE_DIRECT_URL = os.environ.get("ENGINE_DIRECT_URL", "http://127.0.0.1:18090")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "demo-admin-token")


def http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 240.0,
) -> Tuple[int, Any]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def inspect_gguf_identity(model_name: str) -> Dict[str, Any]:
    path = MODELS_DIR / model_name
    real_path = path.resolve()
    stat = real_path.stat()
    with open(real_path, "rb") as f:
        header = f.read(24)
        magic, version, tensor_count, kv_count = struct.unpack("<4sIQQ", header)
        h = hashlib.sha256()
        f.seek(0)
        h.update(f.read(8 * 1024 * 1024))
        fast_sha256 = h.hexdigest()
    return {
        "model_name": model_name,
        "resolved_path": str(real_path),
        "size_bytes": stat.st_size,
        "size_gib": round(stat.st_size / (1024**3), 3),
        "gguf_version": version,
        "tensor_count": tensor_count,
        "kv_count": kv_count,
        "fast_sha256_8mib": fast_sha256,
    }


class GPUPowerSampler:
    def __init__(self, interval_s: float = 0.2):
        self.interval_s = interval_s
        self.samples: List[Dict[str, float]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=power.draw,temperature.gpu,utilization.gpu,memory.used,memory.free",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    timeout=2.0,
                ).strip()
                parts = [p.strip() for p in out.split(",")]
                if len(parts) >= 5:
                    self.samples.append(
                        {
                            "power_w": float(parts[0]),
                            "temp_c": float(parts[1]),
                            "util_pct": float(parts[2]),
                            "mem_used_mib": float(parts[3]),
                            "mem_free_mib": float(parts[4]),
                        }
                    )
            except Exception:
                pass
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self.samples.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if not self.samples:
            return {"sample_count": 0, "mean_power_w": 0.0, "peak_power_w": 0.0}
        powers = [s["power_w"] for s in self.samples]
        temps = [s["temp_c"] for s in self.samples]
        utils = [s["util_pct"] for s in self.samples]
        return {
            "sample_count": len(self.samples),
            "mean_power_w": round(statistics.mean(powers), 2),
            "peak_power_w": round(max(powers), 2),
            "mean_temp_c": round(statistics.mean(temps), 1),
            "mean_util_pct": round(statistics.mean(utils), 1),
        }


def parse_engine_log_buffers(log_path: Path) -> Dict[str, Any]:
    if not log_path.exists():
        return {}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    idx = text.rfind("loading model tensors")
    chunk = text[idx:] if idx >= 0 else text[-12000:]

    def find_float(pattern: str) -> float:
        m = re.findall(pattern, chunk)
        return float(m[-1]) if m else 0.0

    offloaded_m = re.findall(r"offloaded (\d+/\d+) layers to GPU", chunk)
    return {
        "offloaded_layers": offloaded_m[-1] if offloaded_m else "unknown",
        "cuda0_model_mib": find_float(r"CUDA0 model buffer size\s*=\s*([\d.]+)\s*MiB"),
        "rpc0_model_mib": find_float(r"RPC0\[[^\]]+\] model buffer size\s*=\s*([\d.]+)\s*MiB"),
        "cpu_mapped_model_mib": find_float(r"CPU_Mapped model buffer size\s*=\s*([\d.]+)\s*MiB"),
        "cuda0_kv_mib": find_float(r"CUDA0 KV buffer size\s*=\s*([\d.]+)\s*MiB"),
        "rpc0_kv_mib": find_float(r"RPC0\[[^\]]+\] KV buffer size\s*=\s*([\d.]+)\s*MiB"),
        "cuda0_rs_mib": find_float(r"CUDA0 RS buffer size\s*=\s*([\d.]+)\s*MiB"),
        "rpc0_rs_mib": find_float(r"RPC0\[[^\]]+\] RS buffer size\s*=\s*([\d.]+)\s*MiB"),
        "cuda0_compute_mib": find_float(r"CUDA0 compute buffer size\s*=\s*([\d.]+)\s*MiB"),
        "rpc0_compute_mib": find_float(r"RPC0\[[^\]]+\] compute buffer size\s*=\s*([\d.]+)\s*MiB"),
        "cpu_compute_mib": find_float(r"CPU compute buffer size\s*=\s*([\d.]+)\s*MiB"),
    }


def ensure_group_dissolved_for_single_gpu() -> None:
    """If currently in merged mode, dissolve the group cleanly using Qwen3-4B-Q4_K_M.gguf (present on both nodes)."""
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    _, topo = http_json(f"{GATEWAY_URL}/api/cluster/topology", headers=headers)
    if isinstance(topo, dict) and topo.get("groups"):
        print("  [Topology] Dissolving merged group to split mode via Qwen3-4B-Q4_K_M.gguf...", flush=True)
        status, resp = http_json(
            f"{GATEWAY_URL}/api/cluster/topology",
            method="POST",
            payload={
                "mode": "split",
                "primary_worker_id": "pc-001",
                "rpc_worker_ids": ["pc-win-01"],
                "model": "Qwen3-4B-Q4_K_M.gguf",
                "kv_type": "f16",
            },
            headers=headers,
            timeout=180.0,
        )
        if status != 200:
            raise RuntimeError(f"Failed to dissolve group ({status}): {resp}")


def switch_mode_and_model(
    mode: str,
    model_name: str,
    tensor_split: str = "48,52",
    n_cpu_moe: int = 0,
    kv_type: str = "f16",
) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    if mode in ("split", "smart_1gpu"):
        ensure_group_dissolved_for_single_gpu()
        payload: Dict[str, Any] = {
            "mode": mode,
            "primary_worker_id": "pc-001",
            "rpc_worker_ids": [],
            "model": model_name,
            "kv_type": kv_type,
        }
        if mode == "smart_1gpu":
            payload["n_cpu_moe"] = n_cpu_moe
    elif mode == "merged":
        payload = {
            "mode": "merged",
            "primary_worker_id": "pc-001",
            "rpc_worker_ids": ["pc-win-01"],
            "model": model_name,
            "kv_type": kv_type,
            "tensor_split": tensor_split,
            "rpc_port": 50052,
        }
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    t0 = time.perf_counter()
    status, resp = http_json(
        f"{GATEWAY_URL}/api/cluster/topology",
        method="POST",
        payload=payload,
        headers=headers,
        timeout=240.0,
    )
    if status != 200:
        raise RuntimeError(f"Switch to {mode}/{model_name} failed ({status}): {resp}")

    deadline = time.time() + 180.0
    while time.time() < deadline:
        h_status, h_resp = http_json(f"{ENGINE_DIRECT_URL}/health", timeout=5.0)
        if h_status == 200 and isinstance(h_resp, dict) and h_resp.get("status") == "ok":
            break
        time.sleep(0.5)

    cold_load_s = round(time.perf_counter() - t0, 3)
    time.sleep(1.2)
    _, workers = http_json(f"{GATEWAY_URL}/api/workers", headers=headers)
    buffers = parse_engine_log_buffers(ENGINE_LOG_PATH)

    w_map = {w["worker_id"]: w for w in (workers if isinstance(workers, list) else [])}
    return {
        "mode": mode,
        "model": model_name,
        "tensor_split": tensor_split if mode == "merged" else "",
        "n_cpu_moe": n_cpu_moe if mode == "smart_1gpu" else 0,
        "cold_load_seconds": cold_load_s,
        "llama_log_buffers": buffers,
        "pc001_vram_free_mib": round(w_map.get("pc-001", {}).get("vram_free_bytes", 0) / (1024**2), 1),
        "pcwin01_vram_free_mib": round(w_map.get("pc-win-01", {}).get("vram_free_bytes", 0) / (1024**2), 1),
        "pc001_buckets": w_map.get("pc-001", {}).get("buckets", {}),
    }


def stream_direct_engine(
    model_name: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 320,
    temperature: float = 0.1,
) -> Dict[str, Any]:
    """Queries pc-001 llama-server (port 18090) directly so single-GPU vs 2-GPU modes are compared without multi-worker round-robin noise."""
    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "cache_prompt": False,
    }
    req = urllib.request.Request(
        f"{ENGINE_DIRECT_URL}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    t_start = time.perf_counter()
    t_first_token: Optional[float] = None
    t_last_token: Optional[float] = None
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    chunk_count = 0
    timings: Dict[str, Any] = {}
    usage: Dict[str, Any] = {}

    with urllib.request.urlopen(req, timeout=180.0) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            now = time.perf_counter()
            if "timings" in chunk and isinstance(chunk["timings"], dict):
                timings = chunk["timings"]
            if "usage" in chunk and isinstance(chunk["usage"], dict):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                c_piece = delta.get("content") or ""
                r_piece = delta.get("reasoning_content") or ""
                if c_piece or r_piece:
                    if t_first_token is None:
                        t_first_token = now
                    t_last_token = now
                    if c_piece:
                        content_parts.append(c_piece)
                    if r_piece:
                        reasoning_parts.append(r_piece)
                    chunk_count += 1

    t_end = time.perf_counter()
    if t_first_token is None:
        t_first_token = t_end
    if t_last_token is None:
        t_last_token = t_end

    content_text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    combined_text = (f"<think>\n{reasoning_text}\n</think>\n" if reasoning_text else "") + content_text

    prompt_tokens = timings.get("prompt_n") or usage.get("prompt_tokens") or 0
    output_tokens = timings.get("predicted_n") or usage.get("completion_tokens") or chunk_count
    prompt_ms = timings.get("prompt_ms") or ((t_first_token - t_start) * 1000.0)
    predicted_ms = timings.get("predicted_ms") or ((t_last_token - t_first_token) * 1000.0)

    ttft_ms = (t_first_token - t_start) * 1000.0
    total_ms = (t_end - t_start) * 1000.0
    tpot_ms = ((t_last_token - t_first_token) * 1000.0) / max(1, output_tokens - 1)
    decode_tok_s = timings.get("predicted_per_second") or (
        (output_tokens / (predicted_ms / 1000.0)) if predicted_ms > 0 else 0.0
    )
    prefill_tok_s = timings.get("prompt_per_second") or (
        (prompt_tokens / (prompt_ms / 1000.0)) if prompt_ms > 0 else 0.0
    )

    return {
        "prompt_tokens": int(prompt_tokens),
        "output_tokens": int(output_tokens),
        "ttft_ms": round(ttft_ms, 2),
        "tpot_ms": round(tpot_ms, 2),
        "t_prefill_ms": round(prompt_ms, 2),
        "t_decode_ms": round(predicted_ms, 2),
        "t_total_ms": round(total_ms, 2),
        "prefill_tok_s": round(prefill_tok_s, 2),
        "decode_tok_s": round(decode_tok_s, 2),
        "has_thinking": bool(reasoning_text or "<think>" in content_text),
        "content_text": content_text,
        "combined_text": combined_text,
    }


def extract_python_code(content_text: str, combined_text: str, entry_point: str) -> str:
    # Strip <think>...</think> first if present in content_text
    cleaned = re.sub(r"<think>.*?</think>", "", content_text, flags=re.DOTALL).strip()
    for candidate_src in (cleaned, content_text, combined_text):
        fence_matches = re.findall(r"```(?:python)?\s*\n(.*?)```", candidate_src, flags=re.DOTALL)
        if fence_matches:
            for block in fence_matches:
                if f"def {entry_point}" in block:
                    return block.strip()
            return fence_matches[-1].strip()
        # Handle unclosed ```python fence
        if "```python" in candidate_src:
            tail = candidate_src.split("```python")[-1]
            return tail.replace("```", "").strip()
    return cleaned


def run_benchmark_for_config(
    model_name: str,
    mode: str,
    tensor_split: str = "48,52",
    n_cpu_moe: int = 0,
    is_reasoning_model: bool = False,
) -> Dict[str, Any]:
    print(f"\n>>> Benchmarking {model_name} | Mode={mode} (split={tensor_split}, ncmoe={n_cpu_moe}) <<<", flush=True)
    deploy = switch_mode_and_model(
        mode=mode,
        model_name=model_name,
        tensor_split=tensor_split,
        n_cpu_moe=n_cpu_moe,
        kv_type="f16",
    )
    print(
        f"  Loaded in {deploy['cold_load_seconds']}s | Offloaded={deploy['llama_log_buffers'].get('offloaded_layers')} | "
        f"CUDA0={deploy['llama_log_buffers'].get('cuda0_model_mib')} MiB | RPC0={deploy['llama_log_buffers'].get('rpc0_model_mib')} MiB",
        flush=True,
    )

    bench_messages = [
        {
            "role": "system",
            "content": "You are an expert software engineer. Write clean Python 3 code. Return code directly.",
        },
        {
            "role": "user",
            "content": (
                "Implement a thread-safe `LRUCacheWithTTL` class in Python using `collections.OrderedDict` and `threading.RLock` "
                "with `get(key)`, `put(key, value, ttl_seconds=None)`, and `evict_expired()`."
            ),
        },
    ]

    # 1 warmup + 3 measured runs with cache_prompt=False so every run measures true Prefill + Decode
    print("  [Warmup] Running...", flush=True)
    _ = stream_direct_engine(model_name, bench_messages, max_tokens=192, temperature=0.1)

    sampler = GPUPowerSampler(interval_s=0.2)
    sampler.start()
    runs: List[Dict[str, Any]] = []
    for i in range(3):
        res = stream_direct_engine(model_name, bench_messages, max_tokens=256, temperature=0.1)
        runs.append({k: v for k, v in res.items() if k not in ("content_text", "combined_text")})
        print(
            f"  [Run {i+1}/3] TTFT={res['ttft_ms']}ms | TPOT={res['tpot_ms']}ms | "
            f"Prefill={res['prefill_tok_s']} tok/s | Decode={res['decode_tok_s']} tok/s",
            flush=True,
        )
    power = sampler.stop()

    mean_decode = round(statistics.mean([r["decode_tok_s"] for r in runs]), 2)
    mean_prefill = round(statistics.mean([r["prefill_tok_s"] for r in runs]), 2)
    mean_ttft = round(statistics.mean([r["ttft_ms"] for r in runs]), 2)
    mean_tpot = round(statistics.mean([r["tpot_ms"] for r in runs]), 2)

    pc001_w = power.get("mean_power_w", 0.0)
    rpc_w = 85.0 if mode == "merged" else 0.0
    total_w = round(pc001_w + rpc_w, 2)
    joule_per_tok = round(total_w / mean_decode, 4) if mean_decode > 0 else 0.0

    # Evaluate 8 canonical HumanEval coding problems in Sandbox
    target_ids = [
        "HumanEval/0",
        "HumanEval/1",
        "HumanEval/2",
        "HumanEval/3",
        "HumanEval/4",
        "HumanEval/31",
        "HumanEval/45",
        "HumanEval/55",
    ]
    problems: List[Dict[str, Any]] = []
    with open(HUMANEVAL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row["task_id"] in target_ids:
                problems.append(row)

    heval_tasks: List[Dict[str, Any]] = []
    passed_count = 0
    heval_max_tokens = 896 if is_reasoning_model else 384

    for prob in problems:
        task_id = prob["task_id"]
        entry_point = prob["entry_point"]
        prompt = prob["prompt"]
        test_code = prob["test"]

        sys_prompt = (
            "You are an expert Python programmer. Complete the requested function. "
            "Be concise and output ONLY a single ```python ... ``` block containing the complete function."
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Complete this Python function:\n\n```python\n{prompt}\n```"},
        ]
        res = stream_direct_engine(model_name, messages, max_tokens=heval_max_tokens, temperature=0.0)
        extracted = extract_python_code(res["content_text"], res["combined_text"], entry_point)
        if f"def {entry_point}" not in extracted:
            full_prog = prompt + "\n" + extracted + "\n\n" + test_code + f"\ncheck({entry_point})\n"
        else:
            full_prog = "from typing import *\nimport math\n\n" + extracted + "\n\n" + test_code + f"\ncheck({entry_point})\n"

        passed = False
        err_msg = ""
        with tempfile.TemporaryDirectory(prefix="aicf_kd_") as tmpdir:
            sp = Path(tmpdir) / "sol.py"
            sp.write_text(full_prog, encoding="utf-8")
            try:
                proc = subprocess.run([sys.executable, "-I", str(sp)], cwd=tmpdir, capture_output=True, text=True, timeout=5.0)
                if proc.returncode == 0:
                    passed = True
                    passed_count += 1
                else:
                    err_msg = (proc.stderr or proc.stdout).strip()[-200:]
            except subprocess.TimeoutExpired:
                err_msg = "TimeoutExpired (5s)"

        heval_tasks.append(
            {
                "task_id": task_id,
                "entry_point": entry_point,
                "passed": passed,
                "output_tokens": res["output_tokens"],
                "decode_tok_s": res["decode_tok_s"],
                "total_ms": res["t_total_ms"],
                "has_thinking": res["has_thinking"],
                "error": err_msg,
            }
        )

    pass_rate = round(100.0 * passed_count / len(problems), 2)
    mean_task_ms = round(statistics.mean([t["total_ms"] for t in heval_tasks]), 2)
    mean_task_tokens = round(statistics.mean([t["output_tokens"] for t in heval_tasks]), 1)
    print(
        f"  [HumanEval 8-Task] Pass={passed_count}/{len(problems)} ({pass_rate}%) | "
        f"Mean Tokens/Task={mean_task_tokens} | Mean Task Time={mean_task_ms}ms",
        flush=True,
    )

    return {
        "deployment": deploy,
        "latency_throughput": {
            "runs": runs,
            "mean_ttft_ms": mean_ttft,
            "mean_tpot_ms": mean_tpot,
            "mean_prefill_tok_s": mean_prefill,
            "mean_decode_tok_s": mean_decode,
        },
        "power_energy": {
            "pc001_rtx3060": power,
            "estimated_rpc_gpu_w": rpc_w,
            "total_gpu_power_w": total_w,
            "joule_per_token": joule_per_tok,
            "wh_per_1m_tokens": round(joule_per_tok * 1_000_000 / 3600.0, 2),
        },
        "humaneval_8_tasks": {
            "passed": passed_count,
            "total": len(problems),
            "pass_rate_pct": pass_rate,
            "mean_output_tokens_per_task": mean_task_tokens,
            "mean_wall_time_ms_per_task": mean_task_ms,
            "joule_per_coding_task": round(total_w * (mean_task_ms / 1000.0), 2),
            "tasks": heval_tasks,
        },
    }


def main() -> None:
    models_meta = {
        "deepseek-coder-6.7b-instruct.Q4_K_M.gguf": inspect_gguf_identity("deepseek-coder-6.7b-instruct.Q4_K_M.gguf"),
        "Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf": inspect_gguf_identity("Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf"),
        "DeepSeek-V2-Lite-Chat.Q3_K_M.gguf": inspect_gguf_identity("DeepSeek-V2-Lite-Chat.Q3_K_M.gguf"),
        "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf": inspect_gguf_identity("DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf"),
    }

    experiments: List[Dict[str, Any]] = []

    # 1. DeepSeek-Coder-6.7B-Instruct (Dense 6.7B specialized coder)
    experiments.append(
        run_benchmark_for_config(
            model_name="deepseek-coder-6.7b-instruct.Q4_K_M.gguf",
            mode="split",
            is_reasoning_model=False,
        )
    )
    experiments.append(
        run_benchmark_for_config(
            model_name="deepseek-coder-6.7b-instruct.Q4_K_M.gguf",
            mode="merged",
            tensor_split="48,52",
            is_reasoning_model=False,
        )
    )

    # 2. Qwen3.5-9B-Kimi-k3-Distilled (Kimi-k3 Distilled 9B hybrid recurrent+attention)
    experiments.append(
        run_benchmark_for_config(
            model_name="Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf",
            mode="split",
            is_reasoning_model=True,
        )
    )
    experiments.append(
        run_benchmark_for_config(
            model_name="Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf",
            mode="merged",
            tensor_split="48,52",
            is_reasoning_model=True,
        )
    )

    # 3. DeepSeek-R1-Distill-Qwen-14B (Dense 14.8B reasoning + coding distill)
    experiments.append(
        run_benchmark_for_config(
            model_name="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
            mode="split",
            is_reasoning_model=True,
        )
    )
    experiments.append(
        run_benchmark_for_config(
            model_name="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
            mode="merged",
            tensor_split="48,52",
            is_reasoning_model=True,
        )
    )

    # 4. DeepSeek-V2-Lite-Chat 16B-A2.4B (MoE + MLA architecture deepseek2, 3 modes!)
    experiments.append(
        run_benchmark_for_config(
            model_name="DeepSeek-V2-Lite-Chat.Q3_K_M.gguf",
            mode="split",
            is_reasoning_model=False,
        )
    )
    experiments.append(
        run_benchmark_for_config(
            model_name="DeepSeek-V2-Lite-Chat.Q3_K_M.gguf",
            mode="smart_1gpu",
            n_cpu_moe=10,
            is_reasoning_model=False,
        )
    )
    experiments.append(
        run_benchmark_for_config(
            model_name="DeepSeek-V2-Lite-Chat.Q3_K_M.gguf",
            mode="merged",
            tensor_split="48,52",
            is_reasoning_model=False,
        )
    )

    # Restore cluster to strongest production default: Qwen3-Coder-30B-A3B-Instruct in merged mode
    print("\n>>> Restoring Cluster to Production Default: Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf (merged 48:52) <<<", flush=True)
    switch_mode_and_model(
        mode="merged",
        model_name="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        tensor_split="48,52",
        kv_type="f16",
    )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models_identity": models_meta,
        "experiments": experiments,
    }
    REPORT_JSON_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== ALL KIMI & DEEPSEEK BENCHMARKS COMPLETED! Saved to {REPORT_JSON_PATH} ===", flush=True)


if __name__ == "__main__":
    main()

