"""The committed SBOMs verify against deploy/security/cosign.pub, and verification fails for a tampered, unsigned or
wrong-key SBOM. Needs the cosign binary (COSIGN=/path or on PATH); skipped otherwise.
What this proves: the files are unchanged since signing by the holder of the matching private key. The key is a development
key generated on the build machine; it is not an organisational trust root."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COSIGN = os.environ.get("COSIGN") or shutil.which("cosign")
pytestmark = pytest.mark.skipif(not COSIGN, reason="cosign not installed (set COSIGN)")


def tree(tmp_path):
    """A copy of the repo parts the script uses, so tests can damage it freely."""
    (tmp_path / "deploy/security").mkdir(parents=True)
    for n in ("verify_sboms.sh", "cosign.pub"):
        shutil.copy(ROOT / "deploy/security" / n, tmp_path / "deploy/security" / n)
    sec = tmp_path / "benchmarks/reports/security"
    sec.mkdir(parents=True)
    for f in (ROOT / "benchmarks/reports/security").glob("sbom_*"):
        shutil.copy(f, sec / f.name)
    return tmp_path, sec


def run(t):
    return subprocess.run(["bash", str(t / "deploy/security/verify_sboms.sh")], capture_output=True, text=True, env={**os.environ, "COSIGN": COSIGN})


def test_committed_sboms_verify(tmp_path):
    t, sec = tree(tmp_path)
    assert len(list(sec.glob("sbom_*.json"))) >= 4
    r = run(t)
    assert r.returncode == 0, r.stdout


def test_tampered_unsigned_and_wrong_key_are_all_rejected(tmp_path):
    t, sec = tree(tmp_path)
    victim = sorted(sec.glob("sbom_go_*.json"))[0]
    victim.write_text(victim.read_text() + " ")
    r = run(t)
    assert r.returncode == 1 and f"BAD      benchmarks/reports/security/{victim.name}" in r.stdout
    t2, sec2 = tree(tmp_path / "two")
    (sec2 / (sorted(sec2.glob("sbom_go_*.json"))[0].name + ".sig")).unlink()
    r = run(t2)
    assert r.returncode == 1 and "UNSIGNED" in r.stdout
    t3, _ = tree(tmp_path / "three")
    kd = tmp_path / "k"
    kd.mkdir()
    subprocess.run([COSIGN, "generate-key-pair"], cwd=kd, env={**os.environ, "COSIGN_PASSWORD": ""}, capture_output=True, check=True)
    shutil.copy(kd / "cosign.pub", t3 / "deploy/security/cosign.pub")           # same files, different trusted key
    r = run(t3)
    assert r.returncode == 1 and "BAD" in r.stdout and "ok " not in r.stdout
