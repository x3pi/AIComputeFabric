#!/usr/bin/env python3
"""Pha 1.1 of KE_HOACH_GOP_TACH_2_GPU_LIEN_HOP.md: sweep -ncmoe N (x KV type) for one MoE model on ONE card.

For every config it runs llama-bench (pp + tg, repeated), samples peak GPU memory.used (nvidia-smi) and the lowest
MemAvailable while the process runs, keeps the full log, and appends one JSON line per config (also the failed ones).
It measures; it does not decide anything. Run it with no other GPU job active.
"""
import argparse
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

OOM_RE = re.compile(r"out of memory|cudaMalloc failed|failed to allocate|CUDA error", re.I)


def mem_available_mib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return float("nan")


def gpu_used_mib(index: int) -> float:
    out = subprocess.run(["nvidia-smi", "-i", str(index), "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    return float(out)


class Sampler(threading.Thread):
    def __init__(self, gpu: int, period: float = 0.5):
        super().__init__(daemon=True)
        self.gpu, self.period, self.stop = gpu, period, threading.Event()
        self.base_gpu = gpu_used_mib(gpu)
        self.base_ram = mem_available_mib()
        self.peak_gpu = self.base_gpu
        self.min_ram = self.base_ram

    def run(self):
        while not self.stop.is_set():
            try:
                self.peak_gpu = max(self.peak_gpu, gpu_used_mib(self.gpu))
            except Exception:
                pass
            self.min_ram = min(self.min_ram, mem_available_mib())
            time.sleep(self.period)


def run_config(a, n: int, kv: str, logdir: Path) -> dict:
    cmd = [a.bench, "-m", a.model, "-ngl", "99", "-fa", "on", "-ncmoe", str(n), "-ctk", kv, "-ctv", kv,
           "-p", str(a.pp), "-n", str(a.tg), "-r", str(a.reps), "-o", "json"]
    log = logdir / f"p3_ncmoe_N{n}_kv{kv}.log"
    s = Sampler(a.gpu)
    s.start()
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = -9, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "TIMEOUT"
    wall = time.time() - t0
    s.stop.set()
    s.join()
    log.write_text(f"$ {' '.join(cmd)}\nrc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
    res = {"n_cpu_moe": n, "kv": kv, "rc": rc, "wall_s": round(wall, 1),
           "peak_gpu_used_mib": s.peak_gpu, "gpu_used_before_mib": s.base_gpu,
           "min_mem_available_mib": round(s.min_ram), "mem_available_before_mib": round(s.base_ram),
           "oom": bool(OOM_RE.search(err + out)), "log": str(log), "tests": []}
    if rc == 0:
        try:
            for row in json.loads(out):
                res["tests"].append({"n_prompt": row.get("n_prompt"), "n_gen": row.get("n_gen"),
                                     "avg_ts": row.get("avg_ts"), "stddev_ts": row.get("stddev_ts"),
                                     "samples_ts": row.get("samples_ts")})
        except Exception as e:
            res["parse_error"] = str(e)
    return res


def main():
    here = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser()
    p.add_argument("--bench", default=str(here.parent / ".aicf-work/bin/llama-bench"))
    p.add_argument("--model", default=str(here.parent / ".aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf"))
    p.add_argument("--gpu", type=int, default=0)
    p.add_argument("--ns", default="18,22,26,30,34,48")
    p.add_argument("--kvs", default="f16,q8_0")
    p.add_argument("--pp", type=int, default=512)
    p.add_argument("--tg", type=int, default=128)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--out", default=str(here / "benchmarks/reports/p3_ncmoe_sweep.jsonl"))
    a = p.parse_args()
    logdir = Path(a.out).parent / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    env_note = {"model": a.model, "bench": a.bench, "ld_library_path": os.environ.get("LD_LIBRARY_PATH", "")}
    with open(a.out, "a") as f:
        f.write(json.dumps({"_meta": env_note, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n")
        for n in [int(x) for x in a.ns.split(",")]:
            for kv in a.kvs.split(","):
                res = run_config(a, n, kv, logdir)
                f.write(json.dumps(res) + "\n")
                f.flush()
                tg = next((t["avg_ts"] for t in res["tests"] if t.get("n_gen")), None)
                print(f"N={n} kv={kv} rc={res['rc']} oom={res['oom']} peakGPU={res['peak_gpu_used_mib']:.0f}MiB "
                      f"minRAMavail={res['min_mem_available_mib']}MiB tg={tg}", flush=True)


if __name__ == "__main__":
    main()
