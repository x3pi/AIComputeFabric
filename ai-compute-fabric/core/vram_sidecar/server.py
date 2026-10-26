"""HTTP sidecar that turns a GGUF file under --models-root into estimator facts (plan P1.4).

GET /facts?file=<name relative to the models root>  ->  200 {facts} | 400/403/404 {"error": ...}

Only *.gguf files that resolve (symlinks followed) to a path inside the models root are read, and only their header.
Binds to loopback by default; it has no authentication, so it must not be exposed beyond the gateway host."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.vram_sidecar.gguf import GgufError, read_facts


def make_handler(root: Path):
    root = root.resolve()
    cache: dict = {}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path != "/facts":
                return self._send(404, {"error": "not found"})
            name = (parse_qs(u.query).get("file") or [""])[0]
            if not name or not name.lower().endswith(".gguf") or "\x00" in name:
                return self._send(400, {"error": "file must be a .gguf name"})
            try:
                p = (root / name).resolve()
            except (OSError, RuntimeError, ValueError):
                return self._send(400, {"error": "bad file name"})
            if root != p and root not in p.parents:
                return self._send(403, {"error": "file is outside the models root"})
            if not p.is_file():
                return self._send(404, {"error": "no such model file"})
            st = p.stat()
            key = (str(p), st.st_mtime_ns, st.st_size)
            if key not in cache:
                try:
                    cache[key] = read_facts(p).as_dict()
                except GgufError as e:
                    return self._send(422, {"error": f"not a usable GGUF: {e}"})
            self._send(200, cache[key])

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models-root", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18095)
    a = ap.parse_args()
    ThreadingHTTPServer((a.host, a.port), make_handler(Path(a.models_root))).serve_forever()


if __name__ == "__main__":
    main()
