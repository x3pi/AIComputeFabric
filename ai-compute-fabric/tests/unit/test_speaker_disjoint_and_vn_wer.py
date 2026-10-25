import pytest

from audio.evaluation.vn_normalizer import cer, normalize, number_to_vi, wer
from audio.ingestion.split import speaker_disjoint_split


def recs(n_speakers=200, per=5):
    return [{"speaker_id": f"s{i}", "utt": j} for i in range(n_speakers) for j in range(per)]


def test_speaker_overlap_is_empty():
    sp = speaker_disjoint_split(recs(), seed=7)
    ids = {k: {r["speaker_id"] for r in v} for k, v in sp.items()}
    assert not (ids["train"] & ids["test"])
    assert not (ids["train"] & ids["validation"])
    assert not (ids["validation"] & ids["test"])
    assert sum(len(v) for v in sp.values()) == 1000          # nothing lost


def test_split_is_deterministic_and_roughly_proportional():
    a = speaker_disjoint_split(recs(), seed=1)
    b = speaker_disjoint_split(recs(), seed=1)
    assert a == b
    assert 0.7 < len(a["train"]) / 1000 < 0.9


def test_missing_speaker_or_bad_ratio_rejected():
    with pytest.raises(ValueError):
        speaker_disjoint_split([{"utt": 1}])
    with pytest.raises(ValueError):
        speaker_disjoint_split(recs(2), ratios=(0.5, 0.5, 0.5))


def test_tone_placement_variants_normalise_equal():
    assert normalize("hòa bình") == normalize("hoà bình")
    assert normalize("thúy") == normalize("thuý")


def test_qu_syllables_unchanged():
    assert normalize("quý") == "quý"
    assert normalize("quà") == "quà"


def test_date_matches_spoken_form():
    spoken = "hai mươi lăm tháng mười năm hai nghìn không trăm hai mươi sáu"
    assert normalize("25/10/2026") == normalize(spoken)


@pytest.mark.parametrize("n,words", [
    (0, "không"), (5, "năm"), (10, "mười"), (11, "mười một"), (15, "mười lăm"),
    (21, "hai mươi mốt"), (25, "hai mươi lăm"), (100, "một trăm"),
    (105, "một trăm lẻ năm"), (1000, "một nghìn"), (2026, "hai nghìn không trăm hai mươi sáu"),
])
def test_number_words(n, words):
    assert number_to_vi(n) == words


def test_unicode_forms_and_case_and_punctuation():
    import unicodedata
    s = "Xin chào, Việt Nam!"
    assert normalize(unicodedata.normalize("NFD", s)) == normalize(s) == "xin chào việt nam"


def test_wer_cer_known_values():
    assert wer("một hai ba bốn", "một hai ba bốn") == 0
    assert wer("một hai ba bốn", "một hai ba") == pytest.approx(0.25)     # 1 deletion / 4
    assert wer("một hai ba", "một năm ba") == pytest.approx(1 / 3)        # 1 substitution
    assert cer("abc", "abd") == pytest.approx(1 / 3)


def test_wer_ignores_orthographic_variants():
    assert wer("hòa bình", "hoà bình") == 0
