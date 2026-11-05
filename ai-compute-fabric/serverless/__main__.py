"""python -m serverless --nats ... --key-file ... --secret-file ... --policy-file ...  (listens on 127.0.0.1:18110)"""
from __future__ import annotations

import argparse
import time

from agents.sl_security import load_keys
from agents.sl_store import NatsStore
from agents.task_bus import BusExecutor, list_live_agents
from agents.tasks import TaskType
from serverless.app import serve
from serverless.dispatch import BusDispatcher
from serverless.quota import Quotas
from serverless.service import ServerlessService


def main() -> None:
    ap = argparse.ArgumentParser(description="AICF serverless service")
    ap.add_argument("--nats", default="nats://127.0.0.1:4222")
    ap.add_argument("--key-file", required=True, help="HMAC signing key file (0600)")
    ap.add_argument("--secret-file", required=True, help="secret shared with site-gateway (--serverless-secret-file)")
    ap.add_argument("--policy-file", required=True, help="per-tenant policy JSON; tenants not listed have serverless disabled")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18110)
    a = ap.parse_args()
    secret = open(a.secret_file).read().strip()
    if len(secret) < 16:
        raise SystemExit("secret must be at least 16 characters")
    bx = BusExecutor(a.nats, task_timeout=720.0, identity="sl-service", max_tenant_inflight=64)
    store = NatsStore(a.nats, identity="sl-service-store")

    cache = {"t": 0.0, "v": []}

    def live():                     # one KV scan per second, not one per call
        now = time.monotonic()
        if now - cache["t"] > 1.0:
            cache["v"], cache["t"] = bx._run(list_live_agents(bx.js, TaskType.SERVERLESS_EXEC)), now
        return cache["v"]

    svc = ServerlessService(keys=load_keys(a.key_file), quotas=Quotas.load(a.policy_file), dispatch=BusDispatcher(bx),
                            live_workers=live, blobs=store)
    srv = serve(svc, secret, a.host, a.port)
    print(f"aicf serverless listening on {a.host}:{a.port}", flush=True)
    try:
        srv.serve_forever()
    finally:
        bx.close()
        store.close()


if __name__ == "__main__":
    main()
