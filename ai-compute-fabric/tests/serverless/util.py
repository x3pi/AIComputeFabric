"""Helpers shared by the serverless tests: build runner frames the way the SDK will (cloudpickle by value)."""
import base64
import hashlib
import sys
import uuid

import cloudpickle


def by_value(module_name: str) -> None:
    """Pickle functions of this test module by value: the sandbox does not have the tests package."""
    cloudpickle.register_pickle_by_value(sys.modules[module_name])


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def bundle_of(obj, kind: str = "function", enter=(), exit=()):
    raw = cloudpickle.dumps({"kind": kind, "obj": obj, "enter": list(enter), "exit": list(exit)})
    return hashlib.sha256(raw).hexdigest(), b64(raw)


def frame(sha: str, *args, method: str = None, result_format: str = "cloudpickle", **kwargs) -> dict:
    f = {"call_id": uuid.uuid4().hex, "sha": sha, "args_b64": b64(cloudpickle.dumps((args, kwargs))), "args_format": "cloudpickle",
         "result_format": result_format}
    if method:
        f["method"] = method
    return f


def value(resp: dict):
    assert resp["ok"], resp
    raw = base64.b64decode(resp["result_b64"])
    return cloudpickle.loads(raw) if resp["format"] == "cloudpickle" else __import__("json").loads(raw)


class RunnerProc:
    """Talks the frame protocol to agents/_warm_runner.py started as a plain subprocess (unit tests only; the production path is Docker)."""

    def __init__(self, extra_env=None):
        import os
        import subprocess
        from pathlib import Path
        runner = Path(__file__).resolve().parents[2] / "agents/_warm_runner.py"
        self.p = subprocess.Popen([sys.executable, "-I", "-B", str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env={**os.environ, **(extra_env or {})})
        self.ready = self.recv()

    def send(self, o: dict) -> None:
        import json
        import struct
        b = json.dumps(o).encode()
        self.p.stdin.write(struct.pack(">I", len(b)) + b)
        self.p.stdin.flush()

    def recv(self) -> dict:
        import json
        import struct
        h = self.p.stdout.read(4)
        assert len(h) == 4, "runner closed the channel"
        return json.loads(self.p.stdout.read(struct.unpack(">I", h)[0]))

    def call(self, sha, bundle=None, *args, method=None, result_format="cloudpickle", **kw) -> dict:
        f = {"op": "call", **frame(sha, *args, method=method, result_format=result_format, **kw)}
        if bundle:
            f["bundle_b64"] = bundle
        self.send(f)
        return self.recv()

    def close(self) -> None:
        try:
            self.send({"op": "shutdown"})
        except Exception:
            pass
        self.p.wait(timeout=10)
