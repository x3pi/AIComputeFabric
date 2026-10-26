"""Composition root: the only place that knows which methods exist and how they are wired."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from agents.knowledge import EmbeddingClient, KnowledgeStore, RerankClient
from studio.config import Settings
from studio.db import Db
from studio.jobs import JobStore, Runner, free_vram_mib
from studio.llm import ChatClient
from studio.methods.full_ft import FullFineTuneMethod
from studio.methods.lora import LoraMethod
from studio.methods.prompts import PromptsMethod
from studio.methods.rag import RagMethod
from studio.registry import Registry
from studio.web import Request, Router


@dataclass
class App:
    settings: Settings
    router: Router
    registry: Registry
    runner: Runner

    def close(self):
        self.runner.stop()


def build_app(s: Settings, free_vram=free_vram_mib, start_runner: bool = True) -> App:
    db = Db(s.app_dsn)
    llm = ChatClient(s.llm_url)
    store = KnowledgeStore(s.app_dsn, EmbeddingClient(s.embed_url), RerankClient(s.rerank_url) if s.rerank_url else None) if s.embed_url else None
    jobs = JobStore(db, s.data_dir, s.quotas.max_active_jobs)
    runner = Runner(jobs, {}, free_vram=free_vram)
    prompts = PromptsMethod(s, db, llm)
    lora = LoraMethod(s, jobs, runner)
    runner.kinds[lora.kind] = lora.job_kind()
    reg = Registry()
    for m in (RagMethod(s, store, llm, prompts), prompts, lora, FullFineTuneMethod(s)):
        reg.register(m)
    router = Router()
    reg.mount_all(router)

    def methods(req: Request):
        return {"methods": [dataclasses.asdict(i) for i in reg.infos()]}

    def health(req: Request):
        return {"ok": True, "knowledge_base": store is not None, "llm": bool(s.llm_url), "trainer": bool(s.train_python or s.lora_runner), "runner": runner.status()}
    router.add("GET", "/v1/methods", methods)
    router.add("GET", "/v1/health", health)
    if start_runner:
        runner.start()
    return App(s, router, reg, runner)
