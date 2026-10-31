"""Hardware-hiding compute agent discovery client (Pha A.3, KE_HOACH_BEST_OF_N_CODE.md §2B).

This module ONLY interacts with GET /api/compute-agents (and /api/compute-agents/stream).
It strips any unexpected fields so the orchestrator only ever sees:
  [{agent_id, kind, model, ctx_per_slot, slots, tok_s_ref, labels, state, busy}]
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComputeAgentInfo:
    agent_id: str
    model: str
    ctx_per_slot: int
    slots: int
    tok_s_ref: float
    labels: list[str] = field(default_factory=list)
    state: str = "running"
    kind: str = "single"
    busy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "kind": self.kind,
            "model": self.model,
            "ctx_per_slot": self.ctx_per_slot,
            "slots": self.slots,
            "tok_s_ref": self.tok_s_ref,
            "labels": list(self.labels),
            "state": self.state,
            "busy": self.busy,
        }

    @property
    def capacity_score(self) -> float:
        return max(1, self.slots) * max(1.0, self.tok_s_ref)


class ComputeAgentsClient:
    """Fetches and caches the hardware-hiding list of ComputeAgents from the gateway."""

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:18080",
        api_key: str = "",
        cache_ttl_s: float = 0.5,
        timeout_s: float = 5.0,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s
        self._cached: list[ComputeAgentInfo] = []
        self._cached_at: float = 0.0

    def list_agents(
        self,
        label: str = "",
        model: str = "",
        state: str = "",
        force_refresh: bool = False,
    ) -> list[ComputeAgentInfo]:
        now = time.monotonic()
        if (
            not force_refresh
            and not label
            and not model
            and not state
            and self._cached
            and (now - self._cached_at) <= self.cache_ttl_s
        ):
            return list(self._cached)

        params: dict[str, str] = {"limit": "500"}
        if label:
            params["label"] = label
        if model:
            params["model"] = model
        if state:
            params["state"] = state
        qs = urllib.parse.urlencode(params)
        url = f"{self.gateway_url}/api/compute-agents?{qs}"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        agents: list[ComputeAgentInfo] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("agent_id", "")).strip()
            mdl = str(item.get("model", "")).strip()
            if not aid or not mdl or mdl.startswith("rpc://") or mdl == "rpc-server":
                continue
            agents.append(
                ComputeAgentInfo(
                    agent_id=aid,
                    kind=str(item.get("kind", "single")),
                    model=mdl,
                    ctx_per_slot=max(1, int(item.get("ctx_per_slot", 4096))),
                    slots=max(1, int(item.get("slots", 1))),
                    tok_s_ref=max(1.0, float(item.get("tok_s_ref", 50.0))),
                    labels=[str(x) for x in item.get("labels", []) if x],
                    state=str(item.get("state", "running")),
                    busy=bool(item.get("busy", False)),
                )
            )
        if not label and not model and not state:
            self._cached = list(agents)
            self._cached_at = now
        return agents

    def list_dicts(
        self,
        label: str = "",
        model: str = "",
        state: str = "",
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            a.to_dict()
            for a in self.list_agents(label=label, model=model, state=state, force_refresh=force_refresh)
        ]

