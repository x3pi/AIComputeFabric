"""Vietnamese text normaliser + WER/CER (plan P6.1, D20).

wer/cer are a dependency-free reference implementation; production uses
`evaluate`/`jiwer`, and tests cross-check against known values.
"""
from __future__ import annotations

import re
import unicodedata

_UNITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
_MARKS = ["̀", "́", "̉", "̃", "̣"]  # huyền sắc hỏi ngã nặng


def _tone_table() -> dict[str, str]:
    """new-style tone placement (hoà, thuý) -> old style (hòa, thúy)."""
    t = {}
    for first, second in (("o", "a"), ("o", "e"), ("u", "y")):
        for m in _MARKS:
            new = first + unicodedata.normalize("NFC", second + m)
            old = unicodedata.normalize("NFC", first + m) + second
            t[unicodedata.normalize("NFC", new)] = old
    return t


_TONE = _tone_table()
_TONE_RE = re.compile("(?<!q)(" + "|".join(map(re.escape, _TONE)) + ")")


def _two(n: int) -> str:
    if n < 10:
        return _UNITS[n]
    t, u = divmod(n, 10)
    if t == 1:
        head = "mười"
    else:
        head = _UNITS[t] + " mươi"
    if u == 0:
        return head
    if u == 1 and t > 1:
        return head + " mốt"
    if u == 5:
        return head + " lăm"
    return head + " " + _UNITS[u]


def _below_1000(n: int, lead: bool) -> str:
    h, r = divmod(n, 100)
    if h == 0 and lead:
        return _two(r)
    parts = [_UNITS[h] + " trăm"]
    if r == 0:
        return parts[0]
    parts.append(("lẻ " + _UNITS[r]) if r < 10 else _two(r))
    return " ".join(parts)


def number_to_vi(n: int) -> str:
    if n < 0 or n >= 10**9:
        raise ValueError("supported range: 0 <= n < 1e9")
    if n < 1000:
        return _below_1000(n, True)
    groups = []
    for name, size in (("triệu", 10**6), ("nghìn", 10**3)):
        g, n = divmod(n, size)
        if g:
            groups.append((g, name))
    words, first = [], True
    for g, name in groups:
        words.append(_below_1000(g, first) + " " + name)
        first = False
    if n:
        words.append(_below_1000(n, False) if n >= 100 else (("không trăm lẻ " + _UNITS[n]) if n < 10 else "không trăm " + _two(n)))
    return " ".join(words)


_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_INT = re.compile(r"\b\d+\b")


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFC", text).lower()
    s = _DATE.sub(lambda m: f"{number_to_vi(int(m[1]))} tháng {number_to_vi(int(m[2]))} năm {number_to_vi(int(m[3]))}", s)
    s = _INT.sub(lambda m: number_to_vi(int(m[0])), s)
    s = _TONE_RE.sub(lambda m: _TONE[m[0]], s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref).split(), normalize(hyp).split()
    return _edit_distance(r, h) / max(len(r), 1)


def cer(ref: str, hyp: str) -> float:
    r, h = list(normalize(ref).replace(" ", "")), list(normalize(hyp).replace(" ", ""))
    return _edit_distance(r, h) / max(len(r), 1)
