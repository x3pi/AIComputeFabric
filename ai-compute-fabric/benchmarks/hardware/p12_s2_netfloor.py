#!/usr/bin/env python3
"""P12 / S2 - network floor: ping-pong latency of small gloo and NCCL messages between rank 0 (Linux) and rank 1 (WSL2).
Usage (both nodes): python p12_s2_netfloor.py RANK MASTER_ADDR   -> prints JSON lines to stdout."""
import json, sys, time, statistics as st
import torch, torch.distributed as dist

rank, master = int(sys.argv[1]), sys.argv[2]
torch.cuda.set_device(0)
dist.init_process_group("nccl", init_method=f"tcp://{master}:29510", rank=rank, world_size=2,
                        device_id=torch.device("cuda:0"))
cpu = dist.new_group(backend="gloo")
peer = 1 - rank

def pct(xs, p): xs = sorted(xs); return xs[min(len(xs) - 1, int(len(xs) * p))]

def bench(name, fn, n=500, warm=50):
    ts = []
    for i in range(n + warm):
        t = time.perf_counter(); fn(); dt = (time.perf_counter() - t) * 1e3
        if i >= warm: ts.append(dt)
    if rank == 0:
        print(json.dumps({"test": name, "n": n, "mean_ms": round(st.mean(ts), 3), "p50_ms": round(pct(ts, .5), 3),
                          "p90_ms": round(pct(ts, .9), 3), "p99_ms": round(pct(ts, .99), 3)}), flush=True)

for nbytes in (8, 8192, 65536):
    c = torch.zeros(nbytes, dtype=torch.uint8)
    def gloo_pp():
        if rank == 0: dist.send(c, peer, group=cpu); dist.recv(c, peer, group=cpu)
        else: dist.recv(c, peer, group=cpu); dist.send(c, peer, group=cpu)
    bench(f"gloo_pingpong_{nbytes}B", gloo_pp)
    g = torch.zeros(nbytes, dtype=torch.uint8, device="cuda")
    def nccl_pp():
        if rank == 0: dist.send(g, peer); dist.recv(g, peer)
        else: dist.recv(g, peer); dist.send(g, peer)
        torch.cuda.current_stream().synchronize()
    bench(f"nccl_pingpong_{nbytes}B", nccl_pp)
h = torch.zeros(4096, dtype=torch.float16, device="cuda")
def nccl_oneway():
    if rank == 0:
        w = dist.isend(h, peer); w.wait(); torch.cuda.current_stream().synchronize()
        dist.recv(h[:1], peer)
    else:
        dist.recv(h, peer); dist.send(h[:1], peer); torch.cuda.current_stream().synchronize()
bench("nccl_oneway_hidden_plus_ack", nccl_oneway)
dist.barrier(); dist.destroy_process_group()
