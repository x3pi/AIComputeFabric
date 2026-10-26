"""P8 release-readiness checks: NOTICE, release-manifest-v1.0.yaml, and docs/SRE_RUNBOOK.md
must exist, parse cleanly, and reference only real files in the repository."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_notice_and_sre_runbook_exist_and_cover_core_topics():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    for required in ("llama.cpp", "gguf-parser-go", "vLLM", "LangGraph", "NATS", "Qwen3"):
        assert required in notice, f"NOTICE missing attribution for {required}"

    runbook = (ROOT / "docs/SRE_RUNBOOK.md").read_text(encoding="utf-8")
    for topic in ("site-gateway", "worker-guard", "gcp-server", "verify_sboms.sh", "pg_dump", "G-RECLAIM"):
        assert topic in runbook, f"SRE_RUNBOOK.md missing section/topic {topic}"


def test_release_manifest_v1_references_real_files_and_valid_sboms():
    manifest_path = ROOT / "release-manifest-v1.0.yaml"
    assert manifest_path.is_file()
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    rel = data["release"]
    for key in ("upstream_manifest", "openapi_contract", "notice_file", "sre_runbook"):
        p = ROOT / rel[key]
        assert p.is_file(), f"missing {key}: {rel[key]}"
    for proto in rel["protobuf_contracts"]:
        assert (ROOT / proto).is_file(), f"missing proto: {proto}"

    for b in data["binaries"]:
        assert (ROOT / b["module"] / "go.mod").is_file(), f"missing go.mod for {b['name']}"

    sec = data["security_and_sbom"]
    assert (ROOT / sec["public_key"]).is_file()
    assert (ROOT / sec["verify_script"]).is_file()
    assert (ROOT / sec["scan_script"]).is_file()
    for sbom in sec["sboms"]:
        assert (ROOT / sbom).is_file(), f"missing SBOM: {sbom}"
        assert (ROOT / (sbom + ".sig")).is_file(), f"missing SBOM signature: {sbom}.sig"

    for gate in data["verified_evidence_rtx3060_linux"]:
        for rep in gate["reports"]:
            assert (ROOT / rep).is_file(), f"missing evidence report: {rep}"
