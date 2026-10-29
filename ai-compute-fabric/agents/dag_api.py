"""Read-only HTTP view of agent workflows for the dashboard (screen 4a).

    GET /workflows            list with per-state counts
    GET /workflows/<id>       nodes, dependencies and status of every step

It reads the orchestrator's own tables, so it also works for workflows another process is running.
It never returns task inputs or outputs (they can hold the documents the agent is reading). It has no
authentication of its own: run it on loopback and let the site gateway proxy it (--agents-url), which
applies the admin roles."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote

from agents.orchestrator import Orchestrator
from agents.task_bus import BusMetricsCollector, ElasticSupervisor

WF = re.compile(r"^/workflows/([^/]{1,200})/?$")


def make_handler(
    orch: Optional[Orchestrator] = None,
    metrics_collector: Optional[BusMetricsCollector] = None,
    supervisor: Optional[ElasticSupervisor] = None,
):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _send_text(self, code: int, text: str, content_type: str = "text/plain; version=0.0.4"):
            b = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.rstrip("/") == "/metrics" and metrics_collector is not None:
                return self._send_text(200, metrics_collector.render_prometheus())
            if path.rstrip("/") == "/supervisor/recommendations" and metrics_collector is not None:
                sup = supervisor or ElasticSupervisor(propose_only=True)
                recs = sup.evaluate(metrics_collector.collect())
                return self._send(200, [asdict(r) for r in recs])
            if orch is not None:
                if path.rstrip("/") == "/workflows":
                    return self._send(200, orch.workflows())
                m = WF.match(path)
                if m:
                    snap = orch.snapshot(unquote(m.group(1)))
                    return self._send(200, snap) if snap else self._send(404, {"error": "unknown workflow"})
            self._send(404, {"error": "not found"})

        def _no(self):
            self._send(405, {"error": "read-only"})
        do_POST = do_PUT = do_PATCH = do_DELETE = _no

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--listen", default="127.0.0.1:18200")
    ap.add_argument("--nats-url", default="")
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)
    class NoExec:
        def execute(self, *x):
            raise RuntimeError("the dashboard API never executes tasks")
    orch = Orchestrator(a.dsn, NoExec())
    collector = BusMetricsCollector(a.nats_url) if a.nats_url else None
    sup = ElasticSupervisor(propose_only=True) if a.nats_url else None
    ThreadingHTTPServer((host, int(port)), make_handler(orch, collector, sup)).serve_forever()


if __name__ == "__main__":
    main()
