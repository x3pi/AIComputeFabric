"""Agent-aware sampler for Best-of-N code generation (Pha 2.1 & §2B).

Works EXCLUSIVELY with ComputeAgentInfo from ComputeAgentsClient:
  - Allocates N samples across available compute agents proportional to slots * tok_s_ref,
    subject to ctx_per_slot >= required_ctx and max_share_per_agent cap when multiple agents exist.
  - Bounds concurrent requests sent to each agent by agent.slots.
  - Sends POST /v1/chat/completions to the gateway with X-Priority: batch and X-AICF-Agent: <agent_id>.
  - Automatically reassigns and regenerates samples on another live agent if an agent disappears
    or becomes unavailable mid-run (e.g. during a topology merge/split or node failure).
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from bestofn.agents_client import ComputeAgentInfo, ComputeAgentsClient
from bestofn.planner import PlanVariant


@dataclass
class SampleResult:
    sample_index: int
    strategy_id: str
    temperature: float
    seed: int
    ok: bool
    agent_id: str
    model: str
    content: str = ""
    gen_ms: float = 0.0
    completion_tokens: int = 0
    prompt_tokens: int = 0
    tok_s: float = 0.0
    attempts: int = 1
    reassigned_from: list[str] = field(default_factory=list)
    error: str = ""


def select_eligible_agents(
    agents: list[ComputeAgentInfo],
    requested_model: str = "",
    required_label: str = "code",
    required_ctx: int = 512,
    allow_multi_model: bool = False,
) -> list[ComputeAgentInfo]:
    """Filter running compute agents that satisfy ctx_per_slot, label, and model constraints."""
    running = [
        a for a in agents
        if a.state == "running" and a.ctx_per_slot >= required_ctx
    ]
    if required_label:
        lbl = required_label.lower()
        labeled = [a for a in running if lbl in [x.lower() for x in a.labels]]
        if labeled:
            running = labeled

    if not running:
        return []

    clean_model = requested_model.strip()
    if clean_model.startswith("bon:"):
        clean_model = clean_model[4:].strip()

    if allow_multi_model:
        return running

    if clean_model and clean_model not in ("*", "auto", "any"):
        exact = [a for a in running if a.model == clean_model]
        if exact:
            return exact

    # If no specific model was requested (or requested model changed after a topology switch),
    # pick the highest-capacity model family unless allow_multi_model is enabled.
    best_agent = max(running, key=lambda a: a.capacity_score)
    return [a for a in running if a.model == best_agent.model]


def allocate_samples_to_agents(
    variants: list[PlanVariant],
    agents: list[ComputeAgentInfo],
    max_share_per_agent: float = 0.70,
) -> dict[int, ComputeAgentInfo]:
    """Allocate N sample indices across K compute agents weighted by (slots * tok_s_ref).

    When K >= 2 and max_share_per_agent < 1.0, caps any single agent at ceil(N * max_share_per_agent)
    so a single fast agent failure cannot invalidate the entire batch (§2B.4).
    """
    n = len(variants)
    if not agents:
        raise RuntimeError("No eligible compute agents available")
    if len(agents) == 1:
        return {v.sample_index: agents[0] for v in variants}

    ordered = sorted(agents, key=lambda a: (-a.capacity_score, a.agent_id))
    total_cap = sum(a.capacity_score for a in ordered)
    max_per_agent = n
    if len(ordered) >= 2 and 0.0 < max_share_per_agent < 1.0:
        # Ensure total cap across all agents is at least n
        cap_candidate = max(1, math.ceil(n * max_share_per_agent))
        if cap_candidate * len(ordered) >= n:
            max_per_agent = cap_candidate

    counts: dict[str, int] = {a.agent_id: 0 for a in ordered}
    quotas: list[tuple[float, ComputeAgentInfo]] = []
    assigned_total = 0
    for a in ordered:
        exact = n * (a.capacity_score / total_cap)
        base = min(max_per_agent, int(math.floor(exact)))
        counts[a.agent_id] = base
        assigned_total += base
        quotas.append((exact - base, a))

    quotas.sort(key=lambda x: (-x[0], -x[1].capacity_score, x[1].agent_id))
    while assigned_total < n:
        progressed = False
        for _, a in quotas:
            if assigned_total >= n:
                break
            if counts[a.agent_id] < max_per_agent:
                counts[a.agent_id] += 1
                assigned_total += 1
                progressed = True
        if not progressed:
            # Fallback if max_per_agent was reached everywhere
            for _, a in quotas:
                if assigned_total >= n:
                    break
                counts[a.agent_id] += 1
                assigned_total += 1

    # Interleave assignments round-robin across agents so early variants start on distinct agents immediately
    by_id = {a.agent_id: a for a in ordered}
    remaining = dict(counts)
    schedule: list[ComputeAgentInfo] = []
    while len(schedule) < n:
        for a in ordered:
            if remaining[a.agent_id] > 0:
                schedule.append(by_id[a.agent_id])
                remaining[a.agent_id] -= 1
                if len(schedule) >= n:
                    break

    return {variants[i].sample_index: schedule[i] for i in range(n)}


def _post_completion(
    gateway_url: str,
    api_key: str,
    agent: ComputeAgentInfo,
    variant: PlanVariant,
    max_tokens: int,
    timeout_s: float,
) -> dict[str, Any]:
    url = f"{gateway_url.rstrip('/')}/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": agent.model,
        "messages": variant.messages,
        "temperature": variant.temperature,
        "max_tokens": max_tokens,
        "seed": variant.seed,
        "stream": False,
    }
    if variant.temperature > 0.0:
        payload["top_p"] = 0.95
    headers = {
        "Content-Type": "application/json",
        "X-Priority": "batch",
        "X-AICF-Agent": agent.agent_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = time.perf_counter()
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            dt_ms = (time.perf_counter() - t0) * 1000.0
            served_agent = resp.headers.get("X-AICF-Agent") or agent.agent_id
            data = json.loads(raw.decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            timings = data.get("timings", {})
            comp_tokens = int(usage.get("completion_tokens") or timings.get("predicted_n") or 0)
            prompt_tokens = int(usage.get("prompt_tokens") or timings.get("prompt_n") or 0)
            tok_s = float(
                timings.get("predicted_per_second")
                or ((comp_tokens / (dt_ms / 1000.0)) if dt_ms > 0 else 0.0)
            )
            return {
                "ok": True,
                "agent_id": served_agent,
                "model": agent.model,
                "content": content,
                "gen_ms": round(dt_ms, 2),
                "completion_tokens": comp_tokens,
                "prompt_tokens": prompt_tokens,
                "tok_s": round(tok_s, 2),
            }
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:300]
        retry_after = float(exc.headers.get("Retry-After", "0.5") or "0.5")
        return {
            "ok": False,
            "status_code": exc.code,
            "retry_after": retry_after,
            "error": f"HTTP {exc.code}: {err_body}",
            "gen_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "retry_after": 0.5,
            "error": str(exc),
            "gen_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }


def sample_candidates(
    variants: list[PlanVariant],
    agents_client: ComputeAgentsClient,
    gateway_url: str = "http://127.0.0.1:18080",
    api_key: str = "",
    requested_model: str = "",
    required_label: str = "code",
    max_tokens: int = 512,
    timeout_s: float = 90.0,
    max_concurrency: int = 16,
    max_share_per_agent: float = 0.70,
    allow_multi_model: bool = False,
    max_reassign_attempts: int = 4,
) -> tuple[list[SampleResult], dict[str, Any]]:
    """Sample all N variants across discovered compute agents with slot-aware concurrency and failover."""
    t_start = time.perf_counter()
    max_prompt_est = max((v.est_prompt_tokens for v in variants), default=256)
    required_ctx = max_prompt_est + max_tokens

    live_agents = agents_client.list_agents(force_refresh=True)
    eligible = select_eligible_agents(
        live_agents,
        requested_model=requested_model,
        required_label=required_label,
        required_ctx=required_ctx,
        allow_multi_model=allow_multi_model,
    )
    if not eligible:
        raise RuntimeError(
            f"No eligible compute agents found (discovered={len(live_agents)}, required_ctx={required_ctx})"
        )

    initial_plan = allocate_samples_to_agents(
        variants, eligible, max_share_per_agent=max_share_per_agent
    )

    sem_lock = threading.Lock()
    agent_semaphores: dict[str, threading.Semaphore] = {
        a.agent_id: threading.Semaphore(max(1, a.slots)) for a in eligible
    }

    def get_sem(agent: ComputeAgentInfo) -> threading.Semaphore:
        with sem_lock:
            if agent.agent_id not in agent_semaphores:
                agent_semaphores[agent.agent_id] = threading.Semaphore(max(1, agent.slots))
            return agent_semaphores[agent.agent_id]

    reassignment_events: list[dict[str, Any]] = []
    events_lock = threading.Lock()

    def run_one(variant: PlanVariant) -> SampleResult:
        current_agent = initial_plan[variant.sample_index]
        reassigned_from: list[str] = []
        last_err = ""
        total_gen_ms = 0.0

        for attempt in range(1, max_reassign_attempts + 2):
            sem = get_sem(current_agent)
            with sem:
                res = _post_completion(
                    gateway_url=gateway_url,
                    api_key=api_key,
                    agent=current_agent,
                    variant=variant,
                    max_tokens=max_tokens,
                    timeout_s=timeout_s,
                )
            total_gen_ms += res.get("gen_ms", 0.0)
            if res["ok"]:
                return SampleResult(
                    sample_index=variant.sample_index,
                    strategy_id=variant.strategy_id,
                    temperature=variant.temperature,
                    seed=variant.seed,
                    ok=True,
                    agent_id=res["agent_id"],
                    model=res["model"],
                    content=res["content"],
                    gen_ms=round(total_gen_ms, 2),
                    completion_tokens=res["completion_tokens"],
                    prompt_tokens=res["prompt_tokens"],
                    tok_s=res["tok_s"],
                    attempts=attempt,
                    reassigned_from=reassigned_from,
                )

            last_err = res.get("error", "unknown error")
            status_code = int(res.get("status_code", 0))
            if attempt > max_reassign_attempts:
                break

            # Refresh agent list to handle agent loss / topology switch / busy slot
            time.sleep(min(max(float(res.get("retry_after", 0.2)), 0.05), 2.0))
            try:
                refreshed = agents_client.list_agents(force_refresh=True)
                now_eligible = select_eligible_agents(
                    refreshed,
                    requested_model=requested_model,
                    required_label=required_label,
                    required_ctx=required_ctx,
                    allow_multi_model=allow_multi_model or True,
                )
            except Exception:
                now_eligible = eligible

            if not now_eligible:
                continue

            # Prefer an alternative non-busy agent if current_agent disappeared or failed
            alternatives = [
                a for a in now_eligible
                if a.agent_id != current_agent.agent_id and not a.busy
            ]
            if not alternatives:
                alternatives = [a for a in now_eligible if a.agent_id != current_agent.agent_id]
            if alternatives:
                next_agent = max(alternatives, key=lambda a: a.capacity_score)
                reassigned_from.append(current_agent.agent_id)
                with events_lock:
                    reassignment_events.append({
                        "sample_index": variant.sample_index,
                        "from_agent_id": current_agent.agent_id,
                        "to_agent_id": next_agent.agent_id,
                        "reason": last_err,
                        "attempt": attempt,
                    })
                current_agent = next_agent
            elif status_code in (429, 503):
                # Same agent is still the only one (e.g. single-agent cluster); retry after short wait
                current_agent = now_eligible[0]

        return SampleResult(
            sample_index=variant.sample_index,
            strategy_id=variant.strategy_id,
            temperature=variant.temperature,
            seed=variant.seed,
            ok=False,
            agent_id=current_agent.agent_id,
            model=current_agent.model,
            gen_ms=round(total_gen_ms, 2),
            attempts=max_reassign_attempts + 1,
            reassigned_from=reassigned_from,
            error=last_err,
        )

    total_slots = sum(max(1, a.slots) for a in eligible)
    pool_concurrency = max(1, min(len(variants), max_concurrency, total_slots * 2))
    with concurrent.futures.ThreadPoolExecutor(pool_concurrency) as ex:
        futures = [ex.submit(run_one, v) for v in variants]
        results = [f.result() for f in futures]

    results.sort(key=lambda r: r.sample_index)
    wall_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

    by_agent_counts: dict[str, int] = {}
    for r in results:
        if r.ok:
            by_agent_counts[r.agent_id] = by_agent_counts.get(r.agent_id, 0) + 1

    telemetry = {
        "discovered_agents": [a.to_dict() for a in eligible],
        "initial_allocation": {str(k): v.agent_id for k, v in initial_plan.items()},
        "served_by_agent": by_agent_counts,
        "reassignments": reassignment_events,
        "sampling_wall_ms": wall_ms,
    }
    return results, telemetry
