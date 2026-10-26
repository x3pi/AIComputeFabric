"""Unit tests for the Audio AI Factory HTTP service (studio/audio_service.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from studio.audio_service import AudioService
from studio.config import Settings
from studio.errors import ApiError
from studio.web import Request, Router


def _call(router: Router, method: str, path: str, body: dict | None = None, role: str = "operator"):
    fn, params, _ = router.match(method, path)
    assert fn is not None, f"no route for {method} {path}"
    req = Request(method=method, path=path, params=params or {}, query={}, body=body or {}, user="tester", role=role)
    return fn(req)


def test_audio_status_and_vietnamese_wer_cer_evaluator(tmp_path: Path):
    s = Settings(data_dir=tmp_path, app_dsn="postgresql://unused")
    svc = AudioService(s)
    r = Router()
    svc.mount(r)

    st = _call(r, "GET", "/v1/audio/status", role="viewer")
    assert st["ok"] is True
    assert "fleurs_vi" in st["benchmarks"]

    ev = _call(r, "POST", "/v1/audio/evaluate", {
        "reference": "Hóa đơn số 15 thanh toán ngày 15/03/2026 tại Hoà Bình.",
        "hypothesis": "hóa đơn số mười lăm thanh toán ngày mười lăm tháng ba năm hai nghìn không trăm hai mươi sáu tại hòa bình",
    })
    assert ev["wer"] == 0.0
    assert ev["cer"] == 0.0

    batch = _call(r, "POST", "/v1/audio/evaluate", {
        "pairs": [
            {"ref": "xin chào quý khách", "hyp": "xin chào quý khách"},
            {"ref": "mười hai ngày", "hyp": "12 ngày"},
        ]
    })
    assert batch["mean_wer"] == 0.0
    assert len(batch["rows"]) == 2


def test_audio_sample_wav_and_vad_spectral_gate_analysis(tmp_path: Path):
    s = Settings(data_dir=tmp_path, app_dsn="postgresql://unused")
    svc = AudioService(s)
    r = Router()
    svc.mount(r)

    sample = _call(r, "POST", "/v1/audio/sample-wav", {"noisy": True})
    assert sample["duration_s"] == 3.2
    assert sample["sample_rate"] == 16000
    assert len(sample["audio_base64"]) > 1000

    res = _call(r, "POST", "/v1/audio/analyze", {"audio_base64": sample["audio_base64"], "enhance": True})
    assert res["duration_s"] == 3.2
    assert len(res["segments"]) >= 2
    assert res["speech_seconds"] > 1.5
    assert len(res["enhanced_audio_base64"]) > 1000


def test_audio_rbac_and_zero_voice_cloning_policy(tmp_path: Path):
    s = Settings(data_dir=tmp_path, app_dsn="postgresql://unused")
    svc = AudioService(s)
    r = Router()
    svc.mount(r)

    # Viewer role is forbidden from running mutations/synthesis
    with pytest.raises(ApiError) as exc_ro:
        _call(r, "POST", "/v1/audio/tts", {"text": "xin chào"}, role="viewer")
    assert exc_ro.value.status == 403

    # Operator role attempting voice cloning or custom voice is refused with 400 voice_cloning_refused
    with pytest.raises(ApiError) as exc_clone:
        _call(r, "POST", "/v1/audio/tts", {"text": "xin chào", "voice": "cloned_ceo", "consent_ref": "c1"}, role="operator")
    assert exc_clone.value.status == 400
    assert exc_clone.value.code == "voice_cloning_refused"
