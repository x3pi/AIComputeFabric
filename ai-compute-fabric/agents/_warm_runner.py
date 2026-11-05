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
import select
import struct
import sys
import threading
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


_LOADED: dict[str, object] = {}
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


def _materialize_files(files: dict) -> None:
    if not files or not isinstance(files, dict):
        return
    import posixpath
    root = "/tmp/aicf_files"
    os.makedirs(root, exist_ok=True)
    if root not in sys.path:
        sys.path.insert(0, root)
    for rel, content in files.items():
        if not isinstance(rel, str):
            continue
        raw = base64.b64decode(content) if isinstance(content, str) else bytes(content)
        clean = posixpath.normpath(rel.lstrip("/"))
        if not clean or clean == "." or clean.startswith("..") or "/../" in f"/{clean}/":
            raise ValueError(f"invalid bundled file path: {rel!r}")
        targets = [os.path.join(root, clean)]
        if rel.startswith("/tmp/"):
            sub = posixpath.normpath(rel[5:].lstrip("/"))
            if sub and sub != "." and not sub.startswith(".."):
                targets.append(os.path.join("/tmp", sub))
        for dst in targets:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(raw)


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
    _materialize_files(bundle.get("files") or {})
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


_IN_CALL = False
_CALL_SEQ = 0


def _watch_stdin():
    """If the worker process on the host dies (even SIGKILL), its pipe to `docker run -i` closes and our _in hits EOF/POLLHUP.
    While a call is running on the main thread, nothing reads _in — without this watcher a long GPU call would outlive its dead
    worker and hold VRAM until the function returned on its own."""
    import fcntl
    import termios
    fd = _in.fileno()
    p = select.poll()
    p.register(fd, select.POLLHUP | select.POLLERR | select.POLLIN)
    while True:
        if not _IN_CALL:
            for _, ev in p.poll(100):
                if ev & (select.POLLHUP | select.POLLERR):
                    os._exit(0)
            time.sleep(0.1)
            continue
        seq0 = _CALL_SEQ
        for _, ev in p.poll(100):
            if ev & (select.POLLHUP | select.POLLERR):
                os._exit(0)
            if _IN_CALL and _CALL_SEQ == seq0 and (ev & select.POLLIN):
                try:
                    nbytes = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\x00\x00\x00\x00"))[0]
                    if nbytes == 0 and _IN_CALL and _CALL_SEQ == seq0:
                        os._exit(0)
                except OSError:
                    os._exit(0)
        time.sleep(0.05)


def main():
    global _IN_CALL, _CALL_SEQ
    threading.Thread(target=_watch_stdin, daemon=True).start()
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
            _CALL_SEQ += 1
            _IN_CALL = True
            try:
                resp = _call(req)
            finally:
                _IN_CALL = False
            _send(resp)
        else:
            _send({"ok": False, "error": {"type": "BadOp", "message": f"unknown op {op!r}", "traceback": ""}})
    _shutdown()


main()
