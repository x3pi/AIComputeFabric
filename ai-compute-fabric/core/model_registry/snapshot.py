"""Pinned, verified snapshots of Hugging Face models, and a LAN mirror to hand them to other PCs (plan P1.3).

Layout under a registry root:   <root>/<org>/<name>/<40-hex revision>/{MANIFEST.json, files/...}

take_snapshot():  refuses anything but a full commit SHA (a branch or tag can move), downloads only safe file types,
                  refuses repos that ask for remote code, hashes every file, and records the manifest.
Mirror + pull():  a worker pulls a snapshot from the mirror on the LAN. The caller must supply the manifest's digest
                  (obtained out of band: from the control plane or a signed release); a mirror that serves both the manifest
                  and the files can otherwise make a tampered model look consistent.

Not covered: downloading from private/gated repos (no token handling), Xet/LFS resume after a partial download,
authentication or TLS on the mirror (it is meant for a trusted LAN segment; put it behind mTLS before exposing it further),
signature of the manifest itself (only a digest pinned by the caller)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.model_registry.security import ALLOWED_SUFFIXES, sha256_file, tokenizer_sha256, validate_model_dir

SHA40 = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
ALLOW_PATTERNS = ["*" + s for s in sorted(ALLOWED_SUFFIXES)]


class SnapshotError(Exception):
    pass


def manifest_digest(manifest: dict) -> str:
    """SHA-256 of the manifest content that matters (repo, revision, file hashes, tokenizer); not the creation time."""
    core = {k: manifest[k] for k in ("schema", "repo", "revision", "files", "sizes", "tokenizer_sha256")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot_dir(root: Path, repo: str, revision: str) -> Path:
    if not REPO.match(repo) or ".." in repo:
        raise SnapshotError(f"bad repo id {repo!r}")
    if not SHA40.match(revision):
        raise SnapshotError("revision must be a full 40-hex commit SHA: a branch or tag name can point to different files tomorrow")
    return Path(root) / repo / revision


def _hf_download(repo: str, revision: str, dest: Path):
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=repo, revision=revision, local_dir=str(dest), allow_patterns=ALLOW_PATTERNS)


def build_manifest(repo: str, revision: str, files_dir: Path) -> dict:
    files, sizes = {}, {}
    for p in sorted(files_dir.rglob("*")):
        if p.is_file() and ".cache" not in p.relative_to(files_dir).parts:
            rel = p.relative_to(files_dir).as_posix()
            files[rel], sizes[rel] = sha256_file(p), p.stat().st_size
    tok = files_dir / "tokenizer.json"
    return {"schema": 1, "repo": repo, "revision": revision, "files": files, "sizes": sizes,
            "tokenizer_sha256": tokenizer_sha256(tok) if tok.is_file() else None}


def take_snapshot(root: Path, repo: str, revision: str, downloader=_hf_download) -> dict:
    """Download (via `downloader(repo, revision, dest)`), validate and register a snapshot. Returns the manifest."""
    target = snapshot_dir(root, repo, revision)
    if (target / "MANIFEST.json").exists():
        return verify_snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=target.parent))
    try:
        files = tmp / "files"
        files.mkdir()
        downloader(repo, revision, files)
        shutil.rmtree(files / ".cache", ignore_errors=True)
        m = build_manifest(repo, revision, files)
        if not m["files"]:
            raise SnapshotError("download produced no files")
        bad = validate_model_dir(files, m)
        if bad:
            raise SnapshotError("snapshot refused: " + "; ".join(f"{v.code} {v.path}" for v in bad[:5]))
        (tmp / "MANIFEST.json").write_text(json.dumps(m, indent=1, sort_keys=True))
        os.replace(tmp, target)
        return m
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_manifest(target: Path) -> dict:
    try:
        return json.loads((Path(target) / "MANIFEST.json").read_text())
    except (OSError, ValueError) as e:
        raise SnapshotError(f"no readable MANIFEST.json in {target}: {e}") from e


def verify_snapshot(target: Path) -> dict:
    """Re-hash everything against the manifest; raises SnapshotError on any difference."""
    m = load_manifest(target)
    bad = validate_model_dir(Path(target) / "files", m)
    if bad:
        raise SnapshotError("snapshot failed verification: " + "; ".join(f"{v.code} {v.path}" for v in bad[:5]))
    return m


# ---------------------------------------------------------------- mirror (server) ----
def make_mirror_handler(root: Path):
    root = Path(root).resolve()

    class H(BaseHTTPRequestHandler):
        def _fail(self, code, msg):
            b = json.dumps({"error": msg}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            parts = [urllib.parse.unquote(x) for x in urllib.parse.urlparse(self.path).path.strip("/").split("/")]
            if len(parts) < 4 or not REPO.match("/".join(parts[:2])) or not SHA40.match(parts[2]):
                return self._fail(404, "not found")
            target = (root / parts[0] / parts[1] / parts[2]).resolve()
            if root not in target.parents:
                return self._fail(403, "outside the registry")
            rel = "/".join(parts[3:])
            f = target / "MANIFEST.json" if rel == "MANIFEST.json" else (target / rel).resolve()
            if rel != "MANIFEST.json":
                files = (target / "files").resolve()
                if not rel.startswith("files/") or files not in f.parents:
                    return self._fail(403, "outside the snapshot")
            if not f.is_file():
                return self._fail(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(f.stat().st_size))
            self.end_headers()
            with open(f, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 1 << 20)

        def log_message(self, *a):
            pass

    return H


def serve_mirror(root: Path, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_mirror_handler(root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- mirror (client) ----
def _get(url: str, timeout: float = 60):
    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except OSError as e:
        raise SnapshotError(f"mirror request failed: {url}: {e}") from e


def pull_from_mirror(mirror_url: str, root: Path, repo: str, revision: str, expected_manifest_sha256: str) -> dict:
    """Fetch a snapshot from a LAN mirror into `root`, verifying every byte against a manifest whose digest the caller pinned."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256 or ""):
        raise SnapshotError("expected_manifest_sha256 is required: without it the mirror could vouch for its own files")
    target = snapshot_dir(root, repo, revision)
    if (target / "MANIFEST.json").exists():
        m = verify_snapshot(target)
        if manifest_digest(m) != expected_manifest_sha256:
            raise SnapshotError("local snapshot exists but its manifest digest differs from the pinned one")
        return m
    base = f"{mirror_url.rstrip('/')}/{repo}/{revision}"
    with _get(base + "/MANIFEST.json") as r:
        raw = r.read(8 << 20)
    try:
        m = json.loads(raw)
        got = manifest_digest(m)
    except (ValueError, KeyError, TypeError) as e:
        raise SnapshotError(f"mirror sent an unusable manifest: {e}") from e
    if got != expected_manifest_sha256:
        raise SnapshotError("manifest digest does not match the pinned one: refusing a manifest the mirror made up")
    if m.get("repo") != repo or m.get("revision") != revision:
        raise SnapshotError("manifest is for a different repo/revision")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=target.parent))
    try:
        files = (tmp / "files").resolve()
        files.mkdir()
        for rel, want in m["files"].items():
            dst = (files / rel).resolve()
            if files not in dst.parents:
                raise SnapshotError(f"manifest names a path outside the snapshot: {rel!r}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            h, n = hashlib.sha256(), 0
            with _get(f"{base}/files/{urllib.parse.quote(rel)}") as r, open(dst, "wb") as out:
                while block := r.read(1 << 20):
                    h.update(block)
                    n += len(block)
                    out.write(block)
            if h.hexdigest() != want or n != m["sizes"][rel]:
                raise SnapshotError(f"{rel}: bytes from the mirror do not match the manifest")
        bad = validate_model_dir(files, m)
        if bad:
            raise SnapshotError("pulled snapshot refused: " + "; ".join(f"{v.code} {v.path}" for v in bad[:5]))
        (tmp / "MANIFEST.json").write_text(json.dumps(m, indent=1, sort_keys=True))
        os.replace(tmp, target)
        return m
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
