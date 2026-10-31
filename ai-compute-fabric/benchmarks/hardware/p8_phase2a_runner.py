#!/usr/bin/env python3
"""Pha 2A (Fixed & Verified): Micro-benchmark of kt-kernel==0.7.2 LLAMAFILE (AVX2) on Qwen3-30B-A3B-Q4_K_M.gguf.

Fixes applied from audit & root-cause analysis:
1. Unbiased without-replacement top-k expert routing (`torch.topk` over 128 logits -> 8 distinct experts)
   sampled independently per layer and per token/step (never reusing a single lucky draw across 48 layers).
   Logs both `expected_cpu_experts_per_tok` and `actual_cpu_experts_per_tok`.
2. Separate subprocess per `cpuinfer_threads` config (because `BaseMoEWrapper._cpu_infer_instance` is a
   process-wide singleton in `kt-kernel`).
3. Loads the 48 `LlamafileMoEWrapper` layers ONCE per subprocess and updates the shared pinned
   `gpu_experts_mask` (`uint8_t*`) and `max_deferred_experts_per_token` in-place across configs,
   preventing C++ `MOE` / CUDA pinned-memory leaks (`NV_ERR_NO_MEMORY`).
4. Respects `CPUInfer`'s internal `NumaPool` per-core thread pinning (`numa_0_m_0` and `numa_0_t_1..th-1`
   on cores `0..th-1`) and isolates host/CUDA callback threads (`python3`, `cuda-EvtHandlr`) onto the
   remaining cores (`th..19`) when `th < 20` so busy-spinning worker threads do not starve CUDA stream callbacks.
5. For decode (`qlen=1`), each repetition measures `decode_steps=16` consecutive tokens across all 48 layers
   (768 layer calls = 6,144 expert evaluations per repetition) and normalizes to ms / token (48 layers).
"""
import argparse
import glob
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path


def mem_available_mib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024.0
    return float("nan")


def check_kernel_oom(since_ts: str) -> list[str]:
    try:
        res = subprocess.run(
            ["journalctl", "-k", f"--since={since_ts}"],
            capture_output=True, text=True, timeout=10
        )
        return [l for l in res.stdout.splitlines() if "oom" in l.lower() or "out of memory" in l.lower()]
    except Exception:
        return []


def isolate_host_threads(worker_threads: int, total_cores: int = 20) -> dict:
    """Move non-WorkerPool threads (python3, cuda-EvtHandlr, etc.) onto cores [worker_threads..total_cores-1]
    when worker_threads < total_cores, and record actual WorkerPool thread affinities from /proc/self/task."""
    worker_pins = {}
    host_cores = set(range(worker_threads, total_cores)) if worker_threads < total_cores else set(range(total_cores))
    for p in sorted(glob.glob("/proc/self/task/*/status")):
        try:
            tid = int(os.path.basename(os.path.dirname(p)))
            txt = Path(p).read_text()
            name = [l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("Name:")][0]
            cpus = [l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("Cpus_allowed_list:")][0]
            if name.startswith("numa_"):
                worker_pins[name] = cpus
            elif worker_threads < total_cores:
                os.sched_setaffinity(tid, host_cores)
        except Exception:
            pass
    return {
        "worker_thread_count": len(worker_pins),
        "worker_cores": f"0-{worker_threads - 1}",
        "host_cores": f"{worker_threads}-{total_cores - 1}" if worker_threads < total_cores else f"0-{total_cores - 1} (shared/oversubscribed)",
    }


