import json
import os

import pytest

from core.model_registry.prompting import speculative_compatible


def make(dir_, vocab, template="T"):
    dir_.mkdir()
    (dir_ / "tokenizer.json").write_text(json.dumps({"model": {"vocab": vocab}}))
    (dir_ / "tokenizer_config.json").write_text(json.dumps({"chat_template": template}))
    return str(dir_)


def test_same_tokenizer_is_compatible_and_key_order_does_not_matter(tmp_path):
    a = make(tmp_path / "a", {"x": 1, "y": 2})
    b = make(tmp_path / "b", {"y": 2, "x": 1})
    ok, why = speculative_compatible(a, b)
    assert ok and "same" in why


def test_different_vocab_or_template_refuses_speculation(tmp_path):
    a = make(tmp_path / "a", {"x": 1})
    ok, why = speculative_compatible(a, make(tmp_path / "b", {"x": 2}))
    assert not ok and "refused" in why
    ok, _ = speculative_compatible(a, make(tmp_path / "c", {"x": 1}, template="other"))
    assert not ok


def test_missing_tokenizer_files_is_an_error_not_a_pass(tmp_path):
    a = make(tmp_path / "a", {"x": 1})
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        speculative_compatible(a, str(tmp_path / "empty"))


@pytest.mark.skipif(not os.environ.get("AICF_QWEN3_TOKENIZERS"), reason="set AICF_QWEN3_TOKENIZERS=dir with tok-Qwen3-0.6B and tok-Qwen3-8B")
def test_real_qwen3_draft_and_target_share_a_tokenizer():
    root = os.environ["AICF_QWEN3_TOKENIZERS"]
    assert speculative_compatible(f"{root}/tok-Qwen3-8B", f"{root}/tok-Qwen3-0.6B")[0]
