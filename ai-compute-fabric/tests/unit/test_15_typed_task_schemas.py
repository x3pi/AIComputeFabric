import copy

import pytest

from agents.tasks import SCHEMAS, Task, TaskResult, TaskType
from agents.validator import validate_result

SRC = {"source_id": "d1", "text": "Doanh thu quý 3 là 1.250 triệu đồng, tăng 12% so với quý 2."}
CITE = {"source_id": "d1", "quote": "Doanh thu quý 3 là 1.250 triệu đồng"}
SCHEMA = {"type": "object", "properties": {"revenue": {"type": "number"}}, "required": ["revenue"]}

# (input, output) known to be valid for every one of the 15 types
OK = {
    TaskType.TEXT_GENERATE: ({"messages": [{"role": "user", "content": "Doanh thu?"}], "sources": [SRC]},
                             {"text": "Doanh thu quý 3 là 1.250 triệu đồng.", "citations": [CITE]}),
    TaskType.OCR: ({"image_ref": "img://1"}, {"text": "xin chào", "confidence": 0.93}),
    TaskType.DOCUMENT_PARSE: ({"document_ref": "doc://1", "mime": "application/pdf"}, {"pages": ["p1", "p2"]}),
    TaskType.EMBEDDING: ({"texts": ["a", "b"]}, {"vectors": [[0.1, 0.2], [0.3, 0.4]], "dim": 2}),
    TaskType.RERANK: ({"query": "q", "documents": ["a", "b", "c"]}, {"ranking": [2, 0, 1], "scores": [0.9, 0.5, 0.1]}),
    TaskType.RAG_RETRIEVE: ({"query": "doanh thu", "tenant_id": "t1"}, {"hits": [SRC], "citations": [CITE]}),
    TaskType.AUDIO_VAD: ({"audio_ref": "a://1"}, {"segments": [(0.0, 1.5), (2.0, 3.1)]}),
    TaskType.AUDIO_ASR: ({"audio_ref": "a://1"}, {"text": "xin chào"}),
    TaskType.AUDIO_TTS: ({"text": "xin chào"}, {"audio_ref": "a://2", "duration_s": 1.2}),
    TaskType.AUDIO_ENHANCE: ({"audio_ref": "a://1"}, {"audio_ref": "a://3"}),
    TaskType.STRUCTURED_EXTRACTION: ({"document": SRC, "json_schema": SCHEMA}, {"data": {"revenue": 1250}, "citations": [CITE]}),
    TaskType.TOOL_EXECUTE: ({"tool": "calc", "args": {"expr": "1+1"}}, {"result": 2}),
    TaskType.RESULT_VALIDATE: ({"target_task_id": "x"}, {"ok": True}),
    TaskType.TRAIN_STEP: ({"run_id": "r", "step": 3, "base_model_sha256": "ab" * 32}, {"run_id": "r", "step": 3, "loss": 1.2, "update_ref": "u://1"}),
    TaskType.MODEL_EVALUATE: ({"model_ref": "m", "dataset_ref": "d", "metric": "wer"}, {"metric": "wer", "value": 0.21}),
}


def mk(tp, inp=None, out=None):
    i, o = OK[tp]
    task = Task(task_id="t1", tenant_id="ten", type=tp, input=inp if inp is not None else i)
    res = TaskResult(task_id="t1", type=tp, output=out if out is not None else o, worker_id="pc-1")
    return task, res


def test_there_are_exactly_15_types_each_with_input_and_output_schema():
    assert len(TaskType) == 15 and set(OK) == set(TaskType) == set(SCHEMAS)
    for tp, (i, o) in SCHEMAS.items():
        assert i.model_json_schema()["type"] == "object" and o.model_json_schema()["type"] == "object"


@pytest.mark.parametrize("tp", list(TaskType))
def test_valid_pair_passes(tp):
    rep = validate_result(*mk(tp))
    assert rep.ok, rep.errors


@pytest.mark.parametrize("tp", list(TaskType))
def test_unknown_extra_field_in_output_is_rejected(tp):
    out = copy.deepcopy(OK[tp][1]); out["injected"] = "x"
    assert not validate_result(*mk(tp, out=out)).ok


@pytest.mark.parametrize("tp", list(TaskType))
def test_empty_output_is_rejected(tp):
    rep = validate_result(*mk(tp, out={}))
    assert not rep.ok and rep.errors[0].startswith("schema")


