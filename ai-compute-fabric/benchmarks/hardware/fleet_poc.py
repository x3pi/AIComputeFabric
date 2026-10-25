#!/usr/bin/env python3
"""Gateway <-> worker-guard end-to-end check (plan P2.2), single machine.

Runs one site-gateway and N REAL worker-guard processes (each with its own
llama-server on the same GPU, small model). It is NOT a fleet benchmark: all
workers share one GPU and one host, so it proves the control path only
(register, heartbeat, pause directive, offline detection), not scale.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path


def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.load(r)


def post(url):
    req = urllib.request.Request(url, method="POST")
    return urllib.request.urlopen(req, timeout=3).status


def wait(pred, timeout, step=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return time.time() - t0
        time.sleep(step)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--guard", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", required=True)
    a = ap.parse_args()
    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    procs, res = [], {"workers": a.workers, "checks": {}}
    api = "http://127.0.0.1:18080"

    def fleet():
        return {w["worker_id"]: w for w in get(api + "/api/workers")}

    try:
        procs.append(subprocess.Popen([a.gateway, "-grpc", "127.0.0.1:17443", "-http", "127.0.0.1:18080"],
                                      stdout=open(wd / "gateway.log", "w"), stderr=subprocess.STDOUT))
        time.sleep(1)
        guards = {}
        for i in range(a.workers):
            wid = f"pc-{i + 1:03d}"
            guards[wid] = subprocess.Popen(
                [a.guard, "--llama-server", a.server, "--model", a.model, "--ctx", "1024", "--headroom-mib", "512",
                 "--resume-stable", "2s", "--engine-port", str(18100 + i), "--listen", f"127.0.0.1:{18200 + i}",
                 "--gateway", "127.0.0.1:17443", "--worker-id", wid],
                stdout=open(wd / f"{wid}.log", "w"), stderr=subprocess.STDOUT)
        procs += guards.values()
        t = wait(lambda: len(fleet()) == a.workers and all(w["state"] == "running" for w in fleet().values()), 240, 0.5)
        res["checks"]["all_running_s"] = t
        assert t is not None, "workers never all running"
        time.sleep(2)
        f = fleet()
        res["checks"]["buckets_reported"] = all(w["buckets"] and w["buckets"]["Weights"] > 0 for w in f.values())

        # operator pause through the gateway -> engine of THAT worker stops, others keep running
        victim = "pc-002"
        post(f"{api}/api/workers/{victim}/pause")
        t = wait(lambda: fleet()[victim]["state"] == "paused", 10)
        res["checks"]["pause_applied_s"] = t
        others = [w["state"] for k, w in fleet().items() if k != victim]
        res["checks"]["others_still_running"] = all(s == "running" for s in others)
        post(f"{api}/api/workers/{victim}/resume")
        t = wait(lambda: fleet()[victim]["state"] == "running", 120, 0.5)
        res["checks"]["resume_to_running_s"] = t

        # a worker that dies must show offline within a few seconds
        dead = "pc-003"
        os.kill(guards[dead].pid, signal.SIGKILL)
        t = wait(lambda: fleet()[dead]["state"] == "offline", 15)
        res["checks"]["offline_detected_s"] = t
        res["checks"]["survivors_online"] = all(w["online"] for k, w in fleet().items() if k != dead)
    finally:
        for p in procs:
            p.terminate()
        time.sleep(2)
        subprocess.run(["pkill", "-x", "llama-server"], check=False)
    c = res["checks"]
    res["pass"] = bool(c.get("buckets_reported") and c.get("pause_applied_s") is not None and c.get("others_still_running")
                       and c.get("resume_to_running_s") is not None and c.get("offline_detected_s") is not None
                       and c.get("survivors_online"))
    Path(a.out).write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
