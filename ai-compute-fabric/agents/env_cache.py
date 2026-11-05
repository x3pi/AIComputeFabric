"""Python environment cache for the serverless runtime (plan Pha 1, ADR-003 D4/D5/D-STORE).

An environment is a venv built from *validated package requirements* inside a throw-away builder container (never on
the host: an sdist runs setup.py = arbitrary code). Rules:
  * requirements are name + optional extras + version specifiers only: no URLs, paths, VCS, options, markers;
  * `uv pip compile --generate-hashes --only-binary :all:` then `uv pip install --require-hashes`: wheels only, hash pinned;
  * the venv is created by the interpreter of the SAME image the runtime uses (glibc, python:3.12-slim), so the ABI matches;
  * the uv cache and all envs live under ONE mount (`/store/cache`, `/store/envs/<env_hash>`) so uv can hardlink wheels:
    one more env costs kilobytes, not hundreds of MB (the host disk is tight);
  * a finished env is immutable and mounted `:ro` into runtime containers.
`env_hash = SHA-256(canonical json of base image, python, cuda tag, normalised packages, local source hash)`."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IMAGE = "python:3.12-slim"
ALLOWED_PYTHON = {"3.12": DEFAULT_IMAGE}
# Always present in every env: the runner needs it to (de)serialise functions. Part of the hash.
RUNTIME_PACKAGES = ("cloudpickle==3.1.2",)

_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?"
_EXTRAS = r"(?:\[[A-Za-z0-9](?:[A-Za-z0-9._,-]{0,62}[A-Za-z0-9])?\])?"
_OP = r"(?:==|>=|<=|~=|!=|<|>)"
_VER = r"[A-Za-z0-9](?:[A-Za-z0-9.*+!_-]{0,40})"
_REQ = re.compile(rf"^{_NAME}{_EXTRAS}(?:{_OP}{_VER}(?:,{_OP}{_VER})*)?$")
_CUDA_TAG = re.compile(r"^(?:|cu[0-9]{2,4}|cpu)$")


class EnvError(RuntimeError):
    """The environment could not be validated or built (message is safe to show the tenant)."""


def normalize_requirement(req: str) -> str:
    """Return the canonical form of one requirement or raise EnvError. No whitespace, URLs, paths, options or markers."""
    if not isinstance(req, str) or not _REQ.match(req):
        raise EnvError(f"invalid package requirement {req!r}: only 'name[extras]==version' style specifiers are allowed")
    m = re.match(rf"^({_NAME})({_EXTRAS})(.*)$", req)
    name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
    return f"{name}{m.group(2).lower()}{m.group(3)}"


@dataclass(frozen=True)
class EnvSpec:
    packages: tuple = ()
    python_version: str = "3.12"
    cuda_tag: str = ""            # part of the hash so a cu130 and a cpu env never collide
    local_source_hash: str = ""   # sha256 of shipped local python sources (part of the hash, shipped by value in the bundle)

    def image(self) -> str:
        if self.python_version not in ALLOWED_PYTHON:
            raise EnvError(f"python_version {self.python_version!r} is not available (allowed: {sorted(ALLOWED_PYTHON)})")
        return ALLOWED_PYTHON[self.python_version]

    def normalized(self) -> tuple:
        if len(self.packages) > 64:
            raise EnvError("too many packages (max 64)")
        if not _CUDA_TAG.match(self.cuda_tag):
            raise EnvError(f"invalid cuda_tag {self.cuda_tag!r}")
        if self.local_source_hash and not re.fullmatch(r"[0-9a-f]{64}", self.local_source_hash):
            raise EnvError("invalid local_source_hash")
        pk = sorted({normalize_requirement(p) for p in (*RUNTIME_PACKAGES, *self.packages)})
        return tuple(pk)

    def env_hash(self) -> str:
        doc = {"image": self.image(), "python": self.python_version, "cuda": self.cuda_tag, "packages": list(self.normalized()),
               "local": self.local_source_hash}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class EnvCache:
    store: Path
    uv: str = field(default_factory=lambda: shutil.which("uv") or str(Path.home() / ".local/bin/uv"))
    index_url: str = ""                  # operator-configured mirror/allowlisted index; empty = uv default
    min_free_bytes: int = 3 * 1024**3    # refuse to build when the disk is this tight (host runs at ~98 %)
    build_timeout_s: float = 600.0
    max_bytes: int = 20 * 1024**3
    builder_image: str = DEFAULT_IMAGE

    def __post_init__(self) -> None:
        self.store = Path(self.store).resolve()
        for d in ("cache", "envs", "locks"):
            (self.store / d).mkdir(parents=True, exist_ok=True)
        (self.store / "cache").chmod(0o777)     # written by uid 65534 inside the builder
        (self.store / "envs").chmod(0o777)
        self.store.chmod(0o755)

    # ---- layout ----------------------------------------------------------------------------------------------
    def env_dir(self, env_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", env_hash):
            raise EnvError("invalid env_hash")
        return self.store / "envs" / env_hash

    def is_ready(self, env_hash: str) -> bool:
        return (self.env_dir(env_hash) / ".ready").exists()

    def python_path(self) -> str:
        return "/env/venv/bin/python"       # path of the interpreter INSIDE a runtime container that mounts env_dir at /env

    # ---- build -----------------------------------------------------------------------------------------------
    def ensure(self, spec: EnvSpec) -> tuple[Path, bool]:
        """Return (env_dir, built_now). Concurrent callers for the same hash serialise on a file lock; the first builds."""
        h = spec.env_hash()
        pk = spec.normalized()
        image = spec.image()
        final = self.env_dir(h)
        if (final / ".ready").exists():
            os.utime(final / ".ready")
            return final, False
        lock = open(self.store / "locks" / f"{h}.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (final / ".ready").exists():
                os.utime(final / ".ready")
                return final, False
            free = shutil.disk_usage(self.store).free
            if free < self.min_free_bytes:
                raise EnvError(f"not enough free disk to build an environment ({free // 2**20} MiB free, need {self.min_free_bytes // 2**20})")
            tmp = self.store / "envs" / f"{h}.tmp-{uuid.uuid4().hex[:8]}"
            tmp.mkdir()
            tmp.chmod(0o777)
            try:
                self._build(image, pk, tmp.name)
                (tmp / "spec.json").write_text(json.dumps({"packages": list(pk), "python": spec.python_version, "cuda": spec.cuda_tag,
                                                            "image": image, "env_hash": h, "built": time.time()}))
                (tmp / ".ready").write_text("ok")
                if final.exists():
                    self._rm(final.name)
                os.rename(tmp, final)
            except BaseException:
                self._rm(tmp.name)
                raise
            return final, True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _builder_cmd(self, name: str, shell: str) -> list:
        return ["docker", "run", "--rm", "--name", name, "--label", "aicf.sl=1", "--label", "aicf.role=builder",
                "--cap-drop", "ALL", "--user", "65534:65534", "--read-only", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256",
                "--cpus", "2", "--tmpfs", "/tmp:size=512m", "-e", "HOME=/tmp", "-e", "UV_CACHE_DIR=/store/cache", "-e", "UV_LINK_MODE=hardlink",
                *(["-e", f"UV_DEFAULT_INDEX={self.index_url}"] if self.index_url else []),
                "-v", f"{self.uv}:/usr/local/bin/uv:ro", "-v", f"{self.store}:/store", self.builder_image, "sh", "-c", shell]

    def _build(self, image: str, packages: tuple, tmp_name: str) -> None:
        reqs = "\n".join(packages) + "\n"
        out = f"/store/envs/{tmp_name}"
        # requirements are passed through stdin-free here-doc built from VALIDATED strings (regex above has no shell metacharacters)
        shell = (f"set -e; printf '%s' '{reqs}' > {out}/requirements.in; "
                 f"uv venv -q {out}/venv; "
                 f"uv pip compile -q --generate-hashes --only-binary :all: --python {out}/venv/bin/python -o {out}/requirements.lock {out}/requirements.in; "
                 f"uv pip install -q --python {out}/venv/bin/python --only-binary :all: --require-hashes -r {out}/requirements.lock")
        if re.search(r"[^A-Za-z0-9._\[\],=<>!~*+\n-]", reqs):
            raise EnvError("internal: unsafe characters in requirements")   # defence in depth; the regex already forbids them
        name = "aicf-sl-b-" + uuid.uuid4().hex[:10]
        try:
            p = subprocess.run(self._builder_cmd(name, shell), capture_output=True, text=True, timeout=self.build_timeout_s)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            raise EnvError(f"environment build timed out after {self.build_timeout_s}s") from None
        if p.returncode != 0:
            msg = " ".join(p.stderr.split())[-400:]
            raise EnvError(f"environment build failed (wheels only, hashes required): {msg}")

    def _rm(self, name: str) -> None:
        """Delete envs/<name>. Files belong to uid 65534, so deletion runs as that uid inside a container."""
        if "/" in name or name in ("", ".", ".."):
            raise EnvError("internal: bad path")
        subprocess.run(["docker", "run", "--rm", "--label", "aicf.sl=1", "--user", "65534:65534", "--network", "none", "-v",
                        f"{self.store / 'envs'}:/e", self.builder_image, "rm", "-rf", f"/e/{name}"], capture_output=True, timeout=120)

    # ---- housekeeping ----------------------------------------------------------------------------------------
    def size_bytes(self) -> int:
        seen, total = set(), 0          # hardlinked wheels (cache <-> envs) are counted once
        for f in self.store.rglob("*"):
            if f.is_file() and not f.is_symlink():
                st = f.stat()
                if (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    total += st.st_size
        return total

    def gc(self, in_use: set = frozenset()) -> list:
        """Remove least-recently-used envs until the store is under max_bytes; never touches envs in `in_use`."""
        removed = []
        envs = sorted((p for p in (self.store / "envs").iterdir() if (p / ".ready").exists() and p.name not in in_use),
                      key=lambda p: (p / ".ready").stat().st_mtime)
        while envs and self.size_bytes() > self.max_bytes:
            victim = envs.pop(0)
            self._rm(victim.name)
            removed.append(victim.name)
        return removed
