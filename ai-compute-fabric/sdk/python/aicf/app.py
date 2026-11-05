"""App / Function / Cls / Volume / Secret / FunctionCall: the Modal-style surface.
`f.remote(...)` pickles `f` BY VALUE with cloudpickle and runs it in an isolated sandbox."""
from __future__ import annotations

import base64
import collections
import concurrent.futures
import hashlib
import importlib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import cloudpickle

from aicf import errors as E
from aicf.client import Client
from aicf.image import Image

MAX_BUNDLE = 8 * 1024 * 1024


def enter():
    """Mark a @app.cls method to run once when the container starts."""
    def deco(fn: Callable) -> Callable:
        setattr(fn, "_aicf_enter", True)
        return fn
    return deco


def exit():
    """Mark a @app.cls method to run once when the container shuts down."""
    def deco(fn: Callable) -> Callable:
        setattr(fn, "_aicf_exit", True)
        return fn
    return deco


def method():
    """Mark a @app.cls method as remotely callable."""
    def deco(fn: Callable) -> Callable:
        setattr(fn, "_aicf_method", True)
        return fn
    return deco


class Secret:
    """Reference to a server-side secret injected as environment variables into the sandbox (never logged or pickled)."""

    def __init__(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise E.InvalidRequest(f"invalid secret name: {name!r}")
        self.name = name

    @classmethod
    def from_name(cls, name: str) -> "Secret":
        return cls(name)


class Volume:
    """Tenant-isolated persistent storage volume (ADR-003 D7). Supports both Modal (`Volume.from_name`) and Beta9 (`Volume(name=..., mount_path=...)`)."""

    def __init__(self, name: str, client: Optional[Client] = None, *, mount_path: Optional[str] = None, create_if_missing: bool = True):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise E.InvalidRequest(f"invalid volume name: {name!r}")
        self.name = name
        self.mount_path = mount_path or f"/mnt/volumes/{name}"
        self.create_if_missing = create_if_missing
        self._client = client

    @classmethod
    def from_name(cls, name: str, client: Optional[Client] = None, *, create_if_missing: bool = True) -> "Volume":
        return cls(name, client=client, create_if_missing=create_if_missing)

    def _c(self) -> Client:
        if self._client is None:
            self._client = Client()
        return self._client

    def put(self, remote_path: str, data: bytes | str | Path) -> dict:
        if isinstance(data, Path):
            raw = data.read_bytes()
        elif isinstance(data, str):
            raw = data.encode("utf-8")
        else:
            raw = bytes(data)
        digest = hashlib.sha256(raw).hexdigest()
        enc_path = "/".join(urllib.parse.quote(p, safe="") for p in remote_path.lstrip("/").split("/"))
        return self._c().put(f"/volumes/{urllib.parse.quote(self.name, safe='')}/{enc_path}", raw, headers={"X-Aicf-Sha256": digest})

    def get(self, remote_path: str) -> bytes:
        enc_path = "/".join(urllib.parse.quote(p, safe="") for p in remote_path.lstrip("/").split("/"))
        raw, hdrs = self._c().get(f"/volumes/{urllib.parse.quote(self.name, safe='')}/{enc_path}", raw_response=True)
        expected = hdrs.get("X-Aicf-Sha256") or hdrs.get("x-aicf-sha256")
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise E.ServerError("volume download failed SHA-256 integrity check")
        return raw

    def ls(self) -> list[dict]:
        doc = self._c().get(f"/volumes/{urllib.parse.quote(self.name, safe='')}")
        return doc.get("files", [])

    def delete(self, remote_path: str) -> dict:
        enc_path = "/".join(urllib.parse.quote(p, safe="") for p in remote_path.lstrip("/").split("/"))
        return self._c().delete(f"/volumes/{urllib.parse.quote(self.name, safe='')}/{enc_path}")


class ModelCache:
    """Admin-populated, tenant-read-only model weights cache (ADR-003 D7)."""

    def __init__(self, name: str, client: Optional[Client] = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise E.InvalidRequest(f"invalid model cache name: {name!r}")
        self.name = name
        self._client = client

    @classmethod
    def from_name(cls, name: str, client: Optional[Client] = None) -> "ModelCache":
        return cls(name, client=client)

    def _c(self) -> Client:
        if self._client is None:
            self._client = Client()
        return self._client

    def ls(self) -> list[dict]:
        doc = self._c().get(f"/model-cache/{urllib.parse.quote(self.name, safe='')}")
        return doc.get("files", [])

    def get(self, remote_path: str) -> bytes:
        enc_path = "/".join(urllib.parse.quote(p, safe="") for p in remote_path.lstrip("/").split("/"))
        raw, _ = self._c().get(f"/model-cache/{urllib.parse.quote(self.name, safe='')}/{enc_path}", raw_response=True)
        return raw

    def put(self, remote_path: str, data: bytes) -> dict:
        enc_path = "/".join(urllib.parse.quote(p, safe="") for p in remote_path.lstrip("/").split("/"))
        return self._c().put(f"/model-cache/{urllib.parse.quote(self.name, safe='')}/{enc_path}", data)


class FunctionCall:
    """Handle to an asynchronous `.spawn()` invocation."""

    def __init__(self, object_id: str, client: Client):
        self.object_id = object_id
        self.call_id = object_id
        self._client = client

    @classmethod
    def from_id(cls, object_id: str, client: Optional[Client] = None) -> "FunctionCall":
        return cls(object_id, client or Client())

    def get(self, timeout: Optional[float] = None) -> Any:
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            wait = 2.0
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise E.FunctionTimeout(f"call {self.object_id} did not complete within timeout={timeout}s")
                wait = min(wait, max(0.05, rem))
            doc = self._client.get(f"/calls/{self.object_id}", params={"wait": f"{wait:.2f}"})
            st = doc.get("status")
            if st in ("pending", "running"):
                if deadline is not None and time.monotonic() >= deadline:
                    raise E.FunctionTimeout(f"call {self.object_id} did not complete within timeout={timeout}s")
                continue
            if st == "cancelled":
                raise E.AicfError(f"call {self.object_id} was cancelled")
            return Function._unwrap(doc)

    def cancel(self) -> dict:
        return self._client.delete(f"/calls/{self.object_id}")

    def logs(self) -> list[dict]:
        raw, _ = self._client.get(f"/calls/{self.object_id}/logs", raw_response=True)
        out = []
        for block in raw.decode("utf-8", errors="replace").split("\n\n"):
            for ln in block.splitlines():
                if ln.startswith("data: "):
                    try:
                        out.append(json.loads(ln[6:]))
                    except ValueError:
                        pass
        return out


class Function:
    def __init__(self, app: "App", fn: Callable, *, kind: str = "function", method_name: Optional[str] = None,
                 enter_methods: tuple[str, ...] = (), exit_methods: tuple[str, ...] = (), fn_id_override: Optional[str] = None,
                 web_config: Optional[dict] = None, **opts):
        self.app, self.fn, self.opts = app, fn, opts
        self.kind = kind
        self.method_name = method_name
        self.enter_methods = list(enter_methods)
        self.exit_methods = list(exit_methods)
        self.web_config = web_config
        raw_name = str(fn_id_override or getattr(fn, "__name__", "fn"))
        self.name = (re.sub(r"[^A-Za-z0-9_.-]", "_", raw_name).lstrip("_.-") or "fn")[:64]
        self.__doc__ = getattr(fn, "__doc__", None)

    def local(self, *a, **k):
        if self.kind == "cls":
            inst = self.fn()
            for m in self.enter_methods:
                getattr(inst, m)()
            try:
                return getattr(inst, self.method_name)(*a, **k)
            finally:
                for m in self.exit_methods:
                    getattr(inst, m)()
        return self.fn(*a, **k)

    __call__ = local

    def _bundle(self) -> bytes:
        image: Image = self.opts["image"]
        mods = [importlib.import_module(m) for m in image.local_modules]
        own = sys.modules.get(getattr(self.fn, "__module__", ""))
        if own is not None and own not in mods and getattr(own, "__name__", "__main__") != "__main__":
            mods.append(own)
        for m in mods:
            cloudpickle.register_pickle_by_value(m)
        try:
            payload = {"kind": self.kind, "obj": self.fn, "enter": self.enter_methods, "exit": self.exit_methods}
            files = image.files_dict()
            if files:
                payload["files"] = files
            return cloudpickle.dumps(payload)
        finally:
            for m in mods:
                try:
                    cloudpickle.unregister_pickle_by_value(m)
                except ValueError:
                    pass

    def _request(self, args: tuple, kwargs: dict, *, retries_override: Optional[int] = None, bundle_b64: Optional[str] = None) -> dict:
        o = self.opts
        image: Image = o["image"]
        image.check_local_python()
        if bundle_b64 is None:
            bundle = self._bundle()
            if len(bundle) > MAX_BUNDLE:
                raise E.InvalidRequest(f"function bundle is {len(bundle) // 1024} KiB (max {MAX_BUNDLE // 1024} KiB): move data into arguments/Volumes, not closures")
            bundle_b64 = base64.b64encode(bundle).decode()
        retries = o["retries"] if retries_override is None else int(retries_override)
        secrets = [s.name if isinstance(s, Secret) else str(s) for s in (o.get("secrets") or ())]
        body = {
            "app_name": self.app.name,
            "fn_id": self.name,
            "kind": self.kind,
            "bundle_b64": bundle_b64,
            "args_b64": base64.b64encode(cloudpickle.dumps((args, kwargs))).decode(),
            "env": image.spec(),
            "env_vars": dict(image.env),
            "memory_mb": o["memory_mb"],
            "cpus": o["cpu"],
            "timeout_s": o["timeout_s"],
            "keep_warm_s": o["keep_warm_s"],
            "retries": retries,
            "data_class": o["data_class"],
            "vram_mb": o["vram_mb"],
        }
        if self.method_name:
            body["method"] = self.method_name
        if secrets:
            body["secrets"] = secrets
        if o["gpu"] is not None:
            body["gpu"] = o["gpu"]
        return body

    def remote(self, *args, **kwargs) -> Any:
        client = self.app.client()
        doc = client.post("/invoke", self._request(args, kwargs))
        return self._unwrap(doc)

    def spawn(self, *args, **kwargs) -> FunctionCall:
        client = self.app.client()
        doc = client.post("/spawn", self._request(args, kwargs))
        return FunctionCall(doc["call_id"], client)

    # Beam/Beta9 `@task_queue` alias: `job = my_queue.put(...)` -> `job.get()`
    put = spawn

    def map(self, iterable: Iterable[Any], *, order_outputs: bool = True, max_concurrency: int = 16,
            return_exceptions: bool = False, retries: Optional[int] = None) -> Iterator[Any]:
        return self._map_impl(((x,) for x in iterable), order_outputs=order_outputs, max_concurrency=max_concurrency,
                              return_exceptions=return_exceptions, retries=retries)

    def starmap(self, iterable: Iterable[ Iterable[Any] ], *, order_outputs: bool = True, max_concurrency: int = 16,
                return_exceptions: bool = False, retries: Optional[int] = None) -> Iterator[Any]:
        return self._map_impl((tuple(x) for x in iterable), order_outputs=order_outputs, max_concurrency=max_concurrency,
                              return_exceptions=return_exceptions, retries=retries)

    def for_each(self, iterable: Iterable[Any], *, max_concurrency: int = 16,
                 return_exceptions: bool = False, retries: Optional[int] = None) -> None:
        for _ in self.map(iterable, order_outputs=False, max_concurrency=max_concurrency,
                          return_exceptions=return_exceptions, retries=retries):
            pass

    def _map_impl(self, args_iter: Iterator[tuple], *, order_outputs: bool, max_concurrency: int,
                  return_exceptions: bool, retries: Optional[int]) -> Iterator[Any]:
        if max_concurrency < 1:
            raise E.InvalidRequest("max_concurrency must be >= 1")
        client = self.app.client()
        image: Image = self.opts["image"]
        image.check_local_python()
        bundle = self._bundle()
        if len(bundle) > MAX_BUNDLE:
            raise E.InvalidRequest(f"function bundle is {len(bundle) // 1024} KiB (max {MAX_BUNDLE // 1024} KiB)")
        b64_bundle = base64.b64encode(bundle).decode()
        eff_retries = self.opts["retries"] if retries is None else int(retries)

        def _call_one(call_args: tuple) -> Any:
            req_body = self._request(call_args, {}, retries_override=eff_retries, bundle_b64=b64_bundle)
            attempt = 0
            while True:
                attempt += 1
                try:
                    doc = client.post("/invoke", req_body)
                    return self._unwrap(doc)
                except (E.WorkerLost, E.Preempted, E.NoCapacity):
                    if attempt <= eff_retries:
                        continue
                    raise

        window = max(1, max_concurrency * 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            if order_outputs:
                q: collections.deque[concurrent.futures.Future] = collections.deque()
                for item_args in args_iter:
                    while len(q) >= window:
                        fut = q.popleft()
                        try:
                            yield fut.result()
                        except Exception as exc:
                            if return_exceptions:
                                yield exc
                            else:
                                for pending in q:
                                    pending.cancel()
                                raise
                    q.append(pool.submit(_call_one, item_args))
                while q:
                    fut = q.popleft()
                    try:
                        yield fut.result()
                    except Exception as exc:
                        if return_exceptions:
                            yield exc
                        else:
                            for pending in q:
                                pending.cancel()
                            raise
            else:
                pending_set: set[concurrent.futures.Future] = set()
                for item_args in args_iter:
                    while len(pending_set) >= window:
                        done, pending_set = concurrent.futures.wait(pending_set, return_when=concurrent.futures.FIRST_COMPLETED)
                        for fut in done:
                            try:
                                yield fut.result()
                            except Exception as exc:
                                if return_exceptions:
                                    yield exc
                                else:
                                    for p in pending_set:
                                        p.cancel()
                                    raise
                    pending_set.add(pool.submit(_call_one, item_args))
                while pending_set:
                    done, pending_set = concurrent.futures.wait(pending_set, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        try:
                            yield fut.result()
                        except Exception as exc:
                            if return_exceptions:
                                yield exc
                            else:
                                for p in pending_set:
                                    p.cancel()
                                raise

    def deploy_spec(self) -> dict:
        req = self._request((), {})
        req.pop("args_b64", None)
        return req

    @staticmethod
    def _unwrap(doc: dict) -> Any:
        st = doc["status"]
        if st == "ok":
            if doc.get("result_format") == "json":
                return json.loads(base64.b64decode(doc["result_b64"]))
            return cloudpickle.loads(base64.b64decode(doc["result_b64"]))        # result of YOUR OWN function in YOUR OWN tenant sandbox
        err = doc.get("error") or {}
        msg = err.get("message", "")
        if st == "error":
            raise E.RemoteError(err.get("type", "Error"), msg, err.get("traceback", ""), doc.get("stdout", ""), doc.get("stderr", ""))
        if st == "timeout":
            raise E.FunctionTimeout(f"the function exceeded its timeout_s. Raise timeout_s in @app.function(...) or make it faster. {msg}")
        if st in ("oom", "oom_vram"):
            raise E.FunctionOOM(f"the function ran out of memory. Raise memory_mb/vram_mb in @app.function(...). {msg}")
        if st == "evicted":
            raise E.Preempted("the GPU owner needed its VRAM back and your container was stopped (serverless is a guest on this GPU). "
                              "Retry; set retries=N to do it automatically if the function is idempotent.")
        if st in ("worker_lost", "duplicate"):
            raise E.WorkerLost("the worker running your function disappeared; the function MAY have started. Retry only if it is idempotent "
                             "(or set retries=N in @app.function(...)).")
        if st == "env_failed":
            raise E.EnvBuildFailed(msg + " (only packages with prebuilt wheels are supported)")
        raise E.ServerError(f"{st}: {msg}")


class ClsInstance:
    def __init__(self, methods: dict[str, Function]):
        self._methods = methods
        for name, fn_obj in methods.items():
            setattr(self, name, fn_obj)


class ClsHandle:
    def __init__(self, app: "App", raw_cls: type, **opts):
        self.app, self.raw_cls, self.opts = app, raw_cls, opts
        self.name = (re.sub(r"[^A-Za-z0-9_.-]", "_", raw_cls.__name__).lstrip("_.-") or "cls")[:64]
        enter_m, exit_m, call_m = [], [], []
        for attr_name, val in raw_cls.__dict__.items():
            if attr_name.startswith("_") or not callable(val):
                continue
            if getattr(val, "_aicf_enter", False):
                enter_m.append(attr_name)
            elif getattr(val, "_aicf_exit", False):
                exit_m.append(attr_name)
            elif getattr(val, "_aicf_method", False):
                call_m.append(attr_name)
        if not call_m:
            # If no explicit @aicf.method() decorator was used, expose all public non-enter/non-exit methods
            for attr_name, val in raw_cls.__dict__.items():
                if not attr_name.startswith("_") and callable(val) and attr_name not in enter_m and attr_name not in exit_m:
                    call_m.append(attr_name)
        self.enter_methods = tuple(enter_m)
        self.exit_methods = tuple(exit_m)
        self.methods: dict[str, Function] = {}
        for mname in call_m:
            self.methods[mname] = Function(
                app, raw_cls, kind="cls", method_name=mname,
                enter_methods=self.enter_methods, exit_methods=self.exit_methods,
                fn_id_override=self.name, **opts,
            )

    def __call__(self) -> ClsInstance:
        return ClsInstance(self.methods)


class App:
    def __init__(self, name: str, client: Optional[Client] = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise E.InvalidRequest("app name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        self.name, self._client = name, client
        self.functions: dict[str, Function] = {}
        self.classes: dict[str, ClsHandle] = {}
        self.web_endpoints: dict[str, dict] = {}

    def client(self) -> Client:
        if self._client is None:
            self._client = Client()
        return self._client

    def function(self, *, image: Optional[Image] = None, gpu: Optional[str] = None, vram_mb: int = 0, memory_mb: int = 512, cpu: float = 1.0,
                 timeout_s: float = 30.0, keep_warm_s: int = 60, retries: int = 0, data_class: str = "INTERNAL",
                 secrets: Iterable[Secret | str] = (), volumes: Optional[dict[str, Volume | str]] = None,
                 web_endpoint: bool = False, requires_auth: bool = True):
        opts = dict(image=image or Image.debian_slim(), gpu=gpu, vram_mb=vram_mb, memory_mb=memory_mb, cpu=cpu, timeout_s=timeout_s,
                    keep_warm_s=keep_warm_s, retries=retries, data_class=data_class, secrets=tuple(secrets), volumes=volumes or {})

        def deco(fn):
            wcfg = {"requires_auth": requires_auth, "cors_origins": []} if web_endpoint else getattr(fn, "_aicf_web", None)
            f = Function(self, fn, web_config=wcfg, **opts)
            self.functions[f.name] = f
            if wcfg is not None:
                self.web_endpoints[f.name] = wcfg
            return f
        return deco

    def web_endpoint(self, *, requires_auth: bool = True, cors_origins: Iterable[str] = ()):
        def deco(fn_or_func):
            wcfg = {"requires_auth": requires_auth, "cors_origins": list(cors_origins)}
            if isinstance(fn_or_func, Function):
                fn_or_func.web_config = wcfg
                self.web_endpoints[fn_or_func.name] = wcfg
                return fn_or_func
            setattr(fn_or_func, "_aicf_web", wcfg)
            return fn_or_func
        return deco

    def cls(self, *, image: Optional[Image] = None, gpu: Optional[str] = None, vram_mb: int = 0, memory_mb: int = 512, cpu: float = 1.0,
            timeout_s: float = 30.0, keep_warm_s: int = 60, retries: int = 0, data_class: str = "INTERNAL",
            secrets: Iterable[Secret | str] = (), volumes: Optional[dict[str, Volume | str]] = None):
        opts = dict(image=image or Image.debian_slim(), gpu=gpu, vram_mb=vram_mb, memory_mb=memory_mb, cpu=cpu, timeout_s=timeout_s,
                    keep_warm_s=keep_warm_s, retries=retries, data_class=data_class, secrets=tuple(secrets), volumes=volumes or {})

        def deco(raw_cls: type) -> ClsHandle:
            ch = ClsHandle(self, raw_cls, **opts)
            self.classes[ch.name] = ch
            for mname, f in ch.methods.items():
                self.functions[f"{ch.name}.{mname}"] = f
            return ch
        return deco

    def deploy(self) -> dict:
        fn_specs = [f.deploy_spec() for f in self.functions.values()]
        return self.client().post("/deploy", {
            "app_name": self.name,
            "functions": fn_specs,
            "web_endpoints": self.web_endpoints,
        })

    def rollback(self, version: Optional[str] = None) -> dict:
        body = {"version": version} if version else {}
        return self.client().post(f"/apps/{self.name}/rollback", body)


# ==============================================================================
# Beam / Beta9-Compatible Top-Level Primitives (@endpoint, @task_queue, @function, Sandbox)
# ==============================================================================

def _normalize_volumes(volumes: Optional[dict[str, Volume | str] | Iterable[Volume | str]]) -> dict[str, Volume | str]:
    if not volumes:
        return {}
    if isinstance(volumes, dict):
        return dict(volumes)
    out: dict[str, Volume | str] = {}
    for v in volumes:
        if isinstance(v, Volume):
            out[v.mount_path] = v
        else:
            s = str(v)
            out[f"/mnt/volumes/{s}"] = s
    return out


def endpoint(name: Optional[str] = None, *, gpu: Optional[str] = None, vram_mb: int = 0,
             memory: int = 512, memory_mb: Optional[int] = None, cpu: float = 1.0,
             keep_warm_seconds: int = 60, keep_warm_s: Optional[int] = None,
             timeout: float = 30.0, timeout_s: Optional[float] = None,
             retries: int = 0, image: Optional[Image] = None,
             volumes: Optional[dict[str, Volume | str] | Iterable[Volume | str]] = None,
             secrets: Iterable[Secret | str] = (), requires_auth: bool = True,
             app: Optional[App] = None, client: Optional[Client] = None):
    """Beam/Beta9-style `@endpoint(...)` decorator for synchronous REST API endpoints."""
    def deco(fn: Callable) -> Function:
        import functools
        import inspect
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            pos_params = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            needs_kw_unpack = has_var_kw and len(pos_params) == 0
        except Exception:
            needs_kw_unpack = False

        if needs_kw_unpack:
            @functools.wraps(fn)
            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
                    return fn(**args[0])
                return fn(*args, **kwargs)
            target_fn: Callable = _wrapped
        else:
            target_fn = fn

        app_name = name or getattr(fn, "__name__", "endpoint-app").replace("_", "-")
        target_app = app or App(app_name, client=client)
        eff_mem = memory_mb if memory_mb is not None else memory
        eff_warm = keep_warm_s if keep_warm_s is not None else keep_warm_seconds
        eff_timeout = timeout_s if timeout_s is not None else timeout
        eff_vram = vram_mb or (1024 if gpu else 0)
        return target_app.function(
            image=image, gpu=gpu, vram_mb=eff_vram, memory_mb=eff_mem, cpu=cpu,
            timeout_s=eff_timeout, keep_warm_s=eff_warm, retries=retries,
            secrets=secrets, volumes=_normalize_volumes(volumes),
            web_endpoint=True, requires_auth=requires_auth,
        )(target_fn)
    return deco


def task_queue(name: Optional[str] = None, *, gpu: Optional[str] = None, vram_mb: int = 0,
               memory: int = 512, memory_mb: Optional[int] = None, cpu: float = 1.0,
               keep_warm_seconds: int = 60, keep_warm_s: Optional[int] = None,
               timeout: float = 120.0, timeout_s: Optional[float] = None,
               retries: int = 1, image: Optional[Image] = None,
               volumes: Optional[dict[str, Volume | str] | Iterable[Volume | str]] = None,
               secrets: Iterable[Secret | str] = (),
               app: Optional[App] = None, client: Optional[Client] = None):
    """Beam/Beta9-style `@task_queue(...)` decorator for async background jobs (`fn.put(...)` / `fn.spawn(...)`)."""
    def deco(fn: Callable) -> Function:
        app_name = name or getattr(fn, "__name__", "task-queue-app").replace("_", "-")
        target_app = app or App(app_name, client=client)
        eff_mem = memory_mb if memory_mb is not None else memory
        eff_warm = keep_warm_s if keep_warm_s is not None else keep_warm_seconds
        eff_timeout = timeout_s if timeout_s is not None else timeout
        eff_vram = vram_mb or (1024 if gpu else 0)
        return target_app.function(
            image=image, gpu=gpu, vram_mb=eff_vram, memory_mb=eff_mem, cpu=cpu,
            timeout_s=eff_timeout, keep_warm_s=eff_warm, retries=retries,
            secrets=secrets, volumes=_normalize_volumes(volumes),
            web_endpoint=False,
        )(fn)
    return deco


def function(name: Optional[str] = None, *, gpu: Optional[str] = None, vram_mb: int = 0,
             memory: int = 512, memory_mb: Optional[int] = None, cpu: float = 1.0,
             keep_warm_seconds: int = 60, keep_warm_s: Optional[int] = None,
             timeout: float = 30.0, timeout_s: Optional[float] = None,
             retries: int = 0, image: Optional[Image] = None,
             volumes: Optional[dict[str, Volume | str] | Iterable[Volume | str]] = None,
             secrets: Iterable[Secret | str] = (),
             app: Optional[App] = None, client: Optional[Client] = None):
    """Beam/Beta9-style standalone `@function(...)` decorator (`fn.remote(...)`, `fn.map(...)`, `fn.spawn(...)`)."""
    def deco(fn: Callable) -> Function:
        app_name = name or getattr(fn, "__name__", "fn-app").replace("_", "-")
        target_app = app or App(app_name, client=client)
        eff_mem = memory_mb if memory_mb is not None else memory
        eff_warm = keep_warm_s if keep_warm_s is not None else keep_warm_seconds
        eff_timeout = timeout_s if timeout_s is not None else timeout
        eff_vram = vram_mb or (1024 if gpu else 0)
        return target_app.function(
            image=image, gpu=gpu, vram_mb=eff_vram, memory_mb=eff_mem, cpu=cpu,
            timeout_s=eff_timeout, keep_warm_s=eff_warm, retries=retries,
            secrets=secrets, volumes=_normalize_volumes(volumes),
            web_endpoint=False,
        )(fn)
    return deco


def _sandbox_exec_payload(cmd_or_code: str, mode: str = "code") -> dict:
    import io
    import os
    import subprocess
    import traceback
    from contextlib import redirect_stderr, redirect_stdout

    if mode == "shell":
        proc = subprocess.run(cmd_or_code, shell=True, capture_output=True, text=True, timeout=25)  # noqa: S602 — inside isolated container
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "result": None,
            "pid": os.getpid(),
        }
    out_buf, err_buf = io.StringIO(), io.StringIO()
    ns: dict[str, Any] = {"__name__": "__aicf_sandbox__"}
    exit_code = 0
    res_val = None
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            code_obj = compile(cmd_or_code, "<sandbox>", "exec")
            exec(code_obj, ns)  # noqa: S102 — inside isolated rootless container
            res_val = ns.get("result")
    except Exception:
        exit_code = 1
        err_buf.write(traceback.format_exc())
    return {
        "exit_code": exit_code,
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "result": res_val,
        "pid": os.getpid(),
    }


class SandboxResult:
    """Result of executing Python code or a command inside an `aicf.Sandbox`."""

    def __init__(self, doc: dict):
        self.exit_code: int = int(doc.get("exit_code", 0))
        self.returncode: int = self.exit_code
        self.stdout: str = str(doc.get("stdout", ""))
        self.stderr: str = str(doc.get("stderr", ""))
        self.result: Any = doc.get("result")
        self.pid: int = int(doc.get("pid", 0))

    def logs(self) -> str:
        return self.stdout + (("\n" + self.stderr) if self.stderr else "")

    def __repr__(self) -> str:
        return f"SandboxResult(exit_code={self.exit_code}, stdout={self.stdout!r}, result={self.result!r})"


class _SandboxProcessNamespace:
    def __init__(self, inst: "SandboxInstance"):
        self._inst = inst

    def exec(self, command: str) -> SandboxResult:
        return self._inst.exec(command, mode="shell")


class SandboxInstance:
    """Active isolated Sandbox handle (compatible with `beam-cloud/beta9` `Sandbox.create()`)."""

    def __init__(self, sandbox: "Sandbox", fn: Function):
        self.sandbox = sandbox
        self._fn = fn
        self.process = _SandboxProcessNamespace(self)

    def run_code(self, python_code: str) -> SandboxResult:
        """Execute arbitrary Python code inside the isolated container sandbox."""
        doc = self._fn.remote(python_code, "code")
        return SandboxResult(doc if isinstance(doc, dict) else {"stdout": str(doc)})

    def exec(self, command_or_code: str, *, mode: str = "code") -> SandboxResult:
        """Execute code (`mode='code'`) or shell command (`mode='shell'`) inside the sandbox."""
        doc = self._fn.remote(command_or_code, mode)
        return SandboxResult(doc if isinstance(doc, dict) else {"stdout": str(doc)})

    def terminate(self) -> dict:
        """Evict any warm container associated with this sandbox and release its GPU lease."""
        client = self._fn.app.client()
        containers = client.get("/containers").get("containers", [])
        evicted = []
        for c in containers:
            if c.get("app_name") == self._fn.app.name:
                cid = c.get("container_id")
                if cid:
                    client.delete(f"/containers/{cid}?reason=idle")
                    evicted.append(cid)
        return {"terminated": True, "evicted_containers": evicted}

    def __enter__(self) -> "SandboxInstance":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.terminate()
        except Exception:
            pass


class Sandbox:
    """Beam/Beta9 & Modal-compatible isolated Sandbox environment on AICF Serverless."""

    def __init__(self, *, name: str = "aicf-sandbox", image: Optional[Image] = None,
                 gpu: Optional[str] = None, vram_mb: int = 0, memory: int = 512,
                 memory_mb: Optional[int] = None, cpu: float = 1.0,
                 keep_warm_seconds: int = 120, timeout: float = 30.0,
                 volumes: Optional[dict[str, Volume | str] | Iterable[Volume | str]] = None,
                 client: Optional[Client] = None):
        self.name = name
        self.image = image or Image.debian_slim()
        self.gpu = gpu
        self.vram_mb = vram_mb or (1024 if gpu else 0)
        self.memory_mb = memory_mb if memory_mb is not None else memory
        self.cpu = cpu
        self.keep_warm_s = keep_warm_seconds
        self.timeout_s = timeout
        self.volumes = _normalize_volumes(volumes)
        self._client = client

    def create(self) -> SandboxInstance:
        app = App(self.name, client=self._client)
        fn = app.function(
            image=self.image, gpu=self.gpu, vram_mb=self.vram_mb,
            memory_mb=self.memory_mb, cpu=self.cpu,
            timeout_s=self.timeout_s, keep_warm_s=self.keep_warm_s,
            volumes=self.volumes,
        )(_sandbox_exec_payload)
        return SandboxInstance(self, fn)

