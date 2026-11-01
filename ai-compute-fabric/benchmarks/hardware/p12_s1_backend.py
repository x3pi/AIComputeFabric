#!/usr/bin/env python3
"""P12 / Pha S1a — does the SGLang attention/sampling backend explain the PP=2 C=1 gap to vLLM?

P11 launched SGLang with `--attention-backend triton --sampling-backend pytorch`; vLLM used its defaults.
This script alternates (ABAB, repeated) two SGLang variants on the same PP=2 (13,23) cluster:
  triton   : the P11 command line (baseline)
  default  : the same command with both backend flags removed (SGLang picks its own backends)
Only C=1 decode is measured. Logs go to reports/logs/p12/, results to reports/p12_s1_backend.jsonl.
"""
from __future__ import annotations

import argparse
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0

OUT = P.REPORTS_DIR / "p12_s1_backend.jsonl"
P.LOGS_DIR = S0.P12_LOGS

_src = inspect.getsource(P.launch_sglang_pp2_cluster)
_src = _src.replace('        "--attention-backend", "triton",\n', "")
_src = _src.replace('        "--sampling-backend", "pytorch",\n', "")
_src = _src.replace("  --attention-backend triton \\\\\n", "")
_src = _src.replace("  --sampling-backend pytorch \\\\\n", "")
_src = _src.replace("def launch_sglang_pp2_cluster(", "def launch_sglang_pp2_cluster_default_backends(", 1)
assert "attention-backend" not in _src and "sampling-backend" not in _src, "backend flags still present"
exec(compile(_src, "<p12_default_backends>", "exec"), P.__dict__)   # noqa: S102 (our own, patched copy)
_default = P.launch_sglang_pp2_cluster_default_backends


def run(variant: str, launcher, partition: str, r: int) -> None:
    rec = {"phase": "S1a", "variant": variant, "partition": partition, "round": r,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mem_avail_mib_before": S0.mem_avail_mib()}
    try:
        p0, p1, startup_s, cmd_info = launcher(layer_partition=partition)
        rec["startup_s"] = startup_s
        rec["rank0_cmd"] = cmd_info.get("rank0_cmd")
        dec = p10_client.run_decode_series(base_url="http://127.0.0.1:18090", model="aicf",
                                           cfg_id=f"p12s1_{variant}_r{r}", reps=7, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = dec["decode_tok_s"]
        rec["status"] = "OK"
        print(f"[{variant} r{r}] {rec['decode_tok_s']}", flush=True)
    except Exception as e:  # noqa: BLE001
        rec["status"] = "FAILED"
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"[{variant} r{r}] ERROR {e}", flush=True)
    finally:
        P.append_jsonl(OUT, rec)
        P.stop_all_engines()
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--partition", default="13,23")
    a = ap.parse_args()
    for r in range(1, a.rounds + 1):
        order = [("triton", P.launch_sglang_pp2_cluster), ("default", _default)]
        if r % 2 == 0:
            order.reverse()
        for name, fn in order:
            run(name, fn, a.partition, r)