@pytest.mark.parametrize("tp", list(TaskType))
def test_invalid_input_is_rejected_at_the_boundary(tp):
    inp = copy.deepcopy(OK[tp][0]); inp["unexpected"] = 1
    assert not validate_result(*mk(tp, inp=inp)).ok


def test_result_for_another_task_or_type_is_rejected():
    task, res = mk(TaskType.OCR)
    assert not validate_result(task, res.model_copy(update={"task_id": "other"})).ok
    assert not validate_result(task, res.model_copy(update={"type": TaskType.AUDIO_ASR})).ok


def test_wrong_value_types_are_rejected():
    assert not validate_result(*mk(TaskType.OCR, out={"text": "x", "confidence": "high"})).ok
    assert not validate_result(*mk(TaskType.OCR, out={"text": "x", "confidence": 1.5})).ok
    assert not validate_result(*mk(TaskType.MODEL_EVALUATE, inp={"model_ref": "m", "dataset_ref": "d", "metric": "bleu"})).ok


# ---- citations --------------------------------------------------------------------------
def test_answer_built_on_sources_without_citations_is_rejected():
    assert not validate_result(*mk(TaskType.TEXT_GENERATE, out={"text": "Doanh thu quý 3 là 1.250 triệu đồng."})).ok
    assert not validate_result(*mk(TaskType.STRUCTURED_EXTRACTION, out={"data": {"revenue": 1250}, "citations": []})).ok
    assert not validate_result(*mk(TaskType.RAG_RETRIEVE, out={"hits": [SRC], "citations": []})).ok


def test_generation_without_sources_needs_no_citation():
    task, res = mk(TaskType.TEXT_GENERATE, inp={"messages": [{"role": "user", "content": "hi"}]}, out={"text": "chào"})
    assert validate_result(task, res).ok


def test_citation_to_unknown_source_or_invented_quote_is_rejected():
    bad_src = {"text": "Doanh thu quý 3 là 1.250 triệu đồng.", "citations": [{"source_id": "zz", "quote": "x"}]}
    assert "unknown source" in " ".join(validate_result(*mk(TaskType.TEXT_GENERATE, out=bad_src)).errors)
    bad_quote = {"text": "Doanh thu quý 3 là 1.250 triệu đồng.", "citations": [{"source_id": "d1", "quote": "Doanh thu quý 3 là 9.999 triệu"}]}
    assert "not found verbatim" in " ".join(validate_result(*mk(TaskType.TEXT_GENERATE, out=bad_quote)).errors)


def test_quote_matching_ignores_case_and_whitespace_only():
    out = {"text": "Doanh thu quý 3 là 1.250 triệu đồng.", "citations": [{"source_id": "d1", "quote": "DOANH  THU quý\n3"}]}
    assert validate_result(*mk(TaskType.TEXT_GENERATE, out=out)).ok


def test_number_not_in_cited_source_is_flagged_as_possible_hallucination():
    out = {"text": "Doanh thu quý 3 là 1.250 triệu đồng, tăng 40%.", "citations": [CITE]}
    rep = validate_result(*mk(TaskType.TEXT_GENERATE, out=out))
    assert not rep.ok and any("'40'" in e for e in rep.errors)


def test_extraction_must_satisfy_the_requested_json_schema():
    out = {"data": {"revenue": "nhiều"}, "citations": [CITE]}
    rep = validate_result(*mk(TaskType.STRUCTURED_EXTRACTION, out=out))
    assert not rep.ok and any("requested schema" in e for e in rep.errors)


def test_embedding_and_rerank_shape_checks():
    assert not validate_result(*mk(TaskType.EMBEDDING, out={"vectors": [[0.1, 0.2]], "dim": 2})).ok       # 2 texts, 1 vector
    assert not validate_result(*mk(TaskType.EMBEDDING, out={"vectors": [[0.1], [0.2]], "dim": 2})).ok     # wrong dim
    assert not validate_result(*mk(TaskType.RERANK, out={"ranking": [0, 0, 1], "scores": [1, 1, 1]})).ok  # duplicate
    assert not validate_result(*mk(TaskType.RERANK, out={"ranking": [0, 1, 7], "scores": [1, 1, 1]})).ok  # out of range
