"""CPU instruction-set gate (plan P4.1): never start an engine build whose instructions the CPU lacks.

A missing instruction does not fail politely, it kills the process with SIGILL (Windows: an
illegal-instruction exception). The gate therefore decides from the CPU's feature flags which
builds are allowed, and treats "unknown" as "absent". Linux reads /proc/cpuinfo; Windows asks
IsProcessorFeaturePresent (written from the documentation, not yet run on Windows)."""
from __future__ import annotations

import sys

# Feature each engine build needs. Keys are build names the adapters use.
REQUIRES = {
    "llama_cpp_avx2": {"avx2", "fma", "f16c"},
    "llama_cpp_avx512": {"avx2", "fma", "f16c", "avx512f", "avx512bw", "avx512vl"},
    "llama_cpp_generic": set(),                 # portable fallback, slow
    "ktransformers_amx": {"avx512f", "amx_tile", "amx_int8", "amx_bf16"},
}
# Preference order per task: fastest allowed build first.
PREFERENCE = ["ktransformers_amx", "llama_cpp_avx512", "llama_cpp_avx2", "llama_cpp_generic"]


def linux_flags(cpuinfo_text: str) -> set:
    for line in cpuinfo_text.splitlines():
        if line.startswith("flags"):
            return set(line.split(":", 1)[1].split())
    return set()


def windows_flags() -> set:
    import ctypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    probe = {10: "sse3", 40: "avx2", 41: "avx512f"}  # PF_* constants from winnt.h
    flags = {name for pf, name in probe.items() if k32.IsProcessorFeaturePresent(pf)}
    if "avx2" in flags:
        flags |= {"fma", "f16c"}   # every AVX2 CPU has FMA3 and F16C
    if "avx512f" in flags:
        flags |= {"avx512bw", "avx512vl"}  # not individually exposed; common on AVX-512 server/desktop parts. Verify on a real PC.
    return flags                   # AMX is not exposed this way: reported absent


def detect_flags() -> set:
    if sys.platform == "win32":
        return windows_flags()
    with open("/proc/cpuinfo") as f:
        return linux_flags(f.read())


def allowed_builds(flags: set) -> list:
    return [b for b in PREFERENCE if REQUIRES[b] <= flags]


def select_build(flags: set, wanted: str | None = None) -> str:
    """Best allowed build; an explicitly wanted build that the CPU cannot run is refused."""
    ok = allowed_builds(flags)
    if wanted is not None:
        if wanted not in REQUIRES:
            raise ValueError(f"unknown build {wanted!r}")
        if wanted not in ok:
            missing = sorted(REQUIRES[wanted] - flags)
            raise RuntimeError(f"{wanted} needs CPU features this machine lacks: {', '.join(missing)}")
        return wanted
    return ok[0]
