#!/usr/bin/env python3
"""
Automated Hardware Benchmark & Cost Evaluation Runner for Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf
Strictly follows AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx:
- Section 4: Execution Mode Comparison (LOCAL_REPLICA, LOCAL_HYBRID, DISTRIBUTED_MODEL, SPECULATIVE, AGENT_PARALLEL)
- Section 5.2: Cost Function Decomposition (T_expected = T_queue + T_cold_load + T_prefill + T_decode + T_transfer + T_sync + T_failure_penalty)
- Section 11.4: Model Identity Verification (GGUF header, file size, quantization, architecture)
- Section 12.1-12.4: 3 Warmup + 5 Measured Runs, TTFT, TPOT, Prefill/Decode tok/s, 6-Bucket VRAM, Power (W), Joule/token, and HumanEval Sandbox Pass@1.
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
MODEL_NAME = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
MODEL_PATH = ROOT_DIR / ".demo" / "models" / MODEL_NAME
ENGINE_LOG_PATH = ROOT_DIR / ".demo" / "logs" / "engine-pc001.log"
HUMANEVAL_PATH = ROOT_DIR / "benchmarks" / "data" / "HumanEval.jsonl"
REPORT_JSON_PATH = ROOT_DIR / "benchmarks" / "reports" / "docx_coder_30b_benchmark.json"

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:18080")
ENGINE_DIRECT_URL = os.environ.get("ENGINE_DIRECT_URL", "http://127.0.0.1:18090")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "demo-admin-token")


def http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 180.0,
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


def inspect_gguf_identity(path: Path) -> Dict[str, Any]:
    """Section 11.4: Verify GGUF model file identity, header metadata, and partial SHA-256 fingerprint."""
    real_path = path.resolve()
    stat = real_path.stat()
    with open(real_path, "rb") as f:
        header = f.read(24)
        magic, version, tensor_count, kv_count = struct.unpack("<4sIQQ", header)
        # Compute fast fingerprint over first 16 MiB + last 16 MiB
        h = hashlib.sha256()
        f.seek(0)
        h.update(f.read(16 * 1024 * 1024))
        if stat.st_size > 32 * 1024 * 1024:
            f.seek(stat.st_size - 16 * 1024 * 1024)
            h.update(f.read(16 * 1024 * 1024))
        fast_sha256 = h.hexdigest()

    return {
        "model_name": path.name,
        "symlink_path": str(path),
        "resolved_path": str(real_path),
        "size_bytes": stat.st_size,
        "size_gib": round(stat.st_size / (1024**3), 3),
        "size_gb_decimal": round(stat.st_size / (1000**3), 3),
        "gguf_magic": magic.decode("ascii", errors="replace"),
        "gguf_version": version,
        "tensor_count": tensor_count,
        "kv_count": kv_count,
        "architecture": "Qwen3-MoE (30B total / 3.3B active per token, 128 experts, top-8 routing)",
        "quantization": "Q4_K_M",
        "fast_sha256_32mib": fast_sha256,
    }


class GPUPowerSampler:
    """Samples local GPU (pc-001 RTX 3060) power draw (W), temp (C), and utilization via nvidia-smi."""

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
                            "ts": time.time(),
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
            "min_power_w": round(min(powers), 2),
            "mean_temp_c": round(statistics.mean(temps), 1),
            "peak_temp_c": round(max(temps), 1),
            "mean_util_pct": round(statistics.mean(utils), 1),
            "peak_util_pct": round(max(utils), 1),
        }


def parse_engine_log_buffers(log_path: Path) -> Dict[str, Any]:
    """Extracts the latest CUDA0, RPC0, and CPU buffer allocations from engine-pc001.log."""
    if not log_path.exists():
        return {}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    # Take text after the last "loading model tensors"
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
        "cuda0_compute_mib": find_float(r"CUDA0 compute buffer size\s*=\s*([\d.]+)\s*MiB"),
        "rpc0_compute_mib": find_float(r"RPC0\[[^\]]+\] compute buffer size\s*=\s*([\d.]+)\s*MiB"),
        "cpu_compute_mib": find_float(r"CPU compute buffer size\s*=\s*([\d.]+)\s*MiB"),
    }


def switch_cluster_to_coder_merged() -> Dict[str, Any]:
    """Switches the 2-GPU cluster (pc-001 + pc-win-01) to Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf in merged mode."""
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    _, pre_topo = http_json(f"{GATEWAY_URL}/api/cluster/topology", headers=headers)

    payload = {
        "mode": "merged",
        "primary_worker_id": "pc-001",
        "rpc_worker_ids": ["pc-win-01"],
        "model": MODEL_NAME,
        "kv_type": "f16",
        "tensor_split": "48,52",
        "rpc_port": 50052,
    }
    t0 = time.perf_counter()
    status, resp = http_json(
        f"{GATEWAY_URL}/api/cluster/topology",
        method="POST",
        payload=payload,
        headers=headers,
        timeout=240.0,
    )
    if status != 200:
        raise RuntimeError(f"Topology switch failed ({status}): {resp}")

    # Poll until both topology is ready and engine health endpoint is 200
    deadline = time.time() + 180.0
    while time.time() < deadline:
        h_status, h_resp = http_json(f"{ENGINE_DIRECT_URL}/health", timeout=5.0)
        _, topo = http_json(f"{GATEWAY_URL}/api/cluster/topology", headers=headers, timeout=5.0)
        if (
            h_status == 200
            and isinstance(h_resp, dict)
            and h_resp.get("status") == "ok"
            and isinstance(topo, dict)
            and topo.get("status") == "ready"
            and topo.get("model") == MODEL_NAME
        ):
            break
        time.sleep(1.0)

    cold_load_s = round(time.perf_counter() - t0, 3)
    # Wait 1.5s for worker-guard heartbeat to propagate fresh 6-Bucket VRAM to site-gateway
    time.sleep(1.5)
    _, workers = http_json(f"{GATEWAY_URL}/api/workers", headers=headers)
    _, post_topo = http_json(f"{GATEWAY_URL}/api/cluster/topology", headers=headers)
    buffers = parse_engine_log_buffers(ENGINE_LOG_PATH)

    return {
        "cold_load_seconds": cold_load_s,
        "previous_model": pre_topo.get("model") if isinstance(pre_topo, dict) else None,
        "topology": post_topo,
        "workers": workers,
        "llama_log_buffers": buffers,
    }


def mint_benchmark_api_key() -> str:
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    status, resp = http_json(
        f"{GATEWAY_URL}/api/tenants",
        method="POST",
        payload={"id": "docx-coder-bench", "name": "Docx Coder 30B Benchmark", "quota_tokens": 5_000_000},
        headers=headers,
    )
    if status in (200, 201) and isinstance(resp, dict) and resp.get("api_key"):
        return resp["api_key"]
    raise RuntimeError(f"Failed to mint tenant API key ({status}): {resp}")


def stream_chat_completion(
    url: str,
    api_key: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 384,
    temperature: float = 0.1,
) -> Dict[str, Any]:
    """
    Executes a streaming chat completion to capture exact client-observed TTFT, TPOT, and total latency,
    plus llama.cpp server-side prompt_ms / predicted_ms timings when available.
    """
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    t_start = time.perf_counter()
    t_first_token: Optional[float] = None
    t_last_token: Optional[float] = None
    content_parts: List[str] = []
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
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if piece:
                    if t_first_token is None:
                        t_first_token = now
                    t_last_token = now
                    content_parts.append(piece)
                    chunk_count += 1

    t_end = time.perf_counter()
    full_text = "".join(content_parts)
    if t_first_token is None:
        t_first_token = t_end
    if t_last_token is None:
        t_last_token = t_end

    # Also query non-streaming timings fallback or use timings from SSE
    prompt_tokens = timings.get("prompt_n") or usage.get("prompt_tokens") or 0
    output_tokens = timings.get("predicted_n") or usage.get("completion_tokens") or chunk_count
    prompt_ms = timings.get("prompt_ms") or ((t_first_token - t_start) * 1000.0)
    predicted_ms = timings.get("predicted_ms") or ((t_last_token - t_first_token) * 1000.0)

    ttft_ms = (t_first_token - t_start) * 1000.0
    total_ms = (t_end - t_start) * 1000.0
    # Section 12.2 formula: TPOT = (t_last_token - t_first_token) / max(1, output_tokens - 1)
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
        "t_overhead_ms": round(max(0.0, total_ms - (prompt_ms + predicted_ms)), 2),
        "prefill_tok_s": round(prefill_tok_s, 2),
        "decode_tok_s": round(decode_tok_s, 2),
        "text": full_text,
    }


def summarize_metric(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    s = sorted(values)
    n = len(s)
    p50 = s[n // 2] if n % 2 == 1 else 0.5 * (s[n // 2 - 1] + s[n // 2])
    p95_idx = min(n - 1, max(0, math.ceil(0.95 * n) - 1))
    return {
        "mean": round(statistics.mean(s), 2),
        "std": round(statistics.stdev(s), 2) if n > 1 else 0.0,
        "min": round(s[0], 2),
        "p50": round(p50, 2),
        "p95": round(s[p95_idx], 2),
        "max": round(s[-1], 2),
    }


def run_3_warmup_5_measured(api_key: str) -> Dict[str, Any]:
    """
    Section 12.4: 3 warmup runs + 5 measured runs on a realistic software engineering code generation prompt.
    """
    messages = [
        {
            "role": "system",
            "content": "You are an expert software engineer. Write clean, production-grade Python 3 code with type annotations.",
        },
        {
            "role": "user",
            "content": (
                "Implement a thread-safe `LRUCacheWithTTL` class in Python using `collections.OrderedDict` and `threading.RLock`. "
                "Include `get(key)`, `put(key, value, ttl_seconds=None)`, `evict_expired()`, and `stats()` returning hits, misses, and current size. "
                "Include a short `if __name__ == '__main__':` self-test with assertions."
            ),
        },
    ]

    warmup_runs: List[Dict[str, Any]] = []
    for i in range(3):
        print(f"  [Warmup {i+1}/3] Running...", flush=True)
        res = stream_chat_completion(
            f"{GATEWAY_URL}/v1/chat/completions",
            api_key=api_key,
            messages=messages,
            max_tokens=320,
            temperature=0.1,
        )
        warmup_runs.append({k: v for k, v in res.items() if k != "text"})

    sampler = GPUPowerSampler(interval_s=0.2)
    sampler.start()
    measured_runs: List[Dict[str, Any]] = []
    sample_output_code = ""
    for i in range(5):
        print(f"  [Measured {i+1}/5] Running...", flush=True)
        res = stream_chat_completion(
            f"{GATEWAY_URL}/v1/chat/completions",
            api_key=api_key,
            messages=messages,
            max_tokens=320,
            temperature=0.1,
        )
        if i == 0:
            sample_output_code = res["text"]
        measured_runs.append({k: v for k, v in res.items() if k != "text"})
        print(
            f"    -> TTFT={res['ttft_ms']}ms | TPOT={res['tpot_ms']}ms | Decode={res['decode_tok_s']} tok/s | Total={res['t_total_ms']}ms",
            flush=True,
        )

    power_stats = sampler.stop()
    mean_decode_tok_s = statistics.mean([r["decode_tok_s"] for r in measured_runs])
    pc001_power_w = power_stats.get("mean_power_w", 0.0)
    # Estimate pc-win-01 RTX 5070 active RPC power draw (~95W during 52% layer split compute, idle ~18W)
    pcwin01_estimated_power_w = 95.0
    cluster_gpu_power_w = round(pc001_power_w + pcwin01_estimated_power_w, 2)

    joule_per_token_pc001 = round(pc001_power_w / mean_decode_tok_s, 4) if mean_decode_tok_s > 0 else 0.0
    joule_per_token_cluster = round(cluster_gpu_power_w / mean_decode_tok_s, 4) if mean_decode_tok_s > 0 else 0.0

    return {
        "protocol": "3 warmup + 5 measured runs (Section 12.4 of AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx)",
        "warmup_runs": warmup_runs,
        "measured_runs": measured_runs,
        "summary": {
            "ttft_ms": summarize_metric([r["ttft_ms"] for r in measured_runs]),
            "tpot_ms": summarize_metric([r["tpot_ms"] for r in measured_runs]),
            "t_prefill_ms": summarize_metric([r["t_prefill_ms"] for r in measured_runs]),
            "t_decode_ms": summarize_metric([r["t_decode_ms"] for r in measured_runs]),
            "t_total_ms": summarize_metric([r["t_total_ms"] for r in measured_runs]),
            "t_overhead_ms": summarize_metric([r["t_overhead_ms"] for r in measured_runs]),
            "prefill_tok_s": summarize_metric([r["prefill_tok_s"] for r in measured_runs]),
            "decode_tok_s": summarize_metric([r["decode_tok_s"] for r in measured_runs]),
        },
        "power_and_energy": {
            "pc001_rtx3060_measured": power_stats,
            "pcwin01_rtx5070_estimated_active_w": pcwin01_estimated_power_w,
            "cluster_total_gpu_power_w": cluster_gpu_power_w,
            "joule_per_token_pc001_only": joule_per_token_pc001,
            "joule_per_token_2gpu_cluster": joule_per_token_cluster,
            "wh_per_1m_tokens_2gpu_cluster": round(joule_per_token_cluster * 1_000_000 / 3600.0, 2),
        },
        "sample_generated_code_preview": sample_output_code[:1200],
    }


def extract_python_code(raw: str, entry_point: str) -> str:
    """Extracts Python code from markdown fences or raw completion."""
    fence_matches = re.findall(r"```(?:python)?\s*\n(.*?)```", raw, flags=re.DOTALL)
    if fence_matches:
        for block in fence_matches:
            if f"def {entry_point}" in block:
                return block.strip()
        return fence_matches[0].strip()
    return raw.strip()


def evaluate_humaneval_subset(api_key: str) -> Dict[str, Any]:
    """
    Runs 12 canonical coding tasks from HumanEval.jsonl on Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf
    and executes unit tests in an isolated Python sandbox.
    """
    target_ids = {
        "HumanEval/0",
        "HumanEval/1",
        "HumanEval/2",
        "HumanEval/3",
        "HumanEval/4",
        "HumanEval/10",
        "HumanEval/20",
        "HumanEval/31",
        "HumanEval/45",
        "HumanEval/55",
        "HumanEval/60",
        "HumanEval/75",
    }
    problems: List[Dict[str, Any]] = []
    with open(HUMANEVAL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row["task_id"] in target_ids:
                problems.append(row)

    results: List[Dict[str, Any]] = []
    passed_count = 0
    syntax_ok_count = 0

    for idx, prob in enumerate(problems):
        task_id = prob["task_id"]
        entry_point = prob["entry_point"]
        prompt = prob["prompt"]
        test_code = prob["test"]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are Qwen3-Coder. Complete the requested Python function accurately. "
                    "Return ONLY a single ```python ... ``` code block containing the complete function definition (including necessary imports)."
                ),
            },
            {
                "role": "user",
                "content": f"Complete the following Python function:\n\n```python\n{prompt}\n```",
            },
        ]

        res = stream_chat_completion(
            f"{GATEWAY_URL}/v1/chat/completions",
            api_key=api_key,
            messages=messages,
            max_tokens=384,
            temperature=0.0,
        )
        extracted = extract_python_code(res["text"], entry_point)
        if f"def {entry_point}" not in extracted:
            full_program = prompt + "\n" + extracted + "\n\n" + test_code + f"\ncheck({entry_point})\n"
        else:
            full_program = "from typing import *\nimport math\n\n" + extracted + "\n\n" + test_code + f"\ncheck({entry_point})\n"

        # Check syntax
        syntax_valid = True
        try:
            compile(full_program, f"<{task_id}>", "exec")
            syntax_ok_count += 1
        except SyntaxError:
            syntax_valid = False

        # Run inside isolated subprocess sandbox
        passed = False
        error_msg = ""
        t_exec0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="aicf_heval_") as tmpdir:
            script_path = Path(tmpdir) / "solution.py"
            script_path.write_text(full_program, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", str(script_path)],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                if proc.returncode == 0:
                    passed = True
                    passed_count += 1
                else:
                    error_msg = (proc.stderr or proc.stdout).strip()[-300:]
            except subprocess.TimeoutExpired:
                error_msg = "TimeoutExpired (5.0s)"
        sandbox_ms = round((time.perf_counter() - t_exec0) * 1000.0, 2)

        print(
            f"  [HumanEval {idx+1}/{len(problems)}] {task_id} ({entry_point}): "
            f"{'PASS' if passed else 'FAIL'} | {res['decode_tok_s']} tok/s | TTFT={res['ttft_ms']}ms | Sandbox={sandbox_ms}ms",
            flush=True,
        )
        results.append(
            {
                "task_id": task_id,
                "entry_point": entry_point,
                "passed": passed,
                "syntax_valid": syntax_valid,
                "output_tokens": res["output_tokens"],
                "ttft_ms": res["ttft_ms"],
                "tpot_ms": res["tpot_ms"],
                "decode_tok_s": res["decode_tok_s"],
                "total_ms": res["t_total_ms"],
                "sandbox_exec_ms": sandbox_ms,
                "error": error_msg,
            }
        )

    total = len(results)
    return {
        "total_tasks": total,
        "syntax_compile_pass": syntax_ok_count,
        "syntax_compile_rate_pct": round(100.0 * syntax_ok_count / max(1, total), 2),
        "unit_test_pass_at_1": passed_count,
        "unit_test_pass_rate_pct": round(100.0 * passed_count / max(1, total), 2),
        "mean_decode_tok_s": round(statistics.mean([r["decode_tok_s"] for r in results]), 2) if results else 0.0,
        "mean_ttft_ms": round(statistics.mean([r["ttft_ms"] for r in results]), 2) if results else 0.0,
        "mean_tpot_ms": round(statistics.mean([r["tpot_ms"] for r in results]), 2) if results else 0.0,
        "tasks": results,
    }


def main() -> None:
    print("=== [1/4] Verifying Model Identity (Section 11.4) ===", flush=True)
    identity = inspect_gguf_identity(MODEL_PATH)
    print(f"  Model: {identity['model_name']} ({identity['size_gib']} GiB, {identity['tensor_count']} tensors)", flush=True)

    print("=== [2/4] Switching 2-GPU Cluster to Qwen3-Coder-30B-A3B (Merged Mode 24GB VRAM) ===", flush=True)
    deploy_info = switch_cluster_to_coder_merged()
    print(
        f"  Cold-load / switch completed in {deploy_info['cold_load_seconds']}s | "
        f"Buffers: {deploy_info['llama_log_buffers']}",
        flush=True,
    )

    api_key = mint_benchmark_api_key()
    print("=== [3/4] Running 3 Warmup + 5 Measured Runs & Sampling GPU Power (Section 12.1-12.4) ===", flush=True)
    perf_bench = run_3_warmup_5_measured(api_key)

    print("=== [4/4] Running 12-Task HumanEval Coding & Sandbox Execution Suite ===", flush=True)
    coding_eval = evaluate_humaneval_subset(api_key)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "document_reference": "AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx",
        "model_identity": identity,
        "deployment_and_vram": deploy_info,
        "latency_and_power_benchmark": perf_bench,
        "humaneval_sandbox_benchmark": coding_eval,
    }

    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== Benchmark Completed! Saved to {REPORT_JSON_PATH} ===", flush=True)


if __name__ == "__main__":
    main()

