#!/usr/bin/env python3
"""P12 / S7 - CUDA graphs ENABLED (decode, max bs 16) for SGLang (flag off/on) and vLLM (no --enforce-eager); PP=2 (13,23)."""
import argparse, inspect, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0
import p12_s5_early as E
E.OUT = P.REPORTS_DIR / "p12_s7_graph.jsonl"
SB = inspect.getsource(P.launch_sglang_pp2_cluster); VB = inspect.getsource(P.launch_vllm_pp2_cluster)
A = 'env0["NCCL_P2P_DISABLE"] = "1"\n'; B = "export NCCL_P2P_DISABLE=1\n"

def make_sglang(flag):
    s = SB.replace(A, A + f'    env0["PYTHONPATH"] = "{E.SRC_LINUX}"\n    env0["SGLANG_PP_EARLY_PROXY"] = "{flag}"\n', 1)
    s = s.replace(B, B + f"export PYTHONPATH={E.SRC_WSL}\nexport SGLANG_PP_EARLY_PROXY={flag}\n", 1)
    s = s.replace('        "--disable-decode-cuda-graph",\n', '        "--cuda-graph-max-bs-decode", "16",\n', 1)
    s = s.replace("  --disable-decode-cuda-graph {radix_flag_r1}", "  --cuda-graph-max-bs-decode 16 {radix_flag_r1}", 1)
    assert "--disable-decode-cuda-graph" not in s
    name = f"launch_sglang_pp2_graph{flag}"
    s = s.replace("def launch_sglang_pp2_cluster(", f"def {name}(", 1)
    exec(compile(s, name, "exec"), P.__dict__); return getattr(P, name)

def make_vllm():
    s = VB.replace('        "--enforce-eager",\n', "", 1).replace("  --enforce-eager --headless", "  --headless", 1)
    assert "enforce-eager" not in s
    s = s.replace("def launch_vllm_pp2_cluster(", "def launch_vllm_pp2_graph(", 1)
    exec(compile(s, "vllm_graph", "exec"), P.__dict__); return P.launch_vllm_pp2_graph

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--rounds", type=int, default=1); ap.add_argument("--variants", default="off,on,vllm"); ap.add_argument("--start-round", type=int, default=1)
    a = ap.parse_args()
    vs = {"off": make_sglang(0), "on": make_sglang(1), "vllm": make_vllm()}
    names = a.variants.split(",")
    for r in range(a.start_round, a.start_round + a.rounds):
        k = r % len(names); order = names[k:] + names[:k]
        for n in order: E.run(("graph_" + n), vs[n], r, check=(n != "vllm"))
