"""MODEL_EVALUATE (over recorded predictions) and RESULT_VALIDATE executors; TRAIN_STEP's refusal paths. No GPU."""
import json

import pytest

from agents.doc_executors import RefusedInput
from agents.executors import ValidatorExecutor
from agents.tasks import Task, TaskResult, TaskType
from training.lora_trainer import EvaluateExecutor, TrainExecutor


def task(tp, inp, tid="t"):
    return Task(task_id=tid, tenant_id="acme", type=tp, input=inp)


def write(tmp, name, rows):
    (tmp / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


def ev(tmp, metric, rows):
    write(tmp, "d.jsonl", rows)
    return EvaluateExecutor(tmp).execute(task(TaskType.MODEL_EVALUATE, {"model_ref": "m", "dataset_ref": "d.jsonl", "metric": metric})).output["value"]


def test_wer_is_corpus_weighted_not_a_mean_of_sentence_wers(tmp_path):
    rows = [{"ref": "một hai ba bốn năm sáu bảy tám chín mười", "hyp": "một hai ba bốn năm sáu bảy tám chín mười"},     # 10 words, 0 errors
            {"ref": "xin chào", "hyp": "tạm biệt"}]                                                                     # 2 words, 2 errors
    assert ev(tmp_path, "wer", rows) == pytest.approx(2 / 12, abs=1e-5)           # a plain mean of sentence WERs would be 0.5


def test_wer_ignores_case_and_punctuation_but_not_wrong_words(tmp_path):
    assert ev(tmp_path, "wer", [{"ref": "Xin chào, quý khách.", "hyp": "xin chào quý khách"}]) == 0.0
    assert ev(tmp_path, "wer", [{"ref": "xin chào quý khách", "hyp": "xin chào quý ngài"}]) == pytest.approx(0.25)


def test_cer_and_accuracy(tmp_path):
    assert ev(tmp_path, "cer", [{"ref": "abcd", "hyp": "abxd"}]) == pytest.approx(0.25)
    assert ev(tmp_path, "accuracy", [{"label": "a", "pred": "a"}, {"label": "b", "pred": "c"}, {"label": "d", "pred": "d"}, {"label": "e", "pred": "e"}]) == 0.75


def test_evaluate_refusals(tmp_path):
    write(tmp_path, "empty.jsonl", [])
    mk = lambda ref, m="wer": task(TaskType.MODEL_EVALUATE, {"model_ref": "m", "dataset_ref": ref, "metric": m})
    with pytest.raises(RefusedInput):
        EvaluateExecutor(tmp_path).execute(mk("empty.jsonl"))
    (tmp_path / "bad.jsonl").write_text("not json\n")
    with pytest.raises(RefusedInput):
        EvaluateExecutor(tmp_path).execute(mk("bad.jsonl"))
    write(tmp_path, "wrongkeys.jsonl", [{"x": 1}])
    for m in ("wer", "cer", "accuracy"):
        with pytest.raises(RefusedInput):
            EvaluateExecutor(tmp_path).execute(mk("wrongkeys.jsonl", m))
    with pytest.raises(RefusedInput):
        EvaluateExecutor(tmp_path).execute(mk("../escape.jsonl"))
    with pytest.raises(NotImplementedError):
        EvaluateExecutor(tmp_path).execute(mk("wrongkeys.jsonl", "perplexity"))


# ---- RESULT_VALIDATE ----
def good_and_bad():
    src = {"source_id": "d1", "text": "Tổng tiền là 500 đồng."}
    orig = task(TaskType.STRUCTURED_EXTRACTION, {"document": src, "json_schema": {"type": "object", "properties": {"tien": {"type": "integer"}}, "required": ["tien"]}}, "ex1")
    good = TaskResult(task_id="ex1", type=TaskType.STRUCTURED_EXTRACTION, output={"data": {"tien": 500}, "citations": [{"source_id": "d1", "quote": "Tổng tiền là 500 đồng."}]})
    bad = TaskResult(task_id="ex1", type=TaskType.STRUCTURED_EXTRACTION, output={"data": {"tien": 500}, "citations": [{"source_id": "d1", "quote": "một câu bịa"}]})
    return orig, good, bad


def run_validate(orig, result, tasks=None):
    t = task(TaskType.RESULT_VALIDATE, {"target_task_id": "ex1"}, "v1")
    return ValidatorExecutor(tasks if tasks is not None else {"ex1": orig}).execute(t, 1, {"ex1": result.model_dump(mode="json")}).output


def test_validator_executor_accepts_a_good_result_and_reports_why_a_bad_one_fails():
    orig, good, bad = good_and_bad()
    assert run_validate(orig, good) == {"ok": True, "errors": []}
    out = run_validate(orig, bad)
    assert out["ok"] is False and "not found verbatim" in out["errors"][0]


def test_validator_executor_refuses_unknown_targets_instead_of_calling_them_valid():
    orig, good, _ = good_and_bad()
    with pytest.raises(ValueError):
        run_validate(orig, good, tasks={})                                                  # task unknown
    t = task(TaskType.RESULT_VALIDATE, {"target_task_id": "ex1"}, "v1")
    with pytest.raises(ValueError):
        ValidatorExecutor({"ex1": orig}).execute(t, 1, {})                                  # result not delivered as a dependency
    other = good.model_copy(update={"task_id": "other"})
    with pytest.raises(ValueError):
        ValidatorExecutor({"ex1": orig}).execute(t, 1, {"x": other.model_dump(mode="json")})


# ---- TRAIN_STEP refusals (fire before any model is loaded) ----
def test_train_step_refuses_wrong_base_checkpoint_and_bad_run_ids(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "w.safetensors").write_bytes(b"weights")
    ex = TrainExecutor(tmp_path, base, "w.safetensors", "data.txt")
    mk = lambda run, sha: task(TaskType.TRAIN_STEP, {"run_id": run, "step": 0, "base_model_sha256": sha})
    from training.lora_trainer import sha256_of
    ok_sha = sha256_of(base / "w.safetensors")
    with pytest.raises(RefusedInput, match="does not match"):
        ex.execute(mk("r1", "0" * 64))
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(RefusedInput):
            ex.execute(mk(bad, ok_sha))
    with pytest.raises(RefusedInput, match="consecutive"):
        ex.execute(task(TaskType.TRAIN_STEP, {"run_id": "r1", "step": 3, "base_model_sha256": ok_sha}))
