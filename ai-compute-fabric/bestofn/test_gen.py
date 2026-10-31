"""Self-generated unit test suite generator and validator for Pha 3 (CodeT-style consensus).

When the caller does not provide unit tests:
  1. Generates M independent Python assert test suites (default M=3) from the problem prompt.
  2. Extracts individual assert statements / mini-suites and filters out syntactically invalid ones.
  3. After running N candidates x M test suites in DockerSandbox, filters out test suites that
     pass on 0 candidates (likely broken/hallucinated assertions) before CodeT dual-agreement scoring.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time
import urllib.request
from typing import Any

from bestofn.agents_client import ComputeAgentInfo, ComputeAgentsClient
from bestofn.sampler import select_eligible_agents


TEST_GEN_HINTS = [
    "Focus on basic nominal examples and docstring examples.",
    "Focus on boundary cases: empty inputs, single elements, zeros, negative numbers, and ties.",
    "Focus on type invariants, medium-size inputs, and corner cases.",
]


def _extract_assertions(raw_text: str) -> list[str]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    body = blocks[-1] if blocks else cleaned
    assertions: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("assert "):
            try:
                compile(s, "<gen_assert>", "exec")
                assertions.append(s)
            except SyntaxError:
                continue
    return assertions


def generate_test_suites(
    problem_prompt: str,
    entry_point: str = "",
    m_suites: int = 3,
    gateway_url: str = "http://127.0.0.1:18080",
    api_key: str = "",
    agents_client: ComputeAgentsClient | None = None,
    requested_model: str = "",
    seed: int = 700,
    timeout_s: float = 45.0,
) -> tuple[list[str], dict[str, Any]]:
    """Generate M independent test suites (each containing 2-5 assert statements)."""
    t0 = time.perf_counter()
    if m_suites < 1:
        return [], {"m_requested": 0, "m_valid": 0, "test_gen_wall_ms": 0.0}

    client = agents_client or ComputeAgentsClient(gateway_url=gateway_url, api_key=api_key)
    live = client.list_agents(force_refresh=False)
    eligible = select_eligible_agents(live, requested_model=requested_model, required_label="code", required_ctx=512)
    if not eligible:
        return [], {"m_requested": m_suites, "m_valid": 0, "test_gen_wall_ms": 0.0, "error": "no eligible agent"}

    fn_hint = f" for function `{entry_point}`" if entry_point else ""

    def gen_one(idx: int) -> list[str]:
        agent: ComputeAgentInfo = eligible[idx % len(eligible)]
        hint = TEST_GEN_HINTS[idx % len(TEST_GEN_HINTS)]
        sys_msg = (
            "You are a Python QA engineer. Write 4 standalone Python `assert` statements testing the specified function. "
            "Output ONLY `assert ...` lines inside a ```python ... ``` block. Do not define the function itself. /no_think"
        )
        usr_msg = (
            f"Write 4 Python `assert` statements{fn_hint} for the following specification.\n"
            f"Guidance: {hint}\n\n"
            f"Specification:\n{problem_prompt}\n"
        )
        url = f"{gateway_url.rstrip('/')}/v1/chat/completions"
        payload = {
            "model": agent.model,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": usr_msg},
            ],
            "temperature": round(0.3 + 0.25 * idx, 2),
            "max_tokens": 256,
            "seed": seed + idx * 37,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Priority": "batch",
            "X-AICF-Agent": agent.agent_id,
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return _extract_assertions(content)
        except Exception:
            return []

    total_slots = sum(max(1, a.slots) for a in eligible)
    concurrency = max(1, min(m_suites, total_slots))
    with concurrent.futures.ThreadPoolExecutor(concurrency) as ex:
        futs = [ex.submit(gen_one, i) for i in range(m_suites)]
        raw_lists = [f.result() for f in futs]

    # Flatten unique individual assertions and also group into suites so a single bad assert
    # does not invalidate all other assertions in the same suite
    seen: set[str] = set()
    individual_asserts: list[str] = []
    for lst in raw_lists:
        for a in lst:
            if a not in seen:
                seen.add(a)
                individual_asserts.append(a)

    # Cap at 9 individual assertion micro-suites (M=3 suites x ~3 assertions)
    suites = individual_asserts[: max(3, m_suites * 3)]
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return suites, {
        "m_requested": m_suites,
        "raw_suites_nonempty": sum(1 for lst in raw_lists if lst),
        "unique_assertions": len(suites),
        "test_gen_wall_ms": wall_ms,
    }


def filter_executable_generated_tests(evaluations: list[Any]) -> int:
    """Zero out generated test suites that failed on ALL candidates (Pha 3.1 rule:
    'lọc test chạy được trên ít nhất một bản'). Returns count of kept suites."""
    if not evaluations:
        return 0
    num_suites = max((len(e.suite_results) for e in evaluations), default=0)
    if num_suites == 0:
        return 0

    active_indices = [
        j for j in range(num_suites)
        if any(
            j < len(e.suite_results) and e.suite_results[j].get("passed", False)
            for e in evaluations
            if e.compile_ok
        )
    ]
    if not active_indices:
        return 0

    for e in evaluations:
        if not e.compile_ok:
            e.passed_tests = 0
            e.total_tests = len(active_indices)
            e.pass_rate = 0.0
            e.pass_vector = "0" * len(active_indices)
            continue
        kept_results = [e.suite_results[j] for j in active_indices if j < len(e.suite_results)]
        passed = sum(1 for r in kept_results if r.get("passed", False))
        e.suite_results = kept_results
        e.passed_tests = passed
        e.total_tests = len(active_indices)
        e.pass_rate = round(passed / len(active_indices), 4)
        e.pass_vector = "".join("1" if r.get("passed", False) else "0" for r in kept_results)

    return len(active_indices)

