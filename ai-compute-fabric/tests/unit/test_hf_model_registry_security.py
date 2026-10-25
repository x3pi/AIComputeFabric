import json
from pathlib import Path

import pytest

from core.model_registry.security import sha256_file, tokenizer_sha256, validate_model_dir


def make_model(root: Path):
    (root / "model.safetensors").write_bytes(b"\x01" * 1024)
    (root / "config.json").write_text(json.dumps({"architectures": ["Qwen3ForCausalLM"]}))
    tok = {"model": {"vocab": {"a": 0, "b": 1}}, "added_tokens": []}
    (root / "tokenizer.json").write_text(json.dumps(tok))
    files = {n: sha256_file(root / n) for n in ("model.safetensors", "config.json", "tokenizer.json")}
    return {"files": files, "tokenizer_sha256": tokenizer_sha256(root / "tokenizer.json")}


def codes(vs):
    return {v.code for v in vs}


def test_clean_model_passes(tmp_path):
    m = make_model(tmp_path)
    assert validate_model_dir(tmp_path, m) == []


def test_one_byte_flip_is_rejected(tmp_path):
    m = make_model(tmp_path)
    p = tmp_path / "model.safetensors"
    b = bytearray(p.read_bytes()); b[10] ^= 1; p.write_bytes(bytes(b))
    assert "SHA256_MISMATCH" in codes(validate_model_dir(tmp_path, m))


@pytest.mark.parametrize("name", ["pytorch_model.bin", "w.pkl", "w.pt", "x.ckpt"])
def test_pickle_formats_rejected(tmp_path, name):
    m = make_model(tmp_path)
    (tmp_path / name).write_bytes(b"cos\nsystem\n")
    assert "FORBIDDEN_FORMAT" in codes(validate_model_dir(tmp_path, m))


def test_auto_map_requests_remote_code(tmp_path):
    m = make_model(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"auto_map": {"AutoModel": "evil.Model"}}))
    m["files"]["config.json"] = sha256_file(tmp_path / "config.json")  # even with a "valid" hash
    assert "REMOTE_CODE_REQUESTED" in codes(validate_model_dir(tmp_path, m))


def test_tokenizer_vocab_tampering_detected(tmp_path):
    m = make_model(tmp_path)
    t = json.loads((tmp_path / "tokenizer.json").read_text())
    t["model"]["vocab"]["evil"] = 2
    (tmp_path / "tokenizer.json").write_text(json.dumps(t))
    m["files"]["tokenizer.json"] = sha256_file(tmp_path / "tokenizer.json")
    assert "TOKENIZER_MISMATCH" in codes(validate_model_dir(tmp_path, m))


def test_tokenizer_hash_ignores_key_order_and_whitespace(tmp_path):
    a = tmp_path / "a.json"; b = tmp_path / "b.json"
    a.write_text('{"x":1,"y":[1,2]}')
    b.write_text('{\n "y": [1, 2],\n "x": 1\n}')
    assert tokenizer_sha256(a) == tokenizer_sha256(b)


def test_missing_and_unlisted_files(tmp_path):
    m = make_model(tmp_path)
    (tmp_path / "extra.json").write_text("{}")
    (tmp_path / "model.safetensors").unlink()
    c = codes(validate_model_dir(tmp_path, m))
    assert {"UNLISTED_FILE", "MISSING_FILE"} <= c


def test_unknown_extension_rejected(tmp_path):
    m = make_model(tmp_path)
    (tmp_path / "run.sh").write_text("rm -rf /")
    assert "UNKNOWN_FORMAT" in codes(validate_model_dir(tmp_path, m))
