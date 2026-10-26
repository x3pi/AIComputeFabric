"""Tokenizer/template determinism (plan P1.3 gate: HF vs llama.cpp must match 100 %).

For every conversation (Vietnamese, emoji, code, multi-turn, tool definitions) the prompt text
rendered by Hugging Face and by llama-server (/apply-template), and the token ids from
`AutoTokenizer` and llama.cpp (/tokenize), must be identical. Needs a running llama-server
(AICF_LLAMA_URL) and the model's tokenizer directory (AICF_TOKENIZER_DIR); skipped otherwise."""
import json
import os
import urllib.request

import pytest

from core.model_registry.prompting import encode, load_tokenizer, render_chat_template, tokenizer_sha256

URL, TOKDIR = os.environ.get("AICF_LLAMA_URL"), os.environ.get("AICF_TOKENIZER_DIR")
pytestmark = pytest.mark.skipif(not (URL and TOKDIR), reason="set AICF_LLAMA_URL and AICF_TOKENIZER_DIR")

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Lấy thời tiết / get weather",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}}, "required": ["city"]}}},
         {"type": "function", "function": {"name": "search", "description": "Search the docs", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]

TEXTS = ["Xin chào, bạn khỏe không?", "Hà Nội, Sài Gòn, Đà Nẵng — 3 thành phố lớn nhất Việt Nam.", "Tóm tắt: ánh sáng; ƯƠNG; ơ ớ ở ỡ ợ",
         "print('hello')\nfor i in range(3):\n    print(i)", "😀 emoji 👍🏽 and 日本語 and العربية", "   leading and trailing spaces   ",
         "tab\tseparated\tvalues\n\n\nthree newlines", "<|im_start|> literal special token text <|im_end|>", "1,234,567.89 đồng; 25/12/2026; 3.14159",
         "a" * 700, "SELECT * FROM users WHERE name = 'O''Brien';", "https://example.com/path?q=tiếng+việt&x=1#frag", "", " ", "\n"]


def conversations():
    out = []
    for t in TEXTS:
        out.append([{"role": "user", "content": t or "."}])
        out.append([{"role": "system", "content": "Bạn là trợ lý nội bộ."}, {"role": "user", "content": t or "."}])
    out.append([{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}, {"role": "user", "content": "Tiếp tục nhé"}])
    out.append([{"role": "system", "content": "S"}, {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}, {"role": "user", "content": "q3"}])
    return out


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


@pytest.fixture(scope="module")
def tok():
    return load_tokenizer(TOKDIR)


@pytest.mark.parametrize("with_tools", [False, True])
def test_prompt_text_and_token_ids_match_llama_cpp_100_percent(tok, with_tools):
    convs = conversations()
    bad = []
    for i, msgs in enumerate(convs):
        tools = TOOLS if with_tools else None
        hf = render_chat_template(tok, msgs, tools, enable_thinking=None)
        body = {"messages": msgs}
        if tools:
            body["tools"] = tools
        cpp = post("/apply-template", body)["prompt"]
        if hf != cpp:
            bad.append((i, "prompt", hf[-80:], cpp[-80:]))
            continue
        ids_hf = encode(tok, hf)
        ids_cpp = post("/tokenize", {"content": hf, "add_special": False, "parse_special": True})["tokens"]
        if ids_hf != ids_cpp:
            bad.append((i, "ids", len(ids_hf), len(ids_cpp)))
    assert not bad, f"{len(bad)}/{len(convs)} mismatches: {bad[:3]}"


def test_tokenizer_hash_is_stable_and_changes_when_the_vocab_or_template_changes(tmp_path):
    h = tokenizer_sha256(TOKDIR)
    assert h == tokenizer_sha256(TOKDIR) and len(h) == 64
    import shutil
    for variant, edit in {"vocab": lambda d: d["model"]["vocab"].update({"zzzz_new_token": 999999}),
                          "template": None}.items():
        d = tmp_path / variant
        shutil.copytree(TOKDIR, d)
        if variant == "vocab":
            data = json.loads((d / "tokenizer.json").read_text()); edit(data); (d / "tokenizer.json").write_text(json.dumps(data))
        else:
            cfg = json.loads((d / "tokenizer_config.json").read_text()); cfg["chat_template"] = cfg.get("chat_template", "") + " "
            (d / "tokenizer_config.json").write_text(json.dumps(cfg))
        assert tokenizer_sha256(str(d)) != h, f"hash must change when the {variant} changes"


def test_hash_ignores_files_that_do_not_affect_tokenisation(tmp_path):
    import shutil
    d = tmp_path / "t"; shutil.copytree(TOKDIR, d)
    h = tokenizer_sha256(TOKDIR)
    (d / "generation_config.json").write_text('{"temperature": 0.1}')
    assert tokenizer_sha256(str(d)) == h
