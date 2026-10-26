"""Constrained decoding for STRUCTURED_EXTRACTION against a real llama-server (plan P5.2).

Same model, same 10 invoices, greedy decoding; with and without a JSON-schema grammar. Measures how
often the output is valid JSON for the schema, how many field values are right and whether the
evidence quotes are verbatim (the ResultValidator's check). Needs AICF_LLM_URL; skipped otherwise."""
import json
import os
from pathlib import Path

import pytest

from agents.executors import LlamaExecutor
from agents.tasks import Task, TaskType
from agents.validator import validate_result

URL = os.environ.get("AICF_LLM_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set AICF_LLM_URL to a running llama-server")
DOCS = json.loads((Path(__file__).resolve().parents[1] / "fixtures/extraction_vi.json").read_text())["docs"]
SCHEMA = {"type": "object", "properties": {"nha_cung_cap": {"type": "string"}, "so_tien": {"type": "integer"}, "ngay": {"type": "string"},
                                          "loai": {"type": "string", "enum": ["vanphong", "vanchuyen", "tiepkhach", "dammay"]}},
          "required": ["nha_cung_cap", "so_tien", "ngay", "loai"], "additionalProperties": False}


def run(constrained):
    ex = LlamaExecutor(llm_url=URL, constrained=constrained)
    stats = {"schema_valid": 0, "validator_ok": 0, "fields_right": 0, "fields_total": 0, "n": len(DOCS)}
    for i, d in enumerate(DOCS):
        task = Task(task_id=f"e{i}", tenant_id="acme", type=TaskType.STRUCTURED_EXTRACTION,
                    input={"document": {"source_id": f"doc{i}", "text": d["text"]}, "json_schema": SCHEMA})
        res = ex.execute(task)
        rep = validate_result(task, res)
        data = res.output["data"]
        ok_schema = not any("requested schema" in e or e.startswith("schema") for e in rep.errors) and bool(data)
        stats["schema_valid"] += ok_schema
        stats["validator_ok"] += rep.ok
        for k, v in d["gold"].items():
            stats["fields_total"] += 1
            stats["fields_right"] += (data.get(k) == v)
    return stats


def test_constrained_decoding_gives_schema_valid_output_every_time():
    s = run(True)
    print("constrained  :", s)
    Path(os.environ.get("AICF_EXTRACT_REPORT", os.devnull)).write_text(json.dumps(
        {"set": "tests/fixtures/extraction_vi.json (10 hand-written invoices)", "model": os.environ.get("AICF_LLM_NAME", "unknown"),
         "constrained": s, "unconstrained": run(False),
         "caveat": "tiny set; validator_ok also requires verbatim evidence quotes. The unconstrained 0/10 is a FORMAT failure, not an extraction-ability result: Qwen3-4B Q4 returned the bare fields on 10/10 documents and never the requested {data, evidence} wrapper (checked by hand), so the executor found no data. It shows what the grammar guarantees; it does not show the model cannot read invoices."}, indent=1))
    assert s["schema_valid"] == s["n"], "a grammar-constrained decoder must always produce schema-valid JSON"
    assert s["fields_right"] / s["fields_total"] >= 0.85


def test_unconstrained_run_is_reported_for_comparison_not_asserted():
    s = run(False)
    print("unconstrained:", s)
    assert s["n"] == len(DOCS)
