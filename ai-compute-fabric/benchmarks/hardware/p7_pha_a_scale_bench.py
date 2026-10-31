#!/usr/bin/env python3
"""Pha A.4 — Simulated Scale Benchmark (K = 10, 25, 50 compute agents, N = 8, 16, 32).

Spins up:
  1. A real `site-gateway` instance on loopback (:28080 HTTP, :27443 gRPC).
  2. A Go gRPC feeder pushing Register + Heartbeat for K compute agents (alternating
     `single` and `group` with hidden RPC members) to `site-gateway`.
  3. Local HTTP fake worker endpoints with heterogeneous `tok_s_ref` (25..150 tok/s)
     and `slots` (1..4).
  4. Real `bestofn` planner + sampler + `SandboxPool(pool_size=4)` running N in {8, 16, 32},
     including 20% mid-flight agent drop at K=50, N=32.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bestofn.agents_client import ComputeAgentsClient
from bestofn.planner import build_plan
from bestofn.runner import SandboxPool
from bestofn.sampler import allocate_samples_to_agents, sample_candidates
from bestofn.selector import select_best_candidate

FEEDER_GO_SRC = r"""package main

import (
	"context"
	"encoding/json"
	"flag"
	"net/http"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type simWorker struct {
	WorkerID      string   `json:"worker_id"`
	State         string   `json:"state"`
	Model         string   `json:"model"`
	Slots         uint32   `json:"slots"`
	CtxSize       uint32   `json:"ctx_size"`
	InferenceURL  string   `json:"inference_url"`
	RPCWorkerIDs  []string `json:"rpc_worker_ids"`
	VRAMTotalGiB  uint64   `json:"vram_total_gib"`
	DecodeTokS    float64  `json:"decode_tok_s"`
}

func main() {
	grpcAddr := flag.String("grpc", "127.0.0.1:27443", "")
	simURL := flag.String("sim-url", "http://127.0.0.1:28099/sim/agents", "")
	flag.Parse()

	conn, err := grpc.NewClient(*grpcAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		panic(err)
	}
	defer conn.Close()
	client := pb.NewFleetGatewayClient(conn)
	registered := map[string]string{}
	ctx := context.Background()

	for {
		resp, err := http.Get(*simURL)
		if err == nil {
			var list []simWorker
			_ = json.NewDecoder(resp.Body).Decode(&list)
			resp.Body.Close()
			for _, w := range list {
				if registered[w.WorkerID] != w.InferenceURL {
					_, _ = client.Register(ctx, &pb.RegisterRequest{
						WorkerId:       w.WorkerID,
						Hostname:       w.WorkerID,
						VramTotalBytes: w.VRAMTotalGiB << 30,
						EngineUrl:      w.InferenceURL,
						ParallelSlots:  w.Slots,
					})
					registered[w.WorkerID] = w.InferenceURL
				}
				_, _ = client.Heartbeat(ctx, &pb.HeartbeatRequest{
					State:    w.State,
					Model:    w.Model,
					GpuTempC: 52,
					Capacity: &pb.WorkerCapacity{
						WorkerId:      w.WorkerID,
						VramFreeBytes: 4 << 30,
						Vram: &pb.VramBuckets{
							TotalBytes:       w.VRAMTotalGiB << 30,
							WeightsBytes:     6 << 30,
							KvStateBytes:     1 << 30,
							AllocatableBytes: 4 << 30,
						},
					},
				})
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
}
"""


def build_sim_workers(k: int, fake_port: int) -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    for i in range(k):
        agent_id = f"agent-{i:02d}"
        is_group = (i % 2 == 1)
        slots = (i % 4) + 1
        tok_s = 25.0 + (125.0 * (i / max(1, k - 1)))
        rpc_ids: list[str] = []
        if is_group:
            rpc_id = f"rpc-member-{i:02d}"
            rpc_ids = [rpc_id]
            workers.append({
                "worker_id": rpc_id,
                "state": "running",
                "model": "RPC-server",
                "slots": 1,
                "ctx_size": 0,
                "inference_url": f"rpc://127.0.0.1:{50052 + i}",
                "rpc_worker_ids": [],
                "vram_total_gib": 12,
                "decode_tok_s": 0.0,
            })
        ctx_per_slot = 32768 if is_group else 8192
        kind_tag = "kind_group" if is_group else "kind_single"
        workers.append({
            "worker_id": agent_id,
            "state": "running",
            "model": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
            "slots": slots,
            "ctx_size": ctx_per_slot * slots,
            "inference_url": f"http://127.0.0.1:{fake_port}/toks_{tok_s:.1f}/ctx_{ctx_per_slot}/{kind_tag}/{agent_id}",
            "rpc_worker_ids": rpc_ids,
            "vram_total_gib": 28 if is_group else 16,
            "decode_tok_s": round(tok_s, 2),
        })
    return workers


def main() -> int:
    state_lock = threading.Lock()
    sim_workers: list[dict[str, Any]] = []
    drop_set: set[str] = set()
    call_counts: dict[str, int] = {}

    class SimHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/sim/agents":
                with state_lock:
                    raw = json.dumps(sim_workers).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            parts = [p for p in self.path.split("/") if p]
            if len(parts) >= 4 and parts[-3] == "v1" and parts[-2] == "chat" and parts[-1] == "completions":
                agent_id = parts[-4]
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                tok_s = 80.0
                should_drop = False
                with state_lock:
                    call_counts[agent_id] = call_counts.get(agent_id, 0) + 1
                    for w in sim_workers:
                        if w["worker_id"] == agent_id:
                            tok_s = max(25.0, float(w.get("decode_tok_s", 80.0)))
                            if agent_id in drop_set:
                                w["state"] = "offline"
                                should_drop = True
                            break
                if should_drop:
                    err = json.dumps({"error": {"message": "simulated agent drop", "code": 503}}).encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", "0.05")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                time.sleep(2.5 / tok_s)
                resp = {
                    "choices": [{"message": {"content": "```python\ndef solve_square(x: int) -> int:\n    return x * x\n```"}}],
                    "usage": {"prompt_tokens": 32, "completion_tokens": 16},
                    "timings": {"predicted_per_second": tok_s},
                }
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SimHandler)
    fake_port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    feeder_dir = ROOT / "apps" / "site-gateway" / "cmd" / "simfeeder_tmp"
    feeder_dir.mkdir(parents=True, exist_ok=True)
    feeder_main = feeder_dir / "main.go"
    feeder_bin = ROOT / ".demo" / "bin" / "simfeeder_tmp"
    feeder_main.write_text(FEEDER_GO_SRC, encoding="utf-8")

    gw_proc = None
    feeder_proc = None
    try:
        subprocess.run(
            ["go", "build", "-o", str(feeder_bin), "./cmd/simfeeder_tmp"],
            cwd=str(ROOT / "apps" / "site-gateway"),
            check=True,
        )
    finally:
        if feeder_main.exists():
            feeder_main.unlink()
        if feeder_dir.exists():
            feeder_dir.rmdir()

    with tempfile.TemporaryDirectory():
        gw_http = "127.0.0.1:28080"
        gw_grpc = "127.0.0.1:27443"
        gw_url = f"http://{gw_http}"
        try:
            gw_proc = subprocess.Popen(
                [
                    str(ROOT / ".demo" / "bin" / "site-gateway"),
                    "-grpc", gw_grpc,
                    "-http", gw_http,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            feeder_proc = subprocess.Popen(
                [
                    str(feeder_bin),
                    "-grpc", gw_grpc,
                    "-sim-url", f"http://127.0.0.1:{fake_port}/sim/agents",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.6)

            client = ComputeAgentsClient(gateway_url=gw_url, api_key="", cache_ttl_s=0.05)
            pool = SandboxPool(pool_size=4, timeout_s=5.0)
            test_suites = ["assert solve_square(5) == 25\nassert solve_square(-4) == 16"]

            matrix_results: list[dict[str, Any]] = []
            for k in (10, 25, 50):
                with state_lock:
                    sim_workers[:] = build_sim_workers(k, fake_port)
                    drop_set.clear()
                    call_counts.clear()

                deadline = time.monotonic() + 5.0
                agents = []
                while time.monotonic() < deadline:
                    agents = client.list_agents(state="running", force_refresh=True)
                    if len(agents) == k:
                        break
                    time.sleep(0.15)
                assert len(agents) == k, f"Expected {k} compute agents (RPC hidden), got {len(agents)}"
                assert all(not a.agent_id.startswith("rpc-member-") for a in agents)

                for n in (8, 16, 32):
                    overheads_ms: list[float] = []
                    variants = []
                    alloc_map = {}
                    for _ in range(5):
                        t0 = time.perf_counter()
                        live = client.list_agents(
                            model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                            state="running",
                            force_refresh=True,
                        )
                        variants = build_plan(
                            [{"role": "user", "content": "Write solve_square(x) returning x*x."}],
                            n=n,
                            base_seed=1000,
                        )
                        alloc_map = allocate_samples_to_agents(variants, live)
                        overheads_ms.append((time.perf_counter() - t0) * 1000.0)

                    median_overhead_ms = sorted(overheads_ms)[len(overheads_ms) // 2]

                    with state_lock:
                        call_counts.clear()
                        drop_set.clear()

                    samples, samp_tel = sample_candidates(
                        variants=variants,
                        agents_client=client,
                        gateway_url=gw_url,
                        api_key="",
                        requested_model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                        max_tokens=64,
                        max_concurrency=32,
                    )

                    sandbox_ms = None
                    evals_per_sec = None
                    passed_count = sum(1 for s in samples if s.ok)
                    if k == 50 and n == 32:
                        evals, sb_tel = pool.evaluate_batch(
                            samples=samples,
                            test_suites=test_suites,
                            entry_point="solve_square",
                        )
                        sandbox_ms = sb_tel["sandbox_wall_ms"]
                        evals_per_sec = sb_tel["evals_per_sec"]
                        passed_count = sum(1 for e in evals if e.passed_tests == e.total_tests and e.compile_ok)

                    matrix_results.append({
                        "k_agents": k,
                        "n_samples": n,
                        "visible_compute_agents": len(agents),
                        "hidden_rpc_members": k // 2,
                        "planner_plus_gateway_overhead_ms": round(median_overhead_ms, 2),
                        "sampling_wall_ms": samp_tel["sampling_wall_ms"],
                        "sandbox_pool_m4_wall_ms": sandbox_ms,
                        "sandbox_evals_per_sec": evals_per_sec,
                        "candidates_completed": sum(1 for s in samples if s.ok),
                        "candidates_passed_tests": passed_count,
                        "served_by_agent": samp_tel["served_by_agent"],
                    })

            # Special test required by Pha A Check Pass (d):
            # K=50 agents, N=32 samples, drop 20% (10/50) agents mid-flight -> all 32 complete!
            with state_lock:
                sim_workers[:] = build_sim_workers(50, fake_port)
                drop_set.clear()
                call_counts.clear()
            time.sleep(1.2)
            live_50 = client.list_agents(
                model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                state="running",
                force_refresh=True,
            )
            variants_50 = build_plan(
                [{"role": "user", "content": "Write solve_square(x) returning x*x."}],
                n=32,
                base_seed=2026,
            )
            initial_alloc_50 = allocate_samples_to_agents(variants_50, live_50)
            allocated_agent_ids = sorted({a.agent_id for a in initial_alloc_50.values()})
            dropped_10 = set(allocated_agent_ids[:6])
            for a in live_50:
                if len(dropped_10) >= 10:
                    break
                dropped_10.add(a.agent_id)
            with state_lock:
                drop_set.update(dropped_10)

            samples_drop, samp_drop_tel = sample_candidates(
                variants=variants_50,
                agents_client=client,
                gateway_url=gw_url,
                api_key="",
                requested_model="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                max_tokens=64,
                max_concurrency=32,
            )
            evals_drop, sb_drop_tel = pool.evaluate_batch(
                samples=samples_drop,
                test_suites=test_suites,
                entry_point="solve_square",
            )
            sel_drop = select_best_candidate(evals_drop)

            k50_n32_overhead = next(
                r["planner_plus_gateway_overhead_ms"]
                for r in matrix_results
                if r["k_agents"] == 50 and r["n_samples"] == 32
            )
            report = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "evidence_label": "[ĐO]",
                "matrix": matrix_results,
                "k50_n32_drop_10_agents_test": {
                    "k_agents": 50,
                    "dropped_agents_count": len(dropped_10),
                    "dropped_agents": sorted(dropped_10),
                    "n_samples_requested": 32,
                    "n_samples_completed_ok": sum(1 for s in samples_drop if s.ok),
                    "n_samples_passed_sandbox": sum(1 for e in evals_drop if e.passed_tests == e.total_tests and e.compile_ok),
                    "reassignments_count": len(samp_drop_tel["reassignments"]),
                    "sampling_wall_ms": samp_drop_tel["sampling_wall_ms"],
                    "sandbox_wall_ms": sb_drop_tel["sandbox_wall_ms"],
                    "selected_sample_index": sel_drop.selected.sample_index if sel_drop.selected else None,
                },
                "check_pass_pha_a_d": {
                    "k50_n32_overhead_ms": k50_n32_overhead,
                    "overhead_under_50ms": k50_n32_overhead < 50.0,
                    "drop_10_of_50_completed_all_32": sum(1 for s in samples_drop if s.ok) == 32,
                },
            }
            out_path = ROOT / "benchmarks" / "reports" / "p7_pha_a_scale.json"
            out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
            assert report["check_pass_pha_a_d"]["overhead_under_50ms"], f"Overhead {k50_n32_overhead} ms >= 50 ms"
            assert report["check_pass_pha_a_d"]["drop_10_of_50_completed_all_32"]
        finally:
            if feeder_proc is not None:
                feeder_proc.terminate()
                feeder_proc.wait(timeout=3)
            if gw_proc is not None:
                gw_proc.terminate()
                gw_proc.wait(timeout=3)
            if feeder_bin.exists():
                feeder_bin.unlink()
            srv.shutdown()
            srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
