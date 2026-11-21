"""CadQuery backend: code-as-action in a persistent worker process, measured after every cell, undo by replaying the history."""
import json, os, subprocess, sys, tempfile, threading
from .backend import Backend, ExecResult, Measure

HERE = os.path.dirname(os.path.abspath(__file__))


def default_python():
    """The interpreter that runs the worker: the one that has CadQuery (normally the one running us)."""
    return os.environ.get("CADFABRIC_PYTHON") or sys.executable


class CadQueryBackend(Backend):
    name = "cadquery"

    def __init__(self, python=None, timeout=120):
        self.python, self.timeout = python or default_python(), timeout
        self.history = []                       # successful cells, in order
        self.proc = None
        self._start()

    # --- process management -------------------------------------------------------------------------------------------------------
    def _start(self):
        self.close()
        self.proc = subprocess.Popen([self.python, "-I", os.path.join(HERE, "worker.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", bufsize=1, cwd=tempfile.gettempdir(),
                                     env={k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "CADFABRIC_MEM_GB")})

    def _rpc(self, req):
        """-> reply dict. On timeout/crash the worker is killed and restarted with the history replayed; returns {"ok": False, "error": ...}."""
        def go():
            self.proc.stdin.write(json.dumps(req) + "\n"); self.proc.stdin.flush()
            return self.proc.stdout.readline()
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("line", _safe(go)), daemon=True)
        t.start(); t.join(self.timeout)
        line = box.get("line")
        if line:
            return json.loads(line)
        why = "timed out after %d s" % self.timeout if t.is_alive() else "crashed or ran out of memory"
        self._restart_with_history()
        return {"ok": False, "error": "the CAD worker %s; the session was restored to the last good state" % why}

    def _restart_with_history(self):
        self._start()
        for code in self.history:
            self._rpc_plain({"op": "exec", "code": code})

    def _rpc_plain(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n"); self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    # --- Backend ------------------------------------------------------------------------------------------------------------------
    def execute(self, code):
        r = self._rpc({"op": "exec", "code": code})
        if r.get("ok"):
            self.history.append(code)
        return ExecResult(ok=bool(r.get("ok")), output=r.get("output", ""), error=r.get("error", ""),
                          measure=Measure(**r["measure"]) if r.get("measure") else None, changed=bool(r.get("changed")))

    def measure(self):
        r = self._rpc({"op": "exec", "code": "pass"})
        return Measure(**r["measure"]) if r.get("measure") else None

    def undo(self):
        if not self.history:
            return False
        self.history.pop()
        self._restart_with_history()
        return True

    def reset(self):
        self.history = []
        self._rpc({"op": "reset"})

    def export_step(self, path):
        r = self._rpc({"op": "export", "path": path})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "export failed"))
        return path

    def close(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()
            p.wait()
        if p:
            for f in (p.stdin, p.stdout):
                try: f.close()
                except (OSError, ValueError): pass


def _safe(fn):
    try:
        return fn()
    except (OSError, ValueError):
        return ""
