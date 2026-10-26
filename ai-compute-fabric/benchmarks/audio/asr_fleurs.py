"""Measures Qwen3-ASR-0.6B on real Vietnamese speech (FLEURS vi_vn dev, CC-BY-4.0) through the AUDIO_ASR executor, and
what the spectral-gate ENHANCE step does to WER on the same clips with white noise added at a fixed SNR.
usage: asr_fleurs.py FLEURS_DIR ASR_MODEL_DIR OUT.json [N]"""
import csv
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents.tasks import Task, TaskType
from audio.evaluation.vn_normalizer import wer, cer
from audio.executors import AudioExecutor, load, save, spectral_gate

fleurs, model, out_json = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
n = int(sys.argv[4]) if len(sys.argv) > 4 else 100
rows = list(csv.reader(open(fleurs / "dev.tsv", encoding="utf-8"), delimiter="\t"))
rows = rows[:: max(1, len(rows) // n)][:n]
root = Path(tempfile.mkdtemp())
(root / "a").mkdir()
for r in rows:
    # FLEURS wavs are 32-bit float; the executor accepts PCM16 only, so convert (this is data preparation, not part of the engine)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(fleurs / "dev" / "dev" / r[1]), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(root / "a" / r[1])], check=True)
ex = AudioExecutor(root, asr_model=model)
rng = np.random.default_rng(0)


def asr(ref):
    t = Task(task_id="t", tenant_id="x", type=TaskType.AUDIO_ASR, input={"audio_ref": ref, "lang": "vi"})
    return ex.execute(t).output["text"]


def snr_noise(x, snr_db):
    p = np.mean(x ** 2)
    return (x + rng.normal(0, np.sqrt(p / 10 ** (snr_db / 10)), len(x))).astype(np.float32)


acc = {"clean": [], "noisy": [], "enhanced": []}
secs = 0.0
t0 = time.time()
for i, r in enumerate(rows):
    ref = r[3]
    x, sr = load(root, "a/" + r[1], 16000)
    secs += len(x) / sr
    y = snr_noise(x, 10)
    z = spectral_gate(y, sr)
    save(root / "a", y, sr, f"n{i}"); save(root / "a", z, sr, f"z{i}")
    names = {k: sorted((root / "a").glob(f"{p}{i}-*.wav"))[0].name for k, p in (("noisy", "n"), ("enhanced", "z"))}
    acc["clean"].append((ref, asr("a/" + r[1])))
    acc["noisy"].append((ref, asr("a/" + names["noisy"])))
    acc["enhanced"].append((ref, asr("a/" + names["enhanced"])))
    if i % 10 == 0:
        print(i, flush=True)
rep = {"dataset": "google/fleurs vi_vn dev (CC-BY-4.0), every k-th clip", "clips": len(rows), "audio_seconds": round(secs, 1), "model": "Qwen3-ASR-0.6B (bf16, RTX 3060)",
       "wall_s_for_3_passes": round(time.time() - t0, 1), "noise": "white Gaussian at 10 dB SNR added by this script", "enhance": "numpy spectral gate (not DeepFilterNet)"}
for k, v in acc.items():
    rep[k] = {"wer": round(float(np.mean([wer(a, b) for a, b in v])), 4), "cer": round(float(np.mean([cer(a, b) for a, b in v])), 4),
              "wer_corpus": round(sum(wer(a, b) * len(a.split()) for a, b in v) / sum(len(a.split()) for a, _ in v), 4)}
rep["examples"] = [{"ref": a, "hyp": b} for a, b in acc["clean"][:5]]
rep["caveat"] = "FLEURS is read speech of Wikipedia-style sentences, one language, 16 kHz; not call-centre or meeting audio. Reference text is FLEURS' normalised transcription; our normaliser turns digits into words on both sides."
out_json.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
print(json.dumps({k: rep[k] for k in ("clean", "noisy", "enhanced")}))
