"""P1.3: pinned snapshots and the LAN mirror. The downloader is replaced by a function that writes local files;
the real Hugging Face download is exercised by benchmarks/reports/p1_model_provenance_qwen3_0p6b.json."""
import json
import urllib.error
import urllib.request

import pytest

from core.model_registry.snapshot import (
    SnapshotError, manifest_digest, pull_from_mirror, serve_mirror, snapshot_dir, take_snapshot, verify_snapshot)

REV = "a" * 40
REPO = "acme/tiny-model"


def good_download(repo, revision, dest):
    (dest / "config.json").write_text(json.dumps({"architectures": ["Qwen3ForCausalLM"], "num_hidden_layers": 2}))
    (dest / "tokenizer.json").write_text(json.dumps({"model": {"vocab": {"a": 0}}, "version": "1"}))
    (dest / "model.safetensors").write_bytes(b"\x01" * 4096)
    (dest / "sub").mkdir()
    (dest / "sub" / "extra.json").write_text("{}")


def test_branch_names_and_short_revisions_are_refused(tmp_path):
    for rev in ("main", "v1.0", "abc123", "A" * 40, "a" * 41):
        with pytest.raises(SnapshotError, match="40-hex"):
            take_snapshot(tmp_path, REPO, rev, good_download)


@pytest.mark.parametrize("repo", ["../evil/x", "noslash", "a/b/c", "a/..", "a/b c"])
def test_bad_repo_ids_are_refused(tmp_path, repo):
    with pytest.raises(SnapshotError):
        snapshot_dir(tmp_path, repo, REV)


def test_snapshot_records_hashes_and_verifies(tmp_path):
    m = take_snapshot(tmp_path, REPO, REV, good_download)
    assert set(m["files"]) == {"config.json", "tokenizer.json", "model.safetensors", "sub/extra.json"}
    assert m["tokenizer_sha256"] and m["sizes"]["model.safetensors"] == 4096
    target = snapshot_dir(tmp_path, REPO, REV)
    assert verify_snapshot(target) == m
    assert take_snapshot(tmp_path, REPO, REV, lambda *a: pytest.fail("must not download twice")) == m
    (target / "files" / "model.safetensors").write_bytes(b"\x02" * 4096)
    with pytest.raises(SnapshotError, match="SHA256_MISMATCH"):
        verify_snapshot(target)


def test_pickle_and_remote_code_repos_are_refused_and_leave_nothing_behind(tmp_path):
    def pickled(repo, revision, dest):
        good_download(repo, revision, dest)
        (dest / "pytorch_model.bin").write_bytes(b"pickle")

    def remote(repo, revision, dest):
        good_download(repo, revision, dest)
        (dest / "config.json").write_text(json.dumps({"auto_map": {"AutoModel": "modeling_x.X"}}))

    for dl, code in ((pickled, "FORBIDDEN_FORMAT"), (remote, "REMOTE_CODE_REQUESTED")):
        with pytest.raises(SnapshotError, match=code):
            take_snapshot(tmp_path, REPO, REV, dl)
        assert not snapshot_dir(tmp_path, REPO, REV).exists()
        assert list((tmp_path / "acme" / "tiny-model").iterdir()) == []        # no half-finished .incoming-* directory either


@pytest.fixture
def mirror(tmp_path):
    src = tmp_path / "src"
    m = take_snapshot(src, REPO, REV, good_download)
    srv = serve_mirror(src)
    yield src, f"http://127.0.0.1:{srv.server_port}", manifest_digest(m), tmp_path / "dst"
    srv.shutdown()


def test_pull_from_mirror_verifies_and_installs(mirror):
    src, url, digest, dst = mirror
    m = pull_from_mirror(url, dst, REPO, REV, digest)
    assert verify_snapshot(snapshot_dir(dst, REPO, REV)) == m


def test_pull_requires_a_pinned_digest_and_rejects_a_different_one(mirror):
    src, url, digest, dst = mirror
    for bad in (None, "", "short", "0" * 64):
        with pytest.raises(SnapshotError):
            pull_from_mirror(url, dst, REPO, REV, bad)
    assert not snapshot_dir(dst, REPO, REV).exists()


def test_a_mirror_that_serves_tampered_bytes_is_caught(mirror):
    src, url, digest, dst = mirror
    (snapshot_dir(src, REPO, REV) / "files" / "model.safetensors").write_bytes(b"\x07" * 4096)
    with pytest.raises(SnapshotError, match="do not match the manifest"):
        pull_from_mirror(url, dst, REPO, REV, digest)
    assert not snapshot_dir(dst, REPO, REV).exists()


def test_a_mirror_that_rewrites_manifest_and_files_together_is_caught_by_the_pin(mirror):
    src, url, digest, dst = mirror
    target = snapshot_dir(src, REPO, REV)
    (target / "files" / "model.safetensors").write_bytes(b"\x09" * 4096)
    from core.model_registry.security import sha256_file
    m = json.loads((target / "MANIFEST.json").read_text())
    m["files"]["model.safetensors"] = sha256_file(target / "files" / "model.safetensors")
    (target / "MANIFEST.json").write_text(json.dumps(m))
    with pytest.raises(SnapshotError, match="digest does not match"):
        pull_from_mirror(url, dst, REPO, REV, digest)


def test_manifest_paths_that_escape_are_refused(mirror):
    src, url, digest, dst = mirror
    target = snapshot_dir(src, REPO, REV)
    m = json.loads((target / "MANIFEST.json").read_text())
    m["files"]["../../escape.json"] = "0" * 64
    m["sizes"]["../../escape.json"] = 1
    (target / "MANIFEST.json").write_text(json.dumps(m))
    with pytest.raises(SnapshotError, match="outside the snapshot"):
        pull_from_mirror(url, dst, REPO, REV, manifest_digest(m))
    assert not (dst.parent / "escape.json").exists()


@pytest.mark.parametrize("path", ["/acme/tiny-model/" + REV + "/files/../../../../etc/passwd", "/acme/tiny-model/main/MANIFEST.json",
                                  "/acme/tiny-model/" + REV + "/secret", "/..%2f..%2fetc/passwd", "/"])
def test_mirror_refuses_traversal_and_unknown_paths(mirror, path):
    src, url, digest, dst = mirror
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + path, timeout=5)
    assert e.value.code in (403, 404)
