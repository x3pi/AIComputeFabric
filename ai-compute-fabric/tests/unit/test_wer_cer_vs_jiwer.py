"""P6.1: our WER/CER (audio/evaluation/vn_normalizer.py) against jiwer, the reference implementation, on the same normalised text."""
import os
import random
from pathlib import Path

import pytest

jiwer = pytest.importorskip("jiwer")

from audio.evaluation.vn_normalizer import cer, normalize, wer  # noqa: E402

BUILTIN = ["Người bệnh cũng đã từng tới Nigeria nơi một số ca mắc bệnh Ebola xảy ra.",
           "Hệ thống phân phối tính toán trí tuệ nhân tạo trên nhiều máy tính.",
           "Ngày 15/03/2024 có 1250 người đến dự hội nghị.",
           "Hòa bình và thủy điện: quý khách vui lòng giữ gìn vệ sinh chung!",
           "Một hai ba bốn năm sáu bảy tám chín mười."]
WORDS = "một hai ba người máy tính hệ thống phân phối nhiều trên và của là có không được những cho với".split()


def corrupt(rng, text):
    w = text.split()
    for _ in range(rng.randint(0, 4)):
        op = rng.choice("sdi")
        i = rng.randrange(max(len(w), 1))
        if op == "s" and w:
            w[i % len(w)] = rng.choice(WORDS)
        elif op == "d" and len(w) > 1:
            del w[i % len(w)]
        else:
            w.insert(i, rng.choice(WORDS))
    return " ".join(w)


def corpus():
    rng = random.Random(7)
    lines = list(BUILTIN)
    real = os.environ.get("AICF_FLEURS_TEXT")
    if real and Path(real).is_file():
        lines += Path(real).read_text(encoding="utf-8").splitlines()[:300]
    return [(t, corrupt(rng, t)) for t in lines for _ in range(8)]


def test_wer_matches_jiwer_on_normalised_text():
    for ref, hyp in corpus():
        r, h = normalize(ref), normalize(hyp)
        assert wer(ref, hyp) == pytest.approx(jiwer.wer(r, h), abs=1e-12), (ref, hyp)


def test_cer_matches_jiwer_when_spaces_are_ignored():
    for ref, hyp in corpus():
        r, h = normalize(ref).replace(" ", ""), normalize(hyp).replace(" ", "")
        if r and h:
            assert cer(ref, hyp) == pytest.approx(jiwer.cer(r, h), abs=1e-12), (ref, hyp)


def test_known_values():
    assert wer("một hai ba", "một hai ba") == 0.0
    assert wer("một hai ba", "một ba") == pytest.approx(1 / 3)
    assert wer("một hai ba", "") == 1.0
    assert wer("", "") == 0.0 and wer("", "x") == 1.0   # empty reference: finite instead of an exception
