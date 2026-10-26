"""LoRA worker process. Runs in the training virtualenv (torch + peft + transformers), started by studio.jobs.Runner:

    python -m studio.lora_run <job_dir>

Reads <job_dir>/job.json, train.jsonl, val.jsonl ({"question","answer"} per line); appends progress to progress.jsonl;
writes the adapter to adapter/ (safetensors, no pickle) with a sha256 manifest, and result.json last. A failure writes
result.json {"error": ...} and exits non-zero. Loss is taken over the whole formatted conversation (prompt tokens too),
the same as training/lora_trainer.py was measured with."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

CHUNK = 10


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def format_example(tok, q: str, a: str) -> str:
    msgs = [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    try:
        return tok.apply_chat_template(msgs, tokenize=False, enable_thinking=False)
    except Exception:
        return f"{q}\n{a}"


def read_pairs(p: Path) -> list:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def progress(d: Path, **kw):
    with open(d / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 1), **kw}) + "\n")


def main(d: Path):
    from transformers import AutoTokenizer

    from training.lora_trainer import LoraWorker, sha256_of
    job = json.loads((d / "job.json").read_text())
    base = job["base_dir"]
    tok = AutoTokenizer.from_pretrained(base)
    train = [format_example(tok, r["question"], r["answer"]) for r in read_pairs(d / "train.jsonl")]
    val = [format_example(tok, r["question"], r["answer"]) for r in read_pairs(d / "val.jsonl")]
    progress(d, phase="loading", train=len(train), val=len(val))
    w = LoraWorker(base, train, rank=job["rank"], seq=job["seq"], batch=job["batch"], lr=job["lr"], lora_fa=job["lora_fa"], seed=job.get("seed", 0), persistent_opt=True)
    before = w.evaluate(val, base) if val else None
    progress(d, phase="training", step=0, val_perplexity=before and before["perplexity"])
    steps, done, t0, losses = job["steps"], 0, time.time(), []
    while done < steps:
        h = min(CHUNK, steps - done)
        ls = w.local_steps(h, seed=1000 + done)
        done += h
        losses += ls
        progress(d, phase="training", step=done, steps=steps, loss=sum(ls) / len(ls), elapsed_s=round(time.time() - t0, 1))
    after = w.evaluate(val, base) if val else None
    out = d / "adapter"
    w.model.save_pretrained(out, safe_serialization=True)
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "manifest.json")
    manifest = {"base_model": Path(base).name, "base_weights_sha256": None, "files": {str(p.relative_to(out)): sha256_file(p) for p in files}}
    wf = next((p for p in sorted(Path(base).glob("*.safetensors"))), None)
    if wf:
        manifest["base_weights_sha256"] = sha256_of(wf)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    result = {"steps": steps, "train_examples": len(train), "val_examples": len(val), "seconds": round(time.time() - t0, 1),
              "first_loss": sum(losses[:CHUNK]) / len(losses[:CHUNK]), "last_loss": sum(losses[-CHUNK:]) / len(losses[-CHUNK:]),
              "val_perplexity_before": before and before["perplexity"], "val_perplexity_after": after and after["perplexity"],
              "adapter_files": len(files), "adapter_bytes": sum(p.stat().st_size for p in files)}
    (d / "result.json").write_text(json.dumps(result))
    progress(d, phase="done")


if __name__ == "__main__":
    jd = Path(sys.argv[1])
    try:
        main(jd)
    except BaseException as e:
        traceback.print_exc()
        (jd / "result.json").write_text(json.dumps({"error": f"{type(e).__name__}: {e}"[:500]}))
        sys.exit(1)
