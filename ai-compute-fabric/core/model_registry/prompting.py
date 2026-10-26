"""Shared prompt rendering and tokenizer identity (plan P1.3).

`render_chat_template` is the single place that turns messages (+ tool definitions) into the
prompt text, using the model's own Jinja template and nothing else, so every backend sends the
same tokens. `tokenizer_sha256` identifies the tokenizer + template: two models that must share
tokens (target and draft for speculative decoding) are only allowed to if it is equal."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_FILES = ("tokenizer.json", "tokenizer_config.json")


def tokenizer_sha256(tokenizer_dir: str) -> str:
    """Hash of the tokenizer definition and the chat template. Other files (weights, generation
    config) are deliberately excluded: they do not change how text becomes tokens."""
    d = Path(tokenizer_dir)
    h = hashlib.sha256()
    for name in _FILES:
        p = d / name
        if not p.is_file():
            raise FileNotFoundError(f"{name} missing in {tokenizer_dir}")
        data = json.loads(p.read_text(encoding="utf-8"))
        h.update(name.encode() + b"\0" + json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    return h.hexdigest()


def load_tokenizer(tokenizer_dir: str):
    from transformers import AutoTokenizer
    # trust_remote_code is pinned off (plan D12): a model directory can never run its own code here.
    return AutoTokenizer.from_pretrained(tokenizer_dir, trust_remote_code=False)


def render_chat_template(tok, messages: list, tools: list | None = None, add_generation_prompt: bool = True,
                         enable_thinking: bool | None = None) -> str:
    kw = {}
    if enable_thinking is not None:
        kw["enable_thinking"] = enable_thinking
    return tok.apply_chat_template(messages, tools=tools or None, tokenize=False,
                                   add_generation_prompt=add_generation_prompt, **kw)


def encode(tok, text: str) -> list:
    """Token ids of an already-rendered prompt: special tokens in the text are parsed, none are added."""
    return tok.encode(text, add_special_tokens=False)
