"""Tiny Prometheus text exposition (counters + summaries); labels are escaped, tenant ids are validated upstream."""
from __future__ import annotations

import threading
from collections import defaultdict


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    def __init__(self):
        self._c = defaultdict(float)
        self._help = {}
        self._mu = threading.Lock()

    def inc(self, name: str, help_: str, value: float = 1.0, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._mu:
            self._c[key] += value
            self._help[name] = help_

    def render(self) -> str:
        with self._mu:
            items, helps = sorted(self._c.items()), dict(self._help)
        out, seen = [], set()
        for (name, labels), v in items:
            if name not in seen:
                seen.add(name)
                out += [f"# HELP {name} {helps[name]}", f"# TYPE {name} counter"]
            lab = "{" + ",".join(f'{k}="{_esc(x)}"' for k, x in labels) + "}" if labels else ""
            out.append(f"{name}{lab} {v:g}")
        return "\n".join(out) + "\n"
