#!/usr/bin/env python3
"""Pha 0.2 & Pha 1: Code generation pass@1 evaluator via AICF Gateway + DockerSandbox.

Usage:
  python3 benchmarks/hardware/code_eval.py --mutation-check
  python3 benchmarks/hardware/code_eval.py --dataset humaneval --temperature 0.0 \
      --out benchmarks/reports/p7_pha0_humaneval_run1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.sandbox import SandboxError, SandboxLimits, pick_sandbox  # noqa: E402

SANDBOX_HELPER_CODE = """\
import io
import json
import sys
import traceback

def main():
    real_stdout = sys.stdout
    payload = json.load(sys.stdin)
    code = payload.get("code", "")
    max_out = int(payload.get("max_output_bytes", 65536))
    buf = io.StringIO()
    sys.stdout = buf
    sys.stderr = buf
    passed = False
    err_msg = ""
    err_type = ""
    t0 = __import__("time").perf_counter()
    try:
        ns = {"__name__": "__main__"}
        compiled = compile(code, "<candidate>", "exec")
        exec(compiled, ns)
        passed = True
    except SyntaxError as exc:
        err_type = "SyntaxError"
        err_msg = f"{exc.__class__.__name__}: {exc}"
    except BaseException as exc:
        err_type = exc.__class__.__name__
        err_msg = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    finally:
        sys.stdout = real_stdout
        sys.stderr = sys.__stderr__
    dt_ms = (__import__("time").perf_counter() - t0) * 1000.0
    raw_captured = buf.getvalue().encode("utf-8", errors="replace")
    truncated = len(raw_captured) > max_out
    if truncated:
        raw_captured = raw_captured[:max_out]
    out = {
        "passed": passed,
        "error_type": err_type,
        "error": err_msg[:1000],
        "exec_ms": round(dt_ms, 2),
        "stdout": raw_captured.decode("utf-8", errors="replace"),
        "output_truncated": truncated,
        "stdout_bytes": len(raw_captured),
    }
    real_stdout.write(json.dumps(out))
    real_stdout.flush()

if __name__ == "__main__":
    main()
"""


def ensure_sandbox_helper() -> Path:
    helper_dir = ROOT_DIR / ".demo"
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper_path = helper_dir / "code_eval_sandbox_runner.py"
    if not helper_path.exists() or helper_path.read_text(encoding="utf-8") != SANDBOX_HELPER_CODE:
        helper_path.write_text(SANDBOX_HELPER_CODE, encoding="utf-8")
    os.chmod(helper_path, 0o644)
    return helper_path


def extract_python_code(text: str, entry_point: str = "") -> str:
    """Extract Python code from markdown fences or raw LLM response."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if blocks:
        if entry_point:
            for b in blocks:
                if f"def {entry_point}" in b:
                    return b.rstrip()
        return blocks[-1].rstrip()
    return cleaned.rstrip()


def build_humaneval_program(item: dict[str, Any], extracted_code: str) -> str:
    prompt = item["prompt"]
    ep = item["entry_point"]
    test_code = item["test"]
    lines = [ln for ln in extracted_code.splitlines() if ln.strip()]
    has_def = any(
        re.match(rf"^\s*(?:async\s+)?def\s+{re.escape(ep)}\s*\(", ln)
        for ln in lines
    )
    if has_def:
        return f"{prompt}\n    pass\n\n{extracted_code}\n\n{test_code}\ncheck({ep})\n"
    # Body-only completion: indent if needed
    raw_lines = extracted_code.splitlines()
    first_nonempty = next((ln for ln in raw_lines if ln.strip()), "")
    if first_nonempty and not first_nonempty.startswith((" ", "\t")):
        body = "\n".join(("    " + ln if ln.strip() else "") for ln in raw_lines)
    else:
        body = extracted_code
    return f"{prompt}{body}\n\n{test_code}\ncheck({ep})\n"


def build_mbpp_program(item: dict[str, Any], extracted_code: str) -> str:
    imports = "\n".join(item.get("test_imports", []))
    tests = "\n".join(item.get("test_list", []))
    return f"{imports}\n\n{extracted_code}\n\n{tests}\n"


