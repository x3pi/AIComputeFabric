"""CadQuery backend: code-as-action in a persistent worker process, measured after every cell, undo by replaying the history."""
import json, os, subprocess, sys, tempfile, threading, time
from .backend import Backend, ExecResult, Measure

HERE = os.path.dirname(os.path.abspath(__file__))


def default_python():
    """The interpreter that runs the worker: the one that has CadQuery (normally the one running us)."""
    exe = os.environ.get("CADFABRIC_PYTHON") or sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":       # the windowless launcher: the worker needs a real stdin/stdout
        exe = os.path.join(os.path.dirname(exe), "python.exe")
    return exe


# The only environment variables the worker sees: enough for Python/OCP/ezdxf to start (Windows needs USERPROFILE & co to find a home directory),
# and none of the caller's secrets (CADFABRIC_KEY, SSHPASS, cloud keys, ...).
WORKER_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CADFABRIC_MEM_GB",
                   "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS")


def worker_env(environ=None):
    return {k: v for k, v in (environ if environ is not None else os.environ).items() if k.upper() in WORKER_ENV_KEEP}


NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}                   # CREATE_NO_WINDOW: no black console flashing from a GUI app


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
                                     text=True, encoding="utf-8", bufsize=1, cwd=tempfile.gettempdir(), **NOWIN,
                                     env=worker_env())

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
        lost = self._restart_with_history()
        tail = ("the session was restored to the last good state" if not lost else
                "only the first %d successful step(s) could be restored in time, the last %d were dropped: re-run what is missing, and keep each step's boolean operations small and few" % (len(self.history), lost))
        return {"ok": False, "error": "the CAD worker %s; %s" % (why, tail)}

    def _replay_one(self, code, limit):
        """Run one saved cell again, quietly (no measurement, no delta: only the state matters) and with a time limit. -> True when it finished."""
        box = {}

        def go():
            self.proc.stdin.write(json.dumps({"op": "exec", "code": code, "quiet": True}) + "\n"); self.proc.stdin.flush()
            return self.proc.stdout.readline()
        t = threading.Thread(target=lambda: box.setdefault("line", _safe(go)), daemon=True)
        t.start(); t.join(limit)
        line = box.get("line")
        return bool(line) and bool(json.loads(line).get("ok"))

    def _restart_with_history(self):
        """Start a fresh worker and replay the saved cells within a total time limit (3 x the cell limit, at least 300 s). A cell that cannot be replayed in time is
        dropped together with everything after it: the state stays consistent and the caller is told how many cells were lost. -> number of cells lost."""
        total, lost = max(3 * self.timeout, 300), 0
        for _ in range(3):
            self._start()
            t0, done = time.monotonic(), 0
            for code in self.history:
                left = total - (time.monotonic() - t0)
                if left <= 0 or not self._replay_one(code, min(left, self.timeout)):
                    break
                done += 1
            else:
                return lost
            lost += len(self.history) - done
            self.history = self.history[:done]
        return lost

    def _rpc_plain(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n"); self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    # --- Backend ------------------------------------------------------------------------------------------------------------------
    def execute(self, code):
        r = self._rpc({"op": "exec", "code": code})
        if r.get("ok"):
            self.history.append(code)
        return ExecResult(ok=bool(r.get("ok")), output=r.get("output", ""), error=r.get("error", ""),
                          measure=Measure(**r["measure"]) if r.get("measure") else None, changed=bool(r.get("changed")), delta=r.get("delta"))

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

    def preview_svg(self):
        r = self._rpc({"op": "svg"})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "preview failed"))
        return r["svg"]

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
