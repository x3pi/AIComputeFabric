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
alltest = [l for l in open(test_f, encoding="utf-8").read().split("\n") if l.strip()]
val, test = alltest[:100], alltest[100:250]      # val picks the learning rate, test is reported
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
    return {"val": w.evaluate(val, base)["perplexity"], "test": w.evaluate(test, base)["perplexity"]}


def reset():
    w.set_params(init)


def cond_ddp(seed):
    reset(); w.data, w.batch = all_data, W * B
    w.lr = LR
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
    w.lr = LR
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
    reset(); w.batch = B
    w.lr = LR
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


t0 = time.time()
conds = {"ddp": cond_ddp, "single_worker": cond_single, "diloco_H10": lambda s: cond_diloco(10, s), "diloco_H50": lambda s: cond_diloco(50, s)}
LRS = (2.5e-4, 5e-4, 1e-3)
STORE = out.with_suffix(".jsonl")               # one line per finished run, so an interrupted experiment resumes instead of restarting
done = [json.loads(l) for l in STORE.read_text().splitlines()] if STORE.exists() else []


def have(cond, lr, seed):
    return any(r["cond"] == cond and r["lr"] == lr and r["seed"] == seed for r in done)


def run(cond, lr, seed):
    global LR
    if have(cond, lr, seed):
        return
    LR = lr
    t1 = time.time()
    curve = conds[cond](seed)
    rec = {"cond": cond, "lr": lr, "seed": seed, "ppl_at_step": {str(k): v for k, v in curve.items()}, "seconds": round(time.time() - t1, 1)}
    done.append(rec)
    with open(STORE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(cond, lr, seed, {k: {a: round(b, 2) for a, b in v.items()} for k, v in rec["ppl_at_step"].items()}, flush=True)


def val_at_end(cond, lr):
    v = [r["ppl_at_step"][str(T)]["val"] for r in done if r["cond"] == cond and r["lr"] == lr]
    return float(np.mean(v)) if v else None


# stage A: learning-rate sweep with seed 1 (selection on validation sentences); stage B: seed 2 at each method's chosen lr
for lr in LRS:
    for cond in conds:
        run(cond, lr, 1)
chosen = {c: min(LRS, key=lambda lr: val_at_end(c, lr)) for c in conds}
for cond in conds:
    run(cond, chosen[cond], 2)
results = {}
for r in done:
    results.setdefault(r["cond"], {}).setdefault(str(r["lr"]), []).append(r)
summary = {}
for name, by_lr in results.items():
    best_lr = str(chosen[name])                     # chosen on VALIDATION sentences from the seed-1 sweep
    runs = by_lr[best_lr]
    final = [r["ppl_at_step"][str(T)]["test"] for r in runs]
    summary[name] = {"chosen_lr_by_val": float(best_lr), "final_test_ppl_mean": round(float(np.mean(final)), 3), "final_test_ppl_each_seed": [round(f, 3) for f in final],
                     "test_ppl_before_training": round(runs[0]["ppl_at_step"]["0"]["test"], 3),
                     "final_test_ppl_by_lr_seed1": {lr: round(float([r for r in v if r["seed"] == 1][0]["ppl_at_step"][str(T)]["test"]), 3) for lr, v in by_lr.items()},
                     "best_checkpoint_test_ppl_by_lr_seed1": {lr: round(float(min([r for r in v if r["seed"] == 1][0]["ppl_at_step"][str(k)]["test"] for k in (100, 200))), 3) for lr, v in by_lr.items()}}
summary["comm_bytes_per_worker_fp32"] = {"ddp": comm_bytes_per_worker(n_params, T, W, None), "diloco_H10": comm_bytes_per_worker(n_params, T, W, 10),
                                         "diloco_H50": comm_bytes_per_worker(n_params, T, W, 50), "trainable_params": n_params}
rep = {"model": "Qwen3-0.6B LoRA-FA r=8 (only B trained, A frozen identically)", "data": "FLEURS vi_vn train sentences (1482) / test sentences (150 of 347), disjoint", "workers": W,
       "per_worker_batch": B, "inner_steps_per_worker": T, "ddp_equivalent": f"batch {W*B} per step with a persistent AdamW", "outer": "Nesterov lr 0.7 momentum 0.9",
       "device": "one RTX 3060, workers run sequentially", "seeds": 2, "lr_grid": list(LRS), "selection": "per method, the lr with the lowest VALIDATION perplexity at the last step in the seed-1 sweep; seed 2 is then run at that lr; reported numbers are on separate TEST sentences", "summary": summary, "runs": results, "wall_s": round(time.time() - t0, 1),
       "caveats": ["2 seeds only; differences smaller than the seed spread are not conclusions", "no real network: bytes are analytic (ring all-reduce, fp32, no compression)",
                   "200 steps of a 0.6B model on 1482 sentences: says nothing about convergence of large runs or other tasks", "inner AdamW restarts every round in DiLoCo (as designed) and persists in DDP"]}
out.write_text(json.dumps(rep, indent=1))
print(json.dumps(summary, indent=1))
