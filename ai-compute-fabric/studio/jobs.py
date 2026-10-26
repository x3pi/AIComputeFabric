"""Durable background jobs for the Studio.

* A job is a row in `studio_jobs` (state machine queued -> running -> succeeded | failed | cancelled) plus a directory
  `<data_dir>/jobs/<id>/` that the worker process writes: progress.jsonl, result.json, log.txt, artifacts.
* The work itself runs in a CHILD PROCESS (own session, killable as a group), never in the web process, so a crash, an
  out-of-memory error or a cancel cannot take the service down.
* The runner is one thread. It runs ONE job at a time (one GPU), and does not start a job whose estimated memory does not
  fit in the VRAM that is free right now (the owner of the PC and the inference engine have priority); the job stays queued
  and the reason is reported.
* A service restart marks jobs that were `running` as failed ("interrupted"), because their child was in this service's
  process group; they are never left running forever.
* New kinds of job register a `JobKind`; the runner knows nothing about LoRA."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import psycopg.types.json

from studio.db import Db, check_tenant
from studio.errors import conflict, not_found, quota

log = logging.getLogger("studio.jobs")
STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
FINAL = ("succeeded", "failed", "cancelled")
ID_CHARS = set("0123456789abcdef")


@dataclass(frozen=True)
class JobKind:
    name: str
    command: Callable[[dict, Path], list]            # (job row, job dir) -> argv of the worker process
    required_mib: Callable[[dict], Optional[float]]  # VRAM needed, None = no GPU needed
    env: Callable[[], dict] = dict
    cwd: Optional[Path] = None
    max_seconds: int = 6 * 3600


def free_vram_mib() -> Optional[float]:
    """Free VRAM on GPU 0 in MiB, or None when it cannot be read (no NVIDIA driver): the check is then skipped."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        return float(out.stdout.split()[0]) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def _die_with_parent():
    """Linux: the kernel kills the worker if the service dies (kill -9, OOM), so a training process can never be orphaned onto the GPU."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def kill_stale(pid_file: Path) -> bool:
    """Fallback for systems without PDEATHSIG: kill the process group a previous service run recorded, if it is still a Studio worker."""
    try:
        pid = int(pid_file.read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        if "studio" in cmd or "lora_run" in cmd:
            os.killpg(pid, signal.SIGKILL)
            return True
    except (OSError, ValueError):
        pass
    return False


def valid_job_id(job_id: str) -> bool:
    return len(job_id) == 32 and set(job_id) <= ID_CHARS


class JobStore:
    def __init__(self, db: Db, data_dir: Path, max_active_per_tenant: int):
        self.db, self.root, self.max_active = db, Path(data_dir) / "jobs", max_active_per_tenant
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise not_found("job")
        return self.root / job_id

    def create(self, tenant: str, kind: str, params: dict, user: str, prepare: Optional[Callable[[Path], None]] = None) -> dict:
        check_tenant(tenant)
        job_id = uuid.uuid4().hex
        d = self.dir(job_id)
        d.mkdir(parents=True)
        try:
            if prepare:
                prepare(d)
            with self.db.tenant(tenant) as c:
                active = c.execute("SELECT count(*)::int AS n FROM studio_jobs WHERE status IN ('queued','running')").fetchone()["n"]
                if active >= self.max_active:
                    raise quota(f"at most {self.max_active} unfinished jobs per tenant; wait for one to finish or cancel it")
                row = c.execute("INSERT INTO studio_jobs(id, tenant_id, kind, status, params, created_by) VALUES (%s,%s,%s,'queued',%s,%s) RETURNING *",
                                (job_id, tenant, kind, psycopg.types.json.Jsonb(params), user)).fetchone()
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return self.public(row)

    def get(self, tenant: str, job_id: str) -> dict:
        self.dir(job_id)
        with self.db.tenant(tenant) as c:
            row = c.execute("SELECT * FROM studio_jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            raise not_found("job")
        return row

    def list(self, tenant: str, kind: Optional[str] = None, limit: int = 50) -> list:
        with self.db.tenant(tenant) as c:
            if kind:
                rows = c.execute("SELECT * FROM studio_jobs WHERE kind=%s ORDER BY created_at DESC LIMIT %s", (kind, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM studio_jobs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return rows

    def cancel(self, tenant: str, job_id: str) -> dict:
        self.get(tenant, job_id)
        with self.db.tenant(tenant) as c:
            row = c.execute("UPDATE studio_jobs SET status='cancelled', finished_at=now(), error='cancelled before it started' WHERE id=%s AND status='queued' RETURNING *", (job_id,)).fetchone()
            if row is None:
                row = c.execute("UPDATE studio_jobs SET cancel_requested=true WHERE id=%s AND status='running' RETURNING *", (job_id,)).fetchone()
            if row is None:
                raise conflict("the job already finished", "already_finished")
        return self.public(row)

    def delete(self, tenant: str, job_id: str):
        row = self.get(tenant, job_id)
        if row["status"] not in FINAL:
            raise conflict("cancel the job first; only finished jobs can be deleted", "not_finished")
        with self.db.tenant(tenant) as c:
            c.execute("DELETE FROM studio_jobs WHERE id=%s", (job_id,))
        shutil.rmtree(self.dir(job_id), ignore_errors=True)

    # -- runner side (cross-tenant, explicit) --------------------------------------------------
    def peek_next(self) -> Optional[dict]:
        """Oldest queued job whose files are in THIS service's data dir. A job whose directory is missing is failed once it is
        older than a minute (the create call writes the row only after the directory exists, so a young one is just another
        service's job on a shared database)."""
        with self.db.tenant("*", scope="system") as c:
            rows = c.execute("SELECT *, (now() - created_at) > interval '60 seconds' AS stale FROM studio_jobs WHERE status='queued' ORDER BY created_at LIMIT 50").fetchall()
        for r in rows:
            if (self.root / r["id"]).is_dir():
                return r
            if r["stale"]:
                self.finish(r["id"], "failed", error="the job's files are missing from this service's data directory")
        return None

    def claim(self, job_id: str) -> Optional[dict]:
        with self.db.tenant("*", scope="system") as c:
            return c.execute("UPDATE studio_jobs SET status='running', started_at=now() WHERE id=%s AND status='queued' RETURNING *", (job_id,)).fetchone()

    def finish(self, job_id: str, status: str, result: Optional[dict] = None, error: Optional[str] = None):
        assert status in FINAL
        with self.db.tenant("*", scope="system") as c:
            c.execute("UPDATE studio_jobs SET status=%s, finished_at=now(), result=%s, error=%s WHERE id=%s AND status IN ('queued','running')",
                      (status, psycopg.types.json.Jsonb(result) if result is not None else None, error, job_id))

    def cancel_requested(self, job_id: str) -> bool:
        with self.db.tenant("*", scope="system") as c:
            r = c.execute("SELECT cancel_requested FROM studio_jobs WHERE id=%s", (job_id,)).fetchone()
        return bool(r and r["cancel_requested"])

    def recover_orphans(self) -> int:
        with self.db.tenant("*", scope="system") as c:
            running = [r["id"] for r in c.execute("SELECT id FROM studio_jobs WHERE status='running'").fetchall()]
        for jid in running:
            kill_stale(self.dir(jid) / "pid")
        with self.db.tenant("*", scope="system") as c:
            return c.execute("UPDATE studio_jobs SET status='failed', finished_at=now(), error='interrupted: the Studio service restarted while this job was running' WHERE status='running'").rowcount

    def public(self, row: dict, with_progress: bool = False) -> dict:
        out = {"id": row["id"], "tenant": row["tenant_id"], "kind": row["kind"], "status": row["status"], "params": row["params"], "created_by": row["created_by"],
               "created_at": row["created_at"].isoformat(), "started_at": row["started_at"] and row["started_at"].isoformat(),
               "finished_at": row["finished_at"] and row["finished_at"].isoformat(), "result": row["result"], "error": row["error"],
               "cancel_requested": row["cancel_requested"]}
        if with_progress:
            out["progress"] = read_progress(self.dir(row["id"]))
        return out


def read_progress(d: Path, limit: int = 2000) -> list:
    p = d / "progress.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue                     # a half-written last line
    return rows


class Runner:
    def __init__(self, store: JobStore, kinds: dict, poll_s: float = 1.0, free_vram: Callable[[], Optional[float]] = free_vram_mib):
        self.store, self.kinds, self.poll_s, self.free_vram = store, kinds, poll_s, free_vram
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.current: Optional[str] = None
        self.waiting: Optional[dict] = None          # {"job": id, "needs_mib": x, "free_mib": y}
        self._proc: Optional[subprocess.Popen] = None

    def start(self):
        n = self.store.recover_orphans()
        if n:
            log.warning("marked %d interrupted job(s) as failed", n)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="studio-runner")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._kill(self._proc)
        if self._thread:
            self._thread.join(timeout=15)

    def status(self) -> dict:
        return {"running_job": self.current, "waiting": self.waiting}

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self._tick():
                    self._stop.wait(self.poll_s)
            except Exception:
                log.exception("runner error")
                self._stop.wait(self.poll_s)

    def _tick(self) -> bool:
        row = self.store.peek_next()
        if row is None:
            self.waiting = None
            return False
        kind = self.kinds.get(row["kind"])
        if kind is None:
            self.store.finish(row["id"], "failed", error=f"unknown job kind {row['kind']!r}")
            return True
        need = kind.required_mib(row["params"])
        free = self.free_vram() if need else None
        if need and free is not None and free < need:
            self.waiting = {"job": row["id"], "needs_mib": round(need), "free_mib": round(free)}
            return False
        self.waiting = None
        job = self.store.claim(row["id"])
        if job is None:                       # cancelled between peek and claim
            return True
        self._run(job, kind)
        return True

    @staticmethod
    def _kill(p: subprocess.Popen):
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=10)
        except ProcessLookupError:
            pass

    def _run(self, job: dict, kind: JobKind):
        d = self.store.dir(job["id"])
        self.current = job["id"]
        t0 = time.time()
        log_f = None
        try:
            log_f = open(d / "log.txt", "ab")
            env = {**os.environ, **kind.env(), "HF_HUB_OFFLINE": "1", "PYTHONUNBUFFERED": "1"}
            self._proc = subprocess.Popen(kind.command(job, d), cwd=kind.cwd, env=env, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=_die_with_parent)
            (d / "pid").write_text(str(self._proc.pid))
            cancelled = False
            while self._proc.poll() is None:
                if self._stop.is_set():
                    self._kill(self._proc)
                    self.store.finish(job["id"], "failed", error="interrupted: the Studio service is shutting down")
                    return
                if self.store.cancel_requested(job["id"]):
                    cancelled = True
                    self._kill(self._proc)
                    break
                if time.time() - t0 > kind.max_seconds:
                    self._kill(self._proc)
                    self.store.finish(job["id"], "failed", error=f"stopped after {kind.max_seconds} s (time limit)")
                    return
                time.sleep(0.5)
            if cancelled:
                self.store.finish(job["id"], "cancelled", error="cancelled by a user")
                return
            res_file = d / "result.json"
            result = json.loads(res_file.read_text()) if res_file.exists() else None
            if self._proc.returncode == 0 and result is not None and "error" not in result:
                self.store.finish(job["id"], "succeeded", result=result)
            else:
                err = (result or {}).get("error") or f"worker process exited with code {self._proc.returncode}"
                self.store.finish(job["id"], "failed", result=result, error=str(err)[:1000])
        except Exception as e:
            log.exception("job %s", job["id"])
            self.store.finish(job["id"], "failed", error=f"{type(e).__name__}: {e}"[:1000])
        finally:
            if log_f:
                log_f.close()
            self._proc, self.current = None, None


def tail(path: Path, n_bytes: int = 8000) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - n_bytes))
        return f.read().decode("utf-8", errors="replace")
