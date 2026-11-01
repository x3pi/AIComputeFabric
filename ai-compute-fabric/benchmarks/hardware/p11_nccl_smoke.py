#!/usr/bin/env python3
"""P11 2-Node PyTorch Distributed (Gloo + NCCL) Smoke & Bandwidth Test."""
import argparse
import datetime
import os
import socket
import time
import torch
import torch.distributed as dist


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rank", type=int, required=True)
    parser.add_argument("--world-size", type=int, default=2)
    parser.add_argument("--master-addr", type=str, default="10.42.0.1")
    parser.add_argument("--master-port", type=int, default=29500)
    parser.add_argument("--backend", type=str, default="nccl", choices=["gloo", "nccl"])
    args = parser.parse_args()

    os.environ["MASTER_ADDR"] = args.master_addr
    os.environ["MASTER_PORT"] = str(args.master_port)
    os.environ["RANK"] = str(args.rank)
    os.environ["WORLD_SIZE"] = str(args.world_size)

    print(
        f"[Rank {args.rank}] host={socket.gethostname()} initializing {args.backend} "
        f"master={args.master_addr}:{args.master_port} "
        f"NCCL_SOCKET_IFNAME={os.environ.get('NCCL_SOCKET_IFNAME')} "
        f"GLOO_SOCKET_IFNAME={os.environ.get('GLOO_SOCKET_IFNAME')}",
        flush=True,
    )
    t0 = time.perf_counter()
    dist.init_process_group(
        backend=args.backend,
        init_method=f"tcp://{args.master_addr}:{args.master_port}",
        rank=args.rank,
        world_size=args.world_size,
        timeout=datetime.timedelta(seconds=20),
    )
    init_ms = (time.perf_counter() - t0) * 1000.0
    print(f"[Rank {args.rank}] init_process_group({args.backend}) SUCCESS in {init_ms:.1f} ms", flush=True)

    device = torch.device("cuda:0" if args.backend == "nccl" else "cpu")
    if args.backend == "nccl":
        print(f"[Rank {args.rank}] GPU={torch.cuda.get_device_name(0)}", flush=True)

    # 1. Correctness check: all_reduce
    x = torch.tensor([float(args.rank + 1) * 10.0, 42.0], device=device)
    dist.all_reduce(x, op=dist.ReduceOp.SUM)
    if args.backend == "nccl":
        torch.cuda.synchronize()
    print(f"[Rank {args.rank}] all_reduce result={x.tolist()} (expected [30.0, 84.0])", flush=True)
    assert abs(x[0].item() - 30.0) < 1e-4 and abs(x[1].item() - 84.0) < 1e-4

    # 2. Point-to-point send/recv (simulates Pipeline Parallel activation transfer: [batch=16, seq=1, hidden=4096] fp16 = 128 KB)
    pp_tensor = torch.ones((16, 4096), dtype=torch.float16, device=device) * (args.rank + 1)
    latencies_ms = []
    for i in range(25):
        if args.backend == "nccl":
            torch.cuda.synchronize()
        t_step = time.perf_counter()
        if args.rank == 0:
            dist.send(pp_tensor, dst=1)
            dist.recv(pp_tensor, src=1)
        else:
            dist.recv(pp_tensor, src=0)
            pp_tensor.add_(1.0)
            dist.send(pp_tensor, dst=0)
        if args.backend == "nccl":
            torch.cuda.synchronize()
        if i >= 5:
            latencies_ms.append((time.perf_counter() - t_step) * 1000.0)

    latencies_ms.sort()
    med_rtt = latencies_ms[len(latencies_ms) // 2]
    print(
        f"[Rank {args.rank}] PP p2p 128KB RTT median={med_rtt:.2f} ms "
        f"(one-way ~{med_rtt/2:.2f} ms) min={latencies_ms[0]:.2f} max={latencies_ms[-1]:.2f} ms",
        flush=True,
    )
    dist.destroy_process_group()
    print(f"[Rank {args.rank}] ALL CHECKS PASSED!", flush=True)


if __name__ == "__main__":
    main()

