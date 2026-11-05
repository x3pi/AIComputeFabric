"""Hash-chained metering ledger (compatible with apps/global-control-plane/metering.go::VerifyLedgerFile) and audit log.

* Every ledger line uses the exact Go struct field order of `gcp.entry` (`Usage` + `Prev` + `Hash`), so `gcp.VerifyLedgerFile(path)`
  verifies the Python-written ledger file directly.
* Records both execution usage (`GPUMs`, `VRAMMiBSec`) and idle container lease hold time (`VRAMMiBSec` with `GPUMs=0`).
* `ServerlessAudit` records structured events (`deploy`, `rollback`, `invoke`, `spawn`, `evict`, `volume_denied`, `drain`, `policy_update`)
  without ever storing arguments, results, or secret values.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

GENESIS = "0" * 64


def _entry_dict(site: str, tenant: str, job: str, seq: int, gpu_ms: int, vram_mib_s: int, prev: str, h: str = "") -> dict:
    return {
        "site": site,
        "tenant": tenant,
        "job": job,
        "seq": int(seq),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "gpu_ms": max(0, int(gpu_ms)),
        "vram_mib_s": max(0, int(vram_mib_s)),
        "audio_ms": 0,
        "micro_kwh": 0,
        "prev": prev,
        "hash": h,
    }


def _hash_entry(d: dict) -> str:
    copy = dict(d)
    copy["hash"] = ""
    raw = json.dumps(copy, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class UsageLedger:
    def __init__(self, path: Optional[Path] = None, site: str = "LOCAL"):
        self.path = Path(path) if path else None
        self.site = site
        self._mu = threading.Lock()
        self._entries: list[dict] = []
        self._seq = 0
        self._last_hash = GENESIS
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        prev = GENESIS
        for i, line in enumerate(self.path.read_text("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("prev") != prev or _hash_entry(e) != e.get("hash"):
                raise ValueError(f"ledger broken at line {i}")
            self._entries.append(e)
            self._seq = max(self._seq, int(e["seq"]))
            prev = e["hash"]
        self._last_hash = prev

    def append(self, tenant: str, job: str, gpu_ms: int, vram_mib_s: int) -> dict:
        if not tenant or not job or gpu_ms < 0 or vram_mib_s < 0:
            raise ValueError("invalid usage record")
        with self._mu:
            self._seq += 1
            e = _entry_dict(self.site, tenant, job, self._seq, gpu_ms, vram_mib_s, self._last_hash, "")
            e["hash"] = _hash_entry(e)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                raw = (json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, raw)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._entries.append(e)
            self._last_hash = e["hash"]
            return dict(e)

    def record_call(self, tenant: str, call_id: str, gpu_ms: float, vram_mb: int) -> dict:
        gms = max(0, int(round(gpu_ms))) if vram_mb > 0 or gpu_ms > 0 else 0
        vmibs = max(0, int(round(( max(0, vram_mb) * max(0.0, gpu_ms)) / 1000.0)))
        return self.append(tenant, call_id, gms, vmibs)

    def record_idle_lease(self, tenant: str, lease_id: str, idle_s: float, vram_mb: int) -> Optional[dict]:
        if idle_s <= 0 or vram_mb <= 0 or not tenant:
            return None
        vmibs = max(1, int(round(vram_mb * idle_s)))
        job = f"idle:{lease_id}:{int(time.time() * 1000)}"
        return self.append(tenant, job, 0, vmibs)

    def verify(self) -> tuple[int, str]:
        with self._mu:
            prev = GENESIS
            for i, e in enumerate(self._entries):
                if e.get("prev") != prev or _hash_entry(e) != e.get("hash"):
                    raise ValueError(f"ledger broken at index {i}")
                prev = e["hash"]
            return len(self._entries), prev

    def records(self) -> list[dict]:
        with self._mu:
            return [dict(e) for e in self._entries]

    def totals_by_tenant(self) -> dict[str, dict[str, Any]]:
        with self._mu:
            out: dict[str, dict[str, Any]] = {}
            for e in self._entries:
                t = e["tenant"]
                rec = out.setdefault(t, {"tenant": t, "calls": 0, "idle_records": 0, "gpu_ms": 0, "gpu_seconds": 0.0, "vram_mib_s": 0})
                if str(e["job"]).startswith("idle:"):
                    rec["idle_records"] += 1
                else:
                    rec["calls"] += 1
                rec["gpu_ms"] += e["gpu_ms"]
                rec["vram_mib_s"] += e["vram_mib_s"]
            for rec in out.values():
                rec["gpu_seconds"] = round(rec["gpu_ms"] / 1000.0, 3)
            return out


class ServerlessAudit:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._mu = threading.Lock()
        self._events: list[dict] = []
        self._seq = 0
        self._last = GENESIS

    def record(self, action: str, *, tenant: str = "-", actor: str = "-", detail: Optional[dict] = None) -> dict:
        safe_detail = {}
        for k, v in (detail or {}).items():
            if k in ("env_vars", "secret", "secrets", "args", "args_b64", "bundle_b64", "result_b64", "api_key"):
                continue
            safe_detail[k] = v
        with self._mu:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "action": action,
                "tenant": tenant,
                "actor": actor,
                "detail": safe_detail,
                "prev": self._last,
                "hash": "",
            }
            raw_no_hash = json.dumps(ev, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ev["hash"] = hashlib.sha256(raw_no_hash).hexdigest()
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                raw = (json.dumps(ev, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, raw)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._events.append(ev)
            if len(self._events) > 1000:
                self._events = self._events[-1000:]
            self._last = str(ev["hash"])
            return dict(ev)

    def recent(self, limit: int = 100, tenant: Optional[str] = None) -> list[dict]:
        with self._mu:
            evs = [dict(e) for e in self._events if not tenant or e["tenant"] == tenant]
            return evs[-limit:]

    @staticmethod
    def verify_file(path: str | Path) -> tuple[int, str]:
        p = Path(path)
        if not p.exists():
            return 0, GENESIS
        prev = GENESIS
        count = 0
        for idx, line in enumerate(p.read_text("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            ev = json.loads(line)
            count += 1
            if ev.get("seq") != count:
                raise ValueError(f"audit sequence mismatch at line {idx}: got {ev.get('seq')}, expected {count}")
            if ev.get("prev") != prev:
                raise ValueError(f"audit prev-hash mismatch at line {idx}")
            copy = dict(ev)
            copy["hash"] = ""
            expected = hashlib.sha256(json.dumps(copy, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
            if ev.get("hash") != expected:
                raise ValueError(f"audit hash mismatch at line {idx}")
            prev = str(ev["hash"])
        return count, prev

