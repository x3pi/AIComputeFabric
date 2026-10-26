import json
import os
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from audio.ingestion.dataset import IngestError, energy_vad, read_manifest, resample, sha256_file, stream_dataset


def write_wav(p: Path, x: np.ndarray, sr: int, channels: int = 1):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def tone(freq, sr, secs, amp=0.5):
    t = np.arange(int(sr * secs)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_freq(x, sr):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.argmax(spec) * sr / len(x)


def level_db(x):
    x = x[len(x) // 10 : -len(x) // 10]            # skip filter edges
    return 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


@pytest.mark.parametrize("sr_in", [48000, 44100, 22050, 8000])
def test_resampling_keeps_frequency_and_level(sr_in):
    y = resample(tone(1000, sr_in, 1.0), sr_in, 16000)
    assert abs(len(y) - 16000) <= 1
    assert abs(peak_freq(y, 16000) - 1000) < 2
    assert abs(level_db(y) - level_db(tone(1000, 16000, 1.0))) < 0.5      # amplitude preserved within 0.5 dB


def test_resampling_removes_what_cannot_be_represented():
    y = resample(tone(12000, 48000, 1.0), 48000, 16000)                    # above the 8 kHz Nyquist of the output
    assert level_db(y) < level_db(tone(1000, 16000, 1.0)) - 40, "an out-of-band tone must be attenuated by at least 40 dB, not aliased"


def test_vad_finds_speech_and_ignores_silence():
    sr = 16000
    x = np.concatenate([np.zeros(sr), tone(300, sr, 0.5), np.zeros(sr), tone(500, sr, 0.3)]).astype(np.float32)
    segs = energy_vad(x, sr)
    assert len(segs) == 2
    assert abs(segs[0][0] - 1.0) < 0.06 and abs(segs[0][1] - 1.5) < 0.2
    assert abs(segs[1][0] - 2.5) < 0.06
    assert energy_vad(np.zeros(sr, dtype=np.float32), sr) == []
    assert energy_vad(tone(300, sr, 0.03), sr, min_speech_ms=90) == []     # too short to count


def make_dataset(root: Path, n: int, sr: int = 8000, secs: float = 0.5) -> Path:
    manifest = root / "manifest.jsonl"
    x = tone(440, sr, secs)
    with open(manifest, "w") as m:
        for i in range(n):
            p = root / f"a{i:05d}.wav"
            write_wav(p, x * (0.2 + 0.6 * ((i % 7) / 7)), sr)
            m.write(json.dumps({"path": p.name, "sha256": sha256_file(p), "speaker_id": f"spk{i % 50}", "consent_ref": f"CONSENT-{i % 50}", "text": "xin chào"}) + "\n")
    return manifest


def test_manifest_without_consent_or_checksum_is_an_error_not_a_skip(tmp_path):
    m = make_dataset(tmp_path, 2)
    good = m.read_text().splitlines()
    for field in ("consent_ref", "sha256", "speaker_id", "path"):
        d = json.loads(good[0])
        d.pop(field)
        m.write_text(json.dumps(d) + "\n")
        with pytest.raises(IngestError, match=field):
            list(read_manifest(m))
    d = json.loads(good[0])
    d["consent_ref"] = "   "
    m.write_text(json.dumps(d) + "\n")
    with pytest.raises(IngestError, match="consent_ref"):
        list(read_manifest(m))
    for bad in ("../outside.wav", "/etc/passwd"):
        d = json.loads(good[0])
        d["path"] = bad
        m.write_text(json.dumps(d) + "\n")
        with pytest.raises(IngestError, match="inside"):
            list(read_manifest(m))


def test_corrupted_or_missing_files_are_caught_by_the_checksum(tmp_path):
    m = make_dataset(tmp_path, 3)
    f = tmp_path / "a00001.wav"
    data = bytearray(f.read_bytes())
    data[200] ^= 0x01
    f.write_bytes(bytes(data))
    with pytest.raises(IngestError, match="checksum"):
        list(stream_dataset(m))
    assert len(list(stream_dataset(m, verify=False))) == 3             # explicit opt-out still works
    f.unlink()
    with pytest.raises(IngestError, match="missing"):
        list(stream_dataset(m, verify=False))


def test_stereo_is_mixed_to_mono_and_16bit_only(tmp_path):
    write_wav(tmp_path / "s.wav", tone(500, 16000, 0.5), 16000, channels=2)
    from audio.ingestion.dataset import read_wav
    x, sr = read_wav(tmp_path / "s.wav")
    assert x.ndim == 1 and abs(len(x) - 8000) <= 1
    with wave.open(str(tmp_path / "w8.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(8000); w.writeframes(b"\x80" * 800)
    with pytest.raises(IngestError, match="16-bit"):
        read_wav(tmp_path / "w8.wav")


def rss_mb():
    with open("/proc/self/statm") as f:                       # current resident set (Linux), not the historical peak
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20


def test_ten_thousand_files_stream_with_flat_memory(tmp_path):
    n = 10_000
    m = make_dataset(tmp_path, n)
    rss0 = rss_mb()
    peak = rss0
    t0, count, speakers = time.time(), 0, set()
    for item in stream_dataset(m):
        count += 1
        if count % 500 == 0:
            peak = max(peak, rss_mb())
        speakers.add(item["speaker_id"])
        assert item["sr"] == 16000 and item["consent_ref"].startswith("CONSENT-")
    dt = time.time() - t0
    grew_mb = peak - rss0
    print(f"10k files: {dt:.1f}s ({n / dt:.0f} files/s), peak RSS growth {grew_mb:.0f} MB")
    assert count == n and len(speakers) == 50
    assert grew_mb < 100, f"memory grew {grew_mb:.0f} MB while streaming: something is accumulating"


def test_vad_hangover_bridges_short_pauses_but_not_long_ones():
    sr = 16000
    short = np.concatenate([tone(300, sr, 0.4), np.zeros(int(0.06 * sr)), tone(300, sr, 0.4)]).astype(np.float32)   # 60 ms dip inside a word
    long = np.concatenate([tone(300, sr, 0.4), np.zeros(int(0.30 * sr)), tone(300, sr, 0.4)]).astype(np.float32)    # 300 ms pause between phrases
    assert len(energy_vad(short, sr)) == 1
    assert len(energy_vad(long, sr)) == 2
