#!/usr/bin/env python3
"""P12 / Pha S1c — run SGLang PP=2 (13,23) from the instrumented fork worktree with SGLANG_PP_NET_TRACE=1 and collect the traces.

The P11 launch function is patched at import time (copy of its source) so that both ranks use
PYTHONPATH=<sglang-v0519>/python (Linux) and /opt/sglang-src/python (WSL2) and enable the trace. Nothing is installed.
Output: reports/p12_s6_trace_early.jsonl (tok/s), raw traces in reports/logs/p12/trace_*/ (rank 0 = Linux, rank 1 = WSL2).
"""
from __future__ import annotations

import argparse
import inspect
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P
import p12_s0_repro as S0

OUT = P.REPORTS_DIR / "p12_s6_trace_early.jsonl"
P.LOGS_DIR = S0.P12_LOGS
SRC_LINUX = "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/sglang-v0519/python"
SRC_WSL = "/opt/sglang-src/python"
TRACE_DIR = "/tmp/sglang_pp_trace"

_src = inspect.getsource(P.launch_sglang_pp2_cluster)
_a = 'env0["NCCL_P2P_DISABLE"] = "1"\n'
_b = "export NCCL_P2P_DISABLE=1\n"
assert _src.count(_a) == 1 and _src.count(_b) == 1
_src = _src.replace(_a, _a + f'    env0["PYTHONPATH"] = "{SRC_LINUX}"\n    env0["SGLANG_PP_NET_TRACE"] = "1"\n    env0["SGLANG_PP_EARLY_PROXY"] = "1"\n    env0["SGLANG_PP_NET_TRACE_DIR"] = "{TRACE_DIR}"\n', 1)
_src = _src.replace(_b, _b + f"export PYTHONPATH={SRC_WSL}\nexport SGLANG_PP_NET_TRACE=1\nexport SGLANG_PP_EARLY_PROXY=1\nexport SGLANG_PP_NET_TRACE_DIR={TRACE_DIR}\n", 1)
_src = _src.replace("def launch_sglang_pp2_cluster(", "def launch_sglang_pp2_cluster_traced_early(", 1)
exec(compile(_src, "<p12_traced_launch>", "exec"), P.__dict__)  # noqa: S102 (own patched copy)


def wsl(cmd: str, timeout: int = 60) -> str:
    return P.run_wsl_bash(cmd, timeout=timeout)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="t1")
    ap.add_argument("--partition", default="13,23")
    ap.add_argument("--reps", type=int, default=7)
    a = ap.parse_args()
    shutil.rmtree(TRACE_DIR, ignore_errors=True)
    rec = {"phase": "S1c", "tag": a.tag, "partition": a.partition, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "mem_avail_mib_before": S0.mem_avail_mib()}
    dest = S0.P12_LOGS / f"trace_{a.tag}"
    try:
        P.stop_all_engines()
        wsl(f"rm -rf {TRACE_DIR}; mkdir -p {TRACE_DIR}")
        p0, p1, startup_s, info = P.launch_sglang_pp2_cluster_traced_early(layer_partition=a.partition)
        rec["startup_s"] = startup_s
        dec = p10_client.run_decode_series(base_url="http://127.0.0.1:18090", model="aicf", cfg_id=f"p12trace_{a.tag}",
                                           reps=a.reps, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = dec["decode_tok_s"]
        rec["status"] = "OK"
        print("decode (tracing ON):", rec["decode_tok_s"], flush=True)
    except Exception as e:  # noqa: BLE001
        rec["status"] = "FAILED"
        rec["error"] = f"{type(e).__name__}: {e}"
        print("ERROR", e, flush=True)
    finally:
        P.stop_all_engines()
        time.sleep(2)
        dest.mkdir(parents=True, exist_ok=True)
        if Path(TRACE_DIR).exists():
            shutil.copytree(TRACE_DIR, dest / "rank_linux", dirs_exist_ok=True)
        try:
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", P.SSH_HOST,
                                  f'{P.WSL_EXE} -d Ubuntu-24.04 -u root -- bash -c "cd {TRACE_DIR} && tar -cf - ."'],
                                 capture_output=True, timeout=120)
            (dest / "rank_wsl").mkdir(exist_ok=True)
            subprocess.run(["tar", "-C", str(dest / "rank_wsl"), "-xf", "-"], input=out.stdout, check=False)
        except Exception as e:  # noqa: BLE001
            rec["wsl_trace_error"] = str(e)
        P.append_jsonl(OUT, rec)


if __name__ == "__main__":
    main()
