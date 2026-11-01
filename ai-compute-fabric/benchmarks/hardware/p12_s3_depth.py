#!/usr/bin/env python3
"""P12 / S3a - existing flag --pp-async-batch-depth on SGLang PP=2 (13,23), C=1 decode, interleaved rounds."""
import argparse, inspect, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0
OUT = P.REPORTS_DIR / "p12_s3_depth.jsonl"
BASE = inspect.getsource(P.launch_sglang_pp2_cluster)

def make(depth):
    if depth == 0:
        return P.launch_sglang_pp2_cluster
    src = BASE.replace('        "--pp-size", "2",\n', f'        "--pp-size", "2",\n        "--pp-async-batch-depth", "{depth}",\n')
    src = src.replace("  --pp-size 2 \\\\\n", f"  --pp-size 2 \\\\\n  --pp-async-batch-depth {depth} \\\\\n")
    assert src.count("pp-async-batch-depth") == 2
    name = f"launch_sglang_pp2_depth{depth}"
    src = src.replace("def launch_sglang_pp2_cluster(", f"def {name}(", 1)
    exec(compile(src, f"<p12_depth{depth}>", "exec"), P.__dict__)
    return getattr(P, name)

def run(depth, fn, r):
    rec = {"phase": "S3a", "variant": f"depth{depth}", "round": r, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        p0, p1, s, info = fn(layer_partition="13,23")
        rec["startup_s"] = s; rec["rank0_cmd"] = info.get("rank0_cmd")
        d = p10_client.run_decode_series(base_url="http://127.0.0.1:18090", model="aicf", cfg_id=f"p12s3_d{depth}_r{r}", reps=7, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = d["decode_tok_s"]; rec["status"] = "OK"; print(f"[depth{depth} r{r}] {d['decode_tok_s']}", flush=True)
    except Exception as e:
        rec["status"] = "FAILED"; rec["error"] = f"{type(e).__name__}: {e}"; print("ERR", e, flush=True)
    finally:
        P.append_jsonl(OUT, rec); P.stop_all_engines(); time.sleep(3)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--rounds", type=int, default=4); ap.add_argument("--depths", default="0,1")
    ap.add_argument("--start-round", type=int, default=1); a = ap.parse_args()
    depths = [int(x) for x in a.depths.split(",")]; fns = {d: make(d) for d in depths}
    for r in range(a.start_round, a.start_round + a.rounds):
        order = depths[:]; k = r % len(order); order = order[k:] + order[:k]   # rotate start each round
        for d in order: run(d, fns[d], r)
