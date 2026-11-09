"""Audio AI Factory quality and governance gate tests (§7.4 & §15.1).

Verifies:
1. Speaker-disjoint train/validation/test split guarantees zero speaker overlap.
2. Vietnamese normalizer + WER/CER reference implementation accuracy.
3. Spectral gate noise reduction raises SNR on stationary noise without distorting clean speech.
4. FLEURS `vi_vn` benchmark report (`p6_asr_fleurs_vi_qwen3asr_0p6b.json`) meets the <= 10% clean WER gate
   and spectral enhancement lowers noisy WER/CER.
5. Vietnamese TTS -> ASR round-trip (`p6_tts_asr_roundtrip.json`) meets mean WER <= 5%.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from audio.evaluation.vn_normalizer import cer, normalize, number_to_vi, wer
from audio.executors import spectral_gate
from audio.ingestion.split import speaker_disjoint_split

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "benchmarks" / "reports"


def test_speaker_disjoint_split_has_zero_leakage():
    records = [
        {"path": f"clip_{i:04d}.wav", "speaker_id": f"spk_{i % 60:03d}", "consent_ref": f"C-{i % 60:03d}"}
        for i in range(600)
    ]
    splits = speaker_disjoint_split(records, ratios=(0.8, 0.1, 0.1), seed=42)
    sp_train = {r["speaker_id"] for r in splits["train"]}
    sp_val = {r["speaker_id"] for r in splits["validation"]}
    sp_test = {r["speaker_id"] for r in splits["test"]}

    assert sp_train.isdisjoint(sp_val), "Speaker leaked between train and validation"
    assert sp_train.isdisjoint(sp_test), "Speaker leaked between train and test"
    assert sp_val.isdisjoint(sp_test), "Speaker leaked between validation and test"
    assert sum(len(v) for v in splits.values()) == len(records)


def test_vietnamese_normalizer_and_wer_cer_gates():
    assert number_to_vi(0) == "không"
    assert number_to_vi(15) == "mười lăm"
    assert number_to_vi(21) == "hai mươi mốt"
    assert number_to_vi(2026) == "hai nghìn không trăm hai mươi sáu"

    # Tone canonicalization (hoà -> hòa, thuý -> thúy) and date/number expansion
    ref = "Hoà bình ngày 15/3/2026 có 21 đại biểu."
    hyp = "hòa bình ngày mười lăm tháng ba năm hai nghìn không trăm hai mươi sáu có hai mươi mốt đại biểu"
    assert normalize(ref) == normalize(hyp)
    assert wer(ref, hyp) == 0.0
    assert cer(ref, hyp) == 0.0


def test_fleurs_vi_asr_report_meets_quality_gate():
    report_path = REPORTS / "p6_asr_fleurs_vi_qwen3asr_0p6b.json"
    assert report_path.is_file()
    data = json.loads(report_path.read_text(encoding="utf-8"))

    assert data["clips"] >= 100
    # Clean FLEURS vi_vn WER gate <= 10% (measured 8.11% clip mean, 7.90% corpus)
    assert data["clean"]["wer"] <= 0.10
    assert data["clean"]["cer"] <= 0.06
    # Spectral gate enhancement must improve WER and CER on 10 dB SNR noisy speech
    assert data["enhanced"]["wer"] < data["noisy"]["wer"]
    assert data["enhanced"]["cer"] < data["noisy"]["cer"]


def test_tts_asr_roundtrip_intelligibility_gate():
    report_path = REPORTS / "p6_tts_asr_roundtrip.json"
    assert report_path.is_file()
    data = json.loads(report_path.read_text(encoding="utf-8"))

    assert len(data["rows"]) >= 5
    assert data["mean_wer"] <= 0.05
    for row in data["rows"]:
        assert row["duration_s"] > 0.5
        assert row["wer"] <= 0.10


def test_spectral_gate_improves_snr_without_clipping():
    sr = 16000
    t = np.arange(sr * 2) / sr
    clean = (0.35 * np.sin(2 * np.pi * 240 * t) + 0.15 * np.sin(2 * np.pi * 720 * t)).astype(np.float32)
    clean[: sr // 4] = 0.0  # leading silence for noise floor estimation
    rng = np.random.default_rng(7)
    noisy = (clean + 0.04 * rng.standard_normal(len(clean))).astype(np.float32)

    enhanced = spectral_gate(noisy, sr)
    mse_noisy = float(np.mean((noisy - clean) ** 2))
    mse_enhanced = float(np.mean((enhanced - clean) ** 2))
    assert mse_enhanced < mse_noisy
    assert float(np.max(np.abs(enhanced))) <= 1.0

