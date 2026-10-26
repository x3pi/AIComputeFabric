"""RAG knowledge base against real PostgreSQL+pgvector and real embedding/rerank servers (plan P5.3).

Needs AICF_PG_ADMIN_DSN, AICF_PG_APP_DSN, AICF_EMBED_URL, AICF_RERANK_URL (otherwise skipped, NOT passed)."""
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from agents.knowledge import EmbeddingClient, KnowledgeStore, RerankClient, chunk_text, setup_schema
from agents.tasks import Task, TaskResult, TaskType
from agents.validator import validate_result

ENV = {k: os.environ.get(k) for k in ("AICF_PG_ADMIN_DSN", "AICF_PG_APP_DSN", "AICF_EMBED_URL", "AICF_RERANK_URL")}
needs = pytest.mark.skipif(not all(ENV.values()), reason="set AICF_PG_ADMIN_DSN, AICF_PG_APP_DSN, AICF_EMBED_URL, AICF_RERANK_URL")
DATA = json.loads((Path(__file__).resolve().parents[1] / "fixtures/rag_vi.json").read_text())


def test_chunks_are_sentence_aligned_and_bounded():
    text = " ".join(f"Câu số {i} nói về một chủ đề khác nhau hoàn toàn." for i in range(40))
    chunks = chunk_text(text, max_chars=200)
    assert len(chunks) > 5 and all(len(c) <= 260 for c in chunks)
    assert all(c.endswith(".") for c in chunks), "a chunk must not cut a sentence in half"
    assert chunk_text("") == [] and chunk_text("Một câu.") == ["Một câu."]


@pytest.fixture(scope="module")
def stores():
    setup_schema(ENV["AICF_PG_ADMIN_DSN"])
    emb, rr = EmbeddingClient(ENV["AICF_EMBED_URL"]), RerankClient(ENV["AICF_RERANK_URL"])
    return KnowledgeStore(ENV["AICF_PG_APP_DSN"], emb, rr)


@pytest.fixture()
def tenant(stores):
    t = "t-" + uuid.uuid4().hex[:8]
    yield t
    with psycopg.connect(ENV["AICF_PG_ADMIN_DSN"], autocommit=True) as c:
        c.execute("DELETE FROM aicf_chunks WHERE tenant_id = %s", (t,))


@needs
def test_retrieval_quality_on_the_small_vietnamese_set(stores, tenant):
    for p in DATA["passages"]:
        stores.ingest(tenant, p["id"], p["text"])
    def recall(k, rerank):
        hit = 0
        for q in DATA["questions"]:
            docs = [h.doc_id for h in stores.search(tenant, q["q"], top_k=k, rerank=rerank)]
            hit += q["gold"] in docs
        return hit / len(DATA["questions"])
    r1d, r3d, r3r, r1r = recall(1, False), recall(3, False), recall(3, True), recall(1, True)
    print(f"recall@1 dense {r1d:.2f} / rerank {r1r:.2f}; recall@3 dense {r3d:.2f} / rerank {r3r:.2f}")
    assert r3d >= 0.9 and r3r >= 0.9 and r1r >= r1d - 0.05
    Path(os.environ.get("AICF_RAG_REPORT", os.devnull)).write_text(json.dumps(
        {"set": "tests/fixtures/rag_vi.json (20 hand-written passages, 20 paraphrased questions)", "embedding": "Qwen3-Embedding-0.6B Q8_0",
         "reranker": "bge-reranker-v2-m3 Q8_0", "recall_at_1": {"dense": r1d, "dense+rerank": r1r}, "recall_at_3": {"dense": r3d, "dense+rerank": r3r},
         "caveat": "tiny hand-made set: validates the pipeline, is not a retrieval benchmark"}, indent=1, ensure_ascii=False))


@needs
def test_tenants_cannot_see_each_others_chunks_even_with_unfiltered_sql(stores, tenant):
    other = tenant + "-other"
    stores.ingest(tenant, "mine", "Mật khẩu quản trị của tenant này là bí mật số một.")
    stores.ingest(other, "theirs", "Tài liệu riêng của tenant khác, không được lộ ra.")
    try:
        assert {h.doc_id for h in stores.search(tenant, "tài liệu riêng của tenant khác")} == {"mine"}
        # an application bug: raw SQL with no WHERE tenant clause
        with psycopg.connect(ENV["AICF_PG_APP_DSN"]) as c:
            assert c.execute("SELECT count(*) FROM aicf_chunks").fetchone()[0] == 0, "no tenant named -> no rows"
            c.execute("SELECT set_config('aicf.tenant', %s, true)", (tenant,))
            docs = {r[0] for r in c.execute("SELECT doc_id FROM aicf_chunks").fetchall()}
            assert docs == {"mine"}
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("INSERT INTO aicf_chunks VALUES (%s,'x',0,'t','INTERNAL',array_fill(0.1::real, ARRAY[1024])::vector)", (other,))
        with psycopg.connect(ENV["AICF_PG_APP_DSN"]) as c:
            c.execute("SELECT set_config('aicf.tenant', %s, true)", (tenant,))
            assert c.execute("DELETE FROM aicf_chunks WHERE doc_id='theirs'").rowcount == 0
        with pytest.raises(ValueError):
            stores.search("", "x")
    finally:
        with psycopg.connect(ENV["AICF_PG_ADMIN_DSN"], autocommit=True) as c:
            c.execute("DELETE FROM aicf_chunks WHERE tenant_id = %s", (other,))


@needs
def test_retrieve_returns_a_typed_answer_that_passes_the_result_validator(stores, tenant):
    for p in DATA["passages"][:10]:
        stores.ingest(tenant, p["id"], p["text"])
    out = stores.retrieve(tenant, "Mỗi năm tôi được nghỉ bao nhiêu ngày phép?", top_k=3)
    task = Task(task_id="r1", tenant_id=tenant, type=TaskType.RAG_RETRIEVE, input={"query": "nghỉ phép", "tenant_id": tenant, "top_k": 3})
    rep = validate_result(task, TaskResult(task_id="r1", type=TaskType.RAG_RETRIEVE, output=out.model_dump(mode="json")))
    assert rep.ok, rep.errors
    assert out.hits[0].source_id.startswith("hr1")


@needs
def test_reingesting_a_document_replaces_its_chunks(stores, tenant):
    stores.ingest(tenant, "d", "Phiên bản một của tài liệu. Có hai câu.")
    stores.ingest(tenant, "d", "Phiên bản hai.")
    texts = [h.text for h in stores.search(tenant, "tài liệu", top_k=10, rerank=False)]
    assert texts == ["Phiên bản hai."]
