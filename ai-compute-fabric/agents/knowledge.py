"""Knowledge base for RAG (plan P5.3): chunking, embeddings, pgvector search, rerank, tenant isolation.

Embedding and rerank models run in llama.cpp servers (Qwen3-Embedding / bge-reranker GGUF), so no
PyTorch is needed on the worker. Tenant isolation is enforced by PostgreSQL row-level security,
not only by application code: the application role sees nothing unless the transaction names a
tenant, and then only that tenant's rows, whatever SQL runs."""
from __future__ import annotations

import json
import math
import re
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

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
    chunks: list[str] = []
    cur: list[str] = []
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, doc_id, chunk_idx));
ALTER TABLE aicf_chunks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
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
    def __init__(self, app_dsn: str, embedder: EmbeddingClient, reranker: RerankClient | None = None, max_connections: int = 8):
        self.dsn, self.embedder, self.reranker, self.max_connections = app_dsn, embedder, reranker, max_connections
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    @property
    def pool(self) -> ConnectionPool:
        """One pool, connections configured once. register_vector() on every new connection created adapter classes that were
        never freed (measured: ~22 KiB of RSS per search); a pool registers it once per connection for the life of the service."""
        with self._lock:
            if self._pool is None:
                self._pool = ConnectionPool(self.dsn, min_size=1, max_size=self.max_connections, open=True, configure=register_vector,
                                            kwargs={"row_factory": dict_row, "autocommit": False})
            return self._pool

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def _conn(self, tenant: str):
        """A transaction scoped to one tenant (set_config is_local: it ends with the transaction, so a pooled connection never carries it over)."""
        if not tenant:
            raise ValueError("tenant is required")
        with self.pool.connection() as c:
            c.execute("SELECT set_config('aicf.tenant', %s, true)", (tenant,))
            yield c

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

    # ---- management (used by the Studio) ----
    def list_documents(self, tenant: str) -> list:
        with self._conn(tenant) as c:
            return c.execute("SELECT doc_id, count(*)::int AS chunks, sum(length(text))::int AS chars, min(data_class) AS data_class, max(created_at) AS created_at "
                             "FROM aicf_chunks GROUP BY doc_id ORDER BY max(created_at) DESC, doc_id").fetchall()

    def document_chunks(self, tenant: str, doc_id: str) -> list:
        with self._conn(tenant) as c:
            return c.execute("SELECT chunk_idx, text FROM aicf_chunks WHERE doc_id = %s ORDER BY chunk_idx", (doc_id,)).fetchall()

    def delete_document(self, tenant: str, doc_id: str) -> int:
        with self._conn(tenant) as c:
            return c.execute("DELETE FROM aicf_chunks WHERE doc_id = %s", (doc_id,)).rowcount

    def totals(self, tenant: str) -> dict:
        with self._conn(tenant) as c:
            r = c.execute("SELECT count(DISTINCT doc_id)::int AS documents, coalesce(sum(length(text)), 0)::bigint AS chars FROM aicf_chunks").fetchone()
        return {"documents": r["documents"], "chars": int(r["chars"])}

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
