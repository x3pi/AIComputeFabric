import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_probe():
    out = subprocess.run([sys.executable, str(ROOT / "benchmarks/hardware/probe_node.py")],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_probe_schema():
    d = run_probe()
    assert d["schema_version"] == 1
    assert {"hostname", "os", "cpu", "ram", "gpus", "warnings"} <= d.keys()
    assert set(d["cpu"]["isa"]) == {"avx2", "avx512f", "avx512_vnni", "amx_tile", "amx_int8"}
    assert isinstance(d["gpus"], list)


def test_probe_reports_real_values_where_measurable():
    d = run_probe()
    assert d["cpu"]["logical_cores"] and d["cpu"]["logical_cores"] > 0
    if sys.platform.startswith("linux"):
        assert d["ram"]["total_bytes"] and d["ram"]["total_bytes"] > 1 << 30
    # unmeasured values must be null, never invented
    assert d["ram"]["bandwidth_gbps"] is None


def test_no_gpu_is_reported_not_faked():
    d = run_probe()
    if not d["gpus"]:
        assert any("nvidia-smi" in w for w in d["warnings"])
