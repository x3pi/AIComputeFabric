#!/usr/bin/env python3
"""Pha T0–T5 Runner: CPU Thread/Affinity (P/E-core), Load-Mode, and KV-cache (q8_0) Optimization
for llama.cpp Mode B (-ncmoe) on pc-001 (Intel Core Ultra 7 265K + RTX 3060 12 GB).

Strictly implements KE_HOACH_TOI_UU_CPU_KV_NCMOE.md:
- Fixed binary (.aicf-work/bin/llama-server, build 11913) and model (.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf).
- Per config: fresh process start, --no-warmup --fit off, 1 burn-in request (logged separately for cold-start analysis),
  followed by >= 7 decode repetitions (n_predict=256, temp=0, fixed seed) and >= 3 long-prompt prefill repetitions.
- Anti-LCP-cache protection: unique [RunID=...] prefix per repetition + verification of `timings.prompt_n`.
- Live hardware & OS thread affinity verification during active decode & prefill:
  reads `/proc/<pid>/task/<tid>/status` (`Cpus_allowed_list`), `ps -L -p <pid> -o tid,comm,psr,pcpu,stat`,
  P-core (0-7) / E-core (8-19) frequencies (`scaling_cur_freq`), and `x86_pkg_temp`.
- Background `nvidia-smi` polling every 0.5 s for peak VRAM, GPU temperature, and GPU power.
"""
import argparse
import glob
import hashlib
import json
import os
import random
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SEED_BASE = 20261030


def read_meminfo() -> dict:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            info[parts[0].rstrip(":")] = int(parts[1]) / 1024.0  # MiB
    avail = info.get("MemAvailable", float("nan"))
    swap_total = info.get("SwapTotal", 0.0)
    swap_free = info.get("SwapFree", 0.0)
    return {
        "mem_available_mib": round(avail, 1),
        "swap_used_mib": round(swap_total - swap_free, 1),
        "swap_free_mib": round(swap_free, 1),
    }


def read_cpu_telemetry() -> dict:
    gov0 = "unknown"
    try:
        gov0 = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except Exception:
        pass

    p_freqs = []
    e_freqs = []
    for c in range(20):
        p = Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
        if p.exists():
            try:
                mhz = int(p.read_text().strip()) / 1000.0
                if c < 8:
                    p_freqs.append(mhz)
                else:
                    e_freqs.append(mhz)
            except Exception:
                pass

    pkg_temp_c = None
    for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            ttype = Path(f"{tz}/type").read_text().strip()
            if ttype == "x86_pkg_temp":
                pkg_temp_c = round(int(Path(f"{tz}/temp").read_text().strip()) / 1000.0, 1)
        except Exception:
            pass

    return {
        "governor": gov0,
        "p_core_avg_mhz": round(statistics.mean(p_freqs), 0) if p_freqs else None,
        "p_core_max_mhz": round(max(p_freqs), 0) if p_freqs else None,
        "e_core_avg_mhz": round(statistics.mean(e_freqs), 0) if e_freqs else None,
        "e_core_max_mhz": round(max(e_freqs), 0) if e_freqs else None,
        "cpu_pkg_temp_c": pkg_temp_c,
    }


def inspect_process_threads(pid: int) -> dict:
    """Capture live thread CPU affinity (`Cpus_allowed_list`) and active processor (`psr`) from /proc and ps."""
    cpus_allowed_set = set()
    thread_Count = 0
    for status_file in sorted(glob.glob(f"/proc/{pid}/task/*/status")):
        try:
            txt = Path(status_file).read_text()
            thread_Count += 1
            for line in txt.splitlines():
                if line.startswith("Cpus_allowed_list:"):
                    cpus_allowed_set.add(line.split(":", 1)[1].strip())
        except Exception:
            pass

    active_psrs = []
    all_psrs = []
    ps_lines = []
    try:
        res = subprocess.run(
            ["ps", "-L", "-p", str(pid), "-o", "tid=,comm=,psr=,pcpu=,stat="],
            capture_output=True, text=True, timeout=5
        )
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                tid, comm, psr_str, pcpu_str, stat_str = parts[0], parts[1], parts[2], parts[3], parts[4]
                psr = int(psr_str)
                pcpu = float(pcpu_str)
                all_psrs.append(psr)
                if "R" in stat_str or pcpu >= 5.0:
                    active_psrs.append(psr)
                    ps_lines.append(f"tid={tid}({comm}):psr={psr},pcpu={pcpu}%,stat={stat_str}")
    except Exception:
        pass

    return {
        "total_os_threads": thread_Count,
        "cpus_allowed_lists": sorted(list(cpus_allowed_set)),
        "active_psrs": sorted(list(set(active_psrs))),
        "all_psrs": sorted(list(set(all_psrs))),
        "active_thread_samples": ps_lines[:24],
    }


