"""Knowledge base for RAG (plan P5.3): chunking, embeddings, pgvector search, rerank, tenant isolation.

Embedding and rerank models run in llama.cpp servers (Qwen3-Embedding / bge-reranker GGUF), so no
PyTorch is needed on the worker. Tenant isolation is enforced by PostgreSQL row-level security,
not only by application code: the application role sees nothing unless the transaction names a
tenant, and then only that tenant's rows, whatever SQL runs."""
from __future__ import annotations

import json
import math
import re
import urllib.request
from dataclasses import dataclass

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from agents.tasks import Citation, RagRetrieveOut, Source

DIM = 1024


def _post(url: str, body: dict, timeout: float = 120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class EmbeddingClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def embed(self, texts: list) -> list:
        out = []
        for i in range(0, len(texts), 16):
            d = _post(self.url + "/v1/embeddings", {"input": texts[i:i + 16], "model": "embedding"})["data"]
            for item in sorted(d, key=lambda x: x["index"]):
                v = item["embedding"]
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / n for x in v])   # unit length: cosine distance == 1 - dot
        return out


class RerankClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def rerank(self, query: str, docs: list) -> list:
        """Indices of docs, best first, with scores."""
        res = _post(self.url + "/v1/rerank", {"query": query, "documents": docs})["results"]
        res.sort(key=lambda r: -r["relevance_score"])
        return [(r["index"], r["relevance_score"]) for r in res]


_SENT = re.compile(r"(?<=[.!?。])\s+|\n+")


def chunk_text(text: str, max_chars: int = 600, overlap_sentences: int = 1) -> list:
    """Sentence-aligned chunks so a citation quote (a sentence) is always inside one chunk."""
    sents = [s.strip() for s in _SENT.split(text) if s.strip()]
    chunks, cur = [], []
    for s in sents:
        if cur and sum(len(x) + 1 for x in cur) + len(s) > max_chars:
            chunks.append(" ".join(cur))
            cur = cur[-overlap_sentences:] if overlap_sentences else []
        cur.append(s)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS aicf_chunks (
  tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, chunk_idx INT NOT NULL, text TEXT NOT NULL,
  data_class TEXT NOT NULL DEFAULT 'INTERNAL', embedding vector(%(dim)s) NOT NULL,
  PRIMARY KEY (tenant_id, doc_id, chunk_idx));
CREATE INDEX IF NOT EXISTS aicf_chunks_hnsw ON aicf_chunks USING hnsw (embedding vector_cosine_ops);
ALTER TABLE aicf_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE aicf_chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON aicf_chunks;
CREATE POLICY tenant_isolation ON aicf_chunks
  USING (tenant_id = current_setting('aicf.tenant', true))
  WITH CHECK (tenant_id = current_setting('aicf.tenant', true));
"""


def setup_schema(admin_dsn: str, app_user: str = "aicf_app", app_password: str = "aicf_app_pw", dim: int = DIM):
    """Run once as a privileged user: creates the table, RLS policy and an unprivileged application role."""
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(SCHEMA_SQL % {"dim": dim})
        exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (app_user,)).fetchone()
        if not exists:
            c.execute(psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                psycopg.sql.Identifier(app_user), psycopg.sql.Literal(app_password)))
        c.execute(psycopg.sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON aicf_chunks TO {}").format(psycopg.sql.Identifier(app_user)))


@dataclass
class Hit:
    doc_id: str
    chunk_idx: int
    text: str
    distance: float
    rerank_score: float | None = None


class KnowledgeStore:
    def __init__(self, app_dsn: str, embedder: EmbeddingClient, reranker: RerankClient | None = None):
        self.dsn, self.embedder, self.reranker = app_dsn, embedder, reranker

    def _conn(self, tenant: str):
        if not tenant:
            raise ValueError("tenant is required")
        c = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)
        register_vector(c)
        c.execute("SELECT set_config('aicf.tenant', %s, true)", (tenant,))   # transaction-local: dies with the transaction
        return c

    def ingest(self, tenant: str, doc_id: str, text: str, data_class: str = "INTERNAL") -> int:
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vecs = self.embedder.embed(chunks)
        with self._conn(tenant) as c:
            c.execute("DELETE FROM aicf_chunks WHERE doc_id = %s", (doc_id,))
            for i, (t, v) in enumerate(zip(chunks, vecs)):
                c.execute("INSERT INTO aicf_chunks(tenant_id, doc_id, chunk_idx, text, data_class, embedding) VALUES (%s,%s,%s,%s,%s,%s)",
                          (tenant, doc_id, i, t, data_class, Vector(v)))
        return len(chunks)

    def search(self, tenant: str, query: str, top_k: int = 5, candidates: int = 20, rerank: bool = True) -> list:
        qv = self.embedder.embed([query])[0]
        with self._conn(tenant) as c:
            rows = c.execute("SELECT doc_id, chunk_idx, text, embedding <=> %s AS distance FROM aicf_chunks ORDER BY embedding <=> %s LIMIT %s",
                             (Vector(qv), Vector(qv), candidates)).fetchall()
        hits = [Hit(r["doc_id"], r["chunk_idx"], r["text"], r["distance"]) for r in rows]
        if rerank and self.reranker and hits:
            order = self.reranker.rerank(query, [h.text for h in hits])
            hits = [Hit(**{**hits[i].__dict__, "rerank_score": s}) for i, s in order]
        return hits[:top_k]

    def retrieve(self, tenant: str, query: str, top_k: int = 5) -> RagRetrieveOut:
        """Typed RAG_RETRIEVE answer: hits plus, for each, a verbatim sentence that best matches the query."""
        hits = self.search(tenant, query, top_k)
        srcs, cites = [], []
        for h in hits:
            sid = f"{h.doc_id}#{h.chunk_idx}"
            srcs.append(Source(source_id=sid, text=h.text))
            cites.append(Citation(source_id=sid, quote=best_sentence(h.text, query)))
        return RagRetrieveOut(hits=srcs, citations=cites)


def best_sentence(text: str, query: str) -> str:
    sents = [s.strip() for s in _SENT.split(text) if s.strip()] or [text.strip()]
    q = set(re.findall(r"\w+", query.casefold()))
    return max(sents, key=lambda s: len(q & set(re.findall(r"\w+", s.casefold()))))
