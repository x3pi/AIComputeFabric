#!/usr/bin/env python3
"""Multi-node E2E Benchmark: Linux (RTX 3060) + Windows (RTX 5070) via site-gateway mTLS.
Measures:
  1. Linux-only (pc-001 running, pc-win-01 paused)
  2. Windows-only (pc-win-01 running, pc-001 paused)
  3. Multi-node Parallel (pc-001 + pc-win-01 both running in parallel)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

GW_HTTP = "http://127.0.0.1:18080"
ADMIN_TOKEN = "demo-admin-token"


def req(path: str, method: str = "GET", data: dict | None = None) -> dict | list:
    url = f"{GW_HTTP}{path}"
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    r = urllib.request.Request(url, headers=headers, data=body, method=method)
    with urllib.request.urlopen(r, timeout=10) as resp:
        if resp.status == 204:
            return {}
        return json.loads(resp.read().decode("utf-8"))


def set_worker_state(worker_id: str, action: str):
    req(f"/api/workers/{worker_id}/{action}", method="POST")


def wait_workers(expected: dict[str, str], timeout: float = 30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        workers = {w["worker_id"]: w["state"] for w in req("/api/workers")}
        if all(workers.get(wid) == st for wid, st in expected.items()):
            return workers
        time.sleep(0.5)
    raise RuntimeError(f"workers did not reach {expected} within {timeout}s: got {workers}")


def run_load(inferload: str, concurrency: int = 4, duration: str = "15s") -> dict:
    cmd = [
        inferload,
        "-url", f"{GW_HTTP}/v1/chat/completions",
        "-model", "Qwen3-4B-Q4_K_M.gguf",
        "-c", str(concurrency),
        "-duration", duration,
        "-max-tokens", "64",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inferload", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", default="15s")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    results = {
        "schema_version": 1,
        "title": "First Multi-node Heterogeneous Cluster Benchmark (Linux RTX 3060 + Windows RTX 5070)",
        "network": "2.5 GbE LAN via gRPC mTLS",
        "nodes": [
            {"id": "pc-001", "os": "Linux x86_64", "gpu": "NVIDIA GeForce RTX 3060 12GB", "ip": "192.168.1.40"},
            {"id": "pc-win-01", "os": "Windows 10 Pro", "gpu": "NVIDIA GeForce RTX 5070 12GB", "ip": "192.168.1.49"},
        ],
        "model": "Qwen3-4B-Q4_K_M.gguf",
        "concurrency": args.concurrency,
        "duration_per_run": args.duration,
        "runs": {},
    }

    # Configuration 1: Linux Only (pc-001 running, pc-win-01 paused)
    print("=== Config 1: Linux Only (pc-001) ===")
    set_worker_state("pc-win-01", "pause")
    set_worker_state("pc-001", "resume")
    wait_workers({"pc-001": "running", "pc-win-01": "paused"})
    time.sleep(2)
    load_linux = run_load(args.inferload, args.concurrency, args.duration)
    results["runs"]["linux_only_rtx3060"] = load_linux
    print(f"Linux Only: {load_linux.get('aggregate_tok_s', 0):.1f} tok/s, ok={load_linux.get('ok')}/{load_linux.get('requests')}, TTFT p50: {load_linux.get('ttft_ms_p50', 0)} ms")

    # Configuration 2: Windows Only (pc-win-01 running, pc-001 paused)
    print("=== Config 2: Windows Only (pc-win-01) ===")
    set_worker_state("pc-001", "pause")
    set_worker_state("pc-win-01", "resume")
    wait_workers({"pc-001": "paused", "pc-win-01": "running"})
    time.sleep(2)
    load_win = run_load(args.inferload, args.concurrency, args.duration)
    results["runs"]["windows_only_rtx5070"] = load_win
    print(f"Windows Only: {load_win.get('aggregate_tok_s', 0):.1f} tok/s, ok={load_win.get('ok')}/{load_win.get('requests')}, TTFT p50: {load_win.get('ttft_ms_p50', 0)} ms")

    # Configuration 3: Dual Node Parallel (pc-001 + pc-win-01 both running)
    print("=== Config 3: Dual Node Parallel (Linux + Windows) ===")
    set_worker_state("pc-001", "resume")
    set_worker_state("pc-win-01", "resume")
    wait_workers({"pc-001": "running", "pc-win-01": "running"})
    time.sleep(2)
    load_parallel = run_load(args.inferload, args.concurrency * 2, args.duration)
    results["runs"]["multinode_parallel_linux_plus_windows"] = load_parallel
    print(f"Dual Node Parallel: {load_parallel.get('aggregate_tok_s', 0):.1f} tok/s, ok={load_parallel.get('ok')}/{load_parallel.get('requests')}, TTFT p50: {load_parallel.get('ttft_ms_p50', 0)} ms")

    # Final stats
    results["gateway_stats_final"] = req("/api/stats")
    results["workers_final"] = req("/api/workers")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Successfully generated {out_path}")


if __name__ == "__main__":
    main()