def run_worker_for_threads(args):
    import torch
    from kt_kernel.experts_base import BaseMoEWrapper
    from kt_kernel.utils.llamafile import LlamafileMoEWrapper

    out_dir = Path(args.out_dir).resolve()
    jsonl_path = out_dir / "p8_ktransformers_p2a_microbench.jsonl"
    log_path = out_dir / "logs" / "p8_p2a_microbench.log"

    th = args.worker_threads
    avail_ram = mem_available_mib()
    print(f"\n=== WORKER SUBPROCESS: threads={th}, MemAvailable={avail_ram:.1f} MiB ===", flush=True)
    if avail_ram < 8192.0:
        raise RuntimeError(f"ABORT: MemAvailable {avail_ram:.1f} MiB < 8192 MiB required!")

    torch.manual_seed(20261030 + th)
    device = "cuda:0"
    stream = torch.cuda.current_stream()
    stream_int = stream.cuda_stream

    qlens = [1, 512, 2048]
    decode_steps = 16  # 16 tokens * 48 layers = 768 layer evaluations per decode repetition
    num_routing_slices = 256  # Pre-generated independent without-replacement top-8 routing tables

    inputs_by_qlen = {}
    for q in qlens:
        hs = torch.randn(q, 2048, dtype=torch.bfloat16, device=device)
        n_slices = num_routing_slices if q == 1 else 64
        # Without-replacement top-8 selection per token via topk over random router logits
        logits = torch.randn(n_slices, q, 128, dtype=torch.float32, device=device)
        topk_vals, topk_ids_pool = torch.topk(logits, k=8, dim=-1)
        topk_ids_pool = topk_ids_pool.to(torch.int64).contiguous()
        topk_weights_pool = torch.softmax(topk_vals, dim=-1).to(torch.bfloat16).contiguous()
        inputs_by_qlen[q] = (hs, topk_ids_pool, topk_weights_pool)

    # Instantiate and load all 48 layers ONCE in this subprocess
    t_load0 = time.time()
    init_mask = torch.zeros(128, dtype=torch.bool)
    layers = []
    for layer_i in range(48):
        w = LlamafileMoEWrapper(
            layer_idx=layer_i,
            num_experts=128,
            num_experts_per_tok=8,
            hidden_size=2048,
            moe_intermediate_size=768,
            gpu_experts_mask=init_mask,
            cpuinfer_threads=th,
            threadpool_count=1,
            weight_path=args.model,
            chunked_prefill_size=2048,
            max_deferred_experts_per_token=0,
        )
        w.load_weights()
        layers.append(w)
    load_s = time.time() - t_load0

    pin_info = isolate_host_threads(th, total_cores=20)
    print(f"Loaded 48 layers in {load_s:.2f}s. Pin info: {pin_info}", flush=True)

    # Global warmup across all 48 layers at K=0 so all pages and caches are warm
    hs1, ids1, w1 = inputs_by_qlen[1]
    for s in range(8):
        for i, l in enumerate(layers):
            l.forward(hs1, ids1[(s * 48 + i) % num_routing_slices], w1[(s * 48 + i) % num_routing_slices], stream_int)
    torch.cuda.synchronize()

    # Configurations to test for this thread count
    if th == 8:
        test_runs = [{"threads": 8, "k": k, "deferred": 0} for k in [75, 59, 48, 32, 0]]
        for def_val in [1, 2, 4]:
            test_runs.append({"threads": 8, "k": 48, "deferred": def_val})
            test_runs.append({"threads": 8, "k": 75, "deferred": def_val})
    else:
        # For th=18 (8P+10E, 2 cores reserved for host/CUDA) and th=20 (8P+12E, 0 cores reserved)
        test_runs = [{"threads": th, "k": k, "deferred": 0} for k in [75, 48, 0]]

    with open(jsonl_path, "a", encoding="utf-8") as jf, open(log_path, "a", encoding="utf-8") as lf:
        for run_idx, cfg in enumerate(test_runs):
            k = cfg["k"]
            def_val = cfg["deferred"]
            tag = f"th{th}_K{k}_def{def_val}"

            print(f"\n[{run_idx+1}/{len(test_runs)}] Configuring 48 layers for: {tag} (threads={th}, K={k}, CPU_exp={128-k}, deferred={def_val})...", flush=True)
            lf.write(f"\n--- Running {tag} ({pin_info}) ---\n")
            lf.flush()

            # Update shared pinned gpu_experts_mask and max_deferred_experts_per_token in-place
            for l in layers:
                l.gpu_experts_mask.zero_()
                if k > 0:
                    l.gpu_experts_mask[:k] = True
                l.num_gpu_experts = k
                l.max_deferred_experts_per_token = def_val
                BaseMoEWrapper._layer_has_pending_deferred[l.layer_idx] = False

            for q in qlens:
                hs, topk_ids_pool, topk_weights_pool = inputs_by_qlen[q]
                n_slices = topk_ids_pool.shape[0]
                steps_per_rep = decode_steps if q == 1 else 1
                warmup_steps = 4 if q == 1 else 2

                # Compute exact actual CPU experts per token across the slices used in measurement
                total_cpu_hits = 0
                total_token_layers = 0
                for rep in range(args.reps):
                    for s in range(steps_per_rep):
                        for l_idx in range(48):
                            sl = (rep * 53 + s * 17 + l_idx) % n_slices
                            ids_slice = topk_ids_pool[sl]
                            total_cpu_hits += int((ids_slice >= k).sum().item())
                            total_token_layers += q

                actual_cpu_exp_per_tok = total_cpu_hits / float(total_token_layers)
                expected_cpu_exp_per_tok = 8.0 * float(128 - k) / 128.0

                # Warmup passes across all 48 layers
                for l in layers:
                    BaseMoEWrapper._layer_has_pending_deferred[l.layer_idx] = False
                for w_step in range(warmup_steps):
                    for l_idx, l in enumerate(layers):
                        sl = (w_step * 48 + l_idx) % n_slices
                        l.forward(hs, topk_ids_pool[sl], topk_weights_pool[sl], stream_int)
                torch.cuda.synchronize()
                layers[0].cpu_infer.sync()

                # Measurement: args.reps repetitions
                durations_ms = []
                for rep in range(args.reps):
                    for l in layers:
                        BaseMoEWrapper._layer_has_pending_deferred[l.layer_idx] = False
                    torch.cuda.synchronize()
                    t0 = time.perf_counter()
                    for s in range(steps_per_rep):
                        base_idx = rep * 53 + s * 17
                        for l_idx, l in enumerate(layers):
                            sl = (base_idx + l_idx) % n_slices
                            l.forward(hs, topk_ids_pool[sl], topk_weights_pool[sl], stream_int)
                    torch.cuda.synchronize()
                    layers[0].cpu_infer.sync()
                    # Normalize by steps_per_rep so dur_ms is always the time for ONE 48-layer pass of `q` tokens
                    dur_ms = ((time.perf_counter() - t0) * 1000.0) / float(steps_per_rep)
                    durations_ms.append(dur_ms)

                med_total_ms = statistics.median(durations_ms)
                min_total_ms = min(durations_ms)
                max_total_ms = max(durations_ms)
                med_per_layer_ms = med_total_ms / 48.0
                med_per_tok_ms = med_total_ms / float(q)
                cpu_moe_only_tok_s = (float(q) * 1000.0) / med_total_ms

                record = {
                    "tag": tag,
                    "threads": th,
                    "worker_cores": pin_info["worker_cores"],
                    "host_cores": pin_info["host_cores"],
                    "k_gpu_experts": k,
                    "cpu_experts": 128 - k,
                    "expected_cpu_experts_per_tok": round(expected_cpu_exp_per_tok, 3),
                    "actual_cpu_experts_per_tok": round(actual_cpu_exp_per_tok, 3),
                    "max_deferred": def_val,
                    "qlen": q,
                    "steps_per_rep": steps_per_rep,
                    "mode": "decode" if q == 1 else "prefill",
                    "reps": args.reps,
                    "samples_total_ms": [round(x, 3) for x in durations_ms],
                    "total_ms": {
                        "median": round(med_total_ms, 3),
                        "min": round(min_total_ms, 3),
                        "max": round(max_total_ms, 3)
                    },
                    "per_layer_ms": round(med_per_layer_ms, 3),
                    "per_tok_ms": round(med_per_tok_ms, 3),
                    "cpu_moe_only_tok_s": round(cpu_moe_only_tok_s, 2),
                    "load_time_s": round(load_s, 2),
                    "mem_available_mib": round(mem_available_mib(), 1),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")
                }

                jf.write(json.dumps(record, ensure_ascii=False) + "\n")
                jf.flush()

                msg = (
                    f"  qlen={q:4d} ({record['mode']}): 48L_CPU_MoE={med_total_ms:.2f} ms "
                    f"(min={min_total_ms:.2f}, max={max_total_ms:.2f}), "
                    f"per_tok={med_per_tok_ms:.3f} ms, "
                    f"cpu_exp_hit={actual_cpu_exp_per_tok:.2f}/8 (exp={expected_cpu_exp_per_tok:.2f}), "
                    f"cpu_moe_ceiling={cpu_moe_only_tok_s:.1f} tok/s"
                )
                print(msg, flush=True)
                lf.write(msg + "\n")
                lf.flush()


