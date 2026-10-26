"""Persistent subprocess worker for GPU neural audio models (Qwen3-ASR-0.6B and facebook/mms-tts-vie).

Runs under `train_python` (which has torch + transformers installed) and communicates over stdin/stdout JSON lines so
models are loaded lazily once and kept warm across HTTP requests.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

from agents.tasks import Task, TaskType
from audio.executors import AudioExecutor


def main() -> None:
    asr_model = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    tts_model = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    device = "cuda"
    try:
        import torch
        if not torch.cuda.is_available():
            device = "cpu"
    except Exception:
        device = "cpu"

    with tempfile.TemporaryDirectory(prefix="aicf-audio-") as tmp:
        root = Path(tmp)
        ex = AudioExecutor(root, asr_model=asr_model, tts_model=tts_model, device=device)
        sys.stdout.write(json.dumps({"ready": True, "device": device}) + "\n")
        sys.stdout.flush()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                op = req.get("op")
                if op == "tts":
                    text = req["text"]
                    t = Task(task_id="tts", tenant_id="studio", type=TaskType.AUDIO_TTS, input={
                        "text": text,
                        "voice": req.get("voice", "default"),
                        "consent_ref": req.get("consent_ref"),
                    })
                    res = ex.execute(t)
                    wav_path = root / res.output["audio_ref"]
                    b64 = base64.b64encode(wav_path.read_bytes()).decode("ascii")
                    wav_path.unlink(missing_ok=True)
                    resp = {
                        "ok": True,
                        "audio_base64": b64,
                        "duration_s": res.output["duration_s"],
                        "device": device,
                    }
                elif op == "asr":
                    raw = base64.b64decode(req["audio_base64"])
                    in_name = "input.wav"
                    (root / in_name).write_bytes(raw)
                    t = Task(task_id="asr", tenant_id="studio", type=TaskType.AUDIO_ASR, input={
                        "audio_ref": in_name,
                        "lang": req.get("lang", "vi"),
                    })
                    res = ex.execute(t)
                    (root / in_name).unlink(missing_ok=True)
                    resp = {
                        "ok": True,
                        "text": res.output["text"],
                        "segments": res.output["segments"],
                        "device": device,
                    }
                else:
                    resp = {"ok": False, "error": f"unknown op {op!r}"}
            except Exception as e:
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
