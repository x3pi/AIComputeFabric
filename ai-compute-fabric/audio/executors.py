"""AUDIO_VAD / AUDIO_ASR / AUDIO_TTS / AUDIO_ENHANCE executors (plan P6.1-P6.2).

Engines: energy VAD (audio.ingestion.dataset), Qwen3-ASR-0.6B (transformers) for ASR, facebook/mms-tts-vie for Vietnamese
TTS, a numpy spectral-gate for enhancement. Heavy imports are lazy, so importing this module costs nothing and the
unit tests of the pure parts need neither torch nor a GPU.

Honest limits:
 * ENHANCE is a classical spectral gate, NOT DeepFilterNet (plan names DeepFilterNet; not integrated).
 * TTS: Qwen3-TTS does not list Vietnamese, so Vietnamese uses MMS-TTS (licence CC-BY-NC 4.0: NOT cleared for commercial
   use). Voice cloning is not offered; a request that names a consent_ref for a cloned voice is refused, not faked.
 * WAV PCM16 in/out only. Audio refs are relative to a data root (same guard as documents)."""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from agents.doc_executors import RefusedInput, resolve
from agents.tasks import Task, TaskResult, TaskType, parse_input
from audio.ingestion.dataset import IngestError, energy_vad, read_wav, resample

MAX_SECONDS = 600.0


def vad(x, sr):
    return energy_vad(x, sr, relative_db=30.0)


def load(root: Path, ref: str, sr: int | None = None) -> tuple[np.ndarray, int]:
    path = resolve(root, ref, limit=200 * 1024 * 1024)
    try:
        x, s = read_wav(path, max_seconds=MAX_SECONDS)
    except (IngestError, wave.Error, EOFError) as e:
        raise RefusedInput(f"unreadable audio: {e}") from None
    if sr and s != sr:
        x, s = resample(x, s, sr), sr
    if not np.all(np.isfinite(x)):
        raise RefusedInput("audio contains non-finite samples")
    return x, s


def save(out_dir: Path, x: np.ndarray, sr: int, prefix: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    name = f"{prefix}-{hashlib.sha256(pcm).hexdigest()[:16]}.wav"
    with wave.open(str(out_dir / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return name


def spectral_gate(x: np.ndarray, sr: int, noise_s: float = 0.3, n_fft: int = 512, reduce_db: float = 12.0) -> np.ndarray:
    """Noise estimated from the quietest `noise_s` seconds of frames; bins below noise*1.5 are attenuated by reduce_db
    (smoothed over time). Works on stationary noise; it can distort speech and does nothing for babble or music."""
    hop = n_fft // 4
    if len(x) < n_fft * 2:
        return x.copy()
    win = np.hanning(n_fft).astype(np.float32)
    pad = np.pad(x, (n_fft, n_fft))
    idx = np.arange(0, len(pad) - n_fft, hop)
    frames = np.stack([pad[i:i + n_fft] * win for i in idx])
    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec)
    energy = mag.sum(axis=1)
    k = max(3, int(noise_s * sr / hop))
    quiet = np.argsort(energy)[:k]
    noise = mag[quiet].mean(axis=0)
    floor = 10 ** (-reduce_db / 20)
    gain = np.clip(1.0 - (1.5 * noise / np.maximum(mag, 1e-9)) ** 2, floor, 1.0).astype(np.float32)   # Wiener-style soft gain
    k_t = np.ones(3, dtype=np.float32) / 3
    gain = np.apply_along_axis(lambda g: np.convolve(g, k_t, mode="same"), 0, gain)
    out = np.fft.irfft(spec * gain, n=n_fft, axis=1) * win
    y = np.zeros(len(pad), dtype=np.float32)
    norm = np.zeros(len(pad), dtype=np.float32)
    for j, i in enumerate(idx):
        y[i:i + n_fft] += out[j]
        norm[i:i + n_fft] += win ** 2
    y = y / np.maximum(norm, 1e-6)
    return y[n_fft:n_fft + len(x)].astype(np.float32)


class AudioExecutor:
    def __init__(self, root: str | Path, asr_model: str | None = None, tts_model: str | None = None, device: str = "cuda"):
        self.root = Path(root)
        self.out = self.root / "out"          # produced audio is written here and referenced as out/<name>
        self.asr_model, self.tts_model, self.device = asr_model, tts_model, device
        self._asr = self._tts = None

    # ---- engines (lazy) ----
    def _load_asr(self):
        if self._asr is None:
            if not self.asr_model:
                raise NotImplementedError("AUDIO_ASR needs asr_model (a Qwen3-ASR-*-hf directory)")
            from transformers import AutoModelForMultimodalLM, AutoProcessor
            import torch
            proc = AutoProcessor.from_pretrained(self.asr_model)
            model = AutoModelForMultimodalLM.from_pretrained(self.asr_model, dtype=torch.bfloat16).to(self.device).eval()
            self._asr = (proc, model)
        return self._asr

    def transcribe(self, x16k: np.ndarray, lang: str) -> str:
        proc, model = self._load_asr()
        import torch
        names = {"vi": "Vietnamese", "en": "English"}
        kw = {"language": names[lang]} if lang in names else {}
        inputs = proc.apply_transcription_request(audio=x16k, **kw).to(model.device, model.dtype)
        with torch.no_grad():
            ids = model.generate(**inputs, max_new_tokens=300, do_sample=False)
        return proc.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="transcription_only")[0].strip()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if not self.tts_model:
            raise NotImplementedError("AUDIO_TTS needs tts_model (facebook/mms-tts-vie directory)")
        import torch
        from transformers import AutoTokenizer, VitsModel
        if self._tts is None:
            self._tts = (AutoTokenizer.from_pretrained(self.tts_model), VitsModel.from_pretrained(self.tts_model).to(self.device).eval())
        tok, model = self._tts
        with torch.no_grad():
            wav = model(**tok(text.lower(), return_tensors="pt").to(self.device)).waveform[0].float().cpu().numpy()
        return wav, model.config.sampling_rate

    # ---- Executor interface ----
    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        t = task.type
        inp = parse_input(task)
        if t == TaskType.AUDIO_VAD:
            x, sr = load(self.root, inp.audio_ref)
            out = {"segments": [(round(a, 3), round(b, 3)) for a, b in vad(x, sr)]}
        elif t == TaskType.AUDIO_ASR:
            if inp.lang not in ("vi", "en"):
                raise RefusedInput(f"language {inp.lang!r} not enabled")
            x, _ = load(self.root, inp.audio_ref, 16000)
            segs = [{"start": a, "end": b} for a, b in vad(x, 16000)]
            out = {"text": self.transcribe(x, inp.lang) if segs else "", "segments": segs}
        elif t == TaskType.AUDIO_TTS:
            if inp.consent_ref is not None or inp.voice != "default":
                raise RefusedInput("voice cloning / custom voices are not offered by this engine")
            if len(inp.text) > 2000:
                raise RefusedInput("text too long")
            wav, sr = self.synthesize(inp.text)
            out = {"audio_ref": "out/" + save(self.out, wav, sr, "tts"),
                   "duration_s": round(len(wav) / sr, 3)}
        elif t == TaskType.AUDIO_ENHANCE:
            x, sr = load(self.root, inp.audio_ref)
            y = spectral_gate(x, sr)
            out = {"audio_ref": "out/" + save(self.out, y, sr, "enh")}
        else:
            raise NotImplementedError(t.value)
        return TaskResult(task_id=task.task_id, type=t, output=out, worker_id="audio")
