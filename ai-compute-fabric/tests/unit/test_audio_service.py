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


def test_audio_dataset_speaker_disjoint_split_and_diloco_simulation(tmp_path: Path):
    import json
    s = Settings(data_dir=tmp_path, app_dsn="postgresql://unused")
    svc = AudioService(s)
    r = Router()
    svc.mount(r)

    manifest = "\n".join(
        json.dumps({
            "path": f"wav/spk{i % 6}_{i}.wav",
            "sha256": "a" * 64,
            "speaker_id": f"spk_{i % 6}",
            "consent_ref": f"consent_{i % 6}",
            "text": f"câu số {i}",
        })
        for i in range(18)
    )
    sp = _call(r, "POST", "/v1/audio/dataset/split", {"manifest_jsonl": manifest, "seed": 7})
    assert sp["disjoint_verified"] is True
    assert sp["speaker_overlap_count"] == 0
    assert sp["total_records"] == 18

    # Missing consent_ref must be rejected with 400 missing_governance_field
    with pytest.raises(ApiError) as exc_gov:
        _call(r, "POST", "/v1/audio/dataset/split", {
            "manifest_jsonl": json.dumps({"path": "wav/a.wav", "sha256": "b" * 64, "speaker_id": "spk_1", "consent_ref": ""})
        })
    assert exc_gov.value.status == 400
    assert exc_gov.value.code == "missing_governance_field"

    sim = _call(r, "POST", "/v1/audio/training/simulate-diloco", {
        "workers": 4,
        "inner_steps": 50,
        "total_steps": 200,
        "rank": 8,
        "inject_stale": True,
    })
    assert sim["stale_update_rejected"] is True
    assert sim["lora_math"]["lora_fa_frobenius_error"] < 1e-6
    assert sim["lora_math"]["naive_merge_frobenius_error"] > 0.0
    assert sim["network"]["bandwidth_saved_pct"] > 90.0


def test_custom_voice_profiles_crud_and_dsp_preview(tmp_path: Path):
    s = Settings(data_dir=tmp_path, app_dsn="postgresql://unused")
    svc = AudioService(s)
    r = Router()
    svc.mount(r)

    listed = _call(r, "GET", "/v1/audio/tenants/demo/voices", role="viewer")
    assert len(listed["voices"]) == 4  # 4 builtin presets

    created = _call(r, "POST", "/v1/audio/tenants/demo/voices", {
        "name": "Nữ CSKH Miền Bắc",
        "pitch_semitones": 2.5,
        "speaking_rate": 1.05,
        "noise_scale": 0.55,
        "noise_scale_w": 0.75,
        "warmth_db": -1.5,
    }, role="operator")
    assert created["ok"] is True
    vid = created["voice"]["id"]
    assert vid.startswith("custom_")

    listed_after = _call(r, "GET", "/v1/audio/tenants/demo/voices", role="viewer")
    assert len(listed_after["voices"]) == 5

    # Out-of-bounds pitch must fail validation
    with pytest.raises(ApiError) as exc_bad:
        _call(r, "POST", "/v1/audio/tenants/demo/voices", {
            "name": "Quá cao",
            "pitch_semitones": 12.0,
        }, role="operator")
    assert exc_bad.value.status == 400
    assert exc_bad.value.code == "bad_voice_params"

    # DSP preview (pitch shift + EQ) preserves duration
    dsp_res = _call(r, "POST", "/v1/audio/voices/preview-dsp", {
        "pitch_semitones": 3.0,
        "speaking_rate": 1.0,
        "noise_scale": 0.667,
        "noise_scale_w": 0.8,
        "warmth_db": 2.5,
    }, role="operator")
    assert dsp_res["duration_s"] == 3.2
    assert len(dsp_res["audio_base64"]) > 1000

    # Delete requires admin role
    with pytest.raises(ApiError) as exc_del_ro:
        _call(r, "DELETE", f"/v1/audio/tenants/demo/voices/{vid}", role="operator")
    assert exc_del_ro.value.status == 403

    deleted = _call(r, "DELETE", f"/v1/audio/tenants/demo/voices/{vid}", role="admin")
    assert deleted["ok"] is True
    assert len(_call(r, "GET", "/v1/audio/tenants/demo/voices", role="viewer")["voices"]) == 4


