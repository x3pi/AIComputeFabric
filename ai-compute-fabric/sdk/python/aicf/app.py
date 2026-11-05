"""App / Function: the Modal-style surface. `f.remote(...)` pickles `f` BY VALUE with cloudpickle and runs it in an isolated sandbox."""
from __future__ import annotations

import base64
import importlib
import sys
from typing import Any, Callable, Optional

import cloudpickle

from aicf import errors as E
from aicf.client import Client
from aicf.image import Image

MAX_BUNDLE = 8 * 1024 * 1024


class Function:
    def __init__(self, app: "App", fn: Callable, **opts):
        self.app, self.fn, self.opts = app, fn, opts
        import re
        self.name = (re.sub(r"[^A-Za-z0-9_.-]", "_", fn.__name__).lstrip("_.-") or "fn")[:64]    # '<lambda>' -> 'lambda'
        self.__doc__ = fn.__doc__

    def local(self, *a, **k):
        return self.fn(*a, **k)

    __call__ = local

    def _bundle(self) -> bytes:
        image: Image = self.opts["image"]
        mods = [importlib.import_module(m) for m in image.local_modules]
        own = sys.modules.get(getattr(self.fn, "__module__", ""))      # the script that defines the function is never installed in the sandbox
        if own is not None and own not in mods and getattr(own, "__name__", "__main__") != "__main__":
            mods.append(own)
        for m in mods:
            cloudpickle.register_pickle_by_value(m)
        try:
            return cloudpickle.dumps({"kind": "function", "obj": self.fn, "enter": [], "exit": []})
        finally:
            for m in mods:
                try:
                    cloudpickle.unregister_pickle_by_value(m)
                except ValueError:
                    pass

    def _request(self, args: tuple, kwargs: dict) -> dict:
        o = self.opts
        image: Image = o["image"]
        image.check_local_python()
        bundle = self._bundle()
        if len(bundle) > MAX_BUNDLE:
            raise E.InvalidRequest(f"function bundle is {len(bundle) // 1024} KiB (max {MAX_BUNDLE // 1024} KiB): move data into arguments/Volumes, not closures")
        body = {"app_name": self.app.name, "fn_id": self.name, "bundle_b64": base64.b64encode(bundle).decode(),
                "args_b64": base64.b64encode(cloudpickle.dumps((args, kwargs))).decode(), "env": image.spec(), "env_vars": dict(image.env),
                "memory_mb": o["memory_mb"], "cpus": o["cpu"], "timeout_s": o["timeout_s"], "keep_warm_s": o["keep_warm_s"], "retries": o["retries"],
                "data_class": o["data_class"], "vram_mb": o["vram_mb"]}
        if o["gpu"] is not None:
            body["gpu"] = o["gpu"]
        return body

    def remote(self, *args, **kwargs) -> Any:
        client = self.app.client()
        doc = client.post("/invoke", self._request(args, kwargs))
        return self._unwrap(doc)

    @staticmethod
    def _unwrap(doc: dict) -> Any:
        st = doc["status"]
        if st == "ok":
            if doc.get("result_format") == "json":
                import json
                return json.loads(base64.b64decode(doc["result_b64"]))
            return cloudpickle.loads(base64.b64decode(doc["result_b64"]))        # result of YOUR OWN function in YOUR OWN tenant sandbox
        err = doc.get("error") or {}
        msg = err.get("message", "")
        if st == "error":
            raise E.RemoteError(err.get("type", "Error"), msg, err.get("traceback", ""), doc.get("stdout", ""), doc.get("stderr", ""))
        if st == "timeout":
            raise E.FunctionTimeout(f"the function exceeded its timeout_s. Raise timeout_s in @app.function(...) or make it faster. {msg}")
        if st == "oom":
            raise E.FunctionOOM(f"the function ran out of memory. Raise memory_mb in @app.function(...). {msg}")
        if st in ("worker_lost", "duplicate"):
            raise E.WorkerLost("the worker running your function disappeared; the function MAY have started. Retry only if it is idempotent "
                               "(or set retries=N in @app.function(...)).")
        if st == "env_failed":
            raise E.EnvBuildFailed(msg + " (only packages with prebuilt wheels are supported)")
        raise E.ServerError(f"{st}: {msg}")


class App:
    def __init__(self, name: str, client: Optional[Client] = None):
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise E.InvalidRequest("app name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        self.name, self._client, self.functions = name, client, {}

    def client(self) -> Client:
        if self._client is None:
            self._client = Client()
        return self._client

    def function(self, *, image: Optional[Image] = None, gpu: Optional[str] = None, vram_mb: int = 0, memory_mb: int = 512, cpu: float = 1.0,
                 timeout_s: float = 30.0, keep_warm_s: int = 60, retries: int = 0, data_class: str = "INTERNAL"):
        opts = dict(image=image or Image.debian_slim(), gpu=gpu, vram_mb=vram_mb, memory_mb=memory_mb, cpu=cpu, timeout_s=timeout_s,
                    keep_warm_s=keep_warm_s, retries=retries, data_class=data_class)

        def deco(fn):
            f = Function(self, fn, **opts)
            self.functions[f.name] = f
            return f
        return deco