def run_in_sandbox(
    sb: Any,
    helper_path: Path,
    program_src: str,
    timeout_s: float = 6.0,
    memory_mb: int = 128,
    max_output_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    if not getattr(sb, "isolates_network", False):
        raise RuntimeError(f"Refusing to execute untrusted code: sandbox {sb.name} does not isolate network")
    t0 = time.perf_counter()
    limits = SandboxLimits(
        timeout_s=timeout_s,
        memory_mb=memory_mb,
        pids=64,
        max_output_bytes=max_output_bytes,
    )
    try:
        res = sb.run(
            helper_path,
            {"code": program_src, "max_output_bytes": max_output_bytes // 2},
            limits,
        )
        res["sandbox_wall_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        return res
    except SandboxError as exc:
        return {
            "passed": False,
            "error_type": "SandboxError",
            "error": str(exc),
            "exec_ms": 0.0,
            "sandbox_wall_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "stdout": "",
            "output_truncated": "exceeds" in str(exc),
        }


def run_mutation_checks(sb: Any, helper_path: Path) -> dict[str, Any]:
    """Pha 0 & Pha 2.3 mutation checks on DockerSandbox."""
    results: dict[str, Any] = {}

    # 1. Network socket attempts (external IP + loopback gateway) MUST fail
    net_code = """
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1.5)
s.connect(("1.1.1.1", 80))
"""
    r_net = run_in_sandbox(sb, helper_path, net_code, timeout_s=5.0)
    results["network_external_blocked"] = (not r_net["passed"]) and (
        r_net["error_type"] in ("OSError", "NetworkError", "TimeoutError")
    )

    lo_code = """
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1.5)
s.connect(("127.0.0.1", 18080))
"""
    r_lo = run_in_sandbox(sb, helper_path, lo_code, timeout_s=5.0)
    results["network_loopback_gateway_blocked"] = (not r_lo["passed"]) and (
        r_lo["error_type"] in ("ConnectionRefusedError", "OSError")
    )

    # 2. Read-only filesystem write attempt MUST fail
    fs_code = """
with open("/etc/pwn_test", "w") as f:
    f.write("x")
"""
    r_fs = run_in_sandbox(sb, helper_path, fs_code, timeout_s=5.0)
    results["readonly_fs_blocked"] = (not r_fs["passed"]) and (
        r_fs["error_type"] in ("OSError", "PermissionError")
    )

    # 3. Infinite loop MUST be killed by timeout
    inf_code = """
while True:
    pass
"""
    r_inf = run_in_sandbox(sb, helper_path, inf_code, timeout_s=2.0)
    results["infinite_loop_killed"] = (not r_inf["passed"]) and (
        r_inf["error_type"] == "SandboxError" and "timed out" in r_inf["error"]
    )

    # 4. Excessive stdout output MUST be truncated / bounded
    flood_code = """
print("X" * 200000)
"""
    r_flood = run_in_sandbox(sb, helper_path, flood_code, timeout_s=5.0, max_output_bytes=32768)
    results["output_flood_bounded"] = bool(r_flood.get("output_truncated")) and (
        r_flood.get("stdout_bytes", 999999) <= 16384
    )

    # 5. Positive control: valid code passes
    ok_code = """
def add(a, b):
    return a + b
assert add(2, 3) == 5
"""
    r_ok = run_in_sandbox(sb, helper_path, ok_code, timeout_s=5.0)
    results["valid_code_passes"] = bool(r_ok["passed"])

    all_passed = all(results.values())
    return {"all_passed": all_passed, "checks": results, "details": {
        "network_external": r_net,
        "network_loopback": r_lo,
        "readonly_fs": r_fs,
        "infinite_loop": r_inf,
        "output_flood": {k: v for k, v in r_flood.items() if k != "stdout"},
        "valid_code": r_ok,
    }}


def call_gateway_chat(
    gateway_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 512,
    seed: int = 42,
    priority: str = "batch",
    agent_id: str = "",
    label: str = "",
    timeout_s: float = 120.0,
    max_retries: int = 4,
) -> dict[str, Any]:
    url = gateway_url.rstrip("/") + "/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "seed": seed,
        "stream": False,
    }
    if temperature > 0.0:
        payload["top_p"] = 0.95
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Priority": priority,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if agent_id:
        headers["X-AICF-Agent"] = agent_id
    if label:
        headers["X-AICF-Label"] = label

    for attempt in range(max_retries + 1):
        t0 = time.perf_counter()
        req = urllib.request.Request(url, data=raw_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body_bytes = resp.read()
                dt_ms = (time.perf_counter() - t0) * 1000.0
                worker_id = resp.headers.get("X-AICF-Worker", "")
                data = json.loads(body_bytes.decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                timings = data.get("timings", {})
                comp_tokens = usage.get("completion_tokens") or timings.get("predicted_n") or 0
                tok_s = timings.get("predicted_per_second") or (
                    (comp_tokens / (dt_ms / 1000.0)) if dt_ms > 0 else 0.0
                )
                return {
                    "ok": True,
                    "content": content,
                    "worker_id": worker_id,
                    "gen_ms": round(dt_ms, 2),
                    "completion_tokens": comp_tokens,
                    "prompt_tokens": usage.get("prompt_tokens") or timings.get("prompt_n") or 0,
                    "tok_s": round(float(tok_s), 2),
                    "attempts": attempt + 1,
                }
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < max_retries:
                retry_after = float(exc.headers.get("Retry-After", "1.5"))
                time.sleep(min(max(retry_after, 0.5), 10.0))
                continue
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
            return {
                "ok": False,
                "status_code": exc.code,
                "error": f"HTTP {exc.code}: {err_body}",
                "gen_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "attempts": attempt + 1,
            }
        except Exception as exc:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return {
                "ok": False,
                "status_code": 0,
                "error": str(exc),
                "gen_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "attempts": attempt + 1,
            }
    return {"ok": False, "status_code": 0, "error": "exhausted retries", "gen_ms": 0.0, "attempts": max_retries + 1}


def load_problems(dataset: str, data_dir: Path, limit: int = 0, offset: int = 0) -> list[dict[str, Any]]:
    if dataset == "humaneval":
        path = data_dir / "HumanEval.jsonl"
        items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif dataset == "mbpp":
        path = data_dir / "sanitized-mbpp.json"
        items = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"Unknown dataset: {dataset}")
    if offset > 0:
        items = items[offset:]
    if limit > 0:
        items = items[:limit]
    return items


def build_prompt_messages(dataset: str, item: dict[str, Any], system_hint: str = "") -> list[dict[str, str]]:
    sys_msg = (
        "You are an expert Python programmer. Output ONLY valid Python code inside a ```python ... ``` block. "
        "Do not include explanations or markdown prose outside the code block. /no_think"
    )
    if system_hint:
        sys_msg = f"{sys_msg}\nHint: {system_hint}"
    if dataset == "humaneval":
        user_msg = (
            "Complete the following Python function so that it passes all unit tests. "
            "Include any required imports and the complete function definition:\n\n"
            f"```python\n{item['prompt']}```"
        )
    else:
        first_assert = item["test_list"][0] if item.get("test_list") else ""
        imports = "\n".join(item.get("test_imports", []))
        user_msg = (
            f"{item['prompt']}\n"
            f"{('Required imports:\\n' + imports + '\\n') if imports else ''}"
            f"Your function must satisfy assertions like:\n`{first_assert}`\n"
            "Return only the Python implementation in a ```python``` code block."
        )
    return [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate code generation pass@1 via AICF Gateway + DockerSandbox")
    ap.add_argument("--gateway", default="http://127.0.0.1:18080", help="AICF Gateway HTTP URL")
    ap.add_argument("--api-key", default="", help="Tenant API key (defaults to .demo/bestofn_api_key.txt)")
    ap.add_argument("--model", default="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", help="Model name on gateway")
    ap.add_argument("--dataset", choices=["humaneval", "mbpp"], default="humaneval")
    ap.add_argument("--data-dir", default=str(ROOT_DIR / "benchmarks" / "data"))
    ap.add_argument("--limit", type=int, default=0, help="Number of problems (0 = all)")
    ap.add_argument("--offset", type=int, default=0, help="Start index offset")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--agent-id", default="", help="Optional X-AICF-Agent header")
    ap.add_argument("--label", default="", help="Optional X-AICF-Label header")
    ap.add_argument("--mutation-check", action="store_true", help="Run sandbox isolation mutation checks and exit")
    ap.add_argument("--out", default="", help="Output JSON report path")
    args = ap.parse_args()

    sb = pick_sandbox(require_network_isolation=True)
    if not sb.isolates_network:
        print(f"ERROR: sandbox {sb.name} does not isolate network; refusing to run.", file=sys.stderr)
        return 2
    helper_path = ensure_sandbox_helper()

    if args.mutation_check:
        res = run_mutation_checks(sb, helper_path)
        print(json.dumps(res, indent=2))
        return 0 if res["all_passed"] else 1

    api_key = args.api_key or os.environ.get("AICF_API_KEY", "")
    if not api_key:
        for candidate in (ROOT_DIR / ".demo" / "bestofn_api_key.txt", ROOT_DIR / ".demo" / "vscode_api_key.txt"):
            if candidate.exists():
                api_key = candidate.read_text(encoding="utf-8").strip()
                break

    problems = load_problems(args.dataset, Path(args.data_dir), limit=args.limit, offset=args.offset)
    print(
        f"[code_eval] dataset={args.dataset} count={len(problems)} model={args.model} "
        f"temp={args.temperature} sandbox={sb.name}",
        flush=True,
    )

    records: list[dict[str, Any]] = []
    passed_count = 0
    t_start = time.perf_counter()

    for idx, item in enumerate(problems):
        task_id = str(item["task_id"])
        t_prob = time.perf_counter()
        msgs = build_prompt_messages(args.dataset, item)
        # Include task_id + seed nonce in user prompt comment only if needed; here temperature=0 deterministic
        gen = call_gateway_chat(
            gateway_url=args.gateway,
            api_key=api_key,
            model=args.model,
            messages=msgs,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            seed=args.seed,
            agent_id=args.agent_id,
            label=args.label,
        )
        if not gen["ok"]:
            rec = {
                "task_id": task_id,
                "passed": False,
                "error_type": "GatewayError",
                "error": gen.get("error", ""),
                "gen_ms": gen.get("gen_ms", 0.0),
                "sandbox_ms": 0.0,
                "total_ms": round((time.perf_counter() - t_prob) * 1000.0, 2),
                "tok_s": 0.0,
                "worker_id": "",
            }
        else:
            code = extract_python_code(gen["content"], entry_point=item.get("entry_point", ""))
            prog = (
                build_humaneval_program(item, code)
                if args.dataset == "humaneval"
                else build_mbpp_program(item, code)
            )
            sb_res = run_in_sandbox(sb, helper_path, prog, timeout_s=6.0)
            ok = bool(sb_res["passed"])
            if ok:
                passed_count += 1
            rec = {
                "task_id": task_id,
                "passed": ok,
                "error_type": sb_res.get("error_type", ""),
                "error": sb_res.get("error", ""),
                "gen_ms": gen["gen_ms"],
                "sandbox_ms": sb_res["sandbox_wall_ms"],
                "exec_ms": sb_res.get("exec_ms", 0.0),
                "total_ms": round((time.perf_counter() - t_prob) * 1000.0, 2),
                "completion_tokens": gen["completion_tokens"],
                "prompt_tokens": gen["prompt_tokens"],
                "tok_s": gen["tok_s"],
                "worker_id": gen["worker_id"],
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()[:16],
            }
        records.append(rec)
        status_str = "PASS" if rec["passed"] else f"FAIL({rec['error_type']})"
        print(
            f"[{idx+1:3d}/{len(problems):3d}] {task_id:<14} {status_str:<22} "
            f"gen={rec['gen_ms']:6.0f}ms sb={rec['sandbox_ms']:5.0f}ms tok/s={rec['tok_s']:5.1f} "
            f"running={passed_count}/{idx+1} ({100.0*passed_count/(idx+1):.1f}%)",
            flush=True,
        )

    wall_s = time.perf_counter() - t_start
    total_ms_list = [r["total_ms"] for r in records]
    gen_ms_list = [r["gen_ms"] for r in records if r["gen_ms"] > 0]
    tok_s_list = [r["tok_s"] for r in records if r["tok_s"] > 0]
    summary = {
        "dataset": args.dataset,
        "model": args.model,
        "temperature": args.temperature,
        "seed": args.seed,
        "total_problems": len(problems),
        "passed": passed_count,
        "pass_at_1": round(passed_count / len(problems), 4) if problems else 0.0,
        "pass_at_1_pct": round(100.0 * passed_count / len(problems), 2) if problems else 0.0,
        "wall_time_s": round(wall_s, 2),
        "mean_problem_s": round(statistics.mean(total_ms_list) / 1000.0, 3) if total_ms_list else 0.0,
        "median_problem_s": round(statistics.median(total_ms_list) / 1000.0, 3) if total_ms_list else 0.0,
        "mean_gen_s": round(statistics.mean(gen_ms_list) / 1000.0, 3) if gen_ms_list else 0.0,
        "median_tok_s": round(statistics.median(tok_s_list), 2) if tok_s_list else 0.0,
        "sandbox": sb.name,
        "isolates_network": sb.isolates_network,
        "records": records,
    }
    print(
        f"\n=== SUMMARY ({args.dataset}, {args.model}) ===\n"
        f"pass@1: {passed_count}/{len(problems)} ({summary['pass_at_1_pct']:.2f}%) | "
        f"wall={summary['wall_time_s']}s | mean={summary['mean_problem_s']}s/prob | "
        f"median_tok_s={summary['median_tok_s']} tok/s",
        flush=True,
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Saved report to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

