"""The 15 typed tasks (plan P5.1, [KT] section 6.2).

Every task has a strict input model and a strict output model (pydantic, extra fields forbidden),
so a worker's answer is either well-formed or rejected before anything else consumes it."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskType(str, Enum):
    TEXT_GENERATE = "TEXT_GENERATE"
    OCR = "OCR"
    DOCUMENT_PARSE = "DOCUMENT_PARSE"
    EMBEDDING = "EMBEDDING"
    RERANK = "RERANK"
    RAG_RETRIEVE = "RAG_RETRIEVE"
    AUDIO_VAD = "AUDIO_VAD"
    AUDIO_ASR = "AUDIO_ASR"
    AUDIO_TTS = "AUDIO_TTS"
    AUDIO_ENHANCE = "AUDIO_ENHANCE"
    STRUCTURED_EXTRACTION = "STRUCTURED_EXTRACTION"
    TOOL_EXECUTE = "TOOL_EXECUTE"
    RESULT_VALIDATE = "RESULT_VALIDATE"
    TRAIN_STEP = "TRAIN_STEP"
    MODEL_EVALUATE = "MODEL_EVALUATE"


class Source(Strict):
    source_id: str = Field(min_length=1)
    text: str


class Citation(Strict):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)   # verbatim span of the source that supports the claim


# ---- inputs / outputs ---------------------------------------------------------------------
class TextGenerateIn(Strict):
    messages: list[dict]
    max_tokens: int = Field(default=256, ge=1, le=8192)
    sources: list[Source] = []         # when given, the answer must cite them


class TextGenerateOut(Strict):
    text: str = Field(min_length=1)
    citations: list[Citation] = []


class OcrIn(Strict):
    image_ref: str
    lang: str = "vi"


class OcrOut(Strict):
    text: str
    confidence: float = Field(ge=0, le=1)


class DocumentParseIn(Strict):
    document_ref: str
    mime: str


class DocumentParseOut(Strict):
    pages: list[str]
    tables: list[list[list[str]]] = []


class EmbeddingIn(Strict):
    texts: list[str] = Field(min_length=1)
    model: str = "Qwen3-Embedding-0.6B"


class EmbeddingOut(Strict):
    vectors: list[list[float]]
    dim: int = Field(gt=0)


class RerankIn(Strict):
    query: str
    documents: list[str] = Field(min_length=1)
    top_k: int = Field(default=5, ge=1)


class RerankOut(Strict):
    ranking: list[int]                 # indices into documents, best first
    scores: list[float]


class RagRetrieveIn(Strict):
    query: str
    tenant_id: str
    top_k: int = Field(default=5, ge=1, le=50)


class RagRetrieveOut(Strict):
    hits: list[Source]
    citations: list[Citation]


class AudioVadIn(Strict):
    audio_ref: str


class AudioVadOut(Strict):
    segments: list[tuple[float, float]]


class AudioAsrIn(Strict):
    audio_ref: str
    lang: str = "vi"


class AudioAsrOut(Strict):
    text: str
    segments: list[dict] = []


class AudioTtsIn(Strict):
    text: str = Field(min_length=1)
    voice: str = "default"
    consent_ref: Optional[str] = None  # required for cloned voices (plan P6)


class AudioTtsOut(Strict):
    audio_ref: str
    duration_s: float = Field(gt=0)


class AudioEnhanceIn(Strict):
    audio_ref: str


class AudioEnhanceOut(Strict):
    audio_ref: str


class StructuredExtractionIn(Strict):
    document: Source
    json_schema: dict


class StructuredExtractionOut(Strict):
    data: dict
    citations: list[Citation] = Field(min_length=1)


class ToolExecuteIn(Strict):
    tool: str
    args: dict


class ToolExecuteOut(Strict):
    result: Any


class ResultValidateIn(Strict):
    target_task_id: str


class ResultValidateOut(Strict):
    ok: bool
    errors: list[str] = []


class TrainStepIn(Strict):
    run_id: str
    step: int = Field(ge=0)
    base_model_sha256: str


class TrainStepOut(Strict):
    run_id: str
    step: int
    loss: float
    update_ref: str


class ModelEvaluateIn(Strict):
    model_ref: str
    dataset_ref: str
    metric: Literal["wer", "cer", "accuracy", "perplexity"]


class ModelEvaluateOut(Strict):
    metric: str
    value: float


SCHEMAS: dict[TaskType, tuple[type[Strict], type[Strict]]] = {
    TaskType.TEXT_GENERATE: (TextGenerateIn, TextGenerateOut),
    TaskType.OCR: (OcrIn, OcrOut),
    TaskType.DOCUMENT_PARSE: (DocumentParseIn, DocumentParseOut),
    TaskType.EMBEDDING: (EmbeddingIn, EmbeddingOut),
    TaskType.RERANK: (RerankIn, RerankOut),
    TaskType.RAG_RETRIEVE: (RagRetrieveIn, RagRetrieveOut),
    TaskType.AUDIO_VAD: (AudioVadIn, AudioVadOut),
    TaskType.AUDIO_ASR: (AudioAsrIn, AudioAsrOut),
    TaskType.AUDIO_TTS: (AudioTtsIn, AudioTtsOut),
    TaskType.AUDIO_ENHANCE: (AudioEnhanceIn, AudioEnhanceOut),
    TaskType.STRUCTURED_EXTRACTION: (StructuredExtractionIn, StructuredExtractionOut),
    TaskType.TOOL_EXECUTE: (ToolExecuteIn, ToolExecuteOut),
    TaskType.RESULT_VALIDATE: (ResultValidateIn, ResultValidateOut),
    TaskType.TRAIN_STEP: (TrainStepIn, TrainStepOut),
    TaskType.MODEL_EVALUATE: (ModelEvaluateIn, ModelEvaluateOut),
}
assert len(SCHEMAS) == 15 and set(SCHEMAS) == set(TaskType)

# Types whose outputs make claims about source material and therefore must cite it.
CITATION_REQUIRED = {TaskType.RAG_RETRIEVE, TaskType.STRUCTURED_EXTRACTION}


class Task(Strict):
    task_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    type: TaskType
    input: dict
    data_class: Literal["PUBLIC", "INTERNAL", "RESTRICTED"] = "INTERNAL"


class TaskResult(Strict):
    task_id: str
    type: TaskType
    output: dict
    worker_id: str = ""


def parse_input(task: Task) -> Any:   # the concrete model depends on task.type, which a static checker cannot follow
    return SCHEMAS[task.type][0].model_validate(task.input)
