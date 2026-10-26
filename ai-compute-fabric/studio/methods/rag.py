"""RAG: a tenant's own knowledge base (upload, list, inspect, delete, search, ask with sources)."""
from __future__ import annotations

import base64
import binascii
import re
from typing import Optional

from agents.knowledge import KnowledgeStore, best_sentence
from studio.config import Settings
from studio.db import check_tenant
from studio.errors import bad, not_found, quota, unavailable
from studio.extract import extract_text
from studio.llm import ChatClient
from studio.registry import MethodInfo
from studio.web import Request, Router

DATA_CLASSES = ("PUBLIC", "INTERNAL", "RESTRICTED")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_CITE = re.compile(r"\[(\d{1,2})\]")
ASK_SYSTEM = ("Bạn là trợ lý của doanh nghiệp. Chỉ trả lời dựa trên các NGUỒN được đánh số bên dưới. Sau mỗi ý, ghi số nguồn dạng [1]. "
              "Nếu các nguồn không có thông tin để trả lời, hãy nói rõ là không tìm thấy trong tài liệu, đừng đoán.")


def clean_doc_id(name: str) -> str:
    n = re.sub(r"[\x00-\x1f/\\]", "_", (name or "").strip())
    if not n or len(n) > 200 or n in (".", ".."):
        raise bad("document name must be 1-200 characters", "bad_name")
    return n


