#!/usr/bin/env python3
"""Pha 4 & Pha 5 Verification and Benchmark:
  - Pha 4: /api/bestofn/v1/chat/completions via site-gateway (:18080)
      * Auth check: valid key -> 200, missing key -> 401, invalid key -> 401
      * Input bounds check: n=20 -> 400, language="rust" -> 400, timeout_s=60 -> 400
  - Pha 5: IDE client simulation (model="bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf")
      * N=4 and N=8, both stream=False (JSON) and stream=True (SSE chunks)
      * End-to-end latency breakdown (total_wall_ms, sampling_wall_ms, sandbox_wall_ms)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_URL = "http://127.0.0.1:18080"


def http_call(
    path: str,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout_s: float = 120.0,
) -> tuple[int, str, float]:
    url = f"{GATEWAY_URL}{path}"
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw, round((time.perf_counter() - t0) * 1000.0, 2)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, raw, round((time.perf_counter() - t0) * 1000.0, 2)


def main() -> int:
    key_path = ROOT / ".demo" / "bestofn_api_key.txt"
    api_key = key_path.read_text(encoding="utf-8").strip()
    auth_hdr = {"Authorization": f"Bearer {api_key}"}

    base_msg = [
        {
            "role": "user",
            "content": (
                "Write a Python function `def gcd_list(nums: list[int]) -> int:` that returns the "
                "greatest common divisor of a non-empty list of positive integers."
            ),
        }
    ]
    base_tests = [
        "assert gcd_list([12, 18, 24]) == 6\nassert gcd_list([7, 13]) == 1\nassert gcd_list([42]) == 42"
    ]

    # 1. Pha 4 Auth & Validation Mutation Checks
    st_no_key, body_no_key, ms_no_key = http_call(
        "/api/bestofn/v1/chat/completions",
        headers={},
        body={"model": "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "messages": base_msg, "bon": {"n": 2}},
    )
    st_bad_key, body_bad_key, ms_bad_key = http_call(
        "/api/bestofn/v1/chat/completions",
        headers={"Authorization": "Bearer aicf_invalid_key_mutation_test"},
        body={"model": "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "messages": base_msg, "bon": {"n": 2}},
    )
    st_n20, body_n20, ms_n20 = http_call(
        "/api/bestofn/v1/chat/completions",
        headers=auth_hdr,
        body={"model": "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "messages": base_msg, "bon": {"n": 20}},
    )
    st_lang, body_lang, ms_lang = http_call(
        "/api/bestofn/v1/chat/completions",
        headers=auth_hdr,
        body={
            "model": "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
            "messages": base_msg,
            "bon": {"n": 2, "language": "rust"},
        },
    )
    st_timeout, body_timeout, ms_timeout = http_call(
        "/api/bestofn/v1/chat/completions",
        headers=auth_hdr,
        body={
            "model": "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
            "messages": base_msg,
            "bon": {"n": 2, "timeout_s": 60.0},
        },
    )

    assert st_no_key == 401, f"Expected 401 for missing key, got {st_no_key}: {body_no_key}"
    assert st_bad_key == 401, f"Expected 401 for bad key, got {st_bad_key}: {body_bad_key}"
    assert st_n20 == 400, f"Expected 400 for n=20, got {st_n20}: {body_n20}"
    assert st_lang == 400, f"Expected 400 for language=rust, got {st_lang}: {body_lang}"
    assert st_timeout == 400, f"Expected 400 for timeout_s=60, got {st_timeout}: {body_timeout}"

    # 2. Pha 5 IDE Client Simulation (N=4 and N=8, stream=False and stream=True)
    ide_runs: list[dict[str, Any]] = []
    for n in (4, 8):
        for stream_flag in (False, True):
            st, raw_resp, wall_ms = http_call(
                "/api/bestofn/v1/chat/completions",
                headers=auth_hdr,
                body={
                    "model": "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                    "messages": base_msg,
                    "max_tokens": 256,
                    "stream": stream_flag,
                    "bon": {
                        "n": n,
                        "tests": base_tests,
                        "entry_point": "gcd_list",
                        "timeout_s": 6.0,
                    },
                },
            )
            assert st == 200, f"Expected 200 for N={n} stream={stream_flag}, got {st}: {raw_resp}"
            if not stream_flag:
                parsed = json.loads(raw_resp)
                content = parsed["choices"][0]["message"]["content"]
                bon_rep = parsed["bon_report"]
            else:
                # Parse SSE data: chunks
                data_lines = [
                    ln[6:].strip()
                    for ln in raw_resp.splitlines()
                    if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
                ]
                assert len(data_lines) >= 1
                chunk = json.loads(data_lines[-1])
                content = chunk["choices"][0]["delta"]["content"]
                bon_rep = chunk["bon_report"]

            assert "def gcd_list" in content
            assert bon_rep["n_candidates"] == n
            assert bon_rep["n_all_tests_passed"] >= 1
            ide_runs.append({
                "n": n,
                "stream": stream_flag,
                "http_status": st,
                "client_wall_ms": wall_ms,
                "service_total_wall_ms": bon_rep["total_wall_ms"],
                "sampling_wall_ms": bon_rep["sampling"]["sampling_wall_ms"],
                "sandbox_wall_ms": bon_rep["sandbox_pool"]["sandbox_wall_ms"],
                "n_candidates": bon_rep["n_candidates"],
                "n_compilable": bon_rep["n_compilable"],
                "n_all_tests_passed": bon_rep["n_all_tests_passed"],
                "selected_sample_index": bon_rep["selected_sample_index"],
                "selected_agent_id": bon_rep["selected_agent_id"],
                "confidence": bon_rep["confidence"],
                "consensus_ratio": bon_rep["consensus_ratio"],
            })

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence_label": "[ĐO]",
        "pha4_security_and_validation_checks": {
            "missing_api_key": {"status": st_no_key, "ms": ms_no_key, "passed": st_no_key == 401},
            "invalid_api_key": {"status": st_bad_key, "ms": ms_bad_key, "passed": st_bad_key == 401},
            "n_exceeds_max_16_n20": {"status": st_n20, "ms": ms_n20, "passed": st_n20 == 400},
            "unsupported_language_rust": {"status": st_lang, "ms": ms_lang, "passed": st_lang == 400},
            "timeout_exceeds_max_30s": {"status": st_timeout, "ms": ms_timeout, "passed": st_timeout == 400},
        },
        "pha5_ide_simulation_runs": ide_runs,
    }
    out_path = ROOT / "benchmarks" / "reports" / "p7_pha4_5_api_ide.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
