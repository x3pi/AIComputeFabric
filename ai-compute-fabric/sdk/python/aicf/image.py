"""Image = the Python environment a function runs in. Wheels only; hashes are pinned by the builder, never by the client."""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from aicf.errors import InvalidRequest, PythonVersionMismatch

SUPPORTED_PYTHON = "3.12"
MAX_LOCAL_FILES_BYTES = 4 * 1024 * 1024
_HASH_RE = re.compile(r"--hash=sha256:[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class Image:
    python_version: str = SUPPORTED_PYTHON
    packages: tuple = ()
    cuda_tag: str = ""
    env: tuple = ()                  # (("KEY", "value"), ...)
    local_modules: tuple = ()        # names of local modules shipped BY VALUE with the function
    local_files: tuple = ()          # (("rel_path", b64_str), ...)
    requirements_hash: str = ""

    @classmethod
    def debian_slim(cls, python_version: str = SUPPORTED_PYTHON) -> "Image":
        return cls(python_version=python_version)

    def uv_pip_install(self, *packages: str) -> "Image":
        return Image(self.python_version, tuple(self.packages) + packages, self.cuda_tag, self.env,
                     self.local_modules, self.local_files, self.requirements_hash)

    pip_install = uv_pip_install

    def add_python_packages(self, *packages: str | list[str] | tuple[str, ...]) -> "Image":
        """Beam/Beta9-compatible helper: accepts a list (`add_python_packages(['numpy', 'scipy'])`) or varargs."""
        flat: list[str] = []
        for p in packages:
            if isinstance(p, (list, tuple)):
                flat.extend(str(x) for x in p)
            else:
                flat.append(str(p))
        return self.uv_pip_install(*flat)

    def pip_install_from_requirements(self, requirements_txt: str | Path, *, require_hashes: bool = True) -> "Image":
        p = Path(requirements_txt)
        if not p.is_file():
            raise InvalidRequest(f"requirements file not found: {requirements_txt}")
        raw_text = p.read_text("utf-8")
        # Join continuation lines ending with backslash
        joined_lines: list[str] = []
        buf = ""
        for raw_ln in raw_text.splitlines():
            ln = raw_ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ln.endswith("\\"):
                buf += ln[:-1].strip() + " "
            else:
                joined_lines.append((buf + ln).strip())
                buf = ""
        if buf:
            joined_lines.append(buf.strip())
        if not joined_lines:
            raise InvalidRequest(f"requirements file {requirements_txt} contains no package specifications")

        pkgs: list[str] = []
        for entry in joined_lines:
            if entry.startswith(("-i", "--index-url", "--extra-index-url", "--find-links", "-f", "git+", "http://", "https://")):
                raise InvalidRequest(f"custom index URLs or VCS requirements are forbidden in requirements: {entry!r}")
            if require_hashes and not _HASH_RE.search(entry):
                raise InvalidRequest(f"pip_install_from_requirements requires --hash=sha256:... on every requirement (missing on {entry!r})")
            spec_part = entry.split("--hash=")[0].strip()
            if ";" in spec_part:
                spec_part = spec_part.split(";")[0].strip()
            if not spec_part:
                raise InvalidRequest(f"invalid requirement line: {entry!r}")
            pkgs.append(spec_part)
        req_hash = hashlib.sha256("\n".join(joined_lines).encode("utf-8")).hexdigest()[:16]
        return Image(self.python_version, tuple(self.packages) + tuple(pkgs), self.cuda_tag, self.env,
                     self.local_modules, self.local_files, req_hash)

    def with_cuda(self, tag: str) -> "Image":
        return Image(self.python_version, self.packages, tag, self.env, self.local_modules, self.local_files, self.requirements_hash)

    def env_vars(self, mapping: dict) -> "Image":
        return Image(self.python_version, self.packages, self.cuda_tag, tuple(self.env) + tuple(sorted(mapping.items())),
                     self.local_modules, self.local_files, self.requirements_hash)

    def add_local_python_source(self, *modules: str) -> "Image":
        """Ship these local modules by value (cloudpickle), so the sandbox needs no access to your filesystem."""
        return Image(self.python_version, self.packages, self.cuda_tag, self.env,
                     tuple(self.local_modules) + modules, self.local_files, self.requirements_hash)

    def add_local_file(self, local_path: str | Path, remote_path: str | None = None) -> "Image":
        src = Path(local_path)
        if os.path.islink(src) or not src.is_file():
            raise InvalidRequest(f"add_local_file requires a regular non-symlink file: {local_path}")
        raw = src.read_bytes()
        dst = remote_path or src.name
        clean = posixpath.normpath(dst.replace("\\", "/"))
        if ".." in clean.split("/"):
            raise InvalidRequest(f"remote_path must not contain '..': {remote_path!r}")
        return self._with_files([(dst, raw)])

    def add_local_dir(self, local_dir: str | Path, remote_dir: str = "") -> "Image":
        src_dir = Path(local_dir)
        if os.path.islink(src_dir) or not src_dir.is_dir():
            raise InvalidRequest(f"add_local_dir requires a regular non-symlink directory: {local_dir}")
        skip_dirs = {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", "node_modules"}
        pairs: list[tuple[str, bytes]] = []
        for root, dirnames, filenames in os.walk(src_dir, followlinks=False):
            dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs and not os.path.islink(os.path.join(root, d))]
            for fn in sorted(filenames):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                fp = Path(root) / fn
                if os.path.islink(fp):
                    raise InvalidRequest(f"symlinks inside add_local_dir are forbidden: {fp}")
                rel = fp.relative_to(src_dir).as_posix()
                dst = posixpath.join(remote_dir, rel) if remote_dir else rel
                pairs.append((dst, fp.read_bytes()))
        return self._with_files(pairs)

    def _with_files(self, pairs: list[tuple[str, bytes]]) -> "Image":
        existing = dict(self.local_files)
        for dst, raw in pairs:
            existing[dst] = base64.b64encode(raw).decode("ascii")
        total = sum(len(base64.b64decode(v)) for v in existing.values())
        if total > MAX_LOCAL_FILES_BYTES:
            raise InvalidRequest(f"local files exceed {MAX_LOCAL_FILES_BYTES // 1024} KiB limit ({total // 1024} KiB); use aicf.Volume instead")
        return Image(self.python_version, self.packages, self.cuda_tag, self.env,
                     self.local_modules, tuple(sorted(existing.items())), self.requirements_hash)

    def files_dict(self) -> dict[str, str]:
        return dict(self.local_files)

    def spec(self) -> dict:
        parts = list(self.local_modules)
        if self.local_files:
            for k, v in self.local_files:
                parts.append(f"file:{k}:{hashlib.sha256(v.encode()).hexdigest()[:16]}")
        if self.requirements_hash:
            parts.append(f"req:{self.requirements_hash}")
        h = hashlib.sha256("\n".join(parts).encode()).hexdigest() if parts else ""
        return {"python_version": self.python_version, "packages": list(self.packages), "cuda_tag": self.cuda_tag, "local_source_hash": h}

    def check_local_python(self) -> None:
        local = os.environ.get("AICF_LOCAL_PYTHON_OVERRIDE") or f"{sys.version_info.major}.{sys.version_info.minor}"
        if local != self.python_version:
            raise PythonVersionMismatch(
                f"your local Python is {local} but the image uses {self.python_version}. Functions are shipped as pickles and need the same minor "
                f"version: run with Python {self.python_version} (e.g. `uv run --python {self.python_version} ...`).")
