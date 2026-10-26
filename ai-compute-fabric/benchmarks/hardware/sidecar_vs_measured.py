#!/usr/bin/env python3
"""P1.4: replay every MEASURED llama-server run in the calibration reports through the real chain
GGUF file -> Python sidecar -> site-gateway POST /api/v1/models/estimate-vram, and compare with the measured VRAM.

The measurements were taken on one RTX 3060 12 GB (see the source reports); this script needs only the GGUF files, not a GPU.
usage: sidecar_vs_measured.py MODELS_DIR OUT.json"""
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "benchmarks/reports"
MIB = 1 << 20


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def measured_rows():
    q4 = json.loads((REPORTS / "p1_vram_calibration_rtx3060.json").read_text())
    q8 = json.loads((REPORTS / "p1_vram_calibration_q8_f16_rtx3060.json").read_text())
    rows = [dict(r, source="p1_vram_calibration_rtx3060.json") for r in q4["rows_fit"] + q4["rows_heldout"]]
    rows += [dict(r, source="p1_vram_calibration_q8_f16_rtx3060.json") for r in q8["rows"]]
    return [r for r in rows if r.get("started")]


def main(models, out):
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["go", "build", "-o", str(tmp / "gw"), "."], cwd=ROOT / "apps/site-gateway", check=True)
    sp, hp, gp = free_port(), free_port(), free_port()
    side = subprocess.Popen([sys.executable, "-m", "core.vram_sidecar", "--models-root", models, "--port", str(sp)], cwd=ROOT)
    gw = subprocess.Popen([str(tmp / "gw"), "-http", f"127.0.0.1:{hp}", "-grpc", f"127.0.0.1:{gp}", "-vram-sidecar-url", f"http://127.0.0.1:{sp}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{hp}"
    try:
        time.sleep(2)
        res = []
        for r in measured_rows():
            e = post(base, "/api/v1/models/estimate-vram", {"gguf": r["model"], "ctx_per_slot": r["ctx_per_seq"], "parallel": r["parallel"],
                                                           "flash_attn": r["flash_attn"], "kv_bytes_per_elem": 2, "gpu_total_mib": 12288})
            est, meas = e["calibrated_vram_bytes"], r["measured_delta_bytes"]
            tol = max(0.05 * meas, 300 * MIB)
            res.append({"model": r["model"], "ctx_per_slot": r["ctx_per_seq"], "parallel": r["parallel"], "flash_attn": r["flash_attn"], "source": r["source"],
                        "estimated_bytes": est, "measured_bytes": meas, "error_mib": round((est - meas) / MIB, 1), "within_tolerance": abs(est - meas) <= tol,
                        "underestimate": est < meas})
    finally:
        for p in (gw, side):
            p.terminate()
            p.wait(10)
    n = len(res)
    rep = {"schema_version": 1, "date": time.strftime("%Y-%m-%d"), "chain": "GGUF header -> core.vram_sidecar -> site-gateway /api/v1/models/estimate-vram (calibrated_vram_bytes)",
           "scope": "replays the measured runs of p1_vram_calibration_rtx3060.json and p1_vram_calibration_q8_f16_rtx3060.json (RTX 3060 12 GB, llama.cpp 74bca39, -ngl 99); NOT new GPU measurements",
           "tolerance": "max(5% of measured, 300 MiB)", "n": n, "within_tolerance": sum(r["within_tolerance"] for r in res),
           "underestimates": sum(r["underestimate"] for r in res), "max_abs_error_mib": max(abs(r["error_mib"]) for r in res),
           "max_underestimate_mib": min([r["error_mib"] for r in res] + [0]), "rows": res}
    Path(out).write_text(json.dumps(rep, indent=1))
    print({k: v for k, v in rep.items() if k != "rows"})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