class RagMethod:
    info = MethodInfo(
        id="rag", title="Tri thức nội bộ (RAG)", status="available",
        summary="Tải tài liệu của doanh nghiệp lên; khi hỏi, hệ thống tự tìm các đoạn liên quan nhất và đưa cho model đọc, kèm dẫn nguồn. Không cần huấn luyện, cập nhật tài liệu là có hiệu lực ngay.",
        when_to_use="Dữ liệu thay đổi thường xuyên (quy định, chính sách, báo cáo) và cần biết câu trả lời dựa trên tài liệu nào.",
        limits=("Không thay đổi văn phong hay định dạng đầu ra của model.", "PDF dạng ảnh scan cần OCR trước.", "Mỗi tài liệu tối đa 1 triệu ký tự (nạp khoảng 26 giây); tài liệu lớn hơn cần chia nhỏ.", "Chất lượng tìm kiếm đo trên tập nhỏ tiếng Việt, chưa phải benchmark."),
        ui={"icon": "book"})

    def __init__(self, settings: Settings, store: Optional[KnowledgeStore], llm: ChatClient, prompts=None):
        self.s, self.store, self.llm, self.prompts = settings, store, llm, prompts

    def _store(self) -> KnowledgeStore:
        if self.store is None:
            raise unavailable("the knowledge base is not configured (embedding server and database are required)")
        return self.store

    def mount(self, r: Router):
        b = "/v1/tenants/{tenant}/rag"
        r.add("GET", b + "/documents", self.list_items)
        r.add("POST", b + "/documents", self.upload)
        r.add("GET", b + "/documents/{doc}", self.show)
        r.add("DELETE", b + "/documents/{doc}", self.delete)
        r.add("POST", b + "/search", self.search)
        r.add("POST", b + "/ask", self.ask)

    # -- documents ------------------------------------------------------------------------------
    def list_items(self, req: Request):
        t = check_tenant(req.params["tenant"])
        st = self._store()
        docs = st.list_documents(t)
        q = self.s.quotas
        return {"documents": [{"id": d["doc_id"], "chunks": d["chunks"], "chars": d["chars"], "data_class": d["data_class"], "created_at": d["created_at"].isoformat()} for d in docs],
                "totals": st.totals(t), "quota": {"max_documents": q.max_documents, "max_total_chars": q.max_total_chars}}

    def upload(self, req: Request):
        req.need("operator")
        t = check_tenant(req.params["tenant"])
        st = self._store()
        doc_id = clean_doc_id(req.str_param("name", maxlen=300))
        dc = req.str_param("data_class", "INTERNAL", 20).upper()
        if dc not in DATA_CLASSES:
            raise bad(f"data_class must be one of {', '.join(DATA_CLASSES)}", "bad_data_class")
        if "text" in req.body:
            data = req.str_param("text", maxlen=self.s.quotas.max_upload_bytes).encode()
            filename = doc_id if "." in doc_id else doc_id + ".txt"
        else:
            try:
                data = base64.b64decode(req.str_param("content_base64", maxlen=self.s.quotas.max_upload_bytes * 4 // 3 + 8), validate=True)
            except (binascii.Error, ValueError):
                raise bad("content_base64 is not valid base64") from None
            filename = doc_id
        if len(data) > self.s.quotas.max_upload_bytes:
            raise bad(f"file larger than {self.s.quotas.max_upload_bytes} bytes", "too_large")
        text = extract_text(filename, data)
        totals, existing = st.totals(t), {d["doc_id"]: d for d in st.list_documents(t)}
        replaced = existing.get(doc_id)
        docs_after = totals["documents"] + (0 if replaced else 1)
        chars_after = totals["chars"] - (replaced["chars"] if replaced else 0) + len(text)
        if docs_after > self.s.quotas.max_documents:
            raise quota(f"at most {self.s.quotas.max_documents} documents per tenant")
        if chars_after > self.s.quotas.max_total_chars:
            raise quota(f"at most {self.s.quotas.max_total_chars} characters of text per tenant")
        chunks = st.ingest(t, doc_id, text, dc)
        return 201, {"id": doc_id, "chunks": chunks, "chars": len(text), "data_class": dc, "replaced": bool(replaced)}

    def show(self, req: Request):
        t = check_tenant(req.params["tenant"])
        rows = self._store().document_chunks(t, req.params["doc"])
        if not rows:
            raise not_found("document")
        return {"id": req.params["doc"], "chunks": [{"index": r["chunk_idx"], "text": r["text"]} for r in rows[:200]], "truncated": len(rows) > 200}

    def delete(self, req: Request):
        req.need("admin")
        t = check_tenant(req.params["tenant"])
        if self._store().delete_document(t, req.params["doc"]) == 0:
            raise not_found("document")
        return {"deleted": req.params["doc"]}

    # -- retrieval ------------------------------------------------------------------------------
    def _search(self, t: str, query: str, top_k: int) -> list:
        hits = self._store().search(t, query, top_k=top_k)
        return [{"id": f"{h.doc_id}#{h.chunk_idx}", "doc": h.doc_id, "chunk": h.chunk_idx, "text": h.text, "quote": best_sentence(h.text, query),
                 "distance": round(h.distance, 4), "rerank_score": None if h.rerank_score is None else round(h.rerank_score, 4)} for h in hits]

    @staticmethod
    def _query(req: Request) -> str:
        q = req.str_param("query", maxlen=2000).strip()
        if not q:
            raise bad("query is empty")
        return q

    def search(self, req: Request):
        t = check_tenant(req.params["tenant"])
        top_k = req.int_param("top_k", 5, 1, 20)
        return {"query": self._query(req), "hits": self._search(t, self._query(req), top_k)}

    def ask(self, req: Request):
        t = check_tenant(req.params["tenant"])
        q = self._query(req)
        top_k = req.int_param("top_k", 4, 1, 10)
        hits = self._search(t, q, top_k)
        if not hits:
            return {"query": q, "answer": "Chưa có tài liệu nào trong kho tri thức của tenant này.", "sources": [], "cited": [], "warnings": ["empty_knowledge_base"]}
        numbered = "\n".join(f"[{i + 1}] ({h['doc']}) {h['text']}" for i, h in enumerate(hits))
        system, shots, used = ASK_SYSTEM, [], None
        if self.prompts is not None:
            tpl = self.prompts.resolve(t, req.body.get("prompt_name"), req.body.get("prompt_version"))
            if tpl:
                system, shots, used = tpl["system"] + "\n\n" + ASK_SYSTEM, tpl["examples"], {"name": tpl["name"], "version": tpl["version"]}
        msgs = [{"role": "system", "content": system + "\n\nNGUỒN:\n" + numbered}]
        for ex in shots:
            msgs += [{"role": "user", "content": ex["question"]}, {"role": "assistant", "content": ex["answer"]}]
        msgs.append({"role": "user", "content": q})
        answer = self.llm.chat(msgs, max_tokens=req.int_param("max_tokens", 400, 1, 2000))
        cited = sorted({int(n) for n in _CITE.findall(answer) if 1 <= int(n) <= len(hits)})
        warnings = []
        if not cited:
            warnings.append("no_source_marker")
        pool = " ".join(h["text"] for h in hits)
        extra = sorted(set(_NUM.findall(answer)) - set(_NUM.findall(pool)) - {str(n) for n in range(1, len(hits) + 1)})
        if extra:
            warnings.append("numbers_not_in_sources: " + ", ".join(extra[:6]))
        return {"query": q, "answer": answer, "sources": hits, "cited": cited, "warnings": warnings, "prompt": used}
