#!/usr/bin/env python3
"""Full HumanEval-164 benchmark across every model x execution mode of AI Compute Fabric.

Why this exists (fixes vs kimi_deepseek_coder_benchmark.py / docx_coder_benchmark_runner.py):
  * ALL 164 HumanEval tasks per configuration (old runs used 8-12 tasks -> +-15% noise).
  * Output budget is large enough (1024 non-reasoning / 3072 reasoning); old 384/896 truncated answers.
  * Scoring prefixes the original prompt (imports + helper functions) so helper-dependent tasks are not false-failed.
  * Evaluator is validated first: canonical solutions must give 164/164, a stub body must give ~0 (mutation check).
  * Power of BOTH GPUs is measured (remote 5070 over ssh) only inside request windows -> J/token, not a guessed 85 W.
  * Network bytes (NIC counters) per generated token, CPU utilisation, peak VRAM on both nodes, per-task pass vectors
    (so quality can be compared between modes of the same model), long-prompt prefill and 2-slot concurrency probes.

Resumable: one JSON per (model, mode) in benchmarks/reports/full164/. Existing files are skipped.
"""
from __future__ import annotations

import json
import os
import re
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kimi_deepseek_coder_benchmark import (  # noqa: E402  (reuse proven gateway helpers)
    ENGINE_DIRECT_URL,
    ENGINE_LOG_PATH,
    HUMANEVAL_PATH,
    inspect_gguf_identity,
    parse_engine_log_buffers,
    switch_mode_and_model,
)

TAG = os.environ.get("AICF_TAG", "")
OUT_DIR = HUMANEVAL_PATH.parents[1] / "reports" / "full164"
WIN_SSH = os.environ.get("WIN_SSH", "fios02@10.42.0.225")
NIC = os.environ.get("AICF_NIC", "enp133s0")
# deepseek-coder GGUF has NO embedded chat template -> llama-server falls back to ChatML and the model never stops
# (measured: 148/164 tasks hit the token cap). Use its official instruction format through /completion instead.
DSC_SYS = ("You are an AI programming assistant, utilizing the DeepSeek Coder model, developed by DeepSeek Company, and you only "
           "answer questions related to computer science. For politically sensitive questions, security and privacy issues, "
           "and other non-computer science questions, you will refuse to answer.\n")


def raw_prompt_for(model: str, messages: List[Dict[str, str]]) -> Optional[str]:
    if "deepseek-coder" in model:
        q = "\n\n".join(m["content"] for m in messages)
        return DSC_SYS + "### Instruction:\n" + q + "\n### Response:\n"
    return None


SYS_PROMPT = (
    "You are an expert Python programmer. Complete the requested function. "
    "Output ONLY a single ```python ... ``` block containing the complete function (and any imports it needs)."
)

