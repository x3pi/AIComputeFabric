"""Sandbox pool runner for evaluating Best-of-N code candidates (Pha 2.1 & §2B.1).

Uses pick_sandbox(require_network_isolation=True) and REFUSES to run if no network-isolated
sandbox is available (e.g. NullSandbox on Windows or unisolated host).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.sandbox import SandboxError, SandboxLimits, SandboxUnavailable, pick_sandbox
from bestofn.eda_verilog import (
    evaluate_verilog_candidate,
    extract_verilog_block,
    normalize_verilog_structure,
)

RUNNER_HELPER_SCRIPT = """\
import hashlib
import io
import json
import sys
import traceback

def main():
    real_stdout = sys.stdout
    payload = json.load(sys.stdin)
    setup_code = payload.get("setup_code", "")
    candidate_code = payload.get("candidate_code", "")
    test_suites = payload.get("test_suites", [])
    max_out = int(payload.get("max_output_bytes", 65536))

    buf = io.StringIO()
    sys.stdout = buf
    sys.stderr = buf

    t0 = __import__("time").perf_counter()
    compile_ok = False
    compile_error_type = ""
    compile_error = ""
    ns_base = {"__name__": "__main__"}

    full_def_src = setup_code + "\\n" + candidate_code if setup_code else candidate_code
    try:
        compiled = compile(full_def_src, "<candidate>", "exec")
        exec(compiled, ns_base)
        compile_ok = True
    except SyntaxError as exc:
        compile_error_type = "SyntaxError"
        compile_error = f"{exc.__class__.__name__}: {exc}"
    except BaseException as exc:
        compile_error_type = exc.__class__.__name__
        compile_error = "".join(traceback.format_exception_only(type(exc), exc)).strip()

    suite_results = []
    passed_count = 0
    if compile_ok:
        for idx, suite_src in enumerate(test_suites):
            s_ok = False
            s_err_type = ""
            s_err = ""
            try:
                ns_suite = dict(ns_base)
                comp_suite = compile(suite_src, f"<test_suite_{idx}>", "exec")
                exec(comp_suite, ns_suite)
                s_ok = True
                passed_count += 1
            except BaseException as exc:
                s_err_type = exc.__class__.__name__
                s_err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            suite_results.append({
                "suite_index": idx,
                "passed": s_ok,
                "error_type": s_err_type,
                "error": s_err[:400],
            })

    sys.stdout = real_stdout
    sys.stderr = sys.__stderr__
    dt_ms = (__import__("time").perf_counter() - t0) * 1000.0

    raw_captured = buf.getvalue().encode("utf-8", errors="replace")
    truncated = len(raw_captured) > max_out
    if truncated:
        raw_captured = raw_captured[:max_out]

    pass_vector = "".join("1" if r["passed"] else "0" for r in suite_results)
    out = {
        "compile_ok": compile_ok,
        "compile_error_type": compile_error_type,
        "compile_error": compile_error[:600],
        "passed_tests": passed_count,
        "total_tests": len(test_suites),
        "suite_results": suite_results,
        "pass_vector": pass_vector,
        "exec_ms": round(dt_ms, 2),
        "stdout": raw_captured.decode("utf-8", errors="replace"),
        "stdout_bytes": len(raw_captured),
        "output_truncated": truncated,
    }
    real_stdout.write(json.dumps(out))
    real_stdout.flush()

if __name__ == "__main__":
    main()