def check_kernel_oom(since_ts: str) -> list[str]:
    try:
        res = subprocess.run(
            ["journalctl", "-k", f"--since={since_ts}"],
            capture_output=True, text=True, timeout=10
        )
        return [
            l for l in res.stdout.splitlines()
            if "oom" in l.lower() or "out of memory" in l.lower() or "killed process" in l.lower()
        ]
    except Exception:
        return []


class GPUMonitor:
    def __init__(self, interval_s: float = 0.5):
        self.interval_s = interval_s
        self.peak_vram_mib = 0.0
        self.max_temp_c = 0.0
        self.max_power_w = 0.0
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3.0)

    def _loop(self):
        while not self._stop.is_set():
            try:
                res = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used,temperature.gpu,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True, text=True, timeout=3
                )
                if res.returncode == 0:
                    line = res.stdout.strip().splitlines()[0]
                    parts = [x.strip() for x in line.split(",")]
                    vram = float(parts[0])
                    temp = float(parts[1])
                    pwr = float(parts[2])
                    self.samples.append({"used_mib": vram, "temp_c": temp, "power_w": pwr})
                    if vram > self.peak_vram_mib:
                        self.peak_vram_mib = vram
                    if temp > self.max_temp_c:
                        self.max_temp_c = temp
                    if pwr > self.max_power_w:
                        self.max_power_w = pwr
            except Exception:
                pass
            time.sleep(self.interval_s)


def build_short_prompt(tag: str, rep: int, seed: int) -> str:
    nonce = uuid.uuid4().hex[:12]
    return (
        f"{nonce} [RunID={tag}_decode_rep{rep}_seed{seed}]\n"
        "Hãy viết một hàm Python cài đặt thuật toán tìm đường đi ngắn nhất Dijkstra trên đồ thị có trọng số dương "
        "bằng hàng đợi ưu tiên (heapq), kèm chú thích độ phức tạp thời gian và bộ nhớ chi tiết."
    )


def build_long_prompt(tag: str, target_tokens: int, rep: int, seed: int) -> str:
    rng = random.Random(seed * 1009 + rep * 97 + target_tokens)
    nonce = uuid.uuid4().hex[:12]
    words = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "xray",
        "yankee", "zulu", "tensor", "matrix", "vector", "kernel", "thread", "cache",
        "memory", "socket", "router", "expert", "layer", "token", "stream", "buffer",
    ]
    # In Qwen3 tokenizer, "<word> <3-digit-int> " is ~5.51 tokens per pair.
    # Multiply by 0.176 -> ~7780 tokens for target=8000 (safely < 8192-64) and ~23300 tokens for target=24000.
    n_pairs = int(target_tokens * 0.176)
    chunks = [f"{nonce} [RunID={tag}_prefill{target_tokens}_rep{rep}]\n"]
    for i in range(n_pairs):
        w = rng.choice(words)
        n = rng.randint(100, 999)
        chunks.append(f"{w} {n} ")
        if (i + 1) % 32 == 0:
            chunks.append("\n")
    chunks.append("\nHãy tóm tắt ngắn gọn cấu trúc dữ liệu bên trên trong 3 câu tiếng Việt.")
    return "".join(chunks)


def post_completion(port: int, prompt: str, n_predict: int, seed: int, timeout_s: int = 600) -> dict:
    url = f"http://127.0.0.1:{port}/completion"
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.0,
        "seed": seed,
        "cache_prompt": False,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_server_ready(proc: subprocess.Popen, port: int, timeout_s: int = 180) -> tuple[bool, str]:
    t0 = time.time()
    url = f"http://127.0.0.1:{port}/health"
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            return False, f"process_exited_rc_{proc.returncode}"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="ignore")
                    if '"ok"' in body:
                        return True, "ok"
        except Exception:
            pass
        time.sleep(0.5)
    return False, "timeout"


def stop_server(proc: subprocess.Popen):
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


