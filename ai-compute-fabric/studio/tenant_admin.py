"""Per-tenant data overview and removal. The Studio's tenant is a free-form name (it is NOT a gateway tenant id), so this
is separate from deleting a tenant in the gateway: that revokes the API key, this erases what the tenant stored here."""
from __future__ import annotations

import shutil
from typing import Optional

from agents.knowledge import KnowledgeStore
from studio.db import Db, check_tenant
from studio.errors import conflict
from studio.jobs import JobStore
from studio.web import Request, Router


class TenantAdmin:
    def __init__(self, db: Db, store: Optional[KnowledgeStore], jobs: JobStore):
        self.db, self.store, self.jobs = db, store, jobs

    def mount(self, r: Router):
        r.add("GET", "/v1/tenants/{tenant}", self.summary)
        r.add("DELETE", "/v1/tenants/{tenant}", self.purge)

    def _counts(self, t: str) -> dict:
        with self.db.tenant(t) as c:
            prompts = c.execute("SELECT count(DISTINCT name)::int AS n FROM studio_prompts").fetchone()["n"]
            versions = c.execute("SELECT count(*)::int AS n FROM studio_prompts").fetchone()["n"]
            jobs = c.execute("SELECT count(*)::int AS n, count(*) FILTER (WHERE status IN ('queued','running'))::int AS active FROM studio_jobs").fetchone()
        docs = self.store.totals(t) if self.store else {"documents": 0, "chars": 0}
        return {"documents": docs["documents"], "document_chars": docs["chars"], "prompt_templates": prompts, "prompt_versions": versions,
                "jobs": jobs["n"], "active_jobs": jobs["active"]}

    def summary(self, req: Request):
        return {"tenant": check_tenant(req.params["tenant"]), **self._counts(req.params["tenant"])}

    def purge(self, req: Request):
        """Erase documents, prompt templates, jobs and adapters of one tenant. Refused while a job is queued or running."""
        req.need("admin")
        t = check_tenant(req.params["tenant"])
        before = self._counts(t)
        if before["active_jobs"]:
            raise conflict(f"{before['active_jobs']} job(s) are still queued or running; cancel them first", "active_jobs")
        with self.db.tenant(t) as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM studio_jobs").fetchall()]
            c.execute("DELETE FROM studio_prompt_defaults")
            c.execute("DELETE FROM studio_prompts")
            c.execute("DELETE FROM studio_jobs")
        chunks = self.store.delete_tenant(t) if self.store else 0
        for jid in ids:
            shutil.rmtree(self.jobs.dir(jid), ignore_errors=True)
        return {"deleted": t, "removed": {**before, "chunks": chunks}}
