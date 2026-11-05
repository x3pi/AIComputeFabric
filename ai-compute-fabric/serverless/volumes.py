"""Tenant-partitioned Volume and read-only ModelCache storage (ADR-003 D7, threat T11).

* Every Volume operation is strictly partitioned under `<root>/<tenant_id>/<vol_name>/<norm_path>`.
* Relative paths are normalized with `posixpath.normpath`; empty, `.`, `..`, leading `/`, null bytes, and any symlink component
  anywhere along the path are rejected with 403 `volume_path_denied` and recorded in the audit log.
* Per-tenant total bytes across all volumes are capped by `policy.max_volume_bytes`.
* `ModelCache` is read-only for tenants; only administrators may populate it.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class VolumeError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


class VolumeStore:
    def __init__(self, root: Path, on_denied: Optional[Callable[[str, str, str, str], Any]] = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self.on_denied = on_denied

    def _deny(self, tenant: str, vol: str, rel_path: str, reason: str, status: int = 403, code: str = "volume_path_denied") -> None:
        if self.on_denied:
            self.on_denied(tenant, vol, rel_path, reason)
        raise VolumeError(status, code, reason)

    def _validate_ident(self, tenant: str, vol: str) -> None:
        if not IDENT.match(tenant or ""):
            self._deny(tenant or "-", vol or "-", "", "invalid tenant identifier", 400, "bad_request")
        if not IDENT.match(vol or ""):
            self._deny(tenant, vol or "-", "", f"volume name {vol!r} must match {IDENT.pattern}", 400, "bad_request")

    def _check_no_symlinks(self, tenant: str, vol: str, rel_path: str, base: Path, target: Path) -> None:
        cur = base
        if cur.is_symlink():
            self._deny(tenant, vol, rel_path, "symlink detected on volume root")
        rel_parts = target.relative_to(base).parts if target != base else ()
        for part in rel_parts:
            cur = cur / part
            if os.path.islink(cur):
                self._deny(tenant, vol, rel_path, f"symlink component {part!r} is forbidden in Volume paths")
        resolved = target.resolve(strict=False)
        base_resolved = base.resolve(strict=False)
        if resolved != base_resolved and base_resolved not in resolved.parents:
            self._deny(tenant, vol, rel_path, "path escapes tenant volume root")

    def _resolve_vol_dir(self, tenant: str, vol: str) -> Path:
        self._validate_ident(tenant, vol)
        tenant_dir = self.root / tenant
        vol_dir = tenant_dir / vol
        for p in (tenant_dir, vol_dir):
            if os.path.islink(p):
                self._deny(tenant, vol, "", "symlink detected on tenant/volume directory")
        return vol_dir

    def _resolve_file(self, tenant: str, vol: str, rel_path: str) -> tuple[Path, str]:
        vol_dir = self._resolve_vol_dir(tenant, vol)
        if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path or len(rel_path) > 256:
            self._deny(tenant, vol, str(rel_path), "invalid volume file path", 400, "bad_request")
        raw_parts = rel_path.replace("\\", "/").split("/")
        if rel_path.startswith("/") or ".." in raw_parts:
            self._deny(tenant, vol, rel_path, "relative path without '..' is required")
        norm = posixpath.normpath(rel_path.replace("\\", "/")).lstrip("/")
        if not norm or norm in (".", "..") or norm.startswith("../") or "/../" in f"/{norm}/":
            self._deny(tenant, vol, rel_path, "path traversal refused")
        target = vol_dir / norm
        self._check_no_symlinks(tenant, vol, rel_path, vol_dir, target)
        return target, norm

    def tenant_bytes(self, tenant: str) -> int:
        if not IDENT.match(tenant or ""):
            return 0
        tdir = self.root / tenant
        if not tdir.exists() or os.path.islink(tdir):
            return 0
        total = 0
        for dirpath, dirnames, filenames in os.walk(tdir, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                if not os.path.islink(fp) and not fn.endswith(".tmp"):
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        return total

    def put(self, tenant: str, vol: str, rel_path: str, data: bytes, max_volume_bytes: int = 256 * 1024 * 1024,
            expected_sha256: Optional[str] = None) -> dict:
        if not isinstance(data, (bytes, bytearray)):
            raise VolumeError(400, "bad_request", "volume payload must be bytes")
        raw = bytes(data)
        digest = _sha256(raw)
        if expected_sha256 and expected_sha256.lower() != digest:
            raise VolumeError(400, "checksum_mismatch", f"sha256 mismatch: expected {expected_sha256}, got {digest}")
        with self._mu:
            target, norm = self._resolve_file(tenant, vol, rel_path)
            old_size = target.stat().st_size if target.exists() and not os.path.islink(target) else 0
            used = self.tenant_bytes(tenant)
            if used - old_size + len(raw) > max_volume_bytes:
                self._deny(tenant, vol, norm,
                           f"volume quota exceeded: {used - old_size + len(raw)} bytes > max_volume_bytes={max_volume_bytes}",
                           413, "volume_quota_exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            self._check_no_symlinks(tenant, vol, rel_path, self._resolve_vol_dir(tenant, vol), target)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                tmp.write_bytes(raw)
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return {"volume": vol, "path": norm, "size": len(raw), "sha256": digest}

    def get(self, tenant: str, vol: str, rel_path: str) -> tuple[bytes, str]:
        with self._mu:
            target, norm = self._resolve_file(tenant, vol, rel_path)
            if not target.is_file():
                raise VolumeError(404, "not_found", f"{vol}/{norm} not found")
            data = target.read_bytes()
            return data, _sha256(data)

    def delete(self, tenant: str, vol: str, rel_path: str) -> bool:
        with self._mu:
            target, _ = self._resolve_file(tenant, vol, rel_path)
            if not target.is_file():
                return False
            target.unlink()
            return True

    def ls(self, tenant: str, vol: str) -> list[dict]:
        with self._mu:
            vol_dir = self._resolve_vol_dir(tenant, vol)
            if not vol_dir.exists():
                return []
            items = []
            for dirpath, dirnames, filenames in os.walk(vol_dir, followlinks=False):
                for d in list(dirnames):
                    if os.path.islink(os.path.join(dirpath, d)):
                        self._deny(tenant, vol, d, "symlink directory detected in volume")
                for fn in sorted(filenames):
                    if fn.startswith(".") and fn.endswith(".tmp"):
                        continue
                    fp = Path(dirpath) / fn
                    if os.path.islink(fp):
                        self._deny(tenant, vol, fn, "symlink file detected in volume")
                    rel = fp.relative_to(vol_dir).as_posix()
                    raw = fp.read_bytes()
                    items.append({"path": rel, "size": len(raw), "sha256": _sha256(raw)})
            items.sort(key=lambda x: str(x["path"]))
            return items


class ModelCacheStore:
    """Admin-populated, tenant-read-only model weights/artifacts cache (ADR-003 D7)."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, model: str, rel_path: str = "") -> Path:
        if not IDENT.match(model or ""):
            raise VolumeError(400, "bad_request", f"invalid model cache name {model!r}")
        mdir = self.root / model
        if os.path.islink(mdir):
            raise VolumeError(403, "model_cache_denied", "symlink on model cache directory")
        if not rel_path:
            return mdir
        if rel_path.startswith("/") or ".." in rel_path.replace("\\", "/").split("/"):
            raise VolumeError(403, "model_cache_denied", "path traversal refused")
        norm = posixpath.normpath(rel_path).lstrip("/")
        target = mdir / norm
        if os.path.islink(target):
            raise VolumeError(403, "model_cache_denied", "symlink on model cache file")
        resolved = target.resolve(strict=False)
        base_res = mdir.resolve(strict=False)
        if resolved != base_res and base_res not in resolved.parents:
            raise VolumeError(403, "model_cache_denied", "path escapes model cache")
        return target

    def put_admin(self, role: str, model: str, rel_path: str, data: bytes) -> dict:
        if role != "admin":
            raise VolumeError(403, "model_cache_readonly", "ModelCache is read-only for tenants; only administrators may populate it")
        target = self._resolve(model, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = bytes(data)
        target.write_bytes(raw)
        return {"model": model, "path": target.relative_to(self.root / model).as_posix(), "size": len(raw), "sha256": _sha256(raw)}

    def get(self, model: str, rel_path: str) -> tuple[bytes, str]:
        target = self._resolve(model, rel_path)
        if not target.is_file():
            raise VolumeError(404, "not_found", f"model cache {model}/{rel_path} not found")
        raw = target.read_bytes()
        return raw, _sha256(raw)

    def ls(self, model: Optional[str] = None) -> list[dict]:
        if model is None:
            out = []
            for p in sorted(self.root.iterdir()):
                if p.is_dir() and not os.path.islink(p) and IDENT.match(p.name):
                    out.append({"model": p.name, "files": len(self.ls(p.name))})
            return out
        mdir = self._resolve(model)
        if not mdir.exists():
            return []
        files = []
        for fp in sorted(mdir.rglob("*")):
            if fp.is_file() and not os.path.islink(fp):
                raw = fp.read_bytes()
                files.append({"path": fp.relative_to(mdir).as_posix(), "size": len(raw), "sha256": _sha256(raw)})
        return files

