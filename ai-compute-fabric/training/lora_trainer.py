"""LoRA trainer pieces (plan P6.3): a worker that runs local steps on a causal LM and exposes its adapter as numpy
arrays, so training/coordinator.py can average workers DiLoCo-style; and the TRAIN_STEP / MODEL_EVALUATE executors.

LoRA-FA mode (default): every A matrix is frozen at the same seeded initial value on all workers and only B is trained,
which makes averaging the B deltas exact (the product B*A stays linear in B). Heavy imports (torch, peft) are lazy.

Measured on: Qwen3-0.6B, one RTX 3060, a few hundred steps (benchmarks/reports/p6_diloco_vs_ddp.json). Not covered:
multi-machine transport, models above ~2B on this card, mixed-precision base weights other than bf16, resuming the
optimizer state across processes (AdamW moments restart at each pull, as in DiLoCo's inner loop)."""
from __future__ import annotations

import hashlib
import json
import os
import zlib
from pathlib import Path

import numpy as np

from agents.doc_executors import RefusedInput, resolve
from agents.tasks import Task, TaskResult, TaskType, parse_input


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


class LoraWorker:
    def __init__(self, base_dir: str | Path, texts: list[str], rank: int = 8, seq: int = 128, batch: int = 4, lr: float = 2e-3,
                 lora_fa: bool = True, seed: int = 0, device: str = "cuda"):
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch, self.device, self.seq, self.batch, self.lr = torch, device, seq, batch, lr
        tok = AutoTokenizer.from_pretrained(base_dir)
        ids = [tok(t, return_tensors="pt", truncation=True, max_length=seq).input_ids[0] for t in texts]
        self.pad = tok.pad_token_id if tok.pad_token_id is not None else 0
        self.data = [x for x in ids if len(x) >= 4]
        torch.manual_seed(seed)                                   # same seed everywhere => identical A (and B = 0)
        model = AutoModelForCausalLM.from_pretrained(base_dir, dtype=torch.bfloat16).to(device)
        cfg = LoraConfig(r=rank, lora_alpha=2 * rank, target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"], task_type="CAUSAL_LM")
        self.model = get_peft_model(model, cfg)
        for n, p in self.model.named_parameters():
            if "lora_A" in n:
                p.requires_grad_(not lora_fa)
        self.trainable = {n: p for n, p in self.model.named_parameters() if p.requires_grad}
        self.rng = np.random.default_rng(seed)

    # ---- parameters as numpy ----
    def get_params(self) -> dict:
        return {n: p.detach().float().cpu().numpy().copy() for n, p in self.trainable.items()}

    def set_params(self, params: dict):
        with self.torch.no_grad():
            for n, p in self.trainable.items():
                p.copy_(self.torch.from_numpy(params[n]).to(p.device, p.dtype))

    def _batch(self, rng):
        idx = rng.integers(0, len(self.data), self.batch)
        L = max(len(self.data[i]) for i in idx)
        x = self.torch.full((self.batch, L), self.pad, dtype=self.torch.long)
        y = self.torch.full((self.batch, L), -100, dtype=self.torch.long)
        for j, i in enumerate(idx):
            x[j, :len(self.data[i])] = self.data[i]
            y[j, :len(self.data[i])] = self.data[i]
        return x.to(self.device), y.to(self.device)

    def local_steps(self, h: int, seed: int) -> list[float]:
        """h AdamW steps from the current parameters on batches drawn with `seed`; returns the losses."""
        torch = self.torch
        self.model.train()
        opt = torch.optim.AdamW(list(self.trainable.values()), lr=self.lr, weight_decay=0.0)
        rng = np.random.default_rng(seed)
        losses = []
        for _ in range(h):
            x, y = self._batch(rng)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                loss = self.model(input_ids=x, labels=y).loss
            loss.backward()
            torch.nn.utils.clip_grad_norm_(list(self.trainable.values()), 1.0)
            opt.step()
            opt.zero_grad(set_to_none=True)
            losses.append(float(loss.detach()))
        return losses

    @staticmethod
    def _nll(model, x, y):
        out = model(input_ids=x).logits[:, :-1].float()
        tgt = y[:, 1:]
        import torch
        nll = torch.nn.functional.cross_entropy(out.reshape(-1, out.shape[-1]), tgt.reshape(-1), ignore_index=-100, reduction="sum")
        return float(nll), int((tgt != -100).sum())

    def evaluate(self, texts: list[str], tok_dir: str | Path) -> dict:
        """Token-level NLL and perplexity of the current adapter on `texts`."""
        torch = self.torch
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(tok_dir)
        self.model.eval()
        tot, n = 0.0, 0
        with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
            for t in texts:
                ids = tok(t, return_tensors="pt", truncation=True, max_length=self.seq).input_ids.to(self.device)
                if ids.shape[1] < 2:
                    continue
                a, b = self._nll(self.model, ids, ids)
                tot, n = tot + a, n + b
        return {"nll": tot / n, "perplexity": float(np.exp(tot / n)), "tokens": n}


class TrainExecutor:
    """TRAIN_STEP. One call = `steps_per_call` local steps continuing from run `run_id`'s latest saved adapter.
    The base model is identified by the sha256 of its weights file; a mismatch is refused, so a worker holding the wrong
    checkpoint cannot contribute. Results are cached per (run_id, step): at-least-once delivery re-sends the same answer."""
    def __init__(self, root: str | Path, base_dir: str | Path, weights_file: str, data_ref: str, steps_per_call: int = 1, **worker_kw):
        self.root, self.base_dir = Path(root), Path(base_dir)
        self.weights_sha: str | None = None
        self.weights_file = weights_file
        self.data_ref, self.k, self.kw = data_ref, steps_per_call, worker_kw
        self.worker: LoraWorker | None = None

    def _sha(self) -> str:
        if self.weights_sha is None:
            self.weights_sha = sha256_of(self.base_dir / self.weights_file)
        return self.weights_sha

    def _worker(self) -> LoraWorker:
        if self.worker is None:
            lines = [l for l in resolve(self.root, self.data_ref).read_text(encoding="utf-8").splitlines() if l.strip()]
            self.worker = LoraWorker(self.base_dir, lines, **self.kw)
        return self.worker

    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        if task.type != TaskType.TRAIN_STEP:
            raise NotImplementedError(task.type.value)
        inp = parse_input(task)
        if not all(c.isalnum() or c in "-_" for c in inp.run_id) or not inp.run_id:
            raise RefusedInput("run_id must be alphanumeric, '-' or '_'")
        if inp.base_model_sha256 != self._sha():
            raise RefusedInput("base_model_sha256 does not match the checkpoint on this worker")
        run = self.root / "runs" / inp.run_id
        run.mkdir(parents=True, exist_ok=True)
        cur, meta = run / f"step-{inp.step}.npz", run / f"step-{inp.step}.json"
        if meta.exists() and cur.exists():                                  # already done: same answer again
            m = json.loads(meta.read_text())
            return TaskResult(task_id=task.task_id, type=task.type, output={"run_id": inp.run_id, "step": inp.step, "loss": m["loss"], "update_ref": f"runs/{inp.run_id}/{cur.name}"}, worker_id="trainer")
        prev = run / f"step-{inp.step - 1}.npz"
        if inp.step > 0 and not prev.exists():
            raise RefusedInput(f"step {inp.step - 1} of run {inp.run_id} not found: steps must be consecutive")
        w = self._worker()
        if inp.step > 0:
            with np.load(prev) as z:
                w.set_params({k: z[k] for k in z.files})
        losses = w.local_steps(self.k, seed=zlib.crc32(f"{inp.run_id}/{inp.step}".encode()))
        tmp = run / f".tmp-{os.getpid()}.npz"
        np.savez(tmp, **w.get_params())
        os.replace(tmp, cur)
        meta.write_text(json.dumps({"loss": float(np.mean(losses)), "sha256": sha256_of(cur)}))
        return TaskResult(task_id=task.task_id, type=task.type, output={"run_id": inp.run_id, "step": inp.step, "loss": float(np.mean(losses)), "update_ref": f"runs/{inp.run_id}/{cur.name}"}, worker_id="trainer")


class EvaluateExecutor:
    """MODEL_EVALUATE over RECORDED predictions: dataset_ref is a JSON-lines file (relative to root) of
    {"ref","hyp"} (wer/cer) or {"label","pred"} (accuracy); model_ref is carried into the report only. perplexity needs a
    model in memory and is raised as NotImplementedError here (use LoraWorker.evaluate)."""
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        if task.type != TaskType.MODEL_EVALUATE:
            raise NotImplementedError(task.type.value)
        inp = parse_input(task)
        if inp.metric == "perplexity":
            raise NotImplementedError("perplexity needs a loaded model; not available through recorded predictions")
        rows = []
        for n, line in enumerate(resolve(self.root, inp.dataset_ref).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise RefusedInput(f"dataset line {n} is not JSON") from None
        if not rows:
            raise RefusedInput("empty dataset")
        if inp.metric in ("wer", "cer"):
            from audio.evaluation.vn_normalizer import cer, wer
            f = wer if inp.metric == "wer" else cer
            try:
                vals = [(f(r["ref"], r["hyp"]), len((r["ref"]).split() if inp.metric == "wer" else r["ref"].replace(" ", ""))) for r in rows]
            except (KeyError, TypeError, AttributeError):
                raise RefusedInput("rows need string 'ref' and 'hyp'") from None
            den = sum(w for _, w in vals) or 1
            value = sum(v * w for v, w in vals) / den          # corpus-level: weighted by reference length
        else:
            try:
                value = sum(r["label"] == r["pred"] for r in rows) / len(rows)
            except KeyError:
                raise RefusedInput("rows need 'label' and 'pred'") from None
        return TaskResult(task_id=task.task_id, type=task.type, output={"metric": inp.metric, "value": round(float(value), 6)}, worker_id="evaluator")
