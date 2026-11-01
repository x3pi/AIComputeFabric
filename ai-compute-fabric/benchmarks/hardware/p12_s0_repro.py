#!/usr/bin/env python3
"""P12 / Pha S0 — tái lập khoảng cách PP=2 (13,23) giữa SGLang 0.5.19 và vLLM 0.32.0 ở C=1 (và C=4), xen kẽ ABAB.

Dùng lại hạ tầng của p11_2gpu_runner.py (cùng lệnh khởi động, cùng cầu mạng nccl0, cùng p10_client) nhưng:
  * ghi log vào reports/logs/p12/ (KHÔNG ghi đè log P11),
  * ghi kết quả vào reports/p12_s0_repro.jsonl,
  * mỗi vòng đo: SGLang -> vLLM, lặp --rounds vòng (ABAB).
Không đổi cấu hình dịch vụ nào ngoài việc dừng/khởi động engine thử nghiệm.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p10_client
import p11_2gpu_runner as P

P12_LOGS = P.REPORTS_DIR / "logs" / "p12"
P12_LOGS.mkdir(parents=True, exist_ok=True)
P.LOGS_DIR = P12_LOGS                       # launch_* đọc biến toàn cục này khi tạo file log


def _safe_stop_all_engines() -> None:
    """Stop only the test engines. P11's version runs `pkill -9 -f "[b]in/llama-server"`, which also matches the
    worker-guard command line (it contains --llama-server <path>/bin/llama-server) and kills the production guard."""
    import subprocess as sp
    for pat in ("[s]glang.launch_server", "[s]glang::", "[v]llm/bin/vllm", "[v]llm.entrypoints", "[V]LLM::", "[l]mdeploy serve"):
        sp.run(["pkill", "-9", "-f", pat], check=False)
    try:
        P.run_wsl_bash(
            "pkill -9 -f '[s]glang.launch_server' || true; pkill -9 -f '[s]glang::' || true; "
            "pkill -9 -f '[v]llm/bin/vllm' || true; pkill -9 -f '[v]llm.entrypoints' || true; pkill -9 -f '[V]LLM::' || true; "
            "pkill -9 -f '[l]mdeploy serve' || true")
    except Exception:  # noqa: BLE001
        pass
    for _ in range(30):
        time.sleep(0.5)
        try:
            if P.get_linux_gpu_and_mem()["vram_used_mib"] < 250:
                break
        except Exception:  # noqa: BLE001
            pass
    time.sleep(1.0)


P.stop_all_engines = _safe_stop_all_engines
OUT = P.REPORTS_DIR / "p12_s0_repro.jsonl"


def mem_avail_mib() -> float:
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024, 1)
    return -1.0


def measure(engine: str, launcher, partition: str, round_idx: int, concurrencies: tuple[int, ...]) -> None:
    tag = f"p12_{engine}_{partition.replace(',', '_')}_r{round_idx}"
    p0 = p1 = None
    rec = {"phase": "S0", "engine": engine, "partition": partition, "round": round_idx,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mem_avail_mib_before": mem_avail_mib()}
    try:
        p0, p1, startup_s, cmd_info = launcher(layer_partition=partition)
        rec["startup_s"] = startup_s
        rec["rank0_cmd"] = cmd_info.get("rank0_cmd")
        dec = p10_client.run_decode_series(base_url="http://127.0.0.1:18090", model="aicf",
                                           cfg_id=f"{tag}_decode", reps=7, burn_in=1, max_tokens=256, seed=142)
        rec["decode_tok_s"] = dec["decode_tok_s"]
        rec["decode_raw"] = dec
        rec["hw_after_decode"] = P.get_both_gpus_hw()
        for c in concurrencies:
            if c <= 1:
                continue
            rec[f"multiuser_c{c}"] = P.run_weighted_multiuser_batch(
                base_urls=["http://127.0.0.1:18090"], weights=[1.0], model="aicf",
                cfg_id=f"{tag}_c{c}", concurrency=c, prompt_tokens=512, max_tokens=128)
        rec["status"] = "OK"
        print(f"[{engine} r{round_idx}] decode C=1: {rec['decode_tok_s']}", flush=True)
    except Exception as e:                       # noqa: BLE001
        rec["status"] = "FAILED"
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"[{engine} r{round_idx}] ERROR {e}", flush=True)
    finally:
        rec["mem_avail_mib_after"] = mem_avail_mib()
        P.append_jsonl(OUT, rec)
        P.stop_all_engines()
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--partition", default="13,23")
    ap.add_argument("--concurrency", default="1,4")
    ap.add_argument("--start-round", type=int, default=1)
    ap.add_argument("--alternate", action="store_true", help="swap engine order on even rounds (removes order bias)")
    a = ap.parse_args()
    cs = tuple(int(x) for x in a.concurrency.split(","))
    for r in range(a.start_round, a.start_round + a.rounds):
        order = [("sglang", P.launch_sglang_pp2_cluster), ("vllm", P.launch_vllm_pp2_cluster)]
        if a.alternate and r % 2 == 0:
            order.reverse()
        for name, fn in order:
            measure(name, fn, a.partition, r, cs)
