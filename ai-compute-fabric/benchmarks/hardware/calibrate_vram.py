#!/usr/bin/env python3
"""VRAM calibration harness (plan P1.4 / gate G-VRAM), single-GPU Linux.

For each config it (1) asks gguf-parser for the estimate, (2) launches a real
llama-server, (3) parses the buffer sizes llama.cpp logs, (4) samples nvidia-smi
for the peak used VRAM, (5) compares. Nothing is simulated: if llama-server
fails to start the row records the failure instead of a number.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

MIB = 1024 * 1024
BUF = re.compile(r"(\S+)\s+(model|KV|compute|RS)\s+buffer size\s*=\s*([\d.]+)\s*MiB")


def within_tolerance(est: int, measured: int) -> bool:
    """Same rule as core/vram-estimator.WithinTolerance (max(5%, 300 MB))."""
    return abs(est - measured) <= max(measured // 20, 300_000_000)


def smi_used_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip().splitlines()[0])


def parser_estimate(parser: str, model: Path, ctx: int, parallel: int, fa: bool, ngl: int = -1) -> dict:
    cmd = [parser, "--path", str(model), "--ctx-size", str(ctx), "--parallel-size", str(parallel),
           "--gpu-layers", str(ngl), "--json", "--skip-tokenizer", "--skip-architecture", "--skip-metadata"]
    if fa:
        cmd.append("--flash-attention")
    est = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)["estimate"]["items"][0]
    return {"vram_nonuma": est["vrams"][0]["nonuma"], "vram_uma": est["vrams"][0]["uma"],
            "ram_nonuma": est["ram"]["nonuma"], "full_offload": est["fullOffloaded"],
            "offload_layers": est["offloadLayers"]}


def run_one(server: str, model: Path, ctx: int, parallel: int, fa: bool, port: int, log: Path, ngl: int = 99, timeout=300):
    baseline = smi_used_mib()
    cmd = [server, "-m", str(model), "-c", str(ctx * parallel), "-np", str(parallel), "-ngl", str(ngl),
           "--port", str(port), "--host", "127.0.0.1", "-fa", "on" if fa else "off", "--no-warmup", "--fit", "off", "-lv", "4"]
    with open(log, "w") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        peak, ready, t0 = baseline, False, time.time()
        try:
            while time.time() - t0 < timeout and p.poll() is None:
                peak = max(peak, smi_used_mib())
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                        ready = r.status == 200
                except Exception:
                    ready = False
                if ready:
                    break
                time.sleep(0.25)
            if ready:  # one tiny request so lazily allocated buffers appear
                req = urllib.request.Request(f"http://127.0.0.1:{port}/completion",
                                             data=json.dumps({"prompt": "Xin chào", "n_predict": 8}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=60).read()
                for _ in range(6):
                    peak = max(peak, smi_used_mib()); time.sleep(0.25)
        finally:
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
    time.sleep(1.0)
    text = log.read_text(errors="replace")
    bufs: dict[str, float] = {}
    for dev, kind, mib in BUF.findall(text):
        if dev.startswith("CUDA") and not dev.startswith("CUDA_Host"):
            bufs[kind] = bufs.get(kind, 0.0) + float(mib)
        elif dev.startswith(("CPU", "CUDA_Host")):
            bufs["host_" + kind] = bufs.get("host_" + kind, 0.0) + float(mib)
    return {"started": ready, "baseline_mib": baseline, "peak_mib": peak,
            "measured_delta_bytes": (peak - baseline) * MIB if ready else None,
            "log_buffers_mib": bufs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--parser", required=True)
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--ctx", nargs="+", type=int, default=[2048, 8192, 32768])
    ap.add_argument("--parallel", nargs="+", type=int, default=[1, 4])
    ap.add_argument("--fa", nargs="+", default=["off", "on"])
    ap.add_argument("--ngl", nargs="+", type=int, default=[99])
    ap.add_argument("--out", required=True)
    ap.add_argument("--logdir", required=True)
    a = ap.parse_args()
    Path(a.logdir).mkdir(parents=True, exist_ok=True)
    rows, port = [], 18080
    for m, ctx, par, fa, ngl in itertools.product(a.models, a.ctx, a.parallel, a.fa, a.ngl):
        model, fab = Path(m), fa == "on"
        # llama-server -c is the TOTAL context shared by all slots and the parser's
        # --ctx-size means the same thing, so both get ctx * parallel (verified 2026-10-25:
        # parser ctx=8192,np=4 and ctx=8192,np=1 give the same KV size).
        pngl = -1 if ngl >= 99 else ngl
        est = parser_estimate(a.parser, model, ctx * par, par, fab, pngl)
        # Calibration rule (derived 2026-10-25 from llama.cpp logs, see docs/calibration):
        # compute buffer scales with the PER-SLOT context, KV scales with the total.
        # => parser(ctx per slot, np=1) + (np-1) * KV_per_slot.
        one = parser_estimate(a.parser, model, ctx, 1, fab, pngl)
        est["calibrated_vram"] = one["vram_nonuma"] + (par - 1) * one["vram_uma"]
        row = {"model": model.name, "ngl": ngl, "ctx_per_seq": ctx, "parallel": par, "flash_attn": fab, "parser": est}
        row.update(run_one(a.server, model, ctx, par, fab, port, Path(a.logdir) / f"{model.stem}_c{ctx}_p{par}_fa{fa}_ngl{ngl}.log", ngl))
        if row["started"]:
            meas = row["measured_delta_bytes"]
            row["raw_within_tolerance"] = within_tolerance(est["vram_nonuma"], meas)
            row["raw_error_bytes"] = est["vram_nonuma"] - meas
            row["cal_within_tolerance"] = within_tolerance(est["calibrated_vram"], meas)
            row["cal_error_bytes"] = est["calibrated_vram"] - meas
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("model", "ngl", "ctx_per_seq", "parallel", "flash_attn", "started")}
                         | {"est_MiB": round(est["vram_nonuma"] / MIB), "meas_MiB": (row["measured_delta_bytes"] or 0) // MIB,
                            "err_MiB": round(row.get("raw_error_bytes", 0) / MIB), "cal_err_MiB": round(row.get("cal_error_bytes", 0) / MIB)}), flush=True)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
