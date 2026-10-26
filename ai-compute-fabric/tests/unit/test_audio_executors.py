"""Pure parts of the audio executors (VAD, spectral gate, I/O guards, refusals). No GPU, no model."""
import wave

import numpy as np
import pytest

from agents.doc_executors import RefusedInput
from agents.tasks import Task, TaskType
from agents.validator import validate_result
from audio.executors import AudioExecutor, load, save, spectral_gate
from audio.ingestion.dataset import energy_vad

SR = 16000


def speechlike(seconds=4.0, level=0.3, seed=0):
    """Two bursts of band-limited modulated noise (a stand-in for speech) with silence around them."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(seconds * SR), dtype=np.float32)
    t = np.arange(SR) / SR
    for start in (0.5, 2.3):
        burst = (np.sin(2 * np.pi * 220 * t) + 0.5 * np.sin(2 * np.pi * 660 * t) + 0.2 * rng.standard_normal(SR)) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t))
        i = int(start * SR)
        x[i:i + SR] = (burst / np.abs(burst).max() * level).astype(np.float32)
    return x


def snr_db(clean, test):
    return 10 * np.log10(np.sum(clean ** 2) / np.sum((test - clean) ** 2))


def task(tp, inp):
    return Task(task_id="t", tenant_id="acme", type=tp, input=inp)


def test_fixed_threshold_vad_misses_a_quiet_recording_but_the_relative_one_does_not():
    quiet = speechlike(level=0.02)                                     # about -49 dB rms, like the FLEURS clip that exposed it
    assert energy_vad(quiet, SR) == []                                 # the old fixed -40 dB gate: no speech at all
    seg = energy_vad(quiet, SR, relative_db=30.0)
    assert len(seg) == 2 and abs(seg[0][0] - 0.5) < 0.15 and abs(seg[1][0] - 2.3) < 0.15


def test_relative_vad_is_level_invariant_and_silence_stays_silent():
    a, b = energy_vad(speechlike(level=0.3), SR, relative_db=30.0), energy_vad(speechlike(level=0.03), SR, relative_db=30.0)
    assert len(a) == len(b) == 2
    assert all(abs(p - q) < 0.1 for x, y in zip(a, b) for p, q in zip(x, y))
    assert energy_vad(np.zeros(SR * 3, dtype=np.float32), SR, relative_db=30.0) == []          # floor keeps digital silence silent
    assert energy_vad((np.random.default_rng(1).standard_normal(SR * 3) * 1e-6).astype(np.float32), SR, relative_db=30.0) == []


def test_spectral_gate_raises_snr_on_stationary_noise_and_is_not_a_no_op():
    clean = speechlike()
    rng = np.random.default_rng(3)
    noisy = (clean + rng.standard_normal(len(clean)) * 0.03).astype(np.float32)
    out = spectral_gate(noisy, SR)
    assert len(out) == len(noisy) and np.all(np.isfinite(out))
    assert snr_db(clean, out) > snr_db(clean, noisy) + 3.0, (snr_db(clean, noisy), snr_db(clean, out))


def test_spectral_gate_leaves_clean_loud_speech_mostly_intact_and_handles_short_input():
    clean = speechlike()
    out = spectral_gate(clean, SR)
    assert snr_db(clean, out) > 10.0
    tiny = np.ones(100, dtype=np.float32) * 0.1
    assert np.array_equal(spectral_gate(tiny, SR), tiny)


def test_save_then_load_round_trips(tmp_path):
    x = speechlike()
    name = save(tmp_path / "out", x, SR, "t")
    y, sr = load(tmp_path, "out/" + name)
    assert sr == SR and len(y) == len(x) and np.max(np.abs(x - y)) < 1e-3
    assert save(tmp_path / "out", x, SR, "t") == name          # content-addressed: same audio, same name


def test_vad_task_validates_and_reports_segments(tmp_path):
    save(tmp_path, speechlike(), SR, "a")
    ref = next(tmp_path.glob("a-*.wav")).name
    t = task(TaskType.AUDIO_VAD, {"audio_ref": ref})
    res = AudioExecutor(tmp_path).execute(t)
    assert len(res.output["segments"]) == 2 and validate_result(t, res).ok


def test_enhance_task_writes_a_new_file_under_out(tmp_path):
    save(tmp_path, speechlike(), SR, "a")
    ref = next(tmp_path.glob("a-*.wav")).name
    t = task(TaskType.AUDIO_ENHANCE, {"audio_ref": ref})
    res = AudioExecutor(tmp_path).execute(t)
    assert res.output["audio_ref"].startswith("out/") and (tmp_path / res.output["audio_ref"]).is_file() and validate_result(t, res).ok


@pytest.mark.parametrize("ref", ["../x.wav", "/etc/passwd", "missing.wav"])
def test_audio_references_outside_the_root_or_missing_are_refused(tmp_path, ref):
    with pytest.raises(RefusedInput):
        AudioExecutor(tmp_path).execute(task(TaskType.AUDIO_VAD, {"audio_ref": ref}))


def test_non_wav_24bit_and_overlong_audio_are_refused(tmp_path, monkeypatch):
    (tmp_path / "x.wav").write_bytes(b"RIFF....not really")
    with pytest.raises(RefusedInput):
        load(tmp_path, "x.wav")
    with wave.open(str(tmp_path / "w24.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(3); w.setframerate(SR); w.writeframes(b"\x00" * 3 * SR)
    with pytest.raises(RefusedInput):
        load(tmp_path, "w24.wav")
    save(tmp_path, speechlike(), SR, "ok")
    monkeypatch.setattr("audio.executors.MAX_SECONDS", 1.0)
    with pytest.raises(RefusedInput):
        load(tmp_path, next(tmp_path.glob("ok-*.wav")).name)


def test_voice_cloning_and_other_languages_are_refused_not_faked(tmp_path):
    ex = AudioExecutor(tmp_path, tts_model="unused", asr_model="unused")
    with pytest.raises(RefusedInput):
        ex.execute(task(TaskType.AUDIO_TTS, {"text": "xin chào", "consent_ref": "consent-1"}))
    with pytest.raises(RefusedInput):
        ex.execute(task(TaskType.AUDIO_TTS, {"text": "xin chào", "voice": "someone-else"}))
    save(tmp_path, speechlike(), SR, "a")
    with pytest.raises(RefusedInput):
        ex.execute(task(TaskType.AUDIO_ASR, {"audio_ref": next(tmp_path.glob("a-*.wav")).name, "lang": "fr"}))


def test_asr_and_tts_say_what_they_need_when_no_model_is_configured(tmp_path):
    save(tmp_path, speechlike(), SR, "a")
    ref = next(tmp_path.glob("a-*.wav")).name
    with pytest.raises(NotImplementedError, match="asr_model"):
        AudioExecutor(tmp_path).execute(task(TaskType.AUDIO_ASR, {"audio_ref": ref}))
    with pytest.raises(NotImplementedError, match="tts_model"):
        AudioExecutor(tmp_path).execute(task(TaskType.AUDIO_TTS, {"text": "xin chào"}))


def test_silent_audio_gives_empty_transcript_without_loading_a_model(tmp_path):
    save(tmp_path, np.zeros(SR * 2, dtype=np.float32), SR, "z")
    res = AudioExecutor(tmp_path, asr_model="/does/not/exist").execute(task(TaskType.AUDIO_ASR, {"audio_ref": next(tmp_path.glob("z-*.wav")).name}))
    assert res.output == {"text": "", "segments": []}
