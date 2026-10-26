"""Audio AI Factory HTTP endpoints (plan P6.1-P6.2) mounted under /v1/audio/*.

Exposes:
  - GET  /v1/audio/status       Engine readiness + real GPU benchmark reports (FLEURS-vi & TTS->ASR roundtrip)
  - POST /v1/audio/evaluate     Vietnamese text normalizer + WER / CER calculator
  - POST /v1/audio/sample-wav   Generates a synthetic 16 kHz PCM16 speech+noise test clip for instant UI testing
  - POST /v1/audio/analyze      Runs level-invariant Energy VAD + Wiener Spectral Gate enhancement on WAV audio
  - POST /v1/audio/tts          Synthesizes Vietnamese speech via facebook/mms-tts-vie (refuses voice cloning)
  - POST /v1/audio/asr          Transcribes Vietnamese/English WAV via Qwen3-ASR-0.6B + VAD segments
  - POST /v1/audio/roundtrip    End-to-end TTS -> ASR -> Vietnamese WER/CER verification
"""
from __future__ import annotations

import base64
import dataclasses
import io
import json
import math
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from audio.evaluation.vn_normalizer import cer, normalize, wer
from audio.executors import MAX_SECONDS, spectral_gate, vad
from audio.ingestion.dataset import IngestError, read_wav
from audio.ingestion.split import speaker_disjoint_split
from studio.config import Settings
from studio.errors import bad, unavailable
from studio.web import Request, Router
from training.coordinator import Coordinator, Rejected, Update, comm_bytes_per_worker
from training.lora_merger import exact_delta_w, lora_fa_merge, naive_merge, svd_refactor

ROOT = Path(__file__).resolve().parents[1]