# (model, mode, n_cpu_moe, kind)  kind: direct = non-reasoning coder/instruct, reasoning = emits <think>
CONFIGS: List[Tuple[str, str, int, str]] = [
    ("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "merged", 0, "direct"),
    ("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "smart_1gpu", 26, "direct"),
    ("Qwen3-30B-A3B-Q4_K_M.gguf", "merged", 0, "direct"),
    ("Qwen3-30B-A3B-Q4_K_M.gguf", "smart_1gpu", 26, "direct"),
    ("Qwen3-4B-Q4_K_M.gguf", "split", 0, "direct"),
    ("Qwen3-4B-Q4_K_M.gguf", "merged", 0, "direct"),
    ("Qwen3-8B-Q4_K_M.gguf", "split", 0, "direct"),
    ("Qwen3-8B-Q4_K_M.gguf", "merged", 0, "direct"),
    ("deepseek-coder-6.7b-instruct.Q4_K_M.gguf", "split", 0, "direct"),
    ("deepseek-coder-6.7b-instruct.Q4_K_M.gguf", "merged", 0, "direct"),
    ("DeepSeek-V2-Lite-Chat.Q3_K_M.gguf", "smart_1gpu", 10, "direct"),
    ("DeepSeek-V2-Lite-Chat.Q3_K_M.gguf", "merged", 0, "direct"),
    ("DeepSeek-V2-Lite-Chat.Q4_K_M.gguf", "smart_1gpu", 14, "direct"),
    ("DeepSeek-V2-Lite-Chat.Q4_K_M.gguf", "merged", 0, "direct"),
    ("DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf", "merged", 0, "reasoning"),
    ("DeepSeek-R1-Distill-Qwen-32B-IQ4_XS.gguf", "merged", 0, "reasoning"),
    ("Qwen3.8-27B-UD-Q4_K_M.gguf", "merged", 0, "direct"),
    ("Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf", "split", 0, "reasoning"),
    ("Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf", "merged", 0, "reasoning"),
    ("DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf", "split", 0, "reasoning"),
    ("DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf", "merged", 0, "reasoning"),
    # --- GLM family (added later; files live on /mnt/Data/aicf-glm, symlinked into .demo/models)
    ("GLM-4.7-Flash-Q4_K_M.gguf", "merged", 0, "hybrid"),
    ("GLM-4.7-Flash-Q4_K_M.gguf", "smart_1gpu", 26, "hybrid"),
    ("GLM-4-9B-0414-Q4_K_M.gguf", "split", 0, "direct"),
    ("GLM-4-9B-0414-Q4_K_M.gguf", "merged", 0, "direct"),
    ("GLM-4-32B-0414-Q4_K_M.gguf", "merged", 0, "direct"),
]


KV_TYPE = os.environ.get("AICF_KV", "f16")  # KV-cache type for the measured config (restore step always uses f16)


def slug(model: str, mode: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model.replace(".gguf", "")) + "__" + mode


# --------------------------------------------------------------------------- scoring
def extract_code(content: str, entry_point: str = "") -> str:
    """v2: FIRST fenced block defining entry_point (models that fail to stop ramble after the answer)."""
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    blocks = [b.strip() for b in re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", cleaned, flags=re.DOTALL)]
    for pred in (lambda b: entry_point and f"def {entry_point}" in b, lambda b: "def " in b):
        for b in blocks:
            if pred(b):
                return b
    if blocks:
        return blocks[-1]
    if "```" in cleaned:  # unclosed fence (typically truncated)
        return cleaned.split("```", 1)[1].split("\n", 1)[-1].replace("```", "").strip()
    return cleaned


def build_program(prob: Dict[str, Any], answer: str) -> str:
    ep = prob["entry_point"]
    code = extract_code(answer, ep)
    # Prompt first: provides imports + helper functions (is_palindrome, encode_cyclic, ...). The model's def overrides.
    body = code if f"def {ep}" in code else code and (prob["prompt"] + "\n" + code)
    head = prob["prompt"] + "    pass\n\n" if f"def {ep}" in code else ""
    return "from typing import *\nimport math\n\n" + head + (body or "") + "\n\n" + prob["test"] + f"\ncheck({ep})\n"


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))


def run_sandbox(program: str, timeout: float = 10.0) -> Tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="aicf_f164_") as td:
        p = Path(td) / "sol.py"
        p.write_text(program, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, "-I", str(p)], cwd=td, capture_output=True, text=True,
                               timeout=timeout, preexec_fn=_limits)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout).strip().splitlines()
        return False, (err[-1] if err else "nonzero-exit")[:200]


def load_problems() -> List[Dict[str, Any]]:
    return [json.loads(l) for l in open(HUMANEVAL_PATH, encoding="utf-8") if l.strip()]


def validate_evaluator(problems: List[Dict[str, Any]]) -> Dict[str, Any]:
    canon_ok = stub_ok = 0
    canon_fail: List[str] = []
    for pr in problems:
        ok, _ = run_sandbox(build_program(pr, "```python\n" + pr["prompt"] + pr["canonical_solution"] + "\n```"))
        canon_ok += ok
        if not ok:
            canon_fail.append(pr["task_id"])
        ok2, _ = run_sandbox(build_program(pr, "```python\n" + pr["prompt"] + "    pass\n```"))
        stub_ok += ok2
    return {"total": len(problems), "canonical_pass": canon_ok, "canonical_fail_ids": canon_fail,
            "stub_pass_mutation": stub_ok}


