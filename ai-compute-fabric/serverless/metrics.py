"""Tiny Prometheus text exposition (counters + gauges + histograms); labels are escaped, tenant ids are validated upstream."""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Sequence

DEFAULT_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 5.0)


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _fmt_labels(labels: Sequence[tuple[str, str]]) -> str:
    return "{" + ",".join(f'{k}="{_esc(x)}"' for k, x in labels) + "}" if labels else ""


class Metrics:
    def __init__(self):
        self._c = defaultdict(float)
        self._g = {}
        self._h_buckets = {}
        self._h_counts = defaultdict(lambda: defaultdict(int))
        self._h_sum = defaultdict(float)
        self._h_total = defaultdict(int)
        self._help = {}
        self._type = {}
        self._mu = threading.Lock()

    def inc(self, name: str, help_: str, value: float = 1.0, **labels) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._mu:
            self._c[key] += value
            self._help[name] = help_
            self._type[name] = "counter"

    def set_gauge(self, name: str, help_: str, value: float, **labels) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._mu:
            self._g[key] = float(value)
            self._help[name] = help_
            self._type[name] = "gauge"

    def clear_gauge_family(self, name: str) -> None:
        with self._mu:
            for k in [k for k in self._g if k[0] == name]:
                self._g.pop(k, None)

    def observe(self, name: str, help_: str, value: float, buckets: Sequence[float] = DEFAULT_BUCKETS, **labels) -> None:
        lbl = tuple(sorted((k, str(v)) for k, v in labels.items()))
        key = (name, lbl)
        b_sorted = tuple(sorted(float(b) for b in buckets))
        val = float(value)
        with self._mu:
            self._help[name] = help_
            self._type[name] = "histogram"
            self._h_buckets[name] = b_sorted
            for b in b_sorted:
                if val <= b:
                    self._h_counts[key][b] += 1
            self._h_total[key] += 1
            self._h_sum[key] += val

    def render(self) -> str:
        with self._mu:
            c_items = sorted(self._c.items())
            g_items = sorted(self._g.items())
            h_keys = sorted(self._h_total.keys())
            h_buckets = dict(self._h_buckets)
            h_counts = {k: dict(v) for k, v in self._h_counts.items()}
            h_sum = dict(self._h_sum)
            h_total = dict(self._h_total)
            helps = dict(self._help)
        out: list[str] = []
        seen: set[str] = set()
        for (name, labels), v in c_items:
            if name not in seen:
                seen.add(name)
                out += [f"# HELP {name} {helps[name]}", f"# TYPE {name} counter"]
            out.append(f"{name}{_fmt_labels(labels)} {v:g}")
        for (name, labels), v in g_items:
            if name not in seen:
                seen.add(name)
                out += [f"# HELP {name} {helps[name]}", f"# TYPE {name} gauge"]
            out.append(f"{name}{_fmt_labels(labels)} {v:g}")
        for name, labels in h_keys:
            if name not in seen:
                seen.add(name)
                out += [f"# HELP {name} {helps[name]}", f"# TYPE {name} histogram"]
            for b in h_buckets.get(name, DEFAULT_BUCKETS):
                cnt = h_counts.get((name, labels), {}).get(b, 0)
                b_labels = list(labels) + [("le", f"{b:g}")]
                out.append(f"{name}_bucket{_fmt_labels(b_labels)} {cnt}")
            inf_labels = list(labels) + [("le", "+Inf")]
            out.append(f"{name}_bucket{_fmt_labels(inf_labels)} {h_total[(name, labels)]}")
            out.append(f"{name}_sum{_fmt_labels(labels)} {h_sum[(name, labels)]:g}")
            out.append(f"{name}_count{_fmt_labels(labels)} {h_total[(name, labels)]}")
        return "\n".join(out) + "\n"
