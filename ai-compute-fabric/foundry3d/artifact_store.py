"""Kho hiện vật SHA-256 trên /mnt/Data/aicf-3d-foundry/artifacts/ với cô lập tenant & chống symlink escape (D-3D-1, D-3D-2)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterable

from foundry3d.schemas import StageArtifact

DEFAULT_FOUNDRY_ROOT = Path(os.environ.get("AICF_FOUNDRY3D_ROOT", "/mnt/Data/aicf-3d-foundry"))
DEFAULT_MAX_STORE_BYTES = 25 * 1024 * 1024 * 1024  # Trần 25 GiB theo Quy tắc 3

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ArtifactSecurityError(PermissionError):
    """Vi phạm bảo mật đường dẫn / symlink escape hoặc truy cập chéo tenant."""


class ArtifactQuotaError(RuntimeError):
    """Kho hiện vật vượt trần dung lượng cho phép."""


class ArtifactNotFoundError(FileNotFoundError):
    """Không tìm thấy hiện vật hoặc job không thuộc tenant."""


def _assert_safe_token(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not value or not pattern.match(value) or ".." in value:
        raise ArtifactSecurityError(f"{label} không hợp lệ: {value!r}")
    return value


def _assert_no_symlink_in_tree(path: Path, root: Path) -> None:
    """Từ chối mọi symlink từ root tới path (bao gồm chính tệp đích)."""
    if path.is_symlink():
        raise ArtifactSecurityError(f"Từ chối symlink không tin cậy: {path}")
    root_res = root.resolve(strict=True)
    # Kiểm tra từng thành phần từ path ngược lên tới root
    cur = path
    while True:
        if cur.is_symlink():
            raise ArtifactSecurityError(f"Từ chối symlink trong đường dẫn: {cur}")
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root_res):
        raise ArtifactSecurityError(f"Đường dẫn thoát khỏi sandbox {root_res}: {resolved}")


class ArtifactStore:
    """Quản lý lưu trữ hiện vật 3D theo mã băm SHA-256, tách biệt theo tenant."""

    def __init__(
        self,
        root: Path | str | None = None,
        max_total_bytes: int = DEFAULT_MAX_STORE_BYTES,
    ) -> None:
        base = Path(root) if root is not None else DEFAULT_FOUNDRY_ROOT / "artifacts"
        self.root = base.resolve()
        self.blobs_dir = self.root / "blobs"
        self.tenants_dir = self.root / "tenants"
        self.max_total_bytes = int(max_total_bytes)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tenants_dir.mkdir(parents=True, exist_ok=True)

    def total_size_bytes(self) -> int:
        total = 0
        if not self.blobs_dir.exists():
            return 0
        for p in self.blobs_dir.rglob("*"):
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        return total

    def _manifest_path(self, tenant_id: str, job_id: str) -> Path:
        tid = _assert_safe_token(tenant_id, SAFE_ID_RE, "tenant_id")
        jid = _assert_safe_token(job_id, SAFE_ID_RE, "job_id")
        return self.tenants_dir / tid / jid / "manifest.json"

    def _load_manifest(self, tenant_id: str, job_id: str) -> dict[str, dict]:
        mp = self._manifest_path(tenant_id, job_id)
        if not mp.exists():
            return {}
        return json.loads(mp.read_text(encoding="utf-8"))

    def _save_manifest(self, tenant_id: str, job_id: str, data: dict[str, dict]) -> None:
        mp = self._manifest_path(tenant_id, job_id)
        mp.parent.mkdir(parents=True, exist_ok=True)
        tmp = mp.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(mp)

    def put_file(
        self,
        tenant_id: str,
        job_id: str,
        src_path: Path | str,
        *,
        work_root: Path | str | None = None,
        filename: str | None = None,
        mime_type: str = "application/octet-stream",
        stage: str = "",
    ) -> StageArtifact:
        """Kiểm tra an toàn đường dẫn, băm SHA-256 và thu hồi tệp vào ArtifactStore."""
        src = Path(src_path)
        fname = _assert_safe_token(filename or src.name, SAFE_FILENAME_RE, "filename")
        if src.is_symlink():
            raise ArtifactSecurityError(f"Từ chối thu hồi symlink: {src}")
        if work_root is not None:
            _assert_no_symlink_in_tree(src, Path(work_root))
        elif not src.exists():
            raise ArtifactNotFoundError(f"Không tìm thấy tệp nguồn: {src}")

        if not src.is_file():
            raise ArtifactSecurityError(f"Nguồn không phải tệp thông thường: {src}")

        h = hashlib.sha256()
        size = 0
        with src.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        digest = h.hexdigest()
        uri = f"sha256:{digest}"

        blob_path = self.blobs_dir / digest[:2] / digest
        if not blob_path.exists():
            used = self.total_size_bytes()
            if used + size > self.max_total_bytes:
                raise ArtifactQuotaError(
                    f"ArtifactStore vượt trần dung lượng ({used + size} > {self.max_total_bytes} bytes)"
                )
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_blob = blob_path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
            shutil.copyfile(src, tmp_blob)
            tmp_blob.replace(blob_path)

        art = StageArtifact(
            filename=fname,
            uri=uri,
            size_bytes=size,
            mime_type=mime_type,
            stage=stage,
        )
        manifest = self._load_manifest(tenant_id, job_id)
        manifest[fname] = art.model_dump()
        manifest[fname]["updated_at"] = time.time()
        self._save_manifest(tenant_id, job_id, manifest)
        return art

    def put_bytes(
        self,
        tenant_id: str,
        job_id: str,
        filename: str,
        data: bytes,
        *,
        mime_type: str = "application/octet-stream",
        stage: str = "",
    ) -> StageArtifact:
        fname = _assert_safe_token(filename, SAFE_FILENAME_RE, "filename")
        digest = hashlib.sha256(data).hexdigest()
        uri = f"sha256:{digest}"
        blob_path = self.blobs_dir / digest[:2] / digest
        if not blob_path.exists():
            used = self.total_size_bytes()
            if used + len(data) > self.max_total_bytes:
                raise ArtifactQuotaError(
                    f"ArtifactStore vượt trần dung lượng ({used + len(data)} > {self.max_total_bytes} bytes)"
                )
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_blob = blob_path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
            tmp_blob.write_bytes(data)
            tmp_blob.replace(blob_path)

        art = StageArtifact(
            filename=fname,
            uri=uri,
            size_bytes=len(data),
            mime_type=mime_type,
            stage=stage,
        )
        manifest = self._load_manifest(tenant_id, job_id)
        manifest[fname] = art.model_dump()
        manifest[fname]["updated_at"] = time.time()
        self._save_manifest(tenant_id, job_id, manifest)
        return art

    def list_artifacts(self, tenant_id: str, job_id: str) -> list[StageArtifact]:
        manifest = self._load_manifest(tenant_id, job_id)
        out: list[StageArtifact] = []
        for entry in manifest.values():
            clean = {k: v for k, v in entry.items() if k in StageArtifact.model_fields}
            out.append(StageArtifact(**clean))
        return out

    def get_artifact_path(self, tenant_id: str, job_id: str, filename: str) -> tuple[Path, StageArtifact]:
        fname = _assert_safe_token(filename, SAFE_FILENAME_RE, "filename")
        manifest = self._load_manifest(tenant_id, job_id)
        if fname not in manifest:
            raise ArtifactNotFoundError(f"Tenant {tenant_id!r} không có hiện vật {fname!r} cho job {job_id!r}")
        entry = {k: v for k, v in manifest[fname].items() if k in StageArtifact.model_fields}
        art = StageArtifact(**entry)
        digest = art.uri.split(":", 1)[1]
        blob_path = self.blobs_dir / digest[:2] / digest
        if not blob_path.exists() or blob_path.is_symlink():
            raise ArtifactNotFoundError(f"Thiếu blob {art.uri}")
        return blob_path, art

    def cleanup_intermediate(self, tenant_id: str, job_id: str, keep_filenames: Iterable[str]) -> int:
        """Xóa các mục trung gian khỏi manifest job và dọn blob mồ côi nếu không còn tham chiếu."""
        keep = set(keep_filenames)
        manifest = self._load_manifest(tenant_id, job_id)
        if not manifest:
            return 0
        removed_uris: set[str] = set()
        new_manifest: dict[str, dict] = {}
        removed_count = 0
        for fname, entry in manifest.items():
            if fname in keep:
                new_manifest[fname] = entry
            else:
                removed_uris.add(entry.get("uri", ""))
                removed_count += 1
        if removed_count == 0:
            return 0
        self._save_manifest(tenant_id, job_id, new_manifest)
        self._gc_unreferenced_blobs(removed_uris)
        return removed_count

    def _gc_unreferenced_blobs(self, candidate_uris: Iterable[str]) -> int:
        referenced: set[str] = set()
        for mp in self.tenants_dir.rglob("manifest.json"):
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
                for entry in data.values():
                    u = entry.get("uri")
                    if u:
                        referenced.add(u)
            except Exception:
                continue
        deleted = 0
        for uri in candidate_uris:
            if not uri or uri in referenced or not uri.startswith("sha256:"):
                continue
            digest = uri.split(":", 1)[1]
            blob = self.blobs_dir / digest[:2] / digest
            if blob.exists() and not blob.is_symlink():
                blob.unlink(missing_ok=True)
                deleted += 1
        return deleted

