"""P1.4: the sidecar serves facts only for .gguf files inside its root."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from core.vram_sidecar.server import make_handler
from tests.unit.test_gguf_facts import build


@pytest.fixture
def srv(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    build(root / "m.gguf")
    (tmp_path / "secret.gguf").write_bytes((root / "m.gguf").read_bytes())
    (root / "link.gguf").symlink_to(tmp_path / "secret.gguf")
    (root / "notes.txt").write_text("x")
    (root / "junk.gguf").write_bytes(b"GGUFgarbage")
    s = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def get(base, q):
    try:
        with urllib.request.urlopen(f"{base}/facts?file={q}", timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_serves_facts_for_a_file_in_the_root(srv):
    st, body = get(srv, "m.gguf")
    assert st == 200 and body["layers"] == 4 and body["n_kv"] == 2


@pytest.mark.parametrize("q,code", [
    ("..%2Fsecret.gguf", 403), ("%2Fetc%2Fpasswd.gguf", 403), ("link.gguf", 403),     # traversal, absolute path, symlink out of the root
    ("notes.txt", 400), ("", 400), ("absent.gguf", 404), ("junk.gguf", 422),
])
def test_refusals(srv, q, code):
    assert get(srv, q)[0] == code
