#!/usr/bin/env python3
"""GPU reclaim PoC harness (plan P2.1 / gate: GPU returned to the user in < 2 s).

Starts the real worker-guard (which runs llama-server), then plays "the user" by
launching a second GPU job that takes enough VRAM to push free memory under the
headroom. Timestamps come from this harness's own 10 ms nvidia-smi polling, so
they are independent of the guard's self-reported latency:
  t_cross : first sample with free VRAM < headroom
  t_gone  : first sample where the guard's engine pid no longer holds VRAM
Linux + single GPU only. Windows/WDDM is NOT covered.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path


def smi():
    q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, check=True).stdout.split(",")
    apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True).stdout.strip().splitlines()
    procs = {}
    for ln in apps:
        p = ln.split(",")
        if len(p) == 2:
            procs[int(p[0])] = int(p[1])
    return int(q[0]), int(q[1]), procs


def state(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
        return json.load(r)


def wait_for(pred, timeout, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guard", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--engine-model", required=True)
    ap.add_argument("--user-model", required=True)
    ap.add_argument("--user-ctx", type=int, default=16384)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--headroom", type=int, default=1536)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", required=True)
    a = ap.parse_args()
    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    gport = 18099
    guard = subprocess.Popen([a.guard, "--llama-server", a.server, "--model", a.engine_model, "--listen", f"127.0.0.1:{gport}",
                              "--headroom-mib", str(a.headroom), "--resume-stable", "3s", "--engine-log", str(wd / "engine.log")],
                             stdout=open(wd / "guard.log", "w"), stderr=subprocess.STDOUT)
    trials = []
    try:
        for i in range(a.trials):
            if not wait_for(lambda: state(gport)["state"] == "running", 240, 0.5):
                trials.append({"trial": i, "error": "engine never reached running"})
                break
            time.sleep(1.5)  # let the guard record the engine's own VRAM
            st = state(gport)
            pid, engine_mib = st["engine_pid"], st["engine_mib"]
            # the "user": a second GPU job, 100% independent of the guard
            user = subprocess.Popen([a.server, "-m", a.user_model, "-c", str(a.user_ctx), "-ngl", "99", "--port", "18091",
                                     "--host", "127.0.0.1", "--no-warmup", "--fit", "off"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t_cross = t_gone = None
            t0 = time.time()
            while time.time() - t0 < 60:
                used, total, procs = smi()
                now = time.time()
                if t_cross is None and total - used < a.headroom:
                    t_cross = now
                if t_cross is not None and pid not in procs:
                    t_gone = now
                    break
                if user.poll() is not None:
                    break
            row = {"trial": i, "engine_mib": engine_mib, "user_job_alive": user.poll() is None}
            if t_cross and t_gone:
                row["e2e_cross_to_gone_ms"] = round((t_gone - t_cross) * 1000, 1)
            else:
                row["error"] = "no crossing observed" if not t_cross else "engine pid still on GPU"
            time.sleep(1.0)
            ev = [e for e in state(gport)["events"] if e["kind"] == "reclaim"]
            if ev:
                row["guard_detect_to_freed_ms"] = ev[-1].get("detect_to_freed_ms")
                row["guard_stop_ms"] = ev[-1].get("stop_ms")
                row["signal"] = ev[-1].get("signal")
            trials.append(row)
            print(json.dumps(row), flush=True)
            user.kill()
            user.wait()
            # guard restarts the engine by itself once the GPU stays free
            time.sleep(1.0)
    finally:
        guard.terminate()
        try:
            guard.wait(15)
        except subprocess.TimeoutExpired:
            guard.kill()
        subprocess.run(["pkill", "-x", "llama-server"], check=False)
    e2e = [t["e2e_cross_to_gone_ms"] for t in trials if "e2e_cross_to_gone_ms" in t]
    summ = {"n_ok": len(e2e), "n": len(trials)}
    if e2e:
        s = sorted(e2e)
        summ |= {"min_ms": s[0], "median_ms": statistics.median(s), "p95_ms": s[min(len(s) - 1, int(0.95 * len(s)))], "max_ms": s[-1],
                 "gate_under_2000ms": s[-1] < 2000}
    Path(a.out).write_text(json.dumps({"schema_version": 1, "platform": "linux-single-gpu",
                                       "headroom_mib": a.headroom, "summary": summ, "trials": trials}, indent=2))
    print(json.dumps(summ))


if __name__ == "__main__":
    main()
