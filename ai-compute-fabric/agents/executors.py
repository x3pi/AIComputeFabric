"""Task executors that run typed tasks on real engines (plan P5.1/P5.2).

Only the types this repository can actually serve are implemented; the others raise
NotImplementedError naming the engine they need, so a workflow never silently gets a fake answer."""
from __future__ import annotations

import json
import urllib.request

from agents.knowledge import EmbeddingClient, KnowledgeStore, RerankClient
from agents.orchestrator import WorkerLost
from agents.tasks import Task, TaskResult, TaskType, parse_input
from agents.tool_registry import ToolRegistry


def _post(url, body, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:   # engine gone = GPU reclaimed / worker lost
        raise WorkerLost(str(e)) from e


EXTRACT_SYSTEM = ("Bạn trích xuất dữ liệu từ tài liệu. Chỉ dùng thông tin có trong tài liệu. Trả về JSON gồm 'data' (đúng lược đồ) và "
                  "'evidence': danh sách câu được chép NGUYÊN VĂN từ tài liệu làm bằng chứng cho dữ liệu.")


def extraction_schema(user_schema: dict) -> dict:
    return {"type": "object", "properties": {"data": user_schema, "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
            "required": ["data", "evidence"], "additionalProperties": False}


class LlamaExecutor:
    def __init__(self, llm_url: str | None = None, embed_url: str | None = None, rerank_url: str | None = None,
                 knowledge: KnowledgeStore | None = None, tools: ToolRegistry | None = None, sandbox=None, constrained: bool = True):
        self.llm, self.embed, self.rerank = llm_url, embed_url, rerank_url
        self.knowledge, self.tools, self.sandbox, self.constrained = knowledge, tools, sandbox, constrained

    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        inp = parse_input(task)
        t = task.type
        if t == TaskType.TEXT_GENERATE:
            out = self._generate(inp)
        elif t == TaskType.STRUCTURED_EXTRACTION:
            out = self._extract(inp)
        elif t == TaskType.EMBEDDING:
            vecs = EmbeddingClient(self.embed).embed(inp.texts)
            out = {"vectors": vecs, "dim": len(vecs[0])}
        elif t == TaskType.RERANK:
            order = RerankClient(self.rerank).rerank(inp.query, inp.documents)[: inp.top_k]
            out = {"ranking": [i for i, _ in order], "scores": [s for _, s in order]}
        elif t == TaskType.RAG_RETRIEVE:
            out = self.knowledge.retrieve(inp.tenant_id, inp.query, inp.top_k).model_dump(mode="json")
        elif t == TaskType.TOOL_EXECUTE:
            out = {"result": self.tools.invoke(task.tenant_id, inp.tool, inp.args, self.sandbox)}
        else:
            need = {TaskType.OCR: "an OCR engine", TaskType.DOCUMENT_PARSE: "a document parser", TaskType.AUDIO_VAD: "a VAD model",
                    TaskType.AUDIO_ASR: "Qwen3-ASR", TaskType.AUDIO_TTS: "Qwen3-TTS", TaskType.AUDIO_ENHANCE: "DeepFilterNet",
                    TaskType.TRAIN_STEP: "a trainer", TaskType.MODEL_EVALUATE: "an evaluation harness", TaskType.RESULT_VALIDATE: "the validator service"}[t]
            raise NotImplementedError(f"{t.value} needs {need}, which is not wired up yet")
        return TaskResult(task_id=task.task_id, type=t, output=out, worker_id="llama")

    def _chat(self, messages, max_tokens, response_format=None):
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
        if response_format:
            body["response_format"] = response_format
        return _post(self.llm + "/v1/chat/completions", body)["choices"][0]["message"]["content"]

    def _generate(self, inp):
        msgs = list(inp.messages)
        if inp.sources:   # answer from sources and say where from
            ctx = "\n".join(f"[{s.source_id}] {s.text}" for s in inp.sources)
            msgs = [{"role": "system", "content": "Trả lời chỉ dựa trên các nguồn sau. " + ctx}] + msgs
        return {"text": self._chat(msgs, inp.max_tokens).strip() or "(trống)"}

    def _extract(self, inp):
        rf = {"type": "json_schema", "json_schema": {"name": "extraction", "schema": extraction_schema(inp.json_schema)}} if self.constrained else None
        msgs = [{"role": "system", "content": EXTRACT_SYSTEM + " Lược đồ của data: " + json.dumps(inp.json_schema, ensure_ascii=False)},
                {"role": "user", "content": inp.document.text}]
        raw = self._chat(msgs, 400, rf)
        try:
            parsed = json.loads(raw)
            data, ev = parsed["data"], parsed["evidence"]
        except (ValueError, KeyError, TypeError):
            return {"data": {}, "citations": [{"source_id": inp.document.source_id, "quote": raw[:200] or "?"}]}   # fails validation, by design
        return {"data": data, "citations": [{"source_id": inp.document.source_id, "quote": q} for q in ev if isinstance(q, str) and q]
                or [{"source_id": inp.document.source_id, "quote": "?"}]}