"""


@dataclass
class CandidateEval:
    sample_index: int
    agent_id: str
    model: str
    strategy_id: str
    temperature: float
    code: str
    code_hash: str
    compile_ok: bool
    passed_tests: int
    total_tests: int
    pass_rate: float
    pass_vector: str
    suite_results: list[dict[str, Any]] = field(default_factory=list)
    exec_ms: float = 0.0
    sandbox_ms: float = 0.0
    gen_ms: float = 0.0
    error_type: str = ""
    error: str = ""
    stdout: str = ""
    output_truncated: bool = False


def extract_code_block(raw_text: str, entry_point: str = "") -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if blocks:
        if entry_point:
            for b in blocks:
                if f"def {entry_point}" in b:
                    return b.rstrip()
        return blocks[-1].rstrip()
    # Handle truncated markdown fence if model hit max_tokens before closing ```
    open_match = re.search(r"```(?:python|py)?\s*\n(.*)$", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if open_match:
        return open_match.group(1).rstrip()
    return cleaned.rstrip()


def normalize_code_structure(code: str) -> str:
    """Normalize whitespace and comments to compute structural consensus hash."""
    lines = []
    for ln in code.splitlines():
        s = re.sub(r"#.*$", "", ln).rstrip()
        if s.strip():
            lines.append(s)
    return "\n".join(lines)


class SandboxPool:
    """Bounded pool of network-isolated sandboxes (default pool_size=4 on pc-001)."""

    def __init__(
        self,
        pool_size: int = 4,
        timeout_s: float = 6.0,
        memory_mb: int = 128,
        pids: int = 64,
        max_output_bytes: int = 64 * 1024,
        sandbox_override: Any = None,
    ) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        self.pool_size = pool_size
        self.timeout_s = timeout_s
        self.memory_mb = memory_mb
        self.pids = pids
        self.max_output_bytes = max_output_bytes
        self.sb = sandbox_override if sandbox_override is not None else pick_sandbox(require_network_isolation=True)
        if not getattr(self.sb, "isolates_network", False):
            raise SandboxUnavailable(
                f"Refusing to run untrusted code: sandbox backend '{getattr(self.sb, 'name', 'none')}' "
                "does not provide network isolation"
            )
        self.helper_path = self._ensure_helper()

    @staticmethod
    def _ensure_helper() -> Path:
        root = Path(__file__).resolve().parents[1]
        demo_dir = root / ".demo"
        demo_dir.mkdir(parents=True, exist_ok=True)
        path = demo_dir / "bestofn_sandbox_helper.py"
        if not path.exists() or path.read_text(encoding="utf-8") != RUNNER_HELPER_SCRIPT:
            path.write_text(RUNNER_HELPER_SCRIPT, encoding="utf-8")
        os.chmod(path, 0o644)
        return path

    def evaluate_one(
        self,
        sample_index: int,
        agent_id: str,
        model: str,
        strategy_id: str,
        temperature: float,
        gen_ms: float,
        raw_content: str,
        test_suites: list[str],
        setup_code: str = "",
        entry_point: str = "",
        language: str = "python",
    ) -> CandidateEval:
        lang = (language or "python").strip().lower()
        if lang in ("verilog", "systemverilog", "sv", "rtl"):
            code = extract_verilog_block(raw_text=raw_content, top_module=entry_point)
            norm = normalize_verilog_structure(code)
            code_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
            t0 = time.perf_counter()
            res = evaluate_verilog_candidate(
                candidate_code=code,
                test_suites=test_suites,
                setup_code=setup_code,
                top_module=entry_point,
                timeout_s=self.timeout_s,
                max_output_bytes=self.max_output_bytes // 2,
            )
            sb_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            total_t = max(1, int(res.get("total_tests", len(test_suites))))
            passed_t = int(res.get("passed_tests", 0))
            compile_ok = bool(res.get("compile_ok", False))
            err_type = str(res.get("compile_error_type", ""))
            err_msg = str(res.get("compile_error", ""))
            if compile_ok and passed_t < len(test_suites):
                for sr in res.get("suite_results", []):
                    if not sr.get("passed", False):
                        err_type = str(sr.get("error_type", "SimulationMismatch"))
                        err_msg = str(sr.get("error", ""))
                        break
            return CandidateEval(
                sample_index=sample_index,
                agent_id=agent_id,
                model=model,
                strategy_id=strategy_id,
                temperature=temperature,
                code=code,
                code_hash=code_hash,
                compile_ok=compile_ok,
                passed_tests=passed_t,
                total_tests=len(test_suites),
                pass_rate=round(passed_t / total_t, 4) if test_suites else (1.0 if compile_ok else 0.0),
                pass_vector=str(res.get("pass_vector", "")),
                suite_results=res.get("suite_results", []),
                exec_ms=float(res.get("exec_ms", 0.0)),
                sandbox_ms=sb_ms,
                gen_ms=gen_ms,
                error_type=err_type,
                error=err_msg,
                stdout=str(res.get("stdout", "")),
                output_truncated=bool(res.get("output_truncated", False)),
            )

        code = extract_code_block(raw_text=raw_content, entry_point=entry_point)
        norm = normalize_code_structure(code)
        code_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

        # If setup_code has a trailing function header stub (like HumanEval prompt),
        # handle both full-def and body-only completions cleanly.
        effective_setup = setup_code
        effective_candidate = code
        if setup_code and entry_point:
            has_def = any(
                re.match(rf"^\s*(?:async\s+)?def\s+{re.escape(entry_point)}\s*\(", ln)
                for ln in code.splitlines()
                if ln.strip()
            )
            if has_def:
                effective_setup = setup_code + "\n    pass\n"
            else:
                raw_lines = code.splitlines()
                first_nonempty = next((ln for ln in raw_lines if ln.strip()), "")
                if first_nonempty and not first_nonempty.startswith((" ", "\t")):
                    effective_candidate = "\n".join(("    " + ln if ln.strip() else "") for ln in raw_lines)
                effective_setup = setup_code

        limits = SandboxLimits(
            timeout_s=self.timeout_s,
            memory_mb=self.memory_mb,
            pids=self.pids,
            max_output_bytes=self.max_output_bytes,
        )
        payload = {
            "setup_code": effective_setup,
            "candidate_code": effective_candidate,
            "test_suites": test_suites,
            "max_output_bytes": self.max_output_bytes // 2,
        }
        t0 = time.perf_counter()
        try:
            res = self.sb.run(self.helper_path, payload, limits)
            sb_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            total_t = max(1, int(res.get("total_tests", len(test_suites))))
            passed_t = int(res.get("passed_tests", 0))
            compile_ok = bool(res.get("compile_ok", False))
            err_type = str(res.get("compile_error_type", ""))
            err_msg = str(res.get("compile_error", ""))
            if compile_ok and passed_t < len(test_suites):
                for sr in res.get("suite_results", []):
                    if not sr.get("passed", False):
                        err_type = str(sr.get("error_type", "AssertionError"))
                        err_msg = str(sr.get("error", ""))
                        break
            return CandidateEval(
                sample_index=sample_index,
                agent_id=agent_id,
                model=model,
                strategy_id=strategy_id,
                temperature=temperature,
                code=code,
                code_hash=code_hash,
                compile_ok=compile_ok,
                passed_tests=passed_t,
                total_tests=len(test_suites),
                pass_rate=round(passed_t / total_t, 4) if test_suites else (1.0 if compile_ok else 0.0),
                pass_vector=str(res.get("pass_vector", "")),
                suite_results=res.get("suite_results", []),
                exec_ms=float(res.get("exec_ms", 0.0)),
                sandbox_ms=sb_ms,
                gen_ms=gen_ms,
                error_type=err_type,
                error=err_msg,
                stdout=str(res.get("stdout", "")),
                output_truncated=bool(res.get("output_truncated", False)),
            )
        except SandboxError as exc:
            sb_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return CandidateEval(
                sample_index=sample_index,
                agent_id=agent_id,
                model=model,
                strategy_id=strategy_id,
                temperature=temperature,
                code=code,
                code_hash=code_hash,
                compile_ok=False,
                passed_tests=0,
                total_tests=len(test_suites),
                pass_rate=0.0,
                pass_vector="0" * len(test_suites),
                exec_ms=0.0,
                sandbox_ms=sb_ms,
                gen_ms=gen_ms,
                error_type="SandboxError",
                error=str(exc),
                output_truncated="exceeds" in str(exc),
            )

    def evaluate_batch(
        self,
        samples: list[Any],
        test_suites: list[str],
        setup_code: str = "",
        entry_point: str = "",
        language: str = "python",
    ) -> tuple[list[CandidateEval], dict[str, Any]]:
        t0 = time.perf_counter()
        valid_samples = [s for s in samples if getattr(s, "ok", False)]
        if not valid_samples:
            return [], {"sandbox_wall_ms": 0.0, "pool_size": self.pool_size, "evaluated": 0}

        concurrency = max(1, min(self.pool_size, len(valid_samples)))
        with concurrent.futures.ThreadPoolExecutor(concurrency) as ex:
            futs = [
                ex.submit(
                    self.evaluate_one,
                    s.sample_index,
                    s.agent_id,
                    s.model,
                    s.strategy_id,
                    s.temperature,
                    s.gen_ms,
                    s.content,
                    test_suites,
                    setup_code,
                    entry_point,
                    language,
                )
                for s in valid_samples
            ]
            evals = [f.result() for f in futs]

        evals.sort(key=lambda e: e.sample_index)
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        eval_per_s = round(len(evals) / (wall_ms / 1000.0), 2) if wall_ms > 0 else 0.0
        return evals, {
            "sandbox_backend": self.sb.name,
            "isolates_network": bool(self.sb.isolates_network),
            "pool_size": self.pool_size,
            "evaluated": len(evals),
            "sandbox_wall_ms": wall_ms,
            "evals_per_sec": eval_per_s,
        }