def _wav_bytes_from_float(x: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def _read_wav_b64(b64: str) -> tuple[np.ndarray, int]:
    if not isinstance(b64, str) or not b64.strip():
        raise bad("'audio_base64' is required", "bad_audio")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise bad("audio_base64 is not valid base64", "bad_audio") from None
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        f.write(raw)
        f.flush()
        try:
            x, sr = read_wav(Path(f.name), max_seconds=MAX_SECONDS)
        except (IngestError, wave.Error, EOFError) as e:
            raise bad(f"unreadable WAV (must be 16-bit PCM .wav): {e}", "bad_audio") from None
    if not np.all(np.isfinite(x)):
        raise bad("audio contains non-finite samples", "bad_audio")
    return x, sr


def _rms_db(x: np.ndarray) -> float:
    if len(x) == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return round(20.0 * math.log10(max(rms, 1e-5)), 2)


class AudioService:
    def __init__(self, settings: Settings):
        self.s = settings
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
                self._proc = None

    def _asr_ready(self) -> bool:
        return bool(self.s.asr_model and Path(self.s.asr_model).is_dir() and self.s.train_python)

    def _tts_ready(self) -> bool:
        return bool(self.s.tts_model and Path(self.s.tts_model).is_dir() and self.s.train_python)

    def _call_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.s.train_python:
            raise unavailable("Chưa cấu hình môi trường thực thi Audio GPU (--train-python)")
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/local/bin:/usr/bin:/bin"}
                self._proc = subprocess.Popen(
                    [self.s.train_python, "-m", "studio.audio_worker", self.s.asr_model or "", self.s.tts_model or ""],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(ROOT),
                    env=env,
                )
                ready_line = self._proc.stdout.readline() if self._proc.stdout else ""
                if not ready_line:
                    err = self._proc.stderr.read() if self._proc.stderr else ""
                    self._proc = None
                    raise unavailable(f"Không khởi động được Audio Worker: {err[-300:]}")
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            out_line = self._proc.stdout.readline()
            if not out_line:
                self._proc = None
                raise unavailable("Audio Worker ngắt kết nối bất ngờ")
            resp = json.loads(out_line)
            if not resp.get("ok"):
                err_msg = str(resp.get("error", "audio error"))
                if "RefusedInput" in err_msg:
                    raise bad(err_msg.split(": ", 1)[-1], "refused_input")
                raise unavailable(err_msg)
            return resp

    def mount(self, r: Router) -> None:
        r.add("GET", "/v1/audio/status", self.status)
        r.add("POST", "/v1/audio/evaluate", self.evaluate)
        r.add("POST", "/v1/audio/sample-wav", self.sample_wav)
        r.add("POST", "/v1/audio/analyze", self.analyze)
        r.add("POST", "/v1/audio/tts", self.tts)
        r.add("POST", "/v1/audio/asr", self.asr)
        r.add("POST", "/v1/audio/roundtrip", self.roundtrip)
        r.add("POST", "/v1/audio/dataset/split", self.dataset_split)
        r.add("POST", "/v1/audio/training/simulate-diloco", self.simulate_diloco)

    def status(self, req: Request) -> dict[str, Any]:
        fleurs_report = None
        rt_report = None
        diloco_report = None
        p_fleurs = ROOT / "benchmarks" / "reports" / "p6_asr_fleurs_vi_qwen3asr_0p6b.json"
        p_rt = ROOT / "benchmarks" / "reports" / "p6_tts_asr_roundtrip.json"
        p_diloco = ROOT / "benchmarks" / "reports" / "p6_diloco_vs_ddp.json"
        if p_fleurs.is_file():
            try:
                fleurs_report = json.loads(p_fleurs.read_text(encoding="utf-8"))
            except Exception:
                pass
        if p_rt.is_file():
            try:
                rt_report = json.loads(p_rt.read_text(encoding="utf-8"))
            except Exception:
                pass
        if p_diloco.is_file():
            try:
                raw_d = json.loads(p_diloco.read_text(encoding="utf-8"))
                diloco_report = {
                    "model": raw_d.get("model"),
                    "data": raw_d.get("data"),
                    "workers": raw_d.get("workers"),
                    "inner_steps_per_worker": raw_d.get("inner_steps_per_worker"),
                    "summary": raw_d.get("summary"),
                    "caveats": raw_d.get("caveats"),
                }
            except Exception:
                pass
        return {
            "ok": True,
            "asr_ready": self._asr_ready(),
            "tts_ready": self._tts_ready(),
            "asr_model": Path(self.s.asr_model).name if self.s.asr_model else "Qwen3-ASR-0.6B (chưa gắn đường dẫn)",
            "tts_model": Path(self.s.tts_model).name if self.s.tts_model else "facebook/mms-tts-vie (chưa gắn đường dẫn)",
            "vad_engine": "Energy VAD tương đối (-30 dB so với đỉnh, chống bỏ sót âm lượng nhỏ)",
            "enhance_engine": "Wiener Spectral Gate (512-FFT, giảm nhiễu nền tĩnh 12 dB)",
            "normalizer": "Chuẩn hóa Tiếng Việt (Số -> Chữ, Ngày tháng, Dấu thanh hoà->hòa) + WER/CER",
            "voice_cloning_policy": "Từ chối giả mạo giọng nói (Zero Voice-Cloning) & bắt buộc kiểm tra Consent/SHA-256",
            "benchmarks": {
                "fleurs_vi": fleurs_report,
                "roundtrip": rt_report,
                "diloco_vs_ddp": diloco_report,
            },
        }

    def evaluate(self, req: Request) -> dict[str, Any]:
        b = req.body
        pairs = b.get("pairs")
        if isinstance(pairs, list) and pairs:
            rows: list[dict[str, Any]] = []
            for item in pairs[:100]:
                if not isinstance(item, dict):
                    continue
                r_txt = str(item.get("ref", "")).strip()
                h_txt = str(item.get("hyp", "")).strip()
                if not r_txt and not h_txt:
                    continue
                rows.append({
                    "ref": r_txt,
                    "hyp": h_txt,
                    "ref_norm": normalize(r_txt),
                    "hyp_norm": normalize(h_txt),
                    "wer": round(wer(r_txt, h_txt), 4),
                    "cer": round(cer(r_txt, h_txt), 4),
                })
            if not rows:
                raise bad("Danh sách cặp câu trống", "empty_pairs")
            return {
                "rows": rows,
                "mean_wer": round(sum(float(r["wer"]) for r in rows) / len(rows), 4),
                "mean_cer": round(sum(float(r["cer"]) for r in rows) / len(rows), 4),
            }
        ref_in = b.get("reference")
        hyp_in = b.get("hypothesis")
        if not isinstance(ref_in, str) or not isinstance(hyp_in, str) or not ref_in.strip():
            raise bad("Cần truyền 'reference' (câu chuẩn) và 'hypothesis' (câu nhận dạng)", "bad_input")
        return {
            "reference": ref_in,
            "hypothesis": hyp_in,
            "reference_normalized": normalize(ref_in),
            "hypothesis_normalized": normalize(hyp_in),
            "wer": round(wer(ref_in, hyp_in), 4),
            "cer": round(cer(ref_in, hyp_in), 4),
        }

    def sample_wav(self, req: Request) -> dict[str, Any]:
        req.need("operator")
        sr = 16000
        noisy = bool(req.body.get("noisy", True))
        # Build a 3.2-second synthetic speech-like signal with 2 distinct voiced bursts and silence gaps
        t = np.linspace(0, 3.2, int(sr * 3.2), endpoint=False, dtype=np.float32)
        sig = np.zeros_like(t)
        for a, b, f0 in ((0.35, 1.45, 145.0), (1.85, 2.85, 165.0)):
            mask = (t >= a) & (t <= b)
            tm = t[mask]
            env = np.sqrt(np.maximum(0.0, np.sin(np.pi * (tm - a) / (b - a))))
            # Harmonic series simulating voiced speech formants
            burst = (
                0.55 * np.sin(2 * np.pi * f0 * tm)
                + 0.30 * np.sin(2 * np.pi * (2 * f0) * tm)
                + 0.15 * np.sin(2 * np.pi * (3 * f0) * tm)
                + 0.08 * np.sin(2 * np.pi * 620.0 * tm)
            )
            sig[mask] = (0.78 * env * burst).astype(np.float32)
        if noisy:
            rng = np.random.default_rng(42)
            sig = np.clip(sig + rng.normal(0.0, 0.006, size=sig.shape).astype(np.float32), -0.98, 0.98)
        wav_bytes = _wav_bytes_from_float(sig, sr)
        return {
            "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
            "duration_s": 3.2,
            "sample_rate": sr,
            "description": "Mẫu tín hiệu 16 kHz PCM16 gồm 2 phân đoạn tiếng nói (0.35s–1.45s và 1.85s–2.85s) + nhiễu nền trắng dừng (stationary noise).",
        }

    def analyze(self, req: Request) -> dict[str, Any]:
        req.need("operator")
        x, sr = _read_wav_b64(req.body.get("audio_base64", ""))
        do_enhance = bool(req.body.get("enhance", True))
        segs = [(round(a, 3), round(b, 3)) for a, b in vad(x, sr)]
        dur = round(len(x) / max(sr, 1), 3)
        speech_s = round(sum(b - a for a, b in segs), 3)
        out: dict[str, Any] = {
            "duration_s": dur,
            "sample_rate": sr,
            "samples": int(len(x)),
            "rms_db": _rms_db(x),
            "speech_seconds": speech_s,
            "speech_ratio": round(speech_s / max(dur, 1e-6), 3),
            "segments": [{"start": a, "end": b} for a, b in segs],
        }
        if do_enhance:
            y = spectral_gate(x, sr)
            segs_after = [(round(a, 3), round(b, 3)) for a, b in vad(y, sr)]
            out["enhanced_rms_db"] = _rms_db(y)
            out["enhanced_segments"] = [{"start": a, "end": b} for a, b in segs_after]
            out["enhanced_audio_base64"] = base64.b64encode(_wav_bytes_from_float(y, sr)).decode("ascii")
        return out

    def tts(self, req: Request) -> dict[str, Any]:
        req.need("operator")
        text = req.body.get("text")
        voice = req.body.get("voice", "default")
        consent_ref = req.body.get("consent_ref")
        if consent_ref is not None or voice != "default":
            raise bad("Chính sách Đạo đức AI: Hệ thống từ chối nhân bản giọng nói (Voice Cloning) hoặc giọng tùy chỉnh chưa được cấp phép.", "voice_cloning_refused")
        if not isinstance(text, str) or not text.strip():
            raise bad("Vui lòng nhập văn bản tiếng Việt cần tổng hợp giọng nói", "empty_text")
        if len(text) > 2000:
            raise bad("Văn bản vượt quá giới hạn 2000 ký tự", "text_too_long")
        if not self._tts_ready():
            raise unavailable("Model TTS (facebook/mms-tts-vie) chưa được cấu hình trên máy chủ")
        resp = self._call_worker({"op": "tts", "text": text.strip(), "voice": voice, "consent_ref": consent_ref})
        return {
            "text": text.strip(),
            "audio_base64": resp["audio_base64"],
            "duration_s": resp["duration_s"],
            "engine": "facebook/mms-tts-vie",
            "device": resp.get("device", "cuda"),
            "licence_note": "CC-BY-NC 4.0 (chỉ sử dụng nghiên cứu/nội bộ, không dùng thương mại)",
        }

    def asr(self, req: Request) -> dict[str, Any]:
        req.need("operator")
        lang = req.body.get("lang", "vi")
        if lang not in ("vi", "en"):
            raise bad("Ngôn ngữ chỉ hỗ trợ 'vi' (Tiếng Việt) hoặc 'en' (Tiếng Anh)", "unsupported_lang")
        b64 = req.body.get("audio_base64", "")
        x, sr = _read_wav_b64(b64)
        if not self._asr_ready():
            raise unavailable("Model ASR (Qwen3-ASR-0.6B) chưa được cấu hình trên máy chủ")
        resp = self._call_worker({"op": "asr", "audio_base64": b64, "lang": lang})
        text = resp["text"]
        out: dict[str, Any] = {
            "text": text,
            "segments": resp["segments"],
            "duration_s": round(len(x) / max(sr, 1), 3),
            "engine": "Qwen3-ASR-0.6B",
            "device": resp.get("device", "cuda"),
        }
        ref = req.body.get("reference")
        if isinstance(ref, str) and ref.strip():
            out["eval"] = {
                "reference": ref.strip(),
                "reference_normalized": normalize(ref.strip()),
                "hypothesis_normalized": normalize(text),
                "wer": round(wer(ref.strip(), text), 4),
                "cer": round(cer(ref.strip(), text), 4),
            }
        return out

    def roundtrip(self, req: Request) -> dict[str, Any]:
        req.need("operator")
        tts_res = self.tts(req)
        asr_req = dataclasses.replace(
            req,
            path="/v1/audio/asr",
            body={
                "audio_base64": tts_res["audio_base64"],
                "lang": "vi",
                "reference": tts_res["text"],
            },
        )
        asr_res = self.asr(asr_req)
        return {
            "input_text": tts_res["text"],
            "audio_base64": tts_res["audio_base64"],
            "duration_s": tts_res["duration_s"],
            "asr_text": asr_res["text"],
            "segments": asr_res["segments"],
            "eval": asr_res.get("eval"),
            "engines": "facebook/mms-tts-vie -> Qwen3-ASR-0.6B",
        }

    def dataset_split(self, req: Request) -> dict[str, Any]:
        manifest_text = req.body.get("manifest_jsonl")
        if not isinstance(manifest_text, str) or not manifest_text.strip():
            raise bad("Vui lòng nhập nội dung manifest JSONL", "empty_manifest")
        records: list[dict[str, Any]] = []
        for idx, line in enumerate(manifest_text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                raise bad(f"Dòng {idx}: JSON không hợp lệ ({e})", "bad_manifest") from None
            if not isinstance(obj, dict):
                raise bad(f"Dòng {idx}: phải là JSON object", "bad_manifest")
            for k in ("path", "sha256", "speaker_id", "consent_ref"):
                v = obj.get(k)
                if not isinstance(v, str) or not v.strip():
                    raise bad(f"Dòng {idx}: thiếu trường bắt buộc '{k}' (Data Governance: cấm nạp âm thanh không có consent_ref hoặc sha256)", "missing_governance_field")
            rel = Path(obj["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise bad(f"Dòng {idx}: đường dẫn '{obj['path']}' không hợp lệ (path traversal)", "bad_path")
            records.append(obj)
        if not records:
            raise bad("Manifest không có bản ghi nào", "empty_manifest")

        splits = speaker_disjoint_split(records, ratios=(0.8, 0.1, 0.1), seed=int(req.body.get("seed", 0)))
        sp_train = {r["speaker_id"] for r in splits["train"]}
        sp_val = {r["speaker_id"] for r in splits["validation"]}
        sp_test = {r["speaker_id"] for r in splits["test"]}
        overlap = (sp_train & sp_val) | (sp_train & sp_test) | (sp_val & sp_test)
        return {
            "total_records": len(records),
            "total_speakers": len(sp_train | sp_val | sp_test),
            "speaker_overlap_count": len(overlap),
            "disjoint_verified": len(overlap) == 0,
            "splits": {
                "train": {"records": len(splits["train"]), "speakers": sorted(sp_train)},
                "validation": {"records": len(splits["validation"]), "speakers": sorted(sp_val)},
                "test": {"records": len(splits["test"]), "speakers": sorted(sp_test)},
            },
        }

    def simulate_diloco(self, req: Request) -> dict[str, Any]:
        workers = int(req.body.get("workers", 4))
        h_steps = int(req.body.get("inner_steps", 50))
        total_steps = int(req.body.get("total_steps", 200))
        rank = int(req.body.get("rank", 8))
        inject_stale = bool(req.body.get("inject_stale", True))
        if not (2 <= workers <= 32 and 1 <= h_steps <= 500 and 10 <= total_steps <= 5000 and 1 <= rank <= 64):
            raise bad("Tham số ngoài khoảng cho phép (workers 2..32, inner_steps 1..500, total_steps 10..5000, rank 1..64)", "bad_params")

        rng = np.random.default_rng(42)
        d_model = 64
        # Fixed frozen A across all workers (LoRA-FA), trainable B
        A_frozen = rng.standard_normal((rank, d_model)).astype(np.float32) * 0.05
        As_unfrozen = [A_frozen + rng.standard_normal((rank, d_model)).astype(np.float32) * 0.02 for _ in range(workers)]
        Bs = [rng.standard_normal((d_model, rank)).astype(np.float32) * 0.05 for _ in range(workers)]

        # 1. Compare Naive Merge vs Exact Delta-W vs LoRA-FA
        exact_unfrozen = exact_delta_w(As_unfrozen, Bs, alpha=float(2 * rank), r=rank)
        naive_unfrozen = (2.0) * naive_merge(As_unfrozen, Bs)
        naive_err = float(np.linalg.norm(exact_unfrozen - naive_unfrozen))

        exact_fa = exact_delta_w([A_frozen] * workers, Bs, alpha=float(2 * rank), r=rank)
        fa_merged = (2.0) * (lora_fa_merge(Bs) @ A_frozen)
        fa_err = float(np.linalg.norm(exact_fa - fa_merged))
        _, _, svd_err = svd_refactor(exact_unfrozen, rank)

        # 2. Run real DiLoCo Coordinator outer steps + stale worker test
        coord = Coordinator({"lora_B": np.zeros((d_model, rank), dtype=np.float32)}, outer_lr=0.7, momentum=0.9, min_workers=workers)
        rounds = max(1, min(10, total_steps // h_steps))
        for r_idx in range(rounds):
            ver, base = coord.pull()
            for w_idx in range(workers):
                delta_b = rng.standard_normal((d_model, rank)).astype(np.float32) * (0.02 / (r_idx + 1))
                coord.submit(Update(worker_id=f"worker-{w_idx + 1:02d}", base_version=ver, steps=h_steps, delta={"lora_B": delta_b}))
            coord.outer_step()

        stale_rejected = False
        if inject_stale and coord.version > 0:
            try:
                coord.submit(Update(worker_id="worker-slow-99", base_version=0, steps=h_steps, delta={"lora_B": np.zeros((d_model, rank), dtype=np.float32)}))
            except Rejected as e:
                stale_rejected = e.reason == "stale"

        n_params = 2_752_512 * rank // 8  # scaled from Qwen3-0.6B LoRA-FA r=8 trainable params
        ddp_bytes = comm_bytes_per_worker(n_params, total_steps, workers, h=None)
        diloco_bytes = comm_bytes_per_worker(n_params, total_steps, workers, h=h_steps)
        saved_pct = round(100.0 * (1.0 - diloco_bytes / max(ddp_bytes, 1)), 2)

        return {
            "workers": workers,
            "inner_steps": h_steps,
            "total_steps": total_steps,
            "rank": rank,
            "outer_versions_completed": coord.version,
            "stale_update_rejected": stale_rejected,
            "rejected_counts": coord.rejected,
            "lora_math": {
                "naive_merge_frobenius_error": round(naive_err, 6),
                "lora_fa_frobenius_error": round(fa_err, 8),
                "svd_refactor_frobenius_error": round(svd_err, 6),
            },
            "network": {
                "trainable_params": n_params,
                "ddp_bytes_per_worker": ddp_bytes,
                "diloco_bytes_per_worker": diloco_bytes,
                "ddp_mib": round(ddp_bytes / (1024 * 1024), 2),
                "diloco_mib": round(diloco_bytes / (1024 * 1024), 2),
                "bandwidth_saved_pct": saved_pct,
            },
        }

