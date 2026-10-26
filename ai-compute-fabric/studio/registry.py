"""Method registry: every way of customising a model is a module that describes itself and mounts its routes.

Adding a method (DPO, distillation, ...) means writing one module with a `Method` and registering it in studio/app.py;
the gateway proxy, the OpenAPI conformance harness and the UI tab list (built from GET /v1/methods) need no change."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from studio.web import Router


@dataclass(frozen=True)
class MethodInfo:
    id: str
    title: str
    summary: str                    # one paragraph a non-expert operator can read
    status: str                     # "available" | "unsupported"
    reason: str = ""                # why unsupported / what is missing
    when_to_use: str = ""
    limits: tuple = ()
    ui: dict = field(default_factory=dict)


class Method(Protocol):
    info: MethodInfo

    def mount(self, router: Router) -> None: ...


class Registry:
    def __init__(self):
        self._m: dict = {}

    def register(self, method: Method) -> Method:
        if method.info.id in self._m:
            raise ValueError(f"method {method.info.id!r} registered twice")
        if method.info.status not in ("available", "unsupported"):
            raise ValueError("status must be 'available' or 'unsupported'")
        self._m[method.info.id] = method
        return method

    def infos(self) -> list:
        return [m.info for m in self._m.values()]

    def mount_all(self, router: Router):
        for m in self._m.values():
            m.mount(router)
