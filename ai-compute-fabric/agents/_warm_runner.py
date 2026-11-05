"""Warm runner: runs INSIDE the sandbox container (python -I -B /aicf/runner.py). The ONLY place that calls cloudpickle.loads.

Protocol (over the container's stdin/stdout, length-prefixed frames, big-endian uint32 length + JSON):
  request  {"op":"ping"} | {"op":"shutdown"} |
           {"op":"call","call_id","sha","bundle_b64"?,"method"?,"args_b64","args_format":"cloudpickle"|"json",
            "result_format":"cloudpickle"|"json","max_output_bytes"?}
  response {"ok":true,"result_b64","format","stdout","stderr","truncated","exec_ms","loaded","enter_ms"}
        or {"ok":false,"error":{"type","message","traceback"},"stdout","stderr", ...}
The protocol channel is a private dup of the original fd 0/1; fd 0/1 are re-pointed at /dev/null so user code (and C
extensions) can neither read requests nor corrupt responses. User print() is captured into the response."""
import base64
import hashlib
import io
import json
import os
import struct
import sys
import time
import traceback

MAX_FRAME = int(os.environ.get("AICF_MAX_FRAME", str(16 * 1024 * 1024)))
VRAM_MB = int(os.environ.get("AICF_VRAM_MB", "0") or 0)

_in = os.fdopen(os.dup(0), "rb", buffering=0)
_out = os.fdopen(os.dup(1), "wb", buffering=0)
_null_r = os.open(os.devnull, os.O_RDONLY)
_null_w = os.open(os.devnull, os.O_WRONLY)
os.dup2(_null_r, 0)
os.dup2(_null_w, 1)


def _read_exact(n):
    buf = bytearray()
    while len(buf) < n:
        chunk = _in.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _recv():
    h = _read_exact(4)
    if h is None:
        return None
    (n,) = struct.unpack(">I", h)
    if n > MAX_FRAME:
        raise ValueError(f"frame of {n} bytes exceeds {MAX_FRAME}")
    b = _read_exact(n)
    if b is None:
        return None
    return json.loads(b)


def _send(obj):
    b = json.dumps(obj, separators=(",", ":")).encode()
    if len(b) > MAX_FRAME:
        b = json.dumps({"ok": False, "error": {"type": "ResultTooLarge", "message": f"result frame {len(b)} bytes exceeds {MAX_FRAME}", "traceback": ""}}).encode()
    _out.write(struct.pack(">I", len(b)) + b)


class _Capped(io.TextIOBase):
    def __init__(self, limit):
        self.limit, self.n, self.parts, self.truncated = limit, 0, [], False

    def writable(self):
        return True

    def write(self, s):
        if self.n < self.limit:
            take = s[: self.limit - self.n]
            self.parts.append(take)
            self.n += len(take)
            if len(take) < len(s):
                self.truncated = True
        else:
            self.truncated = True
        return len(s)

    def value(self):
        return "".join(self.parts)


_LOADED = {}
_VRAM_CAPPED = False


def _cap_vram():
    """Soft layer only (a hostile function can undo it): the worker enforces vram_mb from outside the container."""
    global _VRAM_CAPPED
    if _VRAM_CAPPED or not VRAM_MB or "torch" not in sys.modules:
        return
    try:
        torch = sys.modules["torch"]
        if torch.cuda.is_available():
            total = torch.cuda.get_device_properties(0).total_memory
            torch.cuda.set_per_process_memory_fraction(min(1.0, VRAM_MB * 1024 * 1024 / total), 0)
        _VRAM_CAPPED = True
    except Exception:
        pass


def _err(e):
    return {"type": type(e).__name__, "message": str(e)[:2000], "traceback": traceback.format_exc()[-8000:]}


def _load(req):
    import cloudpickle

    sha = req["sha"]
    if sha in _LOADED:
        return _LOADED[sha], False, 0.0
    b64 = req.get("bundle_b64")
    if not b64:
        raise LookupError("bundle not loaded and not supplied")
    raw = base64.b64decode(b64)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("bundle sha256 mismatch")
    bundle = cloudpickle.loads(raw)
    t = time.perf_counter()
    entry = {"kind": bundle["kind"], "obj": bundle["obj"], "inst": None, "exit": list(bundle.get("exit", []))}
    if entry["kind"] == "cls":
        inst = entry["obj"]()
        entry["inst"] = inst
        for name in bundle.get("enter", []):
            getattr(inst, name)()
    _LOADED[sha] = entry
    return entry, True, (time.perf_counter() - t) * 1000


def _call(req):
    import cloudpickle

    limit = int(req.get("max_output_bytes") or 256 * 1024)
    out, err = _Capped(limit), _Capped(limit)
    base = {"stdout": "", "stderr": "", "truncated": False, "loaded": False, "enter_ms": 0.0, "exec_ms": 0.0}
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            entry, loaded, enter_ms = _load(req)
            base["loaded"], base["enter_ms"] = loaded, round(enter_ms, 3)
            raw = base64.b64decode(req["args_b64"])
            if req.get("args_format", "cloudpickle") == "cloudpickle":
                args, kwargs = cloudpickle.loads(raw)
            else:
                d = json.loads(raw)
                args, kwargs = d.get("args", []), d.get("kwargs", {})
            fn = getattr(entry["inst"], req["method"]) if entry["kind"] == "cls" else entry["obj"]
            _cap_vram()
            t = time.perf_counter()
            res = fn(*args, **kwargs)
            base["exec_ms"] = round((time.perf_counter() - t) * 1000, 3)
            if req.get("result_format", "cloudpickle") == "json":
                payload, fmt = json.dumps(res).encode(), "json"
            else:
                payload, fmt = cloudpickle.dumps(res), "cloudpickle"
            r = {"ok": True, "result_b64": base64.b64encode(payload).decode(), "format": fmt}
        except BaseException as e:   # includes SystemExit from user code: the runner must survive it
            r = {"ok": False, "error": _err(e)}
    finally:
        sys.stdout, sys.stderr = old
    base.update(r)
    base["stdout"], base["stderr"], base["truncated"] = out.value(), err.value(), out.truncated or err.truncated
    return base


def _shutdown():
    for entry in _LOADED.values():
        for name in entry["exit"]:
            try:
                getattr(entry["inst"], name)()
            except BaseException:
                pass


def main():
    _send({"ready": True, "pid": os.getpid(), "python": sys.version.split()[0]})
    while True:
        try:
            req = _recv()
        except Exception as e:
            _send({"ok": False, "error": _err(e)})
            break
        if req is None or req.get("op") == "shutdown":
            break
        op = req.get("op")
        if op == "ping":
            _send({"ok": True})
        elif op == "call":
            _send(_call(req))
        else:
            _send({"ok": False, "error": {"type": "BadOp", "message": f"unknown op {op!r}", "traceback": ""}})
    _shutdown()


main()
