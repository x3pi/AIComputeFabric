"""Audio dataset ingestion (plan P6.1): manifest with consent and checksum, streaming reader, resampling, simple VAD.

Everything here is stdlib + numpy and works on PCM16 WAV only. Not covered: other containers/codecs
(mp3, flac, opus), 24-bit audio, the Hugging Face `datasets` streaming API, and a neural VAD (Silero or
similar). The VAD below is a short-time energy gate: fine for clean recordings, unreliable in noise."""
from __future__ import annotations

import hashlib
import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np


class IngestError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    path: Path
    sha256: str
    speaker_id: str
    consent_ref: str
    text: str = ""


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def read_manifest(manifest: Path, root: Path | None = None) -> Iterator[Record]:
    """Streams a JSON-lines manifest. A line without consent_ref, speaker_id or sha256 is an error, not a skip:
    silently dropping or accepting such a record would hide a data-governance gap."""
    root = root or manifest.parent
    with open(manifest, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError as e:
                raise IngestError(f"manifest line {n}: not JSON ({e})") from None
            for k in ("path", "sha256", "speaker_id", "consent_ref"):
                if not isinstance(d.get(k), str) or not d[k].strip():
                    raise IngestError(f"manifest line {n}: missing {k}")
            rel = Path(d["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise IngestError(f"manifest line {n}: path must stay inside the dataset root")
            yield Record(root / rel, d["sha256"].lower(), d["speaker_id"], d["consent_ref"], d.get("text", ""))


def read_wav(p: Path, max_seconds: float = 3600.0) -> tuple[np.ndarray, int]:
    with wave.open(str(p), "rb") as w:
        if w.getsampwidth() != 2:
            raise IngestError(f"{p.name}: only 16-bit PCM is supported")
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        if n / sr > max_seconds:
            raise IngestError(f"{p.name}: longer than {max_seconds}s")
        pcm = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return pcm, sr


def resample(x: np.ndarray, sr_in: int, sr_out: int, taps_per_phase: int = 16) -> np.ndarray:
    """Polyphase windowed-sinc resampler (Kaiser window, cutoff just below the lower Nyquist)."""
    if sr_in == sr_out:
        return x
    g = math.gcd(sr_in, sr_out)
    up, down = sr_out // g, sr_in // g
    n_taps = 2 * taps_per_phase * max(up, down) + 1
    cutoff = 0.95 / (2 * max(up, down))                       # relative to the upsampled rate
    t = np.arange(n_taps) - (n_taps - 1) / 2
    h = np.sinc(2 * cutoff * t) * 2 * cutoff * np.kaiser(n_taps, 8.6) * up
    xu = np.zeros(len(x) * up + n_taps, dtype=np.float32)
    xu[: len(x) * up : up] = x
    y = np.convolve(xu, h.astype(np.float32), mode="full")
    delay = (n_taps - 1) // 2
    y = y[delay : delay + len(x) * up : down]
    return y[: int(math.ceil(len(x) * sr_out / sr_in))].astype(np.float32)


def energy_vad(x: np.ndarray, sr: int, frame_ms: int = 30, threshold_db: float = -40.0, min_speech_ms: int = 90, hangover_ms: int = 150,
               relative_db: float | None = None, floor_db: float = -75.0) -> list:
    """(start_s, end_s) of regions louder than `threshold_db` relative to full scale.
    With `relative_db` the threshold follows the recording: 95th-percentile frame level minus relative_db, never below
    `floor_db` (digital silence stays silent). A fixed -40 dB found NO speech in a quietly recorded real clip (FLEURS, rms -49 dB)."""
    n = int(sr * frame_ms / 1000)
    if n == 0 or len(x) < n:
        return []
    frames = x[: len(x) // n * n].reshape(-1, n)
    db = 10 * np.log10(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)
    if relative_db is not None:
        threshold_db = max(floor_db, float(np.percentile(db, 95)) - relative_db)
    active = db > threshold_db
    hang = max(1, hangover_ms // frame_ms)
    out, start, quiet = [], None, 0
    for i, a in enumerate(active):
        if a:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= hang:
                out.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        out.append((start, len(active) - quiet))
    min_f = max(1, min_speech_ms // frame_ms)
    return [(s * n / sr, e * n / sr) for s, e in out if e - s >= min_f]


def stream_dataset(manifest: Path, target_sr: int = 16000, verify: bool = True, root: Path | None = None) -> Iterator[dict]:
    """One decoded, resampled, mono item at a time: memory use does not grow with the dataset."""
    for rec in read_manifest(manifest, root):
        if not rec.path.is_file():
            raise IngestError(f"{rec.path.name}: file missing")
        if verify and sha256_file(rec.path) != rec.sha256:
            raise IngestError(f"{rec.path.name}: checksum mismatch (file changed or corrupted)")
        x, sr = read_wav(rec.path)
        x = resample(x, sr, target_sr)
        yield {"audio": x, "sr": target_sr, "speech": energy_vad(x, target_sr), "speaker_id": rec.speaker_id,
               "consent_ref": rec.consent_ref, "text": rec.text, "source": str(rec.path.name)}
