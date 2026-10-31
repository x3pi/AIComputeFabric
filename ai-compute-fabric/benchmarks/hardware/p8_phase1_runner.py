#!/usr/bin/env python3
"""Pha 1: llama.cpp Baseline Benchmark for Qwen3-30B-A3B Q4_K_M on RTX 3060.
Measures decode speed (tok/s), prefill speed (tok/s), TTFT (ms), peak VRAM (MiB),
and available RAM across context sizes (8k, 32k) and -ncmoe configurations (20, 26, 30, 36, 48, 0).
Repeats >= 5 times per point to report median, min, max.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

OOM_RE = re.compile(r"out of memory|cudaMalloc failed|failed to allocate|CUDA error|std::bad_alloc", re.I)
SHORT_PROMPT = "Hãy giải thích ngắn gọn bằng tiếng Việt vì sao bầu trời có màu xanh, trong khoảng 120 từ."


def mem_available_mib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024.0
    return float("nan")


def gpu_used_mib(index: int = 0) -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "-i", str(index), "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return float(out)
    except Exception:
        return float("nan")


def check_kernel_oom_since(since_ts: str) -> list[str]:
    try:
        res = subprocess.run(
            ["journalctl", "-k", f"--since={since_ts}"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l for l in res.stdout.splitlines() if "oom" in l.lower() or "out of memory" in l.lower()]
        return lines
    except Exception:
        return []


class VramSampler(threading.Thread):
    def __init__(self, gpu: int = 0, interval: float = 0.2):
        super().__init__(daemon=True)
        self.gpu = gpu
        self.interval = interval
        self.stop_event = threading.Event()
        self.base_gpu = gpu_used_mib(gpu)
        self.peak_gpu = self.base_gpu
        self.base_ram = mem_available_mib()
        self.min_ram = self.base_ram

    def run(self):
        while not self.stop_event.is_set():
            v = gpu_used_mib(self.gpu)
            if v > self.peak_gpu:
                self.peak_gpu = v
            r = mem_available_mib()
            if r < self.min_ram:
                self.min_ram = r
            time.sleep(self.interval)

    def finish(self):
        self.stop_event.set()
        self.join(timeout=2.0)


def post_json(url: str, body: dict, timeout: float = 600.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def make_long_text(server_url: str, target_tokens: int, salt: str) -> str:
    def lines(n: int) -> str:
        return "\n".join(
            f"Record {salt}-{i:06d}: sensor {i*7919%1000:03d} reads {(i*31)%97}.{(i*17)%10} units, state {'ok' if i%5 else 'check'}, code {i*104729%99991:05d}."
            for i in range(n)
        )
    lo, hi = 1, max(8, target_tokens // 12)
    while True:
        try:
            tok_resp = post_json(f"{server_url}/tokenize", {"content": lines(hi)}, timeout=30)
            if len(tok_resp.get("tokens", [])) >= target_tokens:
                break
            hi *= 2
        except Exception:
            break
    while lo < hi:
        mid = (lo + hi) // 2
        tok_resp = post_json(f"{server_url}/tokenize", {"content": lines(mid)}, timeout=30)
        if len(tok_resp.get("tokens", [])) >= target_tokens:
            hi = mid
        else:
            lo = mid + 1
    return lines(lo)


def run_benchmark_config(
    server_bin: str,
    model_path: str,
    ctx: int,
    ncmoe: int,
    port: int,
    reps: int,
    run_long_prompts: bool,
    out_dir: Path,
    tag: str
) -> dict:
    # 1. Pre-flight check RAM
    current_avail_ram = mem_available_mib()
    if current_avail_ram < 8192.0:
        raise RuntimeError(f"ABORT: MemAvailable {current_avail_ram:.1f} MiB < 8192 MiB required!")

    log_path = out_dir / "logs" / f"p8_p1_{tag}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t_start_iso = time.strftime("%Y-%m-%d %H:%M:%S")

    cmd = [
        server_bin,
        "-m", model_path,
        "-c", str(ctx),
        "-np", "1",
        "-ngl", "99",
        "-fa", "on",
        "--no-warmup",
        "--fit", "off",
        "--port", str(port),
        "--host", "127.0.0.1",
        "-lv", "3"
    ]
    if ncmoe > 0:
        cmd.extend(["-ncmoe", str(ncmoe)])

    sampler = VramSampler(gpu=0)
    sampler.start()

    log_file = open(log_path, "w", encoding="utf-8")
    log_file.write(f"$ {' '.join(cmd)}\n")
    log_file.flush()

    record = {
        "tag": tag,
        "ctx": ctx,
        "ncmoe": ncmoe,
        "reps": reps,
        "cmd": cmd,
        "log_path": str(log_path),
        "status": "unknown",
        "load_time_s": None,
        "short_runs": [],
        "long_runs": [],
        "peak_vram_mib": None,
        "base_vram_mib": sampler.base_gpu,
        "min_avail_ram_mib": None,
        "base_avail_ram_mib": sampler.base_ram,
        "oom_detected": False,
        "kernel_oom_logs": []
    }

    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
    t0 = time.time()
    healthy = False

    try:
        # Wait up to 180s for /health
        while time.time() - t0 < 180.0:
            if proc.poll() is not None:
                record["status"] = f"exited_early_rc_{proc.returncode}"
                break
            try:
                resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5)
                if resp.status == 200:
                    healthy = True
                    record["status"] = "healthy"
                    record["load_time_s"] = round(time.time() - t0, 2)
                    break
            except Exception:
                time.sleep(1.0)
        else:
            record["status"] = "start_timeout"

        if healthy:
            print(f"[{tag}] Server healthy in {record['load_time_s']}s. Running {reps} short prompt tests...", flush=True)
            # A. Short prompt tests (>= 5 reps)
            for r_idx in range(reps):
                # Prompt with cache disabled to measure true prefill + TTFT each time
                body = {
                    "model": "qwen3-30b-a3b",
                    "messages": [{"role": "user", "content": f"{SHORT_PROMPT} (Lần {r_idx+1})"}],
                    "max_tokens": 128,
                    "temperature": 0.0,
                    "seed": 42 + r_idx,
                    "cache_prompt": False
                }
                t_req0 = time.time()
                try:
                    resp_json = post_json(f"http://127.0.0.1:{port}/v1/chat/completions", body, timeout=60.0)
                    wall_req_s = time.time() - t_req0
                    tm = resp_json.get("timings", {})
                    p_n = tm.get("prompt_n", 0)
                    p_ms = tm.get("prompt_ms", 0.0)
                    g_n = tm.get("predicted_n", 0)
                    g_ms = tm.get("predicted_ms", 0.0)
                    prefill_s = (p_n / (p_ms / 1000.0)) if p_ms > 0 else 0.0
                    decode_s = tm.get("predicted_per_second", (g_n / (g_ms / 1000.0)) if g_ms > 0 else 0.0)
                    ttft_ms = p_ms # In non-streaming chat, prompt processing time is time to first generated token
                    record["short_runs"].append({
                        "rep": r_idx + 1,
                        "prompt_n": p_n,
                        "prompt_ms": round(p_ms, 2),
                        "prefill_tok_s": round(prefill_s, 2),
                        "ttft_ms": round(ttft_ms, 2),
                        "predicted_n": g_n,
                        "decode_tok_s": round(decode_s, 2),
                        "wall_req_s": round(wall_req_s, 2),
                        "text_sample": resp_json["choices"][0]["message"]["content"][:60]
                    })
                    print(f"  Rep {r_idx+1}: Decode={decode_s:.2f} tok/s, Prefill={prefill_s:.2f} tok/s, TTFT={ttft_ms:.1f} ms", flush=True)
                except Exception as e:
                    record["short_runs"].append({"rep": r_idx + 1, "error": str(e)})
                time.sleep(1.0)

            # B. Long prompt tests (1k, 8k, 24k) if requested
            if run_long_prompts:
                # Target lengths suitable for ctx
                test_targets = [1000, 8000] if ctx <= 8192 else [1000, 8000, 24000]
                print(f"[{tag}] Running long prompt tests for targets: {test_targets}...", flush=True)
                for tgt in test_targets:
                    # Repeat 5 times for long prompt as well
                    long_reps_data = []
                    for lr_idx in range(5):
                        long_text = make_long_text(f"http://127.0.0.1:{port}", tgt, f"{tag}_t{tgt}_r{lr_idx}")
                        l_body = {
                            "model": "qwen3-30b-a3b",
                            "messages": [{"role": "user", "content": long_text + "\n\nHãy tóm tắt các trạng thái check trong danh sách trên."}],
                            "max_tokens": 64,
                            "temperature": 0.0,
                            "seed": 1,
                            "cache_prompt": False
                        }
                        t_l0 = time.time()
                        try:
                            l_resp = post_json(f"http://127.0.0.1:{port}/v1/chat/completions", l_body, timeout=600.0)
                            wall_l = time.time() - t_l0
                            ltm = l_resp.get("timings", {})
                            lp_n = ltm.get("prompt_n", 0)
                            lp_ms = ltm.get("prompt_ms", 0.0)
                            lg_n = ltm.get("predicted_n", 0)
                            lg_ms = ltm.get("predicted_ms", 0.0)
                            l_prefill_s = (lp_n / (lp_ms / 1000.0)) if lp_ms > 0 else 0.0
                            l_decode_s = ltm.get("predicted_per_second", (lg_n / (lg_ms / 1000.0)) if lg_ms > 0 else 0.0)
                            l_ttft_ms = lp_ms
                            long_reps_data.append({
                                "rep": lr_idx + 1,
                                "prompt_n": lp_n,
                                "prompt_ms": round(lp_ms, 2),
                                "prefill_tok_s": round(l_prefill_s, 2),
                                "ttft_ms": round(l_ttft_ms, 2),
                                "predicted_n": lg_n,
                                "decode_tok_s": round(l_decode_s, 2),
                                "wall_s": round(wall_l, 2)
                            })
                            print(f"  Long {tgt} (rep {lr_idx+1}): Prompt_n={lp_n}, Prefill={l_prefill_s:.1f} tok/s, Decode={l_decode_s:.1f} tok/s, TTFT={l_ttft_ms:.1f} ms", flush=True)
                        except Exception as le:
                            long_reps_data.append({"rep": lr_idx + 1, "error": str(le)})
                        time.sleep(1.5)

                    # Compute stats for this long prompt target
                    val_pref = [x["prefill_tok_s"] for x in long_reps_data if "prefill_tok_s" in x]
                    val_dec = [x["decode_tok_s"] for x in long_reps_data if "decode_tok_s" in x]
                    val_ttft = [x["ttft_ms"] for x in long_reps_data if "ttft_ms" in x]
                    record["long_runs"].append({
                        "target_tokens": tgt,
                        "runs": long_reps_data,
                        "stats": {
                            "prefill_tok_s": {
                                "median": round(statistics.median(val_pref), 2) if val_pref else None,
                                "min": min(val_pref) if val_pref else None,
                                "max": max(val_pref) if val_pref else None
                            },
                            "decode_tok_s": {
                                "median": round(statistics.median(val_dec), 2) if val_dec else None,
                                "min": min(val_dec) if val_dec else None,
                                "max": max(val_dec) if val_dec else None
                            },
                            "ttft_ms": {
                                "median": round(statistics.median(val_ttft), 2) if val_ttft else None,
                                "min": min(val_ttft) if val_ttft else None,
                                "max": max(val_ttft) if val_ttft else None
                            }
                        }
                    })

    finally:
        sampler.finish()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
        log_file.close()

    # Read log file to detect OOM
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if OOM_RE.search(log_text):
            record["oom_detected"] = True
    except Exception:
        pass

    record["peak_vram_mib"] = round(sampler.peak_gpu, 1)
    record["min_avail_ram_mib"] = round(sampler.min_ram, 1)

    # Check kernel OOM
    oom_lines = check_kernel_oom_since(t_start_iso)
    if oom_lines:
        record["kernel_oom_logs"] = oom_lines
        record["oom_detected"] = True

    # Compute short prompt summary stats
    dec_speeds = [x["decode_tok_s"] for x in record["short_runs"] if "decode_tok_s" in x]
    pref_speeds = [x["prefill_tok_s"] for x in record["short_runs"] if "prefill_tok_s" in x]
    ttft_vals = [x["ttft_ms"] for x in record["short_runs"] if "ttft_ms" in x]

    record["short_stats"] = {
        "decode_tok_s": {
            "median": round(statistics.median(dec_speeds), 2) if dec_speeds else None,
            "min": min(dec_speeds) if dec_speeds else None,
            "max": max(dec_speeds) if dec_speeds else None,
            "count": len(dec_speeds)
        },
        "prefill_tok_s": {
            "median": round(statistics.median(pref_speeds), 2) if pref_speeds else None,
            "min": min(pref_speeds) if pref_speeds else None,
            "max": max(pref_speeds) if pref_speeds else None
        },
        "ttft_ms": {
            "median": round(statistics.median(ttft_vals), 2) if ttft_vals else None,
            "min": min(ttft_vals) if ttft_vals else None,
            "max": max(ttft_vals) if ttft_vals else None
        }
    }

    return record


def main():
    p = argparse.ArgumentParser(description="Phase 1 llama.cpp Baseline Suite")
    p.add_argument("--server", default=".aicf-work/bin/llama-server")
    p.add_argument("--model", default=".aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf")
    p.add_argument("--port", type=int, default=18199)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--out-dir", default="ai-compute-fabric/benchmarks/reports")
    args = p.parse_args()

    out_dir = Path(args.out_dir).resolve()
    jsonl_path = out_dir / "p8_ktransformers_p1_baseline.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    print("=== STARTING PHASE 1: LLAMA.CPP BASELINE ON RTX 3060 ===")
    print(f"Server binary: {args.server}")
    print(f"Model: {args.model}")
    print(f"Target jsonl: {jsonl_path}")

    configs_to_run = [
        # Context 8192
        {"ctx": 8192, "ncmoe": 20, "long": True, "tag": "c8k_N20"},
        {"ctx": 8192, "ncmoe": 26, "long": False, "tag": "c8k_N26"},
        {"ctx": 8192, "ncmoe": 30, "long": False, "tag": "c8k_N30"},
        {"ctx": 8192, "ncmoe": 36, "long": False, "tag": "c8k_N36"},
        {"ctx": 8192, "ncmoe": 48, "long": False, "tag": "c8k_N48_all_cpu"},
        {"ctx": 8192, "ncmoe": 0,  "long": False, "tag": "c8k_N0_pure_gpu"},
        # Context 32768
        {"ctx": 32768, "ncmoe": 30, "long": True, "tag": "c32k_N30"},
        {"ctx": 32768, "ncmoe": 36, "long": False, "tag": "c32k_N36"},
        {"ctx": 32768, "ncmoe": 48, "long": False, "tag": "c32k_N48_all_cpu"},
        {"ctx": 32768, "ncmoe": 24, "long": False, "tag": "c32k_N24_oom_probe"},
    ]

    for cfg in configs_to_run:
        print(f"\n>>> Running config: ctx={cfg['ctx']}, ncmoe={cfg['ncmoe']} ({cfg['tag']})")
        res = run_benchmark_config(
            server_bin=args.server,
            model_path=args.model,
            ctx=cfg["ctx"],
            ncmoe=cfg["ncmoe"],
            port=args.port,
            reps=args.reps,
            run_long_prompts=cfg["long"],
            out_dir=out_dir,
            tag=cfg["tag"]
        )

        with open(jsonl_path, "a", encoding="utf-8") as jf:
            jf.write(json.dumps(res, ensure_ascii=False) + "\n")
            jf.flush()

        dec_med = res.get("short_stats", {}).get("decode_tok_s", {}).get("median")
        print(f"<<< Completed {cfg['tag']}: Status={res['status']}, PeakVRAM={res['peak_vram_mib']} MiB, Decode_median={dec_med} tok/s, OOM={res['oom_detected']}")
        time.sleep(3.0)

    print("\n=== PHASE 1 BASELINE COMPLETED SUCCESSFULLY ===")


if __name__ == "__main__":
    main()

