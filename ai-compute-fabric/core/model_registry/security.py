"""Offline integrity checks for a model directory (plan P1.3).

validate_model_dir() never executes or unpickles anything: it only hashes files
and inspects JSON, so a hostile artifact cannot run code during validation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_SUFFIXES = {".bin", ".pkl", ".pickle", ".pt", ".pth", ".ckpt", ".joblib", ".npy", ".npz"}
ALLOWED_SUFFIXES = {".safetensors", ".gguf", ".json", ".txt", ".model", ".md", ".jinja", ".tiktoken"}


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    detail: str = ""


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def tokenizer_sha256(tokenizer_json: Path) -> str:
    """Hash of the canonical tokenizer.json content (key-sorted, compact)."""
    data = json.loads(tokenizer_json.read_text(encoding="utf-8"))
    canon = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def validate_model_dir(root: Path, manifest: dict) -> list[Violation]:
    """manifest = {"files": {relpath: sha256}, "tokenizer_sha256": str|None}."""
    out: list[Violation] = []
    root = Path(root)
    expected = manifest.get("files", {})

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        suf = p.suffix.lower()
        if suf in FORBIDDEN_SUFFIXES:
            out.append(Violation("FORBIDDEN_FORMAT", rel, "pickle-capable format"))
            continue
        if suf not in ALLOWED_SUFFIXES:
            out.append(Violation("UNKNOWN_FORMAT", rel, suf))
            continue
        if rel not in expected:
            out.append(Violation("UNLISTED_FILE", rel))
        elif sha256_file(p) != expected[rel]:
            out.append(Violation("SHA256_MISMATCH", rel))

    for rel in expected:
        if not (root / rel).is_file():
            out.append(Violation("MISSING_FILE", rel))

    cfg = root / "config.json"
    if cfg.is_file():
        try:
            c = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out.append(Violation("BAD_CONFIG_JSON", "config.json"))
        else:
            if "auto_map" in c or c.get("trust_remote_code") is True:
                out.append(Violation("REMOTE_CODE_REQUESTED", "config.json", "auto_map/trust_remote_code"))

    want_tok = manifest.get("tokenizer_sha256")
    tok = root / "tokenizer.json"
    if want_tok:
        if not tok.is_file():
            out.append(Violation("MISSING_FILE", "tokenizer.json"))
        else:
            try:
                if tokenizer_sha256(tok) != want_tok:
                    out.append(Violation("TOKENIZER_MISMATCH", "tokenizer.json"))
            except json.JSONDecodeError:
                out.append(Violation("TOKENIZER_MISMATCH", "tokenizer.json", "unparseable"))
    return out
