#!/usr/bin/env python3
"""P12 / S4 - same-session comparison: SGLang depth0, SGLang depth1, vLLM; PP=2 (13,23); C=1 and C=4; rotating order."""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p11_2gpu_runner as P
import p12_s0_repro as S0
import p12_s3_depth as D
S0.OUT = P.REPORTS_DIR / "p12_s4_session.jsonl"
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--rounds", type=int, default=7); ap.add_argument("--start-round", type=int, default=1); a = ap.parse_args()
    variants = [("sglang_d0", D.make(0)), ("sglang_d1", D.make(1)), ("vllm", P.launch_vllm_pp2_cluster)]
    for r in range(a.start_round, a.start_round + a.rounds):
        k = r % 3; order = variants[k:] + variants[:k]
        for name, fn in order:
            S0.measure(name, fn, "13,23", r, (1, 4))
