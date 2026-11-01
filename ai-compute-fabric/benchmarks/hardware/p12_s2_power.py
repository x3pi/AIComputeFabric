#!/usr/bin/env python3
"""P12 / S2c - does the Windows power plan explain the WSL2 speed noise? ABAB Balanced vs High performance,
SGLang single RTX 5070 (WSL2). Restores the original plan at the end."""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p11_2gpu_runner as P
import p12_s2_wsl_single as W
W.OUT = P.REPORTS_DIR / "p12_s2_power.jsonl"
BAL = "381b4222-f694-41f0-9685-ff5bb260df2e"
HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
def plan(g): print(P.run_win_ps(f"powercfg /setactive {g}; powercfg /getactivescheme"), flush=True)
try:
    for i, (name, g) in enumerate([("balanced", BAL), ("high", HIGH), ("high", HIGH), ("balanced", BAL), ("balanced", BAL), ("high", HIGH)], 1):
        plan(g); W.run("sglang", 100 + i)
        P.append_jsonl(W.OUT, {"phase": "S2c", "round": 100 + i, "power_plan": name})
finally:
    plan(BAL)
