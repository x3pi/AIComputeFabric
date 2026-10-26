"""B09-style experiment on ONE GPU: LoRA-FA fine-tuning of Qwen3-0.6B on Vietnamese sentences (FLEURS train), perplexity on
disjoint FLEURS test sentences. W=4 logical workers run one after another on the same card, so this measures
OPTIMISATION quality at equal compute and equal data, and counts network bytes analytically; it measures no real network
time and no multi-machine behaviour.
  ddp      : one big batch (W*b) every step  == data-parallel with gradient averaging every step
  diloco   : each worker runs H local AdamW steps on its own shard, Coordinator averages deltas + Nesterov outer step
  single   : one worker alone, batch b, its own shard only (what one machine would do)
usage: diloco_vs_ddp.py BASE_DIR TRAIN.txt TEST.txt OUT.json"""
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from training.coordinator import Coordinator, Update, comm_bytes_per_worker
from training.lora_trainer import LoraWorker

base, train_f, test_f, out = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
train = [l for l in open(train_f, encoding="utf-8").read().split("\n") if l.strip()]
test = [l for l in open(test_f, encoding="utf-8").read().split("\n") if l.strip()][:150]
W, B, T, EVAL_AT = 4, 4, 200, (100, 200)
shards = [train[i::W] for i in range(W)]
w = LoraWorker(base, train, rank=8, seq=96, batch=B, lr=2e-3, seed=0)
init = w.get_params()
# tokenise the shards with the same settings the worker used
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained(base)
tokenise = lambda texts: [x for x in (tok(t, return_tensors="pt", truncation=True, max_length=96).input_ids[0] for t in texts) if len(x) >= 4]
shard_data = [tokenise(s) for s in shards]
all_data = tokenise(train)
n_params = sum(v.size for v in init.values())


def ppl():
    return w.evaluate(test, base)["perplexity"]


def reset():
    w.set_params(init)


def cond_ddp(seed):
    reset(); w.data, w.batch = all_data, W * B
    import torch
    opt = torch.optim.AdamW(list(w.trainable.values()), lr=w.lr, weight_decay=0.0)
    rng = np.random.default_rng(seed)
    w.model.train()
    curve = {0: ppl()}
    for t in range(1, T + 1):
        x, y = w._batch(rng)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            loss = w.model(input_ids=x, labels=y).loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(list(w.trainable.values()), 1.0)
        opt.step(); opt.zero_grad(set_to_none=True)
        if t in EVAL_AT:
            curve[t] = ppl(); w.model.train()
    return curve


def cond_single(seed):
    reset(); w.data, w.batch = shard_data[0], B
    import torch
    opt = torch.optim.AdamW(list(w.trainable.values()), lr=w.lr, weight_decay=0.0)
    rng = np.random.default_rng(seed)
    w.model.train()
    curve = {0: ppl()}
    for t in range(1, T + 1):
        x, y = w._batch(rng)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            loss = w.model(input_ids=x, labels=y).loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(list(w.trainable.values()), 1.0)
        opt.step(); opt.zero_grad(set_to_none=True)
        if t in EVAL_AT:
            curve[t] = ppl(); w.model.train()
    return curve


def cond_diloco(h, seed, outer_lr=0.7, momentum=0.9):
    co = Coordinator({k: v.copy() for k, v in init.items()}, outer_lr=outer_lr, momentum=momentum, min_workers=W)
    w.batch = B
    curve = {0: ppl()}
    for rnd in range(T // h):
        ver, params = co.pull()
        for k in range(W):
            w.set_params(params); w.data = shard_data[k]
            w.local_steps(h, seed=seed * 1000003 + rnd * 17 + k)
            after = w.get_params()
            co.submit(Update(f"w{k}", ver, h, {n: after[n] - params[n] for n in params}))
        assert co.outer_step()
        if (rnd + 1) * h in EVAL_AT:
            w.set_params(co.params); curve[(rnd + 1) * h] = ppl()
    w.set_params(co.params)
    return curve


results = {}
t0 = time.time()
conds = {"ddp": cond_ddp, "single_worker": cond_single, "diloco_H10": lambda s: cond_diloco(10, s), "diloco_H50": lambda s: cond_diloco(50, s)}
for seed in (1, 2):
    for name, fn in conds.items():
        t1 = time.time()
        curve = fn(seed)
        results.setdefault(name, []).append({"seed": seed, "ppl_at_step": curve, "seconds": round(time.time() - t1, 1)})
        print(name, seed, {k: round(v, 2) for k, v in curve.items()}, flush=True)
summary = {}
for name, runs in results.items():
    final = [r["ppl_at_step"][T] for r in runs]
    summary[name] = {"final_ppl_mean": round(float(np.mean(final)), 3), "final_ppl_each_seed": [round(f, 3) for f in final], "ppl_start": round(runs[0]["ppl_at_step"][0], 3)}
summary["comm_bytes_per_worker_fp32"] = {"ddp": comm_bytes_per_worker(n_params, T, W, None), "diloco_H10": comm_bytes_per_worker(n_params, T, W, 10),
                                         "diloco_H50": comm_bytes_per_worker(n_params, T, W, 50), "trainable_params": n_params}
rep = {"model": "Qwen3-0.6B LoRA-FA r=8 (only B trained, A frozen identically)", "data": "FLEURS vi_vn train sentences (1482) / test sentences (150 of 347), disjoint", "workers": W,
       "per_worker_batch": B, "inner_steps_per_worker": T, "ddp_equivalent": f"batch {W*B} per step with a persistent AdamW", "outer": "Nesterov lr 0.7 momentum 0.9",
       "device": "one RTX 3060, workers run sequentially", "seeds": 2, "summary": summary, "runs": results, "wall_s": round(time.time() - t0, 1),
       "caveats": ["2 seeds only; differences smaller than the seed spread are not conclusions", "no real network: bytes are analytic (ring all-reduce, fp32, no compression)",
                   "200 steps of a 0.6B model on 1482 sentences: says nothing about convergence of large runs or other tasks", "inner AdamW restarts every round in DiLoCo (as designed) and persists in DDP"]}
out.write_text(json.dumps(rep, indent=1))
print(json.dumps(summary, indent=1))