def parse_server_log_info(log_path: Path) -> dict:
    info = {"n_threads": None, "n_threads_batch": None, "mmap_warning": False, "oom_in_log": False}
    if not log_path.exists():
        return info
    txt = log_path.read_text(errors="ignore")
    for line in txt.splitlines():
        if "llama threadpool init" in line and "n_threads =" in line:
            try:
                part = line.split("n_threads =")[1].strip().split()[0].rstrip(",")
                info["n_threads"] = int(part)
            except Exception:
                pass
        if "tensor overrides to CPU are used with mmap enabled" in line:
            info["mmap_warning"] = True
        if "out of memory" in line.lower() or "cuda_error_out_of_memory" in line.lower() or "failed to allocate" in line.lower():
            info["oom_in_log"] = True
    return info


def run_single_config(
    cfg: dict,
    bin_path: str,
    model_path: str,
    port: int,
    out_jsonl: Path,
    log_dir: Path,
    decode_reps: int = 7,
    prefill_reps: int = 3,
) -> dict:
    tag = cfg["tag"]
    ctx = cfg.get("ctx", 8192)
    ncmoe = cfg.get("ncmoe", 20)
    kv_type = cfg.get("kv_type", "f16")
    extra_args = cfg.get("extra_args", [])
    taskset_cores = cfg.get("taskset", None)
    prefill_lengths = cfg.get("prefill_lengths", [])

    mem_before = read_meminfo()
    cpu_before = read_cpu_telemetry()
    if mem_before["mem_available_mib"] < 8192.0:
        raise RuntimeError(f"ABORT: MemAvailable {mem_before['mem_available_mib']} MiB < 8192 MiB safety floor!")

    cmd = []
    if taskset_cores:
        cmd.extend(["taskset", "-c", taskset_cores])
    cmd.extend([
        bin_path,
        "-m", model_path,
        "-c", str(ctx),
        "-np", "1",
        "-ngl", "99",
        "--port", str(port),
        "--host", "127.0.0.1",
        "-fa", "on",
        "-ctk", kv_type,
        "-ctv", kv_type,
        "--no-warmup",
        "--fit", "off",
        "-lv", "3",
    ])
    if ncmoe > 0:
        cmd.extend(["-ncmoe", str(ncmoe)])
    cmd.extend(extra_args)

    log_path = log_dir / f"p9_{tag}.log"
    env = os.environ.copy()
    bin_dir = str(Path(bin_path).resolve().parent)
    env["LD_LIBRARY_PATH"] = f"{bin_dir}:{env.get('LD_LIBRARY_PATH', '')}"

    t_start_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== [{tag}] Starting: {' '.join(cmd)} ===", flush=True)
    print(f"    MemAvailable={mem_before['mem_available_mib']} MiB, SwapUsed={mem_before['swap_used_mib']} MiB, CPU_Temp={cpu_before['cpu_pkg_temp_c']}C", flush=True)

    gpu_mon = GPUMonitor(interval_s=0.5)
    gpu_mon.start()

    t_load0 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
        try:
            ready, status_reason = wait_server_ready(proc, port=port, timeout_s=180)
            load_time_s = round(time.perf_counter() - t_load0, 2)

            if not ready:
                stop_server(proc)
                gpu_mon.stop()
                log_info = parse_server_log_info(log_path)
                oom_k = check_kernel_oom(t_start_iso)
                record = {
                    "tag": tag,
                    "phase": cfg.get("phase", ""),
                    "cmd": cmd,
                    "ctx": ctx,
                    "ncmoe": ncmoe,
                    "kv_type": kv_type,
                    "extra_args": extra_args,
                    "taskset": taskset_cores,
                    "status": status_reason,
                    "oom_detected": bool(log_info["oom_in_log"] or oom_k),
                    "load_time_s": load_time_s,
                    "vram_peak_mib": round(gpu_mon.peak_vram_mib, 1),
                    "mem_before": mem_before,
                    "mem_after": read_meminfo(),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                with open(out_jsonl, "a", encoding="utf-8") as jf:
                    jf.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"    [{tag}] FAILED to start: status={status_reason}, oom={record['oom_detected']}, vram_peak={record['vram_peak_mib']} MiB", flush=True)
                return record

            # 1. Burn-in request (rep=0, discarded from median/min/max per Section 2.2, recorded separately)
            burn_prompt = build_short_prompt(tag, rep=0, seed=SEED_BASE)
            burn_res = post_completion(port, burn_prompt, n_predict=128, seed=SEED_BASE)
            burn_timings = burn_res.get("timings", {})
            cold_decode_tps = round(burn_timings.get("predicted_per_second", 0.0), 2)
            cold_prefill_tps = round(burn_timings.get("prompt_per_second", 0.0), 2)
            print(f"    Burn-in (rep 0): decode={cold_decode_tps:.2f} tok/s, prefill={cold_prefill_tps:.2f} tok/s (prompt_n={burn_timings.get('prompt_n')})", flush=True)

            # 2. Measured Decode repetitions (decode_reps >= 7, n_predict=256)
            decode_samples_tps = []
            decode_samples_ms_per_tok = []
            short_prefill_samples_tps = []
            short_ttft_samples_ms = []
            prompt_n_values = []
            live_decode_Inspection = {}
            live_decode_cpu = {}

            for r in range(1, decode_reps + 1):
                seed = SEED_BASE + r
                p_text = build_short_prompt(tag, rep=r, seed=seed)

                # On rep 2, sample live thread psr/affinity and CPU clocks 1.5s into decode
                sampler_result = {}
                def _sample_during_decode():
                    time.sleep(1.5)
                    if proc.poll() is None:
                        sampler_result["threads"] = inspect_process_threads(proc.pid)
                        sampler_result["cpu"] = read_cpu_telemetry()

                st = None
                if r == 2:
                    st = threading.Thread(target=_sample_during_decode, daemon=True)
                    st.start()

                res = post_completion(port, p_text, n_predict=256, seed=seed)
                if st is not None:
                    st.join(timeout=3.0)
                    live_decode_Inspection = sampler_result.get("threads", {})
                    live_decode_cpu = sampler_result.get("cpu", {})

                t_obj = res.get("timings", {})
                tps = float(t_obj.get("predicted_per_second", 0.0))
                pred_ms = float(t_obj.get("predicted_ms", 0.0))
                pred_n = int(t_obj.get("predicted_n", 256))
                p_tps = float(t_obj.get("prompt_per_second", 0.0))
                p_ms = float(t_obj.get("prompt_ms", 0.0))
                p_n = int(t_obj.get("prompt_n", 0))

                if p_n < 35:
                    raise RuntimeError(f"LCP cache leak detected on {tag} rep {r}: prompt_n={p_n} < 35!")

                decode_samples_tps.append(round(tps, 2))
                decode_samples_ms_per_tok.append(round(pred_ms / max(pred_n, 1), 3))
                short_prefill_samples_tps.append(round(p_tps, 2))
                short_ttft_samples_ms.append(round(p_ms, 1))
                prompt_n_values.append(p_n)

            med_dec = round(statistics.median(decode_samples_tps), 2)
            min_dec = round(min(decode_samples_tps), 2)
            max_dec = round(max(decode_samples_tps), 2)
            med_dec_ms = round(statistics.median(decode_samples_ms_per_tok), 3)

            print(
                f"    Decode ({decode_reps} reps, n=256): median={med_dec:.2f} tok/s ({med_dec_ms:.2f} ms/tok), "
                f"min={min_dec:.2f}, max={max_dec:.2f} | samples={decode_samples_tps}",
                flush=True
            )
            if live_decode_Inspection:
                print(
                    f"    Live Decode Affinity: Cpus_allowed={live_decode_Inspection.get('cpus_allowed_lists')}, "
                    f"active_psrs={live_decode_Inspection.get('active_psrs')}, "
                    f"P_MHz={live_decode_cpu.get('p_core_avg_mhz')}, E_MHz={live_decode_cpu.get('e_core_avg_mhz')}, "
                    f"Temp={live_decode_cpu.get('cpu_pkg_temp_c')}C",
                    flush=True
                )

            # 3. Long-prompt prefill repetitions (if requested for this config)
            long_prefill_results = {}
            for target_len in prefill_lengths:
                pf_tps_list = []
                pf_ttft_ms_list = []
                pf_dec_tps_list = []
                pf_prompt_n_list = []
                live_pf_inspection = {}
                live_pf_cpu = {}

                for r in range(1, prefill_reps + 1):
                    seed = SEED_BASE + 100 * r + target_len
                    lp_text = build_long_prompt(tag, target_tokens=target_len, rep=r, seed=seed)

                    pf_sample = {}
                    def _sample_during_prefill():
                        time.sleep(2.0)
                        if proc.poll() is None:
                            pf_sample["threads"] = inspect_process_threads(proc.pid)
                            pf_sample["cpu"] = read_cpu_telemetry()

                    st_pf = None
                    if r == 1:
                        st_pf = threading.Thread(target=_sample_during_prefill, daemon=True)
                        st_pf.start()

                    res = post_completion(port, lp_text, n_predict=64, seed=seed, timeout_s=900)
                    if st_pf is not None:
                        st_pf.join(timeout=3.0)
                        live_pf_inspection = pf_sample.get("threads", {})
                        live_pf_cpu = pf_sample.get("cpu", {})

                    t_obj = res.get("timings", {})
                    p_tps = float(t_obj.get("prompt_per_second", 0.0))
                    p_ms = float(t_obj.get("prompt_ms", 0.0))
                    p_n = int(t_obj.get("prompt_n", 0))
                    d_tps = float(t_obj.get("predicted_per_second", 0.0))

                    if p_n < int(target_len * 0.80):
                        raise RuntimeError(f"LCP cache leak on long prefill {tag} len={target_len} rep={r}: prompt_n={p_n}")

                    pf_tps_list.append(round(p_tps, 2))
                    pf_ttft_ms_list.append(round(p_ms, 1))
                    pf_dec_tps_list.append(round(d_tps, 2))
                    pf_prompt_n_list.append(p_n)

                long_prefill_results[f"p{target_len}"] = {
                    "target_tokens": target_len,
                    "actual_prompt_n": pf_prompt_n_list[0] if pf_prompt_n_list else 0,
                    "reps": prefill_reps,
                    "prefill_tok_s": {
                        "median": round(statistics.median(pf_tps_list), 2),
                        "min": round(min(pf_tps_list), 2),
                        "max": round(max(pf_tps_list), 2),
                        "samples": pf_tps_list,
                    },
                    "ttft_ms": {
                        "median": round(statistics.median(pf_ttft_ms_list), 1),
                        "min": round(min(pf_ttft_ms_list), 1),
                        "max": round(max(pf_ttft_ms_list), 1),
                        "samples": pf_ttft_ms_list,
                    },
                    "post_prefill_decode_tok_s": {
                        "median": round(statistics.median(pf_dec_tps_list), 2),
                        "min": round(min(pf_dec_tps_list), 2),
                        "max": round(max(pf_dec_tps_list), 2),
                        "samples": pf_dec_tps_list,
                    },
                    "live_prefill_affinity": live_pf_inspection,
                    "live_prefill_cpu": live_pf_cpu,
                }
                lp_r = long_prefill_results[f"p{target_len}"]
                print(
                    f"    Prefill ~{target_len} (prompt_n={lp_r['actual_prompt_n']}): "
                    f"median={lp_r['prefill_tok_s']['median']:.1f} tok/s "
                    f"(min={lp_r['prefill_tok_s']['min']:.1f}, max={lp_r['prefill_tok_s']['max']:.1f}), "
                    f"TTFT={lp_r['ttft_ms']['median']:.1f} ms, "
                    f"post_decode={lp_r['post_prefill_decode_tok_s']['median']:.2f} tok/s | "
                    f"active_psrs={live_pf_inspection.get('active_psrs')}",
                    flush=True
                )
        finally:
            stop_server(proc)
            gpu_mon.stop()
    mem_after = read_meminfo()
    cpu_after = read_cpu_telemetry()
    log_info = parse_server_log_info(log_path)
    oom_k = check_kernel_oom(t_start_iso)

    record = {
        "tag": tag,
        "phase": cfg.get("phase", ""),
        "cmd": cmd,
        "ctx": ctx,
        "ncmoe": ncmoe,
        "kv_type": kv_type,
        "extra_args": extra_args,
        "taskset": taskset_cores,
        "status": "healthy",
        "oom_detected": bool(log_info["oom_in_log"] or oom_k),
        "server_log_info": log_info,
        "load_time_s": load_time_s,
        "vram_peak_mib": round(gpu_mon.peak_vram_mib, 1),
        "gpu_temp_max_c": round(gpu_mon.max_temp_c, 1),
        "gpu_power_max_w": round(gpu_mon.max_power_w, 1),
        "mem_before": mem_before,
        "mem_after": mem_after,
        "swap_delta_mib": round(mem_after["swap_used_mib"] - mem_before["swap_used_mib"], 1),
        "cpu_before": cpu_before,
        "cpu_during_decode": live_decode_cpu,
        "cpu_after": cpu_after,
        "live_decode_affinity": live_decode_Inspection,
        "cold_burnin": {
            "decode_tok_s": cold_decode_tps,
            "prefill_tok_s": cold_prefill_tps,
        },
        "decode": {
            "reps": decode_reps,
            "n_predict": 256,
            "prompt_n": prompt_n_values[0] if prompt_n_values else 0,
            "tok_s": {
                "median": med_dec,
                "min": min_dec,
                "max": max_dec,
                "samples": decode_samples_tps,
            },
            "ms_per_tok": {
                "median": med_dec_ms,
                "min": round(min(decode_samples_ms_per_tok), 3),
                "max": round(max(decode_samples_ms_per_tok), 3),
                "samples": decode_samples_ms_per_tok,
            },
        },
        "short_prefill": {
            "tok_s_median": round(statistics.median(short_prefill_samples_tps), 2),
            "ttft_ms_median": round(statistics.median(short_ttft_samples_ms), 1),
        },
        "long_prefill": long_prefill_results,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    with open(out_jsonl, "a", encoding="utf-8") as jf:
        jf.write(json.dumps(record, ensure_ascii=False) + "\n")

    time.sleep(1.0)
    return record


def get_phase_configs(phase: str, best_decode_args: list[str] = None, best_batch_args: list[str] = None) -> list[dict]:
    if phase == "T0":
        return [
            {
                "tag": "T0_c8k_N20_base",
                "phase": "T0",
                "ctx": 8192,
                "ncmoe": 20,
                "kv_type": "f16",
                "extra_args": [],
                "prefill_lengths": [8000],
            },
            {
                "tag": "T0_c8k_N48_base",
                "phase": "T0",
                "ctx": 8192,
                "ncmoe": 48,
                "kv_type": "f16",
                "extra_args": [],
                "prefill_lengths": [8000],
            },
            {
                "tag": "T0_c32k_N30_base",
                "phase": "T0",
                "ctx": 32768,
                "ncmoe": 30,
                "kv_type": "f16",
                "extra_args": [],
                "prefill_lengths": [8000, 24000],
            },
        ]
    elif phase == "T1":
        # Unpinned thread count sweep on N=48 and N=20, with interleaved baselines
        configs = []
        # First sweep N=48 (all MoE on CPU - clearest signal) with ABAB baseline checks
        configs.append({"tag": "T1_N48_base_t_default_A", "phase": "T1", "ctx": 8192, "ncmoe": 48, "extra_args": []})
        for t in [4, 6, 8, 12, 16, 20]:
            configs.append({"tag": f"T1_N48_t{t}_unpinned", "phase": "T1", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", str(t)]})
        configs.append({"tag": "T1_N48_base_t_default_B", "phase": "T1", "ctx": 8192, "ncmoe": 48, "extra_args": []})

        # Next sweep N=20 (realistic 8k operating point) with ABAB baseline checks
        configs.append({"tag": "T1_N20_base_t_default_A", "phase": "T1", "ctx": 8192, "ncmoe": 20, "extra_args": []})
        for t in [4, 6, 8, 12, 16, 20]:
            configs.append({"tag": f"T1_N20_t{t}_unpinned", "phase": "T1", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", str(t)]})
        configs.append({"tag": "T1_N20_base_t_default_B", "phase": "T1", "ctx": 8192, "ncmoe": 20, "extra_args": []})
        return configs

    elif phase == "T2":
        # Phase T2: Pinning mechanisms (-Cr vs --cpu-strict 1 vs taskset), mutations (E-cores 8-15, hybrid 4-11),
        # and single-parameter tunings (--poll, --prio, --load-mode none)
        return [
            # N=48 Affinity & Mutation matrix
            {"tag": "T2_N48_base_A", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": []},
            {"tag": "T2_N48_t8_unpinned", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8"]},
            {"tag": "T2_N48_t8_Cr0_7_nostrict", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8", "-Cr", "0-7"]},
            {"tag": "T2_N48_t8_Cr0_7_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1"]},
            {"tag": "T2_N48_t8_taskset0_7", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8"], "taskset": "0-7"},
            {"tag": "T2_N48_MUTATION_t8_Cr8_15_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8", "-Cr", "8-15", "--cpu-strict", "1"]},
            {"tag": "T2_N48_MUTATION_t8_taskset8_15", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8"], "taskset": "8-15"},
            {"tag": "T2_N48_MUTATION_t8_Cr4_11_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "8", "-Cr", "4-11", "--cpu-strict", "1"]},
            {"tag": "T2_N48_t12_Cr0_11_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "12", "-Cr", "0-11", "--cpu-strict", "1"]},
            {"tag": "T2_N48_t20_Cr0_19_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": ["-t", "20", "-Cr", "0-19", "--cpu-strict", "1"]},
            {"tag": "T2_N48_base_B", "phase": "T2", "ctx": 8192, "ncmoe": 48, "extra_args": []},

            # N=20 Affinity, Mutation & Single-Parameter Tuning matrix
            {"tag": "T2_N20_base_A", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": []},
            {"tag": "T2_N20_t8_unpinned", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8"]},
            {"tag": "T2_N20_t8_Cr0_7_nostrict", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7"]},
            {"tag": "T2_N20_t8_Cr0_7_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1"]},
            {"tag": "T2_N20_t8_taskset0_7", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8"], "taskset": "0-7"},
            {"tag": "T2_N20_MUTATION_t8_Cr8_15_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "8-15", "--cpu-strict", "1"]},
            {"tag": "T2_N20_MUTATION_t8_taskset8_15", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8"], "taskset": "8-15"},
            {"tag": "T2_N20_MUTATION_t8_Cr4_11_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "4-11", "--cpu-strict", "1"]},
            {"tag": "T2_N20_t12_Cr0_11_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "12", "-Cr", "0-11", "--cpu-strict", "1"]},
            {"tag": "T2_N20_t20_Cr0_19_strict1", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "20", "-Cr", "0-19", "--cpu-strict", "1"]},
            # Single-flag variations on top of t8_Cr0_7_strict1:
            {"tag": "T2_N20_t8_Cr0_7_strict1_poll0", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1", "--poll", "0"]},
            {"tag": "T2_N20_t8_Cr0_7_strict1_poll100", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1", "--poll", "100"]},
            {"tag": "T2_N20_t8_Cr0_7_strict1_prio2", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1", "--prio", "2"]},
            {"tag": "T2_N20_t8_Cr0_7_strict1_loadnone", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1", "--load-mode", "none"]},
            {"tag": "T2_N20_base_B", "phase": "T2", "ctx": 8192, "ncmoe": 20, "extra_args": []},
        ]
    elif phase == "T3":
        dec_args = best_decode_args or ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1"]
        return [
            # 8k context (-ncmoe 20)
            {"tag": "T3_c8k_N20_base_A", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": [], "prefill_lengths": [8000]},
            {"tag": "T3_c8k_N20_tb8_Crb0_7", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": dec_args + ["-tb", "8", "-Crb", "0-7", "--cpu-strict-batch", "1"], "prefill_lengths": [8000]},
            {"tag": "T3_c8k_N20_tb12_Crb0_11", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": dec_args + ["-tb", "12", "-Crb", "0-11", "--cpu-strict-batch", "1"], "prefill_lengths": [8000]},
            {"tag": "T3_c8k_N20_tb16_Crb0_15", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": dec_args + ["-tb", "16", "-Crb", "0-15", "--cpu-strict-batch", "1"], "prefill_lengths": [8000]},
            {"tag": "T3_c8k_N20_tb20_Crb0_19", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": dec_args + ["-tb", "20", "-Crb", "0-19", "--cpu-strict-batch", "1"], "prefill_lengths": [8000]},
            {"tag": "T3_c8k_N20_tb20_Crb0_19_nostrict", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": ["-t", "8", "-Cr", "0-7", "-tb", "20", "-Crb", "0-19"], "prefill_lengths": [8000]},
            {"tag": "T3_c8k_N20_MUTATION_tb4_Crb8_11", "phase": "T3", "ctx": 8192, "ncmoe": 20, "extra_args": dec_args + ["-tb", "4", "-Crb", "8-11", "--cpu-strict-batch", "1"], "prefill_lengths": [8000]},
            # 32k context (-ncmoe 30) with both 8k and 24k prompts
            {"tag": "T3_c32k_N30_base_A", "phase": "T3", "ctx": 32768, "ncmoe": 30, "extra_args": [], "prefill_lengths": [8000, 24000]},
            {"tag": "T3_c32k_N30_tb8_Crb0_7", "phase": "T3", "ctx": 32768, "ncmoe": 30, "extra_args": dec_args + ["-tb", "8", "-Crb", "0-7", "--cpu-strict-batch", "1"], "prefill_lengths": [8000, 24000]},
            {"tag": "T3_c32k_N30_tb20_Crb0_19", "phase": "T3", "ctx": 32768, "ncmoe": 30, "extra_args": dec_args + ["-tb", "20", "-Crb", "0-19", "--cpu-strict-batch", "1"], "prefill_lengths": [8000, 24000]},
            {"tag": "T3_c32k_N30_tb20_Crb0_19_nostrict", "phase": "T3", "ctx": 32768, "ncmoe": 30, "extra_args": ["-t", "8", "-Cr", "0-7", "-tb", "20", "-Crb", "0-19"], "prefill_lengths": [8000, 24000]},
        ]
    elif phase == "T4":
        cpu_args = best_decode_args or ["-t", "8", "-Cr", "0-7", "--cpu-strict", "1"]
        return [
            # 8k context comparison: f16 vs q8_0 at N=20
            {"tag": "T4_c8k_N20_f16_cpuopt", "phase": "T4", "ctx": 8192, "ncmoe": 20, "kv_type": "f16", "extra_args": cpu_args, "prefill_lengths": [8000]},
            {"tag": "T4_c8k_N20_q8_cpuopt", "phase": "T4", "ctx": 8192, "ncmoe": 20, "kv_type": "q8_0", "extra_args": cpu_args, "prefill_lengths": [8000]},
            # 32k context sweep: f16 vs q8_0 across N in {30, 26, 24, 22}
            {"tag": "T4_c32k_N30_f16_base", "phase": "T4", "ctx": 32768, "ncmoe": 30, "kv_type": "f16", "extra_args": [], "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N30_f16_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 30, "kv_type": "f16", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N30_q8_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 30, "kv_type": "q8_0", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N26_f16_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 26, "kv_type": "f16", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N24_f16_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 24, "kv_type": "f16", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N26_q8_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 26, "kv_type": "q8_0", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N24_q8_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 24, "kv_type": "q8_0", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
            {"tag": "T4_c32k_N22_q8_cpuopt", "phase": "T4", "ctx": 32768, "ncmoe": 22, "kv_type": "q8_0", "extra_args": cpu_args, "prefill_lengths": [8000, 24000]},
        ]
    else:
        raise ValueError(f"Unknown phase: {phase}")


def main():
    p = argparse.ArgumentParser(description="P9 CPU Affinity & KV-cache Benchmark Runner")
    p.add_argument("--phase", required=True, choices=["T0", "T1", "T2", "T3", "T4"])
    p.add_argument("--bin", default=".aicf-work/bin/llama-server")
    p.add_argument("--model", default=".aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf")
    p.add_argument("--port", type=int, default=18199)
    p.add_argument("--decode-reps", type=int, default=7)
    p.add_argument("--prefill-reps", type=int, default=3)
    p.add_argument("--out-dir", default="ai-compute-fabric/benchmarks/reports")
    args = p.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    jsonl_map = {
        "T0": out_dir / "p9_t0_baseline.jsonl",
        "T1": out_dir / "p9_t1_threads_unpinned.jsonl",
        "T2": out_dir / "p9_t2_cpu_affinity.jsonl",
        "T3": out_dir / "p9_t3_prefill_batch.jsonl",
        "T4": out_dir / "p9_t4_kv_q8.jsonl",
    }
    out_jsonl = jsonl_map[args.phase]
    if out_jsonl.exists():
        out_jsonl.unlink()

    configs = get_phase_configs(args.phase)
    print(f"=== STARTING PHASE {args.phase} ({len(configs)} configurations, decode_reps={args.decode_reps}, prefill_reps={args.prefill_reps}) ===", flush=True)

    for idx, cfg in enumerate(configs):
        print(f"\n>>> [{idx+1}/{len(configs)}] Running config: {cfg['tag']}", flush=True)
        run_single_config(
            cfg=cfg,
            bin_path=args.bin,
            model_path=args.model,
            port=args.port,
            out_jsonl=out_jsonl,
            log_dir=log_dir,
            decode_reps=args.decode_reps,
            prefill_reps=args.prefill_reps,
        )

    print(f"\n=== PHASE {args.phase} COMPLETED SUCCESSFULLY. Output: {out_jsonl} ===", flush=True)


if __name__ == "__main__":
    main()
