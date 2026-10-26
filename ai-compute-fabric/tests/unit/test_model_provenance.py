"""P1.3: the provenance chain with a stand-in converter script (the real llama.cpp converter run is in
benchmarks/reports/p1_model_provenance_qwen3_0p6b.json)."""
import json
import sys

import pytest

from core.model_registry.provenance import ProvenanceError, convert_with_record, model_identity, verify_provenance
from core.model_registry.snapshot import snapshot_dir, take_snapshot
from tests.unit.test_gguf_facts import build

REV = "b" * 40
REPO = "acme/tiny"


def download(repo, revision, dest):
    (dest / "config.json").write_text(json.dumps({"num_hidden_layers": 4, "num_key_value_heads": 2, "num_attention_heads": 8}))
    (dest / "tokenizer.json").write_text(json.dumps({"model": {"vocab": {"a": 0}}}))
    (dest / "model.safetensors").write_bytes(b"\0" * 1024)


@pytest.fixture
def env(tmp_path):
    take_snapshot(tmp_path / "reg", REPO, REV, download)
    ref = tmp_path / "ref.gguf"
    build(ref, layers=4, heads_kv=2)
    script = tmp_path / "fake_convert.py"
    script.write_text("import shutil, sys\nshutil.copy(%r, sys.argv[sys.argv.index('--outfile') + 1])\n" % str(ref))
    return tmp_path, snapshot_dir(tmp_path / "reg", REPO, REV), script


def run(env, **kw):
    tmp, snap, script = env
    return convert_with_record(snap, tmp / "out" / "m.gguf", script, "74bca39", python=sys.executable, **kw), tmp / "out" / "m.gguf"


def test_record_links_source_converter_and_gguf(env):
    rec, gguf = run(env)
    assert rec["source"]["repo"] == REPO and rec["source"]["revision"] == REV
    assert rec["gguf"]["facts"]["layers"] == 4 and rec["converter"]["llama_cpp_commit"] == "74bca39"
    assert verify_provenance(gguf, lambda r, v: snapshot_dir(env[0] / "reg", r, v)) == rec
    ident = model_identity(rec, "Apache-2.0")
    assert ident["gguf_sha256"] == rec["gguf"]["sha256"] and ident["trust_remote_code"] is False and ident["source_revision"] == REV
    from google.protobuf import json_format  # the dict must be a valid ModelIdentity message
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "contracts/gen/python"))
    from aicf.v1 import fabric_pb2
    json_format.ParseDict({k: v for k, v in ident.items()}, fabric_pb2.ModelIdentity())


def test_a_modified_gguf_fails_verification(env):
    rec, gguf = run(env)
    raw = bytearray(gguf.read_bytes())
    raw[-1] ^= 1
    gguf.write_bytes(bytes(raw))
    with pytest.raises(ProvenanceError, match="differ from the recorded hash"):
        verify_provenance(gguf)


def test_a_modified_source_snapshot_fails_verification_and_blocks_conversion(env):
    rec, gguf = run(env)
    (env[1] / "files" / "model.safetensors").write_bytes(b"\1" * 1024)
    with pytest.raises(ProvenanceError, match="no longer verifies"):
        verify_provenance(gguf, lambda r, v: env[1])
    with pytest.raises(ProvenanceError, match="SHA256_MISMATCH"):
        run(env)


def test_gguf_that_disagrees_with_the_source_config_is_refused(env):
    tmp, snap, script = env
    cfg = snap / "files" / "config.json"
    cfg.write_text(json.dumps({"num_hidden_layers": 28, "num_key_value_heads": 2}))
    # re-register the edited config so the snapshot itself is consistent: only the GGUF/config disagreement remains
    m = json.loads((snap / "MANIFEST.json").read_text())
    from core.model_registry.security import sha256_file
    m["files"]["config.json"], m["sizes"]["config.json"] = sha256_file(cfg), cfg.stat().st_size
    (snap / "MANIFEST.json").write_text(json.dumps(m))
    with pytest.raises(ProvenanceError, match="layers=4 but the source config.json says 28"):
        run(env)


def test_converter_failure_and_bad_outtype(env):
    tmp, snap, _ = env
    bad = tmp / "bad.py"
    bad.write_text("import sys\nsys.exit(3)\n")
    with pytest.raises(ProvenanceError, match="converter failed"):
        convert_with_record(snap, tmp / "o.gguf", bad, "x", python=sys.executable)
    with pytest.raises(ProvenanceError, match="outtype"):
        convert_with_record(snap, tmp / "o.gguf", bad, "x", outtype="q4_k_m")


def test_missing_record_is_an_error(tmp_path):
    (tmp_path / "x.gguf").write_bytes(b"GGUF")
    with pytest.raises(ProvenanceError, match="no readable provenance"):
        verify_provenance(tmp_path / "x.gguf")
