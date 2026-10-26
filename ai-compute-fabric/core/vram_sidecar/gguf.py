"""Read the architecture facts the VRAM estimator needs straight from a GGUF file header (plan P1.4).

Pure standard library; reads only the metadata and the tensor table, never the weights. The numbers it returns are the
same quantities the Go estimator takes (core/vram-estimator ModelFacts): layer count, KV heads, head dimension, total weight
bytes and the bytes outside the repeating blocks. Tensor sizes come from the gaps between consecutive tensor offsets, so
they include at most `alignment - 1` bytes of padding per tensor (about 0.001 % of a real model).

Not handled: split GGUF files (-00001-of-0000N), files whose KV-head count differs per layer (hybrid/SSM models: refused
with an error rather than guessed), GGUF versions below 2."""
from __future__ import annotations

import os
import re
import struct
from dataclasses import asdict, dataclass

MAGIC = b"GGUF"
# ggml file types (general.file_type) that matter for the report; anything else is shown as "type N"
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
              14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 32: "BF16"}
_SCALAR = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
MAX_STRING = 1 << 24
MAX_ITEMS = 1 << 24


class GgufError(ValueError):
    pass


class _R:
    def __init__(self, f):
        self.f = f

    def take(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise GgufError("truncated GGUF file")
        return b

    def unpack(self, fmt, size):
        return struct.unpack(fmt, self.take(size))[0]

    def string(self):
        n = self.unpack("<Q", 8)
        if n > MAX_STRING:
            raise GgufError("string length implausible")
        return self.take(n).decode("utf-8", "replace")

    def value(self, t, keep=True):
        if t in _SCALAR:
            fmt, size = _SCALAR[t]
            return self.unpack(fmt, size)
        if t == 8:
            return self.string()
        if t == 9:
            et = self.unpack("<I", 4)
            n = self.unpack("<Q", 8)
            if n > MAX_ITEMS:
                raise GgufError("array length implausible")
            if et in _SCALAR and not keep:
                self.take(_SCALAR[et][1] * n)
                return None
            items = [self.value(et, keep) for _ in range(n)]
            return items if keep else None
        raise GgufError(f"unknown value type {t}")


@dataclass(frozen=True)
class GgufFacts:
    architecture: str
    name: str
    quantization: str
    layers: int
    layers_attn: int
    n_kv: int
    head_dim: int
    context_length: int
    weights_bytes: int
    non_repeating_bytes: int
    embedding_cpu_bytes: int      # token_embd.weight when output.weight is a separate tensor: llama.cpp keeps it in host memory
    experts: int
    file_bytes: int
    note: str = ""

    def as_dict(self):
        return asdict(self)


_BLK = re.compile(r"^blk\.(\d+)\.")


def read_facts(path: str | os.PathLike) -> GgufFacts:
    size = os.path.getsize(path)
    if re.search(r"-\d{5}-of-\d{5}\.gguf$", str(path)):
        raise GgufError("split GGUF files are not supported; pass a single file")
    with open(path, "rb") as f:
        r = _R(f)
        if r.take(4) != MAGIC:
            raise GgufError("not a GGUF file")
        version = r.unpack("<I", 4)
        if version < 2:
            raise GgufError(f"GGUF version {version} not supported")
        n_tensors, n_kv = r.unpack("<Q", 8), r.unpack("<Q", 8)
        if n_tensors > 1 << 20 or n_kv > 1 << 20:
            raise GgufError("tensor/kv count implausible")
        kv = {}
        for _ in range(n_kv):
            key = r.string()
            t = r.unpack("<I", 4)
            kv[key] = r.value(t, keep=not key.startswith("tokenizer."))
        tensors = []
        for _ in range(n_tensors):
            name = r.string()
            nd = r.unpack("<I", 4)
            if nd > 8:
                raise GgufError("tensor rank implausible")
            [r.unpack("<Q", 8) for _ in range(nd)]
            r.unpack("<I", 4)
            tensors.append((name, r.unpack("<Q", 8)))
        align = int(kv.get("general.alignment", 32))
        data_start = (f.tell() + align - 1) // align * align
    if not tensors:
        raise GgufError("no tensors")
    order = sorted(tensors, key=lambda t: t[1])
    sizes = {}
    for i, (name, off) in enumerate(order):
        end = order[i + 1][1] if i + 1 < len(order) else size - data_start
        if end < off:
            raise GgufError("tensor offsets are inconsistent with the file size")
        sizes[name] = end - off
    arch = str(kv.get("general.architecture", ""))
    if not arch:
        raise GgufError("general.architecture missing")

    def need(suffix):
        k = f"{arch}.{suffix}"
        if k not in kv:
            raise GgufError(f"{k} missing")
        return kv[k]

    layers = int(need("block_count"))
    heads_kv = kv.get(f"{arch}.attention.head_count_kv", kv.get(f"{arch}.attention.head_count"))
    if heads_kv is None:
        raise GgufError(f"{arch}.attention.head_count_kv missing")
    if isinstance(heads_kv, list):
        if len(set(heads_kv)) != 1:
            raise GgufError("KV head count differs per layer (hybrid model): not supported, refusing to guess")
        heads_kv = heads_kv[0]
    n_heads = kv.get(f"{arch}.attention.head_count")
    if isinstance(n_heads, list):
        n_heads = max(n_heads)
    k_raw = kv.get(f"{arch}.attention.key_length")
    if k_raw is None:
        if not n_heads:
            raise GgufError("cannot derive the head dimension")
        klen = vlen = int(need("embedding_length")) // int(n_heads)
    else:
        klen = int(k_raw)
        vlen = int(kv.get(f"{arch}.attention.value_length", klen))
    note = ""
    if (klen + vlen) % 2:
        raise GgufError("key_length + value_length is odd")
    head_dim = (klen + vlen) // 2
    if klen != vlen:
        note = f"key_length {klen} != value_length {vlen}: head_dim is their mean so the KV size stays exact"
    blk = {n: s for n, s in sizes.items() if _BLK.match(n)}
    ft = kv.get("general.file_type")
    quant = "unknown" if ft is None else FILE_TYPES.get(int(ft), f"type {ft}")
    total = sum(sizes.values())
    embd_cpu = sizes.get("token_embd.weight", 0) if "output.weight" in sizes else 0       # tied models reuse token_embd as the output matrix, which runs on the GPU
    return GgufFacts(architecture=arch, name=str(kv.get("general.name", "")), quantization=quant, layers=layers, layers_attn=layers,
                     n_kv=int(heads_kv), head_dim=head_dim, context_length=int(kv.get(f"{arch}.context_length", 0)),
                     weights_bytes=total, non_repeating_bytes=total - sum(blk.values()), embedding_cpu_bytes=embd_cpu, experts=int(kv.get(f"{arch}.expert_count", 0)),
                     file_bytes=size, note=note)
