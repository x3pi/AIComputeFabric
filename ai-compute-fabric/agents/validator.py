"""ResultValidator (plan P5.3): schema, citations and a hallucination check on worker output.

Nothing a worker returns is trusted: the orchestrator passes every result through here before
it is stored or fed to another step."""
from __future__ import annotations

import re
from typing import Any
from dataclasses import dataclass, field

from pydantic import ValidationError

from agents.tasks import CITATION_REQUIRED, SCHEMAS, Citation, Task, TaskResult, TaskType

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def norm(s: str) -> str:
    return _WS.sub(" ", s).strip().casefold()


@dataclass
class Report:
    ok: bool = True
    errors: list = field(default_factory=list)

    def fail(self, msg: str):
        self.ok = False
        self.errors.append(msg)


def check_citations(cites: list, sources: dict, rep: Report):
    for c in cites:
        c = c if isinstance(c, Citation) else Citation.model_validate(c)
        src = sources.get(c.source_id)
        if src is None:
            rep.fail(f"citation names unknown source {c.source_id!r}")
        elif norm(c.quote) not in norm(src):
            rep.fail(f"citation quote not found verbatim in source {c.source_id!r}")


def validate_result(task: Task, result: TaskResult) -> Report:
    rep = Report()
    if result.task_id != task.task_id:
        rep.fail("result belongs to a different task")
    if result.type != task.type:
        rep.fail(f"result type {result.type} does not match task type {task.type}")
    if not rep.ok:
        return rep
    in_model, out_model = SCHEMAS[task.type]
    try:
        inp: Any = in_model.model_validate(task.input)         # concrete model depends on task.type
        out: Any = out_model.model_validate(result.output)
    except ValidationError as e:
        rep.fail("schema: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5]))
        return rep

    sources: dict = {}
    if task.type == TaskType.TEXT_GENERATE:
        sources = {s.source_id: s.text for s in inp.sources}
    elif task.type == TaskType.STRUCTURED_EXTRACTION:
        sources = {inp.document.source_id: inp.document.text}
    elif task.type == TaskType.RAG_RETRIEVE:
        sources = {s.source_id: s.text for s in out.hits}

    cites = getattr(out, "citations", [])
    if (task.type in CITATION_REQUIRED or (task.type == TaskType.TEXT_GENERATE and sources)) and not cites:
        rep.fail("missing citations: an answer built on sources must cite them")
    check_citations(cites, sources, rep)

    if task.type == TaskType.TEXT_GENERATE and sources:
        # Hallucination heuristic (NOT a proof): a number in the answer that appears in no cited source.
        cited = " ".join(sources[c.source_id] for c in cites if c.source_id in sources)
        for n in set(_NUM.findall(out.text)) - set(_NUM.findall(cited)):
            rep.fail(f"number {n!r} in the answer is not in any cited source (possible hallucination)")
    if task.type == TaskType.STRUCTURED_EXTRACTION:
        import jsonschema
        try:
            jsonschema.validate(out.data, inp.json_schema)
        except jsonschema.ValidationError as e:
            rep.fail(f"extracted data violates the requested schema: {e.message}")
    if task.type == TaskType.TOOL_EXECUTE and isinstance(out.result, dict) and "error" in out.result:
        rep.fail(f"the tool reported an error: {str(out.result['error'])[:120]}")   # a refusal inside the sandbox is not a result
    if task.type == TaskType.EMBEDDING:
        if len(out.vectors) != len(inp.texts) or any(len(v) != out.dim for v in out.vectors):
            rep.fail("embedding shape does not match texts/dim")
    if task.type == TaskType.RERANK:
        if sorted(out.ranking) != sorted(set(out.ranking)) or any(i < 0 or i >= len(inp.documents) for i in out.ranking) or len(out.ranking) != len(out.scores):
            rep.fail("rerank indices out of range, duplicated or not aligned with scores")
    return rep