def main():
    p = argparse.ArgumentParser(description="Phase 2A kt-kernel LLAMAFILE Micro-benchmark (Fixed)")
    p.add_argument("--model", default=".aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf")
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--out-dir", default="ai-compute-fabric/benchmarks/reports")
    p.add_argument("--worker-threads", type=int, default=0, help="Internal flag for subprocess worker")
    args = p.parse_args()

    if args.worker_threads > 0:
        run_worker_for_threads(args)
        return

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "p8_ktransformers_p2a_microbench.jsonl"
    log_path = out_dir / "logs" / "p8_p2a_microbench.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if jsonl_path.exists():
        jsonl_path.unlink()
    if log_path.exists():
        log_path.unlink()

    t_start_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    print("=== STARTING FIXED PHASE 2A: KT-KERNEL LLAMAFILE MICRO-BENCHMARK ===", flush=True)

    # Separate subprocess per thread count so BaseMoEWrapper._cpu_infer_instance singleton is fresh:
    # - 8:  8 P-cores (0-7) for WorkerPool, cores 8-19 for host/CUDA callbacks
    # - 18: 8 P-cores + 10 E-cores (0-17) for WorkerPool, cores 18-19 for host/CUDA callbacks
    # - 20: all 20 cores (0-19) for WorkerPool (demonstrates callback starvation when 0 cores left for host)
    for th in [8, 18, 20]:
        cmd = [
            sys.executable, "-I", __file__,
            "--model", args.model,
            "--reps", str(args.reps),
            "--out-dir", args.out_dir,
            "--worker-threads", str(th),
        ]
        print(f"\n>>> Launching subprocess for threads={th}: {' '.join(cmd)}", flush=True)
        rc = subprocess.call(cmd)
        if rc != 0:
            raise RuntimeError(f"Worker subprocess for threads={th} failed with rc={rc}")

    oom_logs = check_kernel_oom(t_start_iso)
    if oom_logs:
        print(f"WARNING: OOM detected during Phase 2A: {oom_logs}", flush=True)
    else:
        print("\nNo kernel OOM detected during Phase 2A.", flush=True)

    print("=== FIXED PHASE 2A MICRO-BENCHMARK COMPLETED SUCCESSFULLY ===", flush=True)


if __name__ == "__main__":
    main()
