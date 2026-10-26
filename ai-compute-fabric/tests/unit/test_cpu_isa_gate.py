import itertools
import os
import sys

import pytest

from core.hardware_profiler.cpu_isa import PREFERENCE, REQUIRES, allowed_builds, detect_flags, linux_flags, select_build

AVX2 = {"avx2", "fma", "f16c", "sse3"}
AVX512 = AVX2 | {"avx512f", "avx512bw", "avx512vl"}
AMX = AVX512 | {"amx_tile", "amx_int8", "amx_bf16"}


def test_best_allowed_build_per_cpu_class():
    assert select_build(AMX) == "ktransformers_amx"
    assert select_build(AVX512) == "llama_cpp_avx512"
    assert select_build(AVX2) == "llama_cpp_avx2"
    assert select_build(set()) == "llama_cpp_generic"


def test_a_build_is_never_allowed_when_any_required_feature_is_missing():
    # exhaustive over subsets of the features: no subset may unlock a build it cannot run
    feats = sorted(set().union(*REQUIRES.values()))
    for r in range(len(feats) + 1):
        for sub in itertools.combinations(feats, r):
            flags = set(sub)
            for b in allowed_builds(flags):
                assert REQUIRES[b] <= flags, (b, sub)


def test_explicit_request_for_unsupported_build_is_refused_with_the_missing_features():
    with pytest.raises(RuntimeError, match="avx512f") as e:
        select_build(AVX2, "llama_cpp_avx512")
    assert "avx512bw" in str(e.value)
    with pytest.raises(RuntimeError, match="amx_tile"):
        select_build(AVX512, "ktransformers_amx")
    with pytest.raises(ValueError):
        select_build(AVX2, "nope")


def test_unknown_feature_state_counts_as_absent():
    assert allowed_builds(set()) == ["llama_cpp_generic"]


def test_linux_parser():
    text = "processor\t: 0\nmodel name\t: X\nflags\t\t: fpu sse3 avx2 fma f16c avx512f\n\nprocessor\t: 1\n"
    assert linux_flags(text) == {"fpu", "sse3", "avx2", "fma", "f16c", "avx512f"}
    assert linux_flags("no flags here") == set()


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.exists("/proc/cpuinfo"), reason="reads this machine's /proc/cpuinfo")
def test_this_machines_selected_build_really_runs_here():
    flags = detect_flags()
    build = select_build(flags)
    assert REQUIRES[build] <= flags
    assert build in PREFERENCE