# --------------------------------------------------------------------------- telemetry
class Telemetry:
    """Streams nvidia-smi of both GPUs (local + Windows via ssh), plus NIC and CPU counters."""

    Q = "power.draw,temperature.gpu,utilization.gpu,memory.used"

    def __init__(self) -> None:
        self.rows: Dict[str, List[Tuple[float, List[float]]]] = {"local": [], "win": []}
        self.procs: List[subprocess.Popen] = []
        self.threads: List[threading.Thread] = []

    def _pump(self, key: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            try:
                vals = [float(x) for x in line.strip().split(",")]
                if len(vals) == 4:
                    self.rows[key].append((time.time(), vals))
            except ValueError:
                pass

    def start(self) -> None:
        base = ["nvidia-smi", f"--query-gpu={self.Q}", "--format=csv,noheader,nounits", "-lms", "500"]
        cmds = {"local": base,
                "win": ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=10", WIN_SSH, " ".join(base)]}
        for k, c in cmds.items():
            p = subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self.procs.append(p)
            t = threading.Thread(target=self._pump, args=(k, p), daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        for p in self.procs:
            p.terminate()

    def window_stats(self, key: str, windows: List[Tuple[float, float]]) -> Dict[str, Any]:
        sel = [v for ts, v in self.rows[key] if any(a <= ts <= b for a, b in windows)]
        allv = [v for _, v in self.rows[key]]
        if not sel:
            return {"samples": 0}
        col = lambda i: [s[i] for s in sel]  # noqa: E731
        return {"samples": len(sel), "mean_power_w": round(statistics.mean(col(0)), 1),
                "peak_power_w": round(max(col(0)), 1), "mean_temp_c": round(statistics.mean(col(1)), 1),
                "mean_util_pct": round(statistics.mean(col(2)), 1),
                "peak_mem_used_mib_in_run": round(max(v[3] for v in sel), 0)}


def nic_bytes() -> Tuple[int, int]:
    for line in open("/proc/net/dev"):
        if line.strip().startswith(NIC + ":"):
            f = line.split(":", 1)[1].split()
            return int(f[0]), int(f[8])
    return 0, 0


def cpu_times() -> Tuple[float, float]:
    f = [float(x) for x in open("/proc/stat").readline().split()[1:]]
    return sum(f) - f[3] - f[4], sum(f)


# --------------------------------------------------------------------------- engine calls
def chat(model: str, messages: List[Dict[str, str]], max_tokens: int, kind: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.0,
                               "seed": 1234, "stream": True, "cache_prompt": False}
    raw = raw_prompt_for(model, messages)
    url = f"{ENGINE_DIRECT_URL}/v1/chat/completions"
    if raw is not None:
        payload = {"prompt": raw, "n_predict": max_tokens, "temperature": 0.0, "seed": 1234, "stream": True,
                   "cache_prompt": False, "stop": ["<|EOT|>"]}
        url = f"{ENGINE_DIRECT_URL}/completion"
    if kind in ("direct", "hybrid") and model.startswith(("Qwen3", "GLM-4.7")):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    wall0 = time.time()
    t_first = t_last = None
    content: List[str] = []
    reasoning: List[str] = []
    timings: Dict[str, Any] = {}
    finish = ""
    n_chunks = 0
    with urllib.request.urlopen(req, timeout=420) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                ch = json.loads(d)
            except json.JSONDecodeError:
                continue
            if isinstance(ch.get("timings"), dict):
                timings = ch["timings"]
            if "content" in ch and "choices" not in ch:  # /completion stream format
                cp = ch.get("content") or ""
                if cp:
                    now = time.perf_counter()
                    t_first = t_first or now
                    t_last = now
                    n_chunks += 1
                    content.append(cp)
                if ch.get("stop"):
                    finish = "length" if ch.get("stop_type") == "limit" else "stop"
            for c in ch.get("choices") or []:
                finish = c.get("finish_reason") or finish
                delta = c.get("delta") or {}
                cp, rp = delta.get("content") or "", delta.get("reasoning_content") or ""
                if cp or rp:
                    now = time.perf_counter()
                    t_first = t_first or now
                    t_last = now
                    n_chunks += 1
                    (content if cp else reasoning).append(cp or rp)
    t_end = time.perf_counter()
    t_first = t_first or t_end
    t_last = t_last or t_end
    out_tok = int(timings.get("predicted_n") or n_chunks)
    return {
        "content": "".join(content), "reasoning_chars": len("".join(reasoning)), "finish": finish,
        "prompt_tokens": int(timings.get("prompt_n") or 0), "output_tokens": out_tok,
        "ttft_ms": (t_first - t0) * 1000, "total_ms": (t_end - t0) * 1000,
        "tpot_ms": (t_last - t_first) * 1000 / max(1, out_tok - 1),
        "decode_tok_s": float(timings.get("predicted_per_second") or 0.0),
        "prefill_tok_s": float(timings.get("prompt_per_second") or 0.0),
        "win": (wall0, time.time()),
    }


def pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 2) if xs else 0.0


def dist(xs: List[float]) -> Dict[str, float]:
    return {"mean": round(statistics.mean(xs), 2), "p50": pct(xs, 50), "p95": pct(xs, 95)} if xs else {}


def long_prompt(problems: List[Dict[str, Any]]) -> str:
    body = "\n\n".join(p["prompt"] for p in problems[:14])
    return "Review this Python module and list the 3 most important bugs or risks, briefly.\n\n```python\n" + body + "\n```"


# --------------------------------------------------------------------------- one configuration
def run_config(model: str, mode: str, ncmoe: int, kind: str, problems: List[Dict[str, Any]], tele: Telemetry) -> Dict[str, Any]:
    print(f"\n>>> {model} | {mode} | ncmoe={ncmoe} | {kind}", flush=True)
    deploy = switch_mode_and_model(mode=mode, model_name=model, tensor_split="48,52", n_cpu_moe=ncmoe, kv_type=KV_TYPE)
    buf = deploy["llama_log_buffers"]
    print(f"  loaded {deploy['cold_load_seconds']}s offload={buf.get('offloaded_layers')} "
          f"cuda0={buf.get('cuda0_model_mib')} rpc0={buf.get('rpc0_model_mib')}", flush=True)
    if "/" in str(buf.get("offloaded_layers", "")):
        pass
    budget = {"reasoning": 3072, "hybrid": 2048}.get(kind, 1024)

    # warmup (also a cold-KV prefill sample)
    chat(model, [{"role": "system", "content": SYS_PROMPT}, {"role": "user", "content": "Write a Python function that reverses a string."}], 64, kind)

    # --- long prompt prefill probe (network-sensitive in merged mode)
    lp = [{"role": "user", "content": long_prompt(problems)}]
    lp_runs = [chat(model, lp, 96, kind) for _ in range(3)]
    long_prefill = {"prompt_tokens": lp_runs[0]["prompt_tokens"],
                    "prefill_tok_s": dist([r["prefill_tok_s"] for r in lp_runs]),
                    "ttft_ms": dist([r["ttft_ms"] for r in lp_runs])}

    # --- 2-slot concurrency probe
    short = [{"role": "system", "content": SYS_PROMPT},
             {"role": "user", "content": "Implement a thread-safe LRU cache class with TTL in Python."}]
    conc: Dict[str, Any] = {}
    for c in (1, 2):
        aggs = []
        for _ in range(3):
            res: List[Dict[str, Any]] = []
            ths = [threading.Thread(target=lambda: res.append(chat(model, short, 200, kind))) for _ in range(c)]
            t0 = time.perf_counter()
            [t.start() for t in ths]
            [t.join() for t in ths]
            aggs.append(sum(r["output_tokens"] for r in res) / (time.perf_counter() - t0))
        conc[f"C{c}_aggregate_tok_s"] = round(statistics.mean(aggs), 2)

    # --- full HumanEval-164 (sequential, single user) with telemetry windows
    rx0, tx0 = nic_bytes()
    cb0, ct0 = cpu_times()
    tasks: List[Dict[str, Any]] = []
    answers: List[Dict[str, Any]] = []
    windows: List[Tuple[float, float]] = []
    t_run0 = time.time()
    deaths: List[Dict[str, Any]] = []
    i = -1
    while i + 1 < len(problems):
        i += 1
        pr = problems[i]
        msgs = [{"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": f"Complete this Python function:\n\n```python\n{pr['prompt']}\n```"}]
        try:
            r = chat(model, msgs, budget, kind)
        except Exception as e:  # engine hiccup -> record and keep going; abort if engine is gone
            try:
                urllib.request.urlopen(f"{ENGINE_DIRECT_URL}/health", timeout=5)
            except Exception:
                # engine died: keep its log (it is overwritten by the next launch), restart same config, retry this task
                log_copy = OUT_DIR / f"{slug(model, mode)}.engine_death_{len(deaths)+1}.log"
                try:
                    log_copy.write_text(ENGINE_LOG_PATH.read_text(errors="replace")[-20000:])
                except Exception:
                    pass
                deaths.append({"at_task": pr["task_id"], "after_tasks": len(tasks), "error": type(e).__name__, "log": log_copy.name})
                print(f"  ENGINE DIED at {pr['task_id']} (death #{len(deaths)}) -> log saved", flush=True)
                if len(deaths) > 2:
                    break
                try:
                    wait_gateway_idle(120)
                    switch_mode_and_model(mode=mode, model_name=model, tensor_split="48,52", n_cpu_moe=ncmoe, kv_type=KV_TYPE)
                except Exception as e2:
                    print("  restart failed:", str(e2)[:160], flush=True)
                    break
                i -= 1  # retry the same task
                continue
            tasks.append({"task_id": pr["task_id"], "passed": False, "cause": f"engine_error:{type(e).__name__}"})
            continue
        windows.append(r["win"])
        answers.append({"task_id": pr["task_id"], "content": r["content"][:8000]})
        ok, err = run_sandbox(build_program(pr, r["content"]))
        cause = "pass" if ok else ("truncated" if r["finish"] == "length" else ("no_code" if "```" not in r["content"] and f"def {pr['entry_point']}" not in r["content"] else err.split(":")[0] or "wrong"))
        tasks.append({"task_id": pr["task_id"], "passed": ok, "cause": cause, "err": err, "finish": r["finish"],
                      "out_tok": r["output_tokens"], "prompt_tok": r["prompt_tokens"], "ttft_ms": round(r["ttft_ms"], 1),
                      "tpot_ms": round(r["tpot_ms"], 2), "decode_tok_s": r["decode_tok_s"],
                      "prefill_tok_s": r["prefill_tok_s"], "total_ms": round(r["total_ms"], 1),
                      "think_chars": r["reasoning_chars"]})
        if (i + 1) % 20 == 0:
            p = sum(t["passed"] for t in tasks)
            print(f"  [{i+1}/164] pass={p} ({100*p/len(tasks):.1f}%) elapsed={time.time()-t_run0:.0f}s", flush=True)
    run_s = time.time() - t_run0
    rx1, tx1 = nic_bytes()
    cb1, ct1 = cpu_times()
    time.sleep(1.0)

    done = [t for t in tasks if "out_tok" in t]
    out_tok_total = sum(t["out_tok"] for t in done)
    in_tok_total = sum(t["prompt_tok"] for t in done)
    gen_s = sum(w[1] - w[0] for w in windows)
    pw = {"local_3060": tele.window_stats("local", windows), "win_5070": tele.window_stats("win", windows)}
    watts = sum(v.get("mean_power_w", 0.0) for v in pw.values()) if mode == "merged" else pw["local_3060"].get("mean_power_w", 0.0)
    joule = watts * gen_s
    causes: Dict[str, int] = {}
    for t in tasks:
        causes[t["cause"]] = causes.get(t["cause"], 0) + 1
    passed = sum(t["passed"] for t in tasks)
    nic_total = (rx1 - rx0) + (tx1 - tx0)
    summary = {
        "pass": passed, "total": len(tasks), "pass_rate_pct": round(100 * passed / max(1, len(tasks)), 2),
        "fail_causes": causes,
        "truncated": causes.get("truncated", 0),
        "hit_token_cap": sum(1 for t in done if t.get("finish") == "length"),
        "decode_tok_s": dist([t["decode_tok_s"] for t in done if t["decode_tok_s"] > 0]),
        "tpot_ms": dist([t["tpot_ms"] for t in done]),
        "ttft_ms": dist([t["ttft_ms"] for t in done]),
        "prefill_tok_s": dist([t["prefill_tok_s"] for t in done if t["prefill_tok_s"] > 0]),
        "out_tokens_per_task": round(out_tok_total / max(1, len(done)), 1),
        "prompt_tokens_per_task": round(in_tok_total / max(1, len(done)), 1),
        "gen_seconds_per_task": round(gen_s / max(1, len(done)), 2),
        "run_seconds_total": round(run_s, 1),
        "tokens_per_correct_task": round(out_tok_total / max(1, passed), 1),
        "seconds_per_correct_task": round(gen_s / max(1, passed), 2),
        "power": pw,
        "gpu_watts_counted": round(watts, 1),
        "joule_per_output_token": round(joule / max(1, out_tok_total), 4),
        "joule_per_task": round(joule / max(1, len(done)), 1),
        "joule_per_correct_task": round(joule / max(1, passed), 1),
        "net_bytes_total": nic_total,
        "net_bytes_per_output_token": round(nic_total / max(1, out_tok_total + in_tok_total), 0),
        "net_mbit_s_avg": round(nic_total * 8 / 1e6 / max(1.0, run_s), 2),
        "cpu_util_pct_host": round(100 * (cb1 - cb0) / max(1.0, ct1 - ct0), 1),
        "long_prompt_prefill": long_prefill,
        "concurrency": conc,
        "engine_deaths": deaths,
    }
    print(f"  => pass {passed}/{len(tasks)} ({summary['pass_rate_pct']}%) decode={summary['decode_tok_s'].get('mean')} tok/s "
          f"J/tok={summary['joule_per_output_token']} trunc={summary['truncated']}", flush=True)
    return {"model": model, "mode": mode, "n_cpu_moe": ncmoe, "kind": kind, "token_budget": budget, "kv_type": KV_TYPE,
            "deployment": deploy, "summary": summary, "tasks": tasks,
            "incomplete": len(tasks) < len(problems),
            "finished_at": datetime.now(timezone.utc).isoformat(), "_answers": answers}


def wait_gateway_idle(max_s: int = 1200) -> bool:
    """After a failed/timeout switch the gateway stays 'switching' until its own 600 s guard expires; wait it out,
    then put the cluster in a known-good state (Qwen3-4B split) so the next config starts clean."""
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            req = urllib.request.Request("http://127.0.0.1:18080/api/cluster/topology",
                                         headers={"Authorization": "Bearer demo-admin-token"})
            st = json.load(urllib.request.urlopen(req, timeout=10)).get("status")
        except Exception:
            st = "unknown"
        if st != "switching":
            break
        time.sleep(15)
    else:
        return False
    try:
        switch_mode_and_model(mode="split", model_name="Qwen3-4B-Q4_K_M.gguf", kv_type="f16")
    except Exception as e:
        print("  recovery switch failed:", str(e)[:200], flush=True)
        return False
    return True


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    problems = load_problems()
    assert len(problems) == 164, len(problems)
    sanity_path = OUT_DIR / "_evaluator_sanity.json"
    if not sanity_path.exists():
        s = validate_evaluator(problems)
        sanity_path.write_text(json.dumps(s, indent=2))
        print("evaluator sanity:", s, flush=True)
    s = json.loads(sanity_path.read_text())
    if s["canonical_pass"] != 164 or s["stub_pass_mutation"] > 5:
        sys.exit(f"Evaluator invalid: {s}")

    only = set(sys.argv[1:])
    tele = Telemetry()
    tele.start()
    time.sleep(3)
    try:
        for model, mode, ncmoe, kind in CONFIGS:
            f = OUT_DIR / (slug(model, mode) + TAG + ".json")
            if f.exists() or (only and slug(model, mode) not in only and model not in only):
                continue
            try:
                res = run_config(model, mode, ncmoe, kind, problems, tele)
                res["identity"] = inspect_gguf_identity(model)
            except Exception as e:  # config could not even load (e.g. VRAM guard / rpc crash)
                print(f"  CONFIG FAILED: {e}", flush=True)
                if not wait_gateway_idle():
                    print("  gateway STILL stuck after 20 min -> STOP (no cascade)", flush=True)
                    break
                if "already in progress" in str(e):  # infra collision, not a property of this model: do not record it
                    continue
                res = {"model": model, "mode": mode, "n_cpu_moe": ncmoe, "kind": kind, "load_failed": str(e)[:600],
                       "engine_log_tail": ENGINE_LOG_PATH.read_text(errors="replace")[-1500:] if ENGINE_LOG_PATH.exists() else ""}
            ans = res.pop("_answers", None)
            if ans:
                (OUT_DIR / (slug(model, mode) + TAG + ".answers.jsonl")).write_text("\n".join(json.dumps(a, ensure_ascii=False) for a in ans))
            f = OUT_DIR / (slug(model, mode) + TAG + (".INCOMPLETE" if res.get("incomplete") else "") + ".json")
            f.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    finally:
        print("\nrestoring production default (Coder-30B merged 48:52)", flush=True)
        try:
            switch_mode_and_model(mode="merged", model_name="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
                                  tensor_split="48,52", kv_type="f16")
        except Exception as e:
            print("restore failed:", e, flush=True)
        tele.stop()
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
