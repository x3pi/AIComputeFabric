"""HTTP service for Best-of-N code generation (Pha 4 & Pha 5, port 18099).

Exposes OpenAI-compatible endpoints (proxied by site-gateway at /api/bestofn/*):
  - GET  /v1/health
  - GET  /v1/models
  - POST /v1/chat/completions
      Supports `model: "bon:<base_model>"` and `bon: {n, tests, setup_code, entry_point, language, timeout_s, generate_tests, m_test_suites, allow_multi_model}`.
      Enforces Pha 4.2 safety limits:
        * 1 <= n <= 16
        * request body <= 256 KiB
        * timeout_s <= 30.0s per sandbox execution
      Supports both `stream: false` (JSON response with `bon_report`) and `stream: true`
      (SSE stream with keepalive comments + OpenAI delta chunks + `bon_report` on final chunk
      so IDE extensions like Continue / Cline do not time out while waiting for N samples).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agents.sandbox import SandboxUnavailable, pick_sandbox
from bestofn.agents_client import ComputeAgentsClient
from bestofn.planner import build_plan
from bestofn.runner import SandboxPool
from bestofn.sampler import sample_candidates
from bestofn.selector import select_best_candidate
from bestofn.test_gen import filter_executable_generated_tests, generate_test_suites

MAX_BON_N = 16
MAX_BODY_BYTES = 256 * 1024
MAX_SANDBOX_TIMEOUT_S = 30.0


def extract_prompt_text(messages: list[dict[str, str]]) -> str:
    user_parts = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
    return "\n\n".join(user_parts).strip()


def infer_entry_point_from_text(text: str) -> str:
    m = re.search(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
    return m.group(1) if m else ""


class BestOfNOrchestrator:
    """High-level Best-of-N workflow executing Plan -> Sample -> (Optional TestGen) -> Run -> Select."""

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:18080",
        api_key: str = "",
        sandbox_pool_size: int = 4,
        default_n: int = 4,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.default_n = default_n
        self.agents_client = ComputeAgentsClient(gateway_url=self.gateway_url, api_key=self.api_key)
        self.sandbox_pool = SandboxPool(pool_size=sandbox_pool_size)

    def run_best_of_n(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        n: int = 4,
        tests: list[str] | str | None = None,
        setup_code: str = "",
        entry_point: str = "",
        timeout_s: float = 6.0,
        max_tokens: int = 512,
        seed: int = 42,
        generate_tests: bool = False,
        m_test_suites: int = 3,
        allow_multi_model: bool = False,
        max_share_per_agent: float = 0.70,
    ) -> dict[str, Any]:
        if n < 1 or n > MAX_BON_N:
            raise ValueError(f"bon.n must be between 1 and {MAX_BON_N}, got {n}")
        if timeout_s <= 0 or timeout_s > MAX_SANDBOX_TIMEOUT_S:
            raise ValueError(f"bon.timeout_s must be in (0, {MAX_SANDBOX_TIMEOUT_S}], got {timeout_s}")

        t_total = time.perf_counter()
        variants = build_plan(base_messages=messages, n=n, base_seed=seed)

        samples, sampling_telemetry = sample_candidates(
            variants=variants,
            agents_client=self.agents_client,
            gateway_url=self.gateway_url,
            api_key=self.api_key,
            requested_model=model,
            required_label="code",
            max_tokens=max_tokens,
            allow_multi_model=allow_multi_model,
            max_share_per_agent=max_share_per_agent,
        )

        # Normalize test suites
        test_suites: list[str] = []
        test_source = "none"
        test_gen_telemetry: dict[str, Any] = {}
        if isinstance(tests, str) and tests.strip():
            test_suites = [tests.strip()]
            test_source = "provided"
        elif isinstance(tests, list):
            test_suites = [str(t).strip() for t in tests if str(t).strip()]
            if test_suites:
                test_source = "provided"

        prompt_text = extract_prompt_text(messages)
        ep = entry_point or infer_entry_point_from_text(setup_code or prompt_text)

        if not test_suites and generate_tests:
            test_suites, test_gen_telemetry = generate_test_suites(
                problem_prompt=prompt_text,
                entry_point=ep,
                m_suites=m_test_suites,
                gateway_url=self.gateway_url,
                api_key=self.api_key,
                agents_client=self.agents_client,
                requested_model=model,
                seed=seed + 500,
            )
            if test_suites:
                test_source = "generated"

        self.sandbox_pool.timeout_s = timeout_s
        evaluations, runner_telemetry = self.sandbox_pool.evaluate_batch(
            samples=samples,
            test_suites=test_suites,
            setup_code=setup_code,
            entry_point=ep,
        )

        kept_gen_tests = len(test_suites)
        if test_source == "generated" and evaluations:
            kept_gen_tests = filter_executable_generated_tests(evaluations)

        sel = select_best_candidate(
            evaluations=evaluations,
            sampling_telemetry=sampling_telemetry,
            runner_telemetry=runner_telemetry,
            test_source=test_source,
            generated_test_count=kept_gen_tests if test_source == "generated" else len(test_suites),
        )
        if test_gen_telemetry:
            sel.bon_report["test_generation"] = test_gen_telemetry
        sel.bon_report["total_wall_ms"] = round((time.perf_counter() - t_total) * 1000.0, 2)

        winner = sel.selected
        selected_code = winner.code if winner else ""
        formatted_content = f"```python\n{selected_code}\n```" if selected_code else ""
        total_prompt_tokens = sum(s.prompt_tokens for s in samples if s.ok)
        total_comp_tokens = sum(s.completion_tokens for s in samples if s.ok)

        return {
            "id": f"chatcmpl-bon-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": f"bon:{winner.model}" if winner and winner.model else (model or "bon:auto"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": formatted_content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_comp_tokens,
                "total_tokens": total_prompt_tokens + total_comp_tokens,
            },
            "bon_report": sel.bon_report,
        }


def make_handler(orchestrator: BestOfNOrchestrator):
    class BestOfNHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("/v1/health", "/health"):
                sb = pick_sandbox(require_network_isolation=True)
                try:
                    agents = orchestrator.agents_client.list_agents(force_refresh=True)
                except Exception:
                    agents = []
                self._send_json(200, {
                    "ok": bool(sb.isolates_network),
                    "sandbox": sb.name,
                    "isolates_network": bool(sb.isolates_network),
                    "compute_agents_online": sum(1 for a in agents if a.state == "running"),
                    "max_n": MAX_BON_N,
                })
                return
            if path == "/v1/models":
                try:
                    agents = orchestrator.agents_client.list_agents(force_refresh=True)
                except Exception:
                    agents = []
                seen: set[str] = set()
                data = [{"id": "bon:auto", "object": "model", "owned_by": "aicf-bestofn"}]
                for a in agents:
                    if a.model and a.model not in seen:
                        seen.add(a.model)
                        data.append({"id": f"bon:{a.model}", "object": "model", "owned_by": "aicf-bestofn"})
                self._send_json(200, {"object": "list", "data": data})
                return
            self._send_json(404, {"error": {"message": f"unknown path {path}", "code": 404}})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path != "/v1/chat/completions":
                self._send_json(404, {"error": {"message": "not found", "code": 404}})
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {
                    "error": {"message": f"request body must be between 1 and {MAX_BODY_BYTES} bytes", "code": 400}
                })
                return

            raw = self.rfile.read(length)
            try:
                req_body = json.loads(raw.decode("utf-8"))
            except ValueError:
                self._send_json(400, {"error": {"message": "body must be valid JSON", "code": 400}})
                return

            messages = req_body.get("messages")
            if not isinstance(messages, list) or not messages:
                self._send_json(400, {"error": {"message": "messages must be a non-empty array", "code": 400}})
                return

            bon_cfg = req_body.get("bon") or {}
            if not isinstance(bon_cfg, dict):
                self._send_json(400, {"error": {"message": "bon must be an object", "code": 400}})
                return

            n = int(bon_cfg.get("n", orchestrator.default_n))
            if n < 1 or n > MAX_BON_N:
                self._send_json(400, {
                    "error": {"message": f"bon.n must be between 1 and {MAX_BON_N} (got {n})", "code": 400}
                })
                return

            language = str(bon_cfg.get("language", "python")).lower()
            if language != "python":
                self._send_json(400, {
                    "error": {"message": f"unsupported language {language!r}; only 'python' is enabled", "code": 400}
                })
                return

            timeout_s = float(bon_cfg.get("timeout_s", 6.0))
            if timeout_s <= 0 or timeout_s > MAX_SANDBOX_TIMEOUT_S:
                self._send_json(400, {
                    "error": {"message": f"bon.timeout_s must be in (0, {MAX_SANDBOX_TIMEOUT_S}]", "code": 400}
                })
                return

            model = str(req_body.get("model", "bon:auto"))
            max_tokens = min(2048, max(64, int(req_body.get("max_tokens", 512))))
            seed = int(req_body.get("seed", 42))
            stream = bool(req_body.get("stream", False))

            try:
                result = orchestrator.run_best_of_n(
                    messages=messages,
                    model=model,
                    n=n,
                    tests=bon_cfg.get("tests"),
                    setup_code=str(bon_cfg.get("setup_code", "")),
                    entry_point=str(bon_cfg.get("entry_point", "")),
                    timeout_s=timeout_s,
                    max_tokens=max_tokens,
                    seed=seed,
                    generate_tests=bool(bon_cfg.get("generate_tests", False)),
                    m_test_suites=int(bon_cfg.get("m_test_suites", 3)),
                    allow_multi_model=bool(bon_cfg.get("allow_multi_model", False)),
                )
            except SandboxUnavailable as exc:
                self._send_json(503, {"error": {"message": str(exc), "code": 503}})
                return
            except ValueError as exc:
                self._send_json(400, {"error": {"message": str(exc), "code": 400}})
                return
            except Exception as exc:
                self._send_json(503, {"error": {"message": f"Best-of-N execution failed: {exc}", "code": 503}})
                return

            if not stream:
                self._send_json(200, result)
                return

            # SSE streaming mode for IDE compatibility (Continue / Cline)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            chunk = {
                "id": result["id"],
                "object": "chat.completion.chunk",
                "created": result["created"],
                "model": result["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": result["choices"][0]["message"]["content"],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "bon_report": result["bon_report"],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()

    return BestOfNHandler


def main() -> int:
    ap = argparse.ArgumentParser(description="Best-of-N code generation HTTP service (:18099)")
    ap.add_argument("--listen", default="127.0.0.1:18099", help="host:port to listen on")
    ap.add_argument("--gateway", default="http://127.0.0.1:18080", help="AICF Gateway URL")
    ap.add_argument("--api-key", default="", help="Tenant API key for calling gateway /v1 and /api/compute-agents")
    ap.add_argument("--pool-size", type=int, default=4, help="Max concurrent Docker sandbox containers")
    ap.add_argument("--default-n", type=int, default=4, help="Default N when bon.n is omitted")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("AICF_BESTOFN_API_KEY", "") or os.environ.get("AICF_API_KEY", "")
    if not api_key:
        root = Path(__file__).resolve().parents[1]
        for p in (root / ".demo" / "bestofn_api_key.txt", root / ".demo" / "vscode_api_key.txt"):
            if p.exists():
                api_key = p.read_text(encoding="utf-8").strip()
                break

    orchestrator = BestOfNOrchestrator(
        gateway_url=args.gateway,
        api_key=api_key,
        sandbox_pool_size=args.pool_size,
        default_n=args.default_n,
    )
    host, port_str = args.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port_str)), make_handler(orchestrator))
    print(f"bestofn service listening on http://{args.listen} -> gateway {args.gateway}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

