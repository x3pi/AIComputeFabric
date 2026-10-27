#!/usr/bin/env python3
"""P0.1 hardware probe. Emits one JSON document (schema_version 1).

Measures nothing it cannot measure: unknown fields are null and listed in
`warnings`, never guessed. Memory bandwidth needs a dedicated run (mbw/STREAM)
and is reported as null here.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import socket
import subprocess
import sys

import os
from pathlib import Path

SCHEMA_VERSION = 1
ISA_FLAGS = ["avx2", "avx512f", "avx512_vnni", "amx_tile", "amx_int8"]

# Ensure repository root is on sys.path so core.* can be imported
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def cpu_info(warnings):
    model, flags = None, set()
    if sys.platform.startswith("linux"):
        txt = open("/proc/cpuinfo", encoding="utf-8", errors="replace").read()
        m = re.search(r"model name\s*:\s*(.+)", txt)
        model = m.group(1).strip() if m else None
        f = re.search(r"flags\s*:\s*(.+)", txt)
        flags = set(f.group(1).split()) if f else set()
    elif sys.platform == "win32":
        model = platform.processor() or None
        try:
            from core.hardware_profiler import cpu_isa
            flags = cpu_isa.windows_flags()
        except Exception as e:
            warnings.append(f"windows cpu flags error: {e}")
    else:
        model = platform.processor() or None
        warnings.append("cpu flags only implemented for Linux and Windows")
    return {"model": model, "logical_cores": os.cpu_count(),
            "isa": {k: (k in flags) for k in ISA_FLAGS}}


def ram_info(warnings):
    total = None
    if sys.platform.startswith("linux"):
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
    elif sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total = int(stat.ullTotalPhys)
            else:
                warnings.append("GlobalMemoryStatusEx failed")
        except Exception as e:
            warnings.append(f"ram total error on windows: {e}")
    else:
        warnings.append("ram total only implemented for Linux and Windows")
    return {"total_bytes": total, "channels": None, "bandwidth_gbps": None}


def gpu_info(warnings):
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        candidates = [
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                exe = c
                break
    if not exe:
        warnings.append("nvidia-smi not found")
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True).stdout
    except (subprocess.SubprocessError, OSError) as e:
        warnings.append(f"nvidia-smi failed: {type(e).__name__}")
        return []
    gpus = []
    for line in out.strip().splitlines():
        name, total, used, drv = [x.strip() for x in line.split(",")]
        gpus.append({"name": name, "vram_total_mib": int(total), "vram_used_mib": int(used), "driver": drv})
    return gpus


def probe():
    warnings: list[str] = []
    doc = {
        "schema_version": SCHEMA_VERSION,
        "hostname": socket.gethostname(),
        "os": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "cpu": cpu_info(warnings),
        "ram": ram_info(warnings),
        "gpus": gpu_info(warnings),
        "warnings": warnings,
    }
    return doc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    text = json.dumps(probe(), indent=2, ensure_ascii=False)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    else:
        print(text)
