"""P1.4: the GGUF header reader, on synthetic files built here (exact expected values) and on malformed ones."""
import struct

import pytest

from core.vram_sidecar.gguf import GgufError, read_facts


def s(x):
    b = x.encode()
    return struct.pack("<Q", len(b)) + b


def kv(key, t, payload):
    return s(key) + struct.pack("<I", t) + payload


def u32(k, v):
    return kv(k, 4, struct.pack("<I", v))


def build(path, layers=4, heads_kv=2, key_len=64, tensors=None, arch="llama", extra=b"", n_extra=0, version=3, file_type=15):
    """Tensor sizes are given in bytes; offsets are 32-byte aligned like llama.cpp writes them."""
    tensors = tensors or {"token_embd.weight": 1000, **{f"blk.{i}.attn_q.weight": 640 for i in range(layers)}, "output.weight": 500}
    meta = [kv("general.architecture", 8, s(arch)), kv("general.name", 8, s("synthetic")), u32("general.file_type", file_type),
            u32(f"{arch}.block_count", layers), u32(f"{arch}.attention.head_count", 8), u32(f"{arch}.attention.head_count_kv", heads_kv),
            u32(f"{arch}.attention.key_length", key_len), u32(f"{arch}.context_length", 4096),
            # a tokenizer array that must be skipped, not kept
            kv("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 3) + s("a") + s("bb") + s("ccc"))]
    head = MAGIC + struct.pack("<IQQ", version, len(tensors), len(meta) + n_extra) + b"".join(meta) + extra
    infos, off = b"", 0
    for name, size in tensors.items():
        infos += s(name) + struct.pack("<I", 1) + struct.pack("<Q", size) + struct.pack("<I", 0) + struct.pack("<Q", off)
        off += (size + 31) // 32 * 32
    pre = head + infos
    pad = (-len(pre)) % 32
    last = list(tensors.values())[-1]
    path.write_bytes(pre + b"\0" * pad + b"\0" * (off - ((last + 31) // 32 * 32) + last))
    return tensors


MAGIC = b"GGUF"


def test_facts_of_a_synthetic_model(tmp_path):
    p = tmp_path / "m.gguf"
    build(p)
    f = read_facts(p)
    assert (f.architecture, f.layers, f.n_kv, f.head_dim, f.quantization, f.context_length) == ("llama", 4, 2, 64, "Q4_K_M", 4096)
    # each tensor is measured up to the next one's offset, so token_embd (1000 B) counts as 1024 with its alignment padding
    assert f.non_repeating_bytes == 1024 + 500
    assert f.weights_bytes == 1024 + 4 * 640 + 500


def test_unequal_key_and_value_length_keeps_the_kv_size_exact(tmp_path):
    p = tmp_path / "m.gguf"
    build(p, key_len=96, extra=u32("llama.attention.value_length", 32), n_extra=1)
    f = read_facts(p)
    assert f.head_dim == 64 and "mean" in f.note       # 96 + 32 = 2 * 64


def test_per_layer_kv_heads_are_refused(tmp_path):
    p = tmp_path / "m.gguf"
    arr = struct.pack("<IQ", 4, 2) + struct.pack("<II", 2, 4)
    build(p, extra=kv("llama.attention.head_count_kv", 9, arr), n_extra=1)
    with pytest.raises(GgufError, match="differs per layer"):
        read_facts(p)


@pytest.mark.parametrize("mutate,msg", [
    (lambda b: b"XXXX" + b[4:], "not a GGUF"),
    (lambda b: b[:4] + struct.pack("<I", 1) + b[8:], "version"),
    (lambda b: b[:60], "truncated"),
    (lambda b: b[:8] + struct.pack("<Q", 1 << 40) + b[16:], "implausible"),
])
def test_malformed_files_are_rejected_with_gguferror(tmp_path, mutate, msg):
    p = tmp_path / "m.gguf"
    build(p)
    p.write_bytes(mutate(p.read_bytes()))
    with pytest.raises(GgufError, match=msg):
        read_facts(p)


def test_split_files_are_refused(tmp_path):
    p = tmp_path / "m-00001-of-00003.gguf"
    build(p)
    with pytest.raises(GgufError, match="split"):
        read_facts(p)


def test_a_giant_string_length_does_not_allocate(tmp_path):
    p = tmp_path / "m.gguf"
    build(p)
    raw = bytearray(p.read_bytes())
    raw[16:24] = struct.pack("<Q", 1 << 60)           # length of the first key
    p.write_bytes(bytes(raw))
    with pytest.raises(GgufError, match="implausible"):
        read_facts(p)
