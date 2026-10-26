import pytest

from core.hardware_profiler.gpu_class import GpuInfo, classify, detect, has_tensor_cores, parse_smi

RTX3060 = GpuInfo("NVIDIA GeForce RTX 3060", (8, 6), 12288)


def test_fleet_card_may_join_a_group():
    assert classify(RTX3060) == "group"


@pytest.mark.parametrize("g,tier", [
    (GpuInfo("NVIDIA GeForce GTX 1080 Ti", (6, 1), 11264), "light"),      # Pascal: no tensor cores
    (GpuInfo("Tesla P40", (6, 1), 24576), "light"),                      # Pascal without the GTX brand: still no tensor cores
    (GpuInfo("NVIDIA GeForce GTX 1660", (7, 5), 6144), "light"),          # Turing but GTX: no tensor cores
    (GpuInfo("NVIDIA GeForce RTX 2060", (7, 5), 6144), "standalone"),     # tensor cores, 6 GB
    (GpuInfo("NVIDIA GeForce RTX 3060", (8, 6), 12288), "group"),
    (GpuInfo("NVIDIA GeForce RTX 3050", (8, 6), 8192), "standalone"),
    (GpuInfo("NVIDIA GeForce GT 1030", (6, 1), 2048), "light"),
    (GpuInfo("old", (5, 0), 1024), "unusable"),
    (GpuInfo("mystery", None, 12288), "light"),                           # unknown capability is never trusted
])
def test_tiers(g, tier):
    assert classify(g) == tier


def test_a_gtx_card_never_reaches_the_group_tier_whatever_its_size():
    for vram in (6144, 12288, 24576, 49152):
        for cc in ((6, 1), (7, 5), (8, 6)):
            assert classify(GpuInfo("GeForce GTX Something", cc, vram)) != "group"


def test_group_tier_needs_enough_vram():
    assert classify(GpuInfo("NVIDIA GeForce RTX 3060", (8, 6), 11000)) == "standalone"
    assert classify(GpuInfo("NVIDIA GeForce RTX 3060", (8, 6), 11776)) == "group"


def test_tensor_core_rule():
    assert has_tensor_cores(RTX3060)
    assert not has_tensor_cores(GpuInfo("GTX 1660", (7, 5), 6144))
    assert not has_tensor_cores(GpuInfo("x", None, 1))


def test_parse_smi_and_garbage():
    g = parse_smi("NVIDIA GeForce RTX 3060, 8.6, 12288 MiB\nbroken line\nfoo, n/a, ??\n")
    assert g[0] == RTX3060 and g[1] == GpuInfo("foo", None, 0) and len(g) == 2


def test_this_machine_if_it_has_a_gpu():
    gpus = detect()
    if not gpus:
        pytest.skip("no NVIDIA GPU visible")
    assert all(classify(g) in ("group", "standalone", "light", "unusable") for g in gpus)


def test_detect_returns_no_gpu_when_nvidia_smi_is_missing_or_hangs(monkeypatch):
    import subprocess

    from core.hardware_profiler import gpu_class

    def missing(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 10)

    for fn in (missing, hang):
        monkeypatch.setattr(gpu_class.subprocess, "run", fn)
        assert gpu_class.detect() == []
