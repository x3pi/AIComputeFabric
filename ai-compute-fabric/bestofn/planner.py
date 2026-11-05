"""Prompt variant planner for Best-of-N code generation (Pha 2.1)."""
from __future__ import annotations

from dataclasses import dataclass

STRATEGY_HINTS: list[tuple[str, str]] = [
    (
        "direct_clean",
        "Write the most direct, concise, and idiomatic Python implementation without extra comments inside the code block.",
    ),
    (
        "edge_cases",
        "Pay special attention to boundary conditions: empty inputs, single-element inputs, negative numbers, zeros, duplicates, and off-by-one indices.",
    ),
    (
        "stdlib_functional",
        "Leverage standard library tools (math, itertools, collections, functools, bisect, heapq, re) for a compact and bug-free implementation.",
    ),
    (
        "algorithmic_invariant",
        "Verify loop invariants, exact numeric types (int vs float division), sorting stability, and tie-breaking rules specified in the docstring.",
    ),
    (
        "step_by_step_decomp",
        "Decompose the logic into clear helper steps or clean local variables, keeping total code concise so it never truncates.",
    ),
    (
        "alternative_paradigm",
        "Use an alternative algorithmic approach (e.g. iterative instead of recursive, or set/dict lookup instead of nested loops) to avoid corner-case traps.",
    ),
    (
        "strict_spec_trace",
        "Trace the docstring examples mentally against your implementation to ensure exact match on return types and edge cases.",
    ),
    (
        "minimal_defensive",
        "Keep the implementation minimal and avoid verbose docstrings inside the generated code block so the complete function fits comfortably.",
    ),
]


@dataclass(frozen=True)
class PlanVariant:
    sample_index: int
    strategy_id: str
    temperature: float
    seed: int
    messages: list[dict[str, str]]
    est_prompt_tokens: int


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(32, total_chars // 3 + 32)


def build_plan(
    base_messages: list[dict[str, str]],
    n: int = 8,
    base_seed: int = 42,
    temp_min: float = 0.4,
    temp_max: float = 0.95,
    include_greedy_anchor: bool = True,
) -> list[PlanVariant]:
    """Create N diverse prompt variants with varied temperatures and strategy hints."""
    if n < 1:
        raise ValueError("n must be >= 1")
    variants: list[PlanVariant] = []
    for idx in range(n):
        strat_id, hint = STRATEGY_HINTS[idx % len(STRATEGY_HINTS)]
        if idx == 0 and include_greedy_anchor:
            temp = 0.0
        elif n <= 2:
            temp = temp_min if idx == 0 else temp_max
        else:
            span = max(0.0, temp_max - temp_min)
            denom = max(1, (n - 2) if include_greedy_anchor else (n - 1))
            step_idx = (idx - 1) if include_greedy_anchor else idx
            temp = round(temp_min + span * (step_idx / denom), 3)

        msgs: list[dict[str, str]] = []
        has_system = any(m.get("role") == "system" for m in base_messages)
        if not has_system:
            sys_content = (
                "You are an expert Python programmer. Output ONLY valid Python code inside a ```python ... ``` block. "
                "Do not repeat long docstrings inside your code block. /no_think\n"
                f"Strategy ({strat_id}): {hint}"
            )
            msgs.append({"role": "system", "content": sys_content})

        for m in base_messages:
            role = str(m.get("role", "user"))
            content = str(m.get("content", ""))
            if role == "system":
                content = (
                    f"{content.rstrip()}\n"
                    "Keep code concise (omit verbose docstrings inside the solution) so it never truncates.\n"
                    f"Strategy ({strat_id}): {hint}"
                )
            msgs.append({"role": role, "content": content})

        variants.append(
            PlanVariant(
                sample_index=idx,
                strategy_id=strat_id,
                temperature=temp,
                seed=base_seed + idx * 101,
                messages=msgs,
                est_prompt_tokens=estimate_tokens(msgs),
            )
        )
    return variants

