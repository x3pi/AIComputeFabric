"""CLI for AICF Serverless: run | deploy | rollback | ps | stop | logs | volume | doctor."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

from aicf import errors as E
from aicf.app import App, Function, FunctionCall, Volume
from aicf.client import Client
from aicf.image import SUPPORTED_PYTHON


def _load(path: str):
    spec = importlib.util.spec_from_file_location("aicf_user_script", path)
    if spec is None or spec.loader is None:
        raise E.InvalidRequest(f"cannot load Python script: {path}")
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


def cmd_deploy(a) -> int:
    try:
        mod = _load(a.script)
        apps = [v for v in vars(mod).values() if isinstance(v, App)]
        if not apps:
            print(f"error: no aicf.App found in {a.script}", file=sys.stderr)
            return 2
        res = apps[0].deploy()
        print(json.dumps(res, indent=2))
        return 0
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_rollback(a) -> int:
    try:
        c = Client()
        body = {"version": a.version} if a.version else {}
        res = c.post(f"/apps/{a.app_name}/rollback", body)
        print(json.dumps(res, indent=2))
        return 0
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_ps(a) -> int:
    try:
        c = Client()
        containers = c.get("/containers").get("containers", [])
        apps = c.get("/apps").get("apps", [])
        print(json.dumps({"containers": containers, "apps": apps}, indent=2))
        return 0
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_stop(a) -> int:
    try:
        c = Client()
        try:
            res = c.delete(f"/containers/{a.target}")
        except E.InvalidRequest:
            res = c.delete(f"/apps/{a.target}")
        print(json.dumps(res, indent=2))
        return 0
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_logs(a) -> int:
    try:
        fc = FunctionCall.from_id(a.call_id)
        for entry in fc.logs():
            if "text" in entry:
                print(entry["text"])
        return 0
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_volume(a) -> int:
    try:
        vol = Volume.from_name(a.volume)
        if a.vcmd == "ls":
            print(json.dumps(vol.ls(), indent=2))
            return 0
        if a.vcmd == "put":
            src = Path(a.local_path)
            dst = a.remote_path or src.name
            res = vol.put(dst, src)
            print(json.dumps(res, indent=2))
            return 0
        if a.vcmd == "get":
            raw = vol.get(a.remote_path)
            if a.local_path:
                Path(a.local_path).write_bytes(raw)
            else:
                sys.stdout.buffer.write(raw)
            return 0
        return 2
    except E.AicfError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_doctor(a) -> int:
    ok = True

    def line(good: bool, msg: str):
        nonlocal ok
        ok &= good
        print(("OK   " if good else "FAIL ") + msg)

    local = os.environ.get("AICF_LOCAL_PYTHON_OVERRIDE") or f"{sys.version_info.major}.{sys.version_info.minor}"
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
        line(False, f"auth/tenant check failed: {e}")
    except E.ConnectionFailed as e:
        line(False, f"connection failed: {e}")
    except E.QuotaExceeded as e:
        line(False, f"quota check failed ({e.code}): {e}")
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

    dep = sub.add_parser("deploy", help="deploy an app from a script")
    dep.add_argument("script", help="script.py containing aicf.App")
    dep.set_defaults(fn=cmd_deploy)

    rb = sub.add_parser("rollback", help="roll back a deployed app to a previous version")
    rb.add_argument("app_name", help="deployed app name")
    rb.add_argument("--version", default=None, help="specific version to roll back to")
    rb.set_defaults(fn=cmd_rollback)

    ps = sub.add_parser("ps", help="list warm containers and deployed apps")
    ps.set_defaults(fn=cmd_ps)

    st = sub.add_parser("stop", help="evict a warm container or stop a deployed app")
    st.add_argument("target", help="container_id or app_name")
    st.set_defaults(fn=cmd_stop)

    lg = sub.add_parser("logs", help="fetch stdout/stderr logs of a call")
    lg.add_argument("call_id", help="call_id")
    lg.set_defaults(fn=cmd_logs)

    vol = sub.add_parser("volume", help="manage tenant volumes")
    vsub = vol.add_subparsers(dest="vcmd", required=True)
    vls = vsub.add_parser("ls", help="list files in a volume")
    vls.add_argument("volume", help="volume name")
    vput = vsub.add_parser("put", help="upload a local file to a volume")
    vput.add_argument("volume", help="volume name")
    vput.add_argument("local_path", help="local file path")
    vput.add_argument("remote_path", nargs="?", default=None, help="destination path in volume")
    vget = vsub.add_parser("get", help="download a file from a volume")
    vget.add_argument("volume", help="volume name")
    vget.add_argument("remote_path", help="path in volume")
    vget.add_argument("local_path", nargs="?", default=None, help="destination local file")
    vol.set_defaults(fn=cmd_volume)

    d = sub.add_parser("doctor", help="check configuration and connectivity")
    d.set_defaults(fn=cmd_doctor)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
