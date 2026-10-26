"""Provenance chain  Hugging Face snapshot -> llama.cpp converter -> GGUF  (plan P1.3).

convert_with_record() runs the converter on a VERIFIED snapshot and writes `<gguf>.provenance.json` tying together:
the source (repo, revision, manifest digest), the converter (script hash, llama.cpp commit, output type), the produced file
(sha256, size, facts read back from its header) and the tokenizer hash. verify_provenance() recomputes those links and
checks that the GGUF really describes the model in the snapshot's config.json. The record maps onto the ModelIdentity
message in contracts/protobuf/aicf/v1/fabric.proto (see model_identity()).

Not covered: the conversion is not reproducible bit-for-bit across converter versions or Python/torch versions (the record
pins the converter commit so a difference is attributable, it does not prevent it); the record is not signed (cosign on the
.provenance.json is the intended next step); quantisation beyond what the converter itself offers (f32/f16/bf16/q8_0)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core.model_registry.security import sha256_file, tokenizer_sha256
from core.model_registry.snapshot import SnapshotError, manifest_digest, verify_snapshot
from core.vram_sidecar.gguf import read_facts

OUTTYPES = {"f32", "f16", "bf16", "q8_0"}


class ProvenanceError(Exception):
    pass


def _cfg(files: Path) -> dict:
    return json.loads((files / "config.json").read_text())


def convert_with_record(snapshot: Path, out_gguf: Path, convert_script: Path, llama_cpp_commit: str, outtype: str = "q8_0",
                        python: str = sys.executable, extra_pythonpath: str | None = None, timeout: int = 1800) -> dict:
    if outtype not in OUTTYPES:
        raise ProvenanceError(f"outtype {outtype!r} not supported by the converter")
    try:
        m = verify_snapshot(snapshot)        # never convert a snapshot that no longer matches its manifest
    except SnapshotError as e:
        raise ProvenanceError(str(e)) from e
    files = Path(snapshot) / "files"
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if extra_pythonpath:
        env["PYTHONPATH"] = extra_pythonpath + os.pathsep + env.get("PYTHONPATH", "")
    out_gguf = Path(out_gguf)
    out_gguf.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    r = subprocess.run([python, str(convert_script), str(files), "--outfile", str(out_gguf), "--outtype", outtype],
                       capture_output=True, text=True, env=env, timeout=timeout)
    if r.returncode != 0 or not out_gguf.is_file():
        raise ProvenanceError(f"converter failed ({r.returncode}): {r.stderr[-400:]}")
    facts = read_facts(out_gguf)
    rec = {
        "schema": 1,
        "source": {"repo": m["repo"], "revision": m["revision"], "manifest_sha256": manifest_digest(m)},
        "converter": {"script": Path(convert_script).name, "script_sha256": sha256_file(Path(convert_script)), "llama_cpp_commit": llama_cpp_commit,
                      "outtype": outtype, "python": sys.version.split()[0], "seconds": round(time.time() - t0, 1)},
        "gguf": {"file": out_gguf.name, "sha256": sha256_file(out_gguf), "bytes": out_gguf.stat().st_size, "facts": facts.as_dict()},
        "tokenizer_sha256": m["tokenizer_sha256"],
    }
    check_matches_config(rec, _cfg(files))
    Path(str(out_gguf) + ".provenance.json").write_text(json.dumps(rec, indent=1, sort_keys=True))
    return rec


def check_matches_config(rec: dict, cfg: dict):
    """The GGUF must describe the model the snapshot's config.json describes."""
    f = rec["gguf"]["facts"]
    want_kv = cfg.get("num_key_value_heads", cfg.get("num_attention_heads"))
    for name, got, want in (("layers", f["layers"], cfg.get("num_hidden_layers")), ("kv heads", f["n_kv"], want_kv)):
        if got != want:
            raise ProvenanceError(f"GGUF has {name}={got} but the source config.json says {want}")


def verify_provenance(gguf: Path, snapshot_root_resolver=None) -> dict:
    """Recompute every link of the record next to `gguf`. `snapshot_root_resolver(repo, revision) -> Path` additionally
    re-verifies the source snapshot on disk (omit it to check only the GGUF side)."""
    gguf = Path(gguf)
    try:
        rec = json.loads(Path(str(gguf) + ".provenance.json").read_text())
    except (OSError, ValueError) as e:
        raise ProvenanceError(f"no readable provenance record for {gguf.name}: {e}") from e
    if rec["gguf"]["sha256"] != sha256_file(gguf) or rec["gguf"]["bytes"] != gguf.stat().st_size:
        raise ProvenanceError("GGUF bytes differ from the recorded hash")
    if read_facts(gguf).as_dict() != rec["gguf"]["facts"]:
        raise ProvenanceError("GGUF header differs from the recorded facts")
    if snapshot_root_resolver is not None:
        snap = Path(snapshot_root_resolver(rec["source"]["repo"], rec["source"]["revision"]))
        try:
            m = verify_snapshot(snap)
        except SnapshotError as e:
            raise ProvenanceError(f"source snapshot no longer verifies: {e}") from e
        if manifest_digest(m) != rec["source"]["manifest_sha256"]:
            raise ProvenanceError("source snapshot manifest differs from the recorded digest")
        tok = snap / "files" / "tokenizer.json"
        if rec["tokenizer_sha256"] != (tokenizer_sha256(tok) if tok.is_file() else None):
            raise ProvenanceError("tokenizer hash differs from the recorded one")
        check_matches_config(rec, _cfg(snap / "files"))
    return rec


def model_identity(rec: dict, license_spdx: str) -> dict:
    """Field-for-field dict of the ModelIdentity protobuf message."""
    return {"model_id": rec["source"]["repo"], "weight_sha256": rec["source"]["manifest_sha256"], "tokenizer_sha256": rec["tokenizer_sha256"] or "",
            "revision": rec["source"]["revision"], "quantization": rec["gguf"]["facts"]["quantization"], "source_repo": rec["source"]["repo"],
            "source_revision": rec["source"]["revision"], "converter": f"llama.cpp@{rec['converter']['llama_cpp_commit']}/{rec['converter']['script']}",
            "gguf_sha256": rec["gguf"]["sha256"], "license_spdx": license_spdx, "trust_remote_code": False}


