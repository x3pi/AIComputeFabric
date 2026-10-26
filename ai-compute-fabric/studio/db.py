"""PostgreSQL access with tenant isolation by row-level security (same mechanism as agents/knowledge.py).

Every connection is opened for ONE tenant (`aicf.tenant`, transaction-local), so a repository bug that forgets a WHERE
clause still cannot see another tenant's rows. The job runner needs a cross-tenant view of the queue and asks for it
explicitly with scope="system"; only studio/jobs.py does."""
from __future__ import annotations

import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from studio.errors import bad

TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def check_tenant(tenant: str) -> str:
    if not isinstance(tenant, str) or not TENANT.match(tenant):
        raise bad("tenant must be 1-64 characters: letters, digits, '.', '_' or '-'", "bad_tenant")
    return tenant


SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_prompts (
  tenant_id TEXT NOT NULL, name TEXT NOT NULL, version INT NOT NULL, system TEXT NOT NULL,
  examples JSONB NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, name, version));
CREATE TABLE IF NOT EXISTS studio_prompt_defaults (
  tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INT NOT NULL, set_by TEXT NOT NULL, set_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS studio_jobs (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, params JSONB NOT NULL,
  created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
  result JSONB, error TEXT, cancel_requested BOOLEAN NOT NULL DEFAULT false);
CREATE INDEX IF NOT EXISTS studio_jobs_queue ON studio_jobs (status, created_at);
"""
RLS_TABLES = ("studio_prompts", "studio_prompt_defaults", "studio_jobs")


def setup_schema(admin_dsn: str, app_user: str = "aicf_app"):
    """Run once as a privileged user. Idempotent."""
    from agents.knowledge import setup_schema as knowledge_schema
    knowledge_schema(admin_dsn, app_user)                       # chunks table + role
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(SCHEMA)
        for t in RLS_TABLES:
            c.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            c.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            c.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
            c.execute(f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = current_setting('aicf.tenant', true) "
                      f"OR current_setting('aicf.scope', true) = 'system') "
                      f"WITH CHECK (tenant_id = current_setting('aicf.tenant', true) OR current_setting('aicf.scope', true) = 'system')")
            c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {app_user}")


class Db:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def tenant(self, tenant: str, scope: str = "tenant"):
        """A transaction scoped to one tenant (or, for the job runner only, scope='system')."""
        if scope == "tenant":
            check_tenant(tenant)
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False) as c:
            c.execute("SELECT set_config('aicf.tenant', %s, true), set_config('aicf.scope', %s, true)", (tenant, scope))
            yield c
