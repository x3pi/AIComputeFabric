"""aicf run script.py[::function] [args...]   |   aicf doctor"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import sys

from aicf import errors as E
from aicf.app import Function


def _load(path: str):
    spec = importlib.util.spec_from_file_location("aicf_user_script", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aicf_user_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def cmd_run(a) -> int:
    target, _, fname = a.target.partition("::")
    mod = _load(target)
    fns = {n: v for n, v in vars(mod).items() if isinstance(v, Function)}
    if not fns:
        print("error: no @app.function found in " + target, file=sys.stderr)
        return 2
    if not fname:
        if len(fns) != 1:
            print(f"error: several functions ({', '.join(fns)}); choose one: aicf run {target}::<name>", file=sys.stderr)
            return 2
        fname = next(iter(fns))
    if fname not in fns:
        print(f"error: no function {fname!r} (found: {', '.join(fns)})", file=sys.stderr)
        return 2
    args = [ast.literal_eval(x) for x in a.args]
    try:
        out = fns[fname].remote(*args)
    except E.RemoteError as e:
        print(f"remote error: {e}", file=sys.stderr)
        if e.stdout:
            print("--- remote stdout ---\n" + e.stdout, file=sys.stderr)
        return 1
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def cmd_doctor(a) -> int:
    from aicf.client import Client
    from aicf.image import SUPPORTED_PYTHON
    ok = True

    def line(good: bool, msg: str):
        nonlocal ok
        ok &= good
        print(("OK   " if good else "FAIL ") + msg)
    local = f"{sys.version_info.major}.{sys.version_info.minor}"
    line(local == SUPPORTED_PYTHON, f"local Python {local} (functions run on {SUPPORTED_PYTHON}; they must match)")
    try:
        c = Client()
    except E.ConfigError as e:
        line(False, str(e))
        return 1
    line(True, f"gateway {c.url}, API key set")
    try:
        c.post("/invoke", {"app_name": "doctor", "fn_id": "doctor", "bundle_b64": "AA==", "args_b64": "AA==", "env": {}, "timeout_s": 1})
    except E.AuthError as e:
        line(False, str(e))
    except E.ConnectionFailed as e:
        line(False, str(e))
    except E.NoCapacity as e:
        line(False, f"gateway reachable and key accepted, but: {e}")
    except E.AicfError as e:
        line(True, f"gateway reachable and key accepted ({type(e).__name__} for the empty probe is expected)")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aicf")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a function from a script remotely")
    r.add_argument("target", help="script.py or script.py::function")
    r.add_argument("args", nargs="*", help="python literals, e.g. 7 or '[1,2]'")
    r.set_defaults(fn=cmd_run)
    d = sub.add_parser("doctor", help="check configuration and connectivity")
    d.set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
