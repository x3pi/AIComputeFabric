"""P1.4: GGUF file -> Python sidecar -> real site-gateway endpoint, plus the committed replay against measured VRAM."""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.unit.test_gguf_facts import build

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "benchmarks/reports/p1_sidecar_estimate_vs_measured.json"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(base, body):
    req = urllib.request.Request(base + "/api/v1/models/estimate-vram", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    import shutil
    if shutil.which("go") is None:
        pytest.skip("Go toolchain not installed")
    d = tmp_path_factory.mktemp("chain")
    models = d / "models"
    models.mkdir()
    build(models / "tiny.gguf", layers=6, heads_kv=2, key_len=64)
    subprocess.run(["go", "build", "-o", str(d / "gw"), "."], cwd=ROOT / "apps/site-gateway", check=True)
    sp, hp, gp = free_port(), free_port(), free_port()
    side = subprocess.Popen([sys.executable, "-m", "core.vram_sidecar", "--models-root", str(models), "--port", str(sp)], cwd=ROOT)
    gw = subprocess.Popen([str(d / "gw"), "-http", f"127.0.0.1:{hp}", "-grpc", f"127.0.0.1:{gp}", "-vram-sidecar-url", f"http://127.0.0.1:{sp}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{hp}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/whoami", timeout=1)
            urllib.request.urlopen(f"http://127.0.0.1:{sp}/facts?file=tiny.gguf", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base
    for p in (gw, side):
        p.terminate()
        p.wait(10)


def test_estimate_from_a_file_the_catalogue_has_never_seen(chain):
    st, out = post(chain, {"gguf": "tiny.gguf", "ctx_per_slot": 2048, "parallel": 1})
    assert st == 200
    m = out["model"]
    assert (m["layers"], m["n_kv"], m["head_dim"], m["confidence"]) == (6, 2, 64, "ESTIMATED")
    assert out["kv_per_slot_bytes"] == 6 * 2 * 64 * 2 * 2048 * 2          # layers * kv heads * (K+V) * head_dim * ctx * 2 bytes
    assert out["fit_status"] and out["layer_splits"][0]["layer_count"] == 6


def test_traversal_missing_and_bad_requests_are_refused(chain):
    assert post(chain, {"gguf": "../../etc/passwd.gguf"})[0] == 403
    assert post(chain, {"gguf": "nope.gguf"})[0] == 404
    assert post(chain, {"gguf": "tiny.txt"})[0] == 400
    assert post(chain, {})[0] == 400


def test_committed_replay_against_measured_vram():
    r = json.loads(REPORT.read_text())
    assert r["n"] == len(r["rows"]) == 39
    assert r["within_tolerance"] == 39 and r["max_abs_error_mib"] < 100
    # the estimator may be a little low only inside the tolerance band; it must never be low by more than 5 % of the measurement
    for row in r["rows"]:
        assert row["estimated_bytes"] >= row["measured_bytes"] * 0.95, row
