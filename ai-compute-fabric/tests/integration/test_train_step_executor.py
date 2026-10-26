"""TRAIN_STEP on a real GPU with Qwen3-0.6B (LoRA-FA). Needs AICF_TRAIN_BASE (HF dir) and AICF_TRAIN_DATA (text file, one
sentence per line). Checks: loss goes down, an interrupted run resumed in a NEW executor equals an uninterrupted one,
a repeated step is answered from its record without training again, a wrong base checkpoint is refused."""
import os
import time
from pathlib import Path

import numpy as np
import pytest

from agents.doc_executors import RefusedInput
from agents.tasks import Task, TaskType
from agents.validator import validate_result
from training.lora_trainer import TrainExecutor, sha256_of

BASE, DATA = os.environ.get("AICF_TRAIN_BASE"), os.environ.get("AICF_TRAIN_DATA")
pytestmark = pytest.mark.skipif(not (BASE and DATA), reason="set AICF_TRAIN_BASE and AICF_TRAIN_DATA")


def mk(root, **kw):
    lines = [l for l in Path(DATA).read_text(encoding="utf-8").splitlines() if l.strip()][:48]
    (root / "d.txt").write_text("\n".join(lines), encoding="utf-8")
    return TrainExecutor(root, BASE, "model.safetensors", "d.txt", steps_per_call=5, rank=8, seq=64, batch=4, lr=3e-3, **kw)


def step(ex, run, n, sha):
    t = Task(task_id=f"{run}-{n}", tenant_id="acme", type=TaskType.TRAIN_STEP, input={"run_id": run, "step": n, "base_model_sha256": sha})
    r = ex.execute(t)
    assert validate_result(t, r).ok
    return r.output


def params(root, ref):
    with np.load(root / ref) as z:
        return {k: z[k] for k in z.files}


def test_train_step_lifecycle(tmp_path):
    sha = sha256_of(Path(BASE) / "model.safetensors")
    a = tmp_path / "a"; a.mkdir()
    ex = mk(a)
    outs = [step(ex, "r1", n, sha) for n in range(6)]
    losses = [o["loss"] for o in outs]
    assert np.mean(losses[-2:]) < np.mean(losses[:2]) - 0.05, losses              # it learns
    saved = params(a, outs[5]["update_ref"])
    assert saved and all("lora_B" in k for k in saved), "LoRA-FA: only B is trained and shipped"
    assert any(np.abs(v).max() > 0 for v in saved.values())
    # repeating a finished step must not train again
    f = a / outs[2]["update_ref"]
    m0 = f.stat().st_mtime_ns
    time.sleep(0.05)
    again = step(ex, "r1", 2, sha)
    assert again == outs[2] and f.stat().st_mtime_ns == m0
    # interrupted at step 3, resumed by a brand-new executor (fresh process state) == uninterrupted
    b = tmp_path / "b"; b.mkdir()
    ex1 = mk(b)
    for n in range(3):
        step(ex1, "r1", n, sha)
    del ex1
    ex2 = mk(b)
    for n in range(3, 6):
        step(ex2, "r1", n, sha)
    pa, pb = params(a, outs[5]["update_ref"]), params(b, "runs/r1/step-5.npz")
    worst = max(float(np.max(np.abs(pa[k] - pb[k]))) for k in pa)
    scale = max(float(np.max(np.abs(v))) for v in pa.values())
    assert worst <= 1e-3 * scale + 1e-6, (worst, scale)
    # the wrong base is refused, a gap in the steps is refused
    with pytest.raises(RefusedInput):
        step(ex, "r2", 0, "0" * 64)
    with pytest.raises(RefusedInput):
        step(ex, "r3", 4, sha)
