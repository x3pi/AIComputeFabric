"""Unit, integration, and mutation tests for Best-of-N (Pha A, Pha 2, Pha 3, Pha 4)."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agents.sandbox import SandboxUnavailable
from bestofn.agents_client import ComputeAgentInfo
from bestofn.planner import build_plan
from bestofn.runner import SandboxPool
from bestofn.sampler import allocate_samples_to_agents
from bestofn.selector import select_best_candidate
from bestofn.service import BestOfNOrchestrator

BESTOFN_DIR = Path(__file__).resolve().parents[2] / "bestofn"

FORBIDDEN_PATTERNS = [
    r"\btensor_split\b",
    r"\bgpu_name\b",
    r"\bvram_[a-z_]*\b",
    r"/api/cluster/topology",
    r"\bworkers\b",
]


def scan_source_for_forbidden_hardware_terms(text: str) -> list[str]:
    violations: list[str] = []
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, text):
            violations.append(pat)
    return violations


def test_static_hardware_hiding_and_mutation() -> None:
    """§2B & Pha A Check Pass: bestofn/ must NEVER reference GPU/topology internals."""
    py_files = sorted(BESTOFN_DIR.glob("*.py"))
    assert len(py_files) >= 6
    for f in py_files:
        src = f.read_text(encoding="utf-8")
        v = scan_source_for_forbidden_hardware_terms(src)
        assert not v, f"Hardware-hiding violation in {f.name}: matched {v}"

    # Mutation check: injecting any forbidden term into the scanner MUST fail (turn red)
    for mutated_snippet in (
        'x = resp["gpu_name"]',
        'ts = cfg["tensor_split"]',
        'free = node["vram_free_bytes"]',
        'url = "/api/cluster/topology"',
        'w = data["workers"]',
    ):
        detected = scan_source_for_forbidden_hardware_terms(mutated_snippet)
        assert detected, f"Mutation check failed: scanner did not catch {mutated_snippet!r}"


def test_allocation_proportional_to_slots_and_tok_s_with_cap() -> None:
    """§2B.4: sample allocation scales with slots * tok_s_ref and respects max_share_per_agent."""
    variants = build_plan([{"role": "user", "content": "def f(x):"}], n=8)
    fast_group = ComputeAgentInfo(
        agent_id="group-1", kind="group", model="Coder-30B", ctx_per_slot=32768, slots=2, tok_s_ref=130.0, labels=["code"]
    )
    slow_single = ComputeAgentInfo(
        agent_id="single-2", kind="single", model="Coder-30B", ctx_per_slot=8192, slots=1, tok_s_ref=45.0, labels=["code"]
    )
    plan = allocate_samples_to_agents(variants, [fast_group, slow_single], max_share_per_agent=0.70)
    counts: dict[str, int] = {}
    for a in plan.values():
        counts[a.agent_id] = counts.get(a.agent_id, 0) + 1
    assert sum(counts.values()) == 8
    assert counts["group-1"] > counts["single-2"]
    assert counts["group-1"] <= 6  # ceil(8 * 0.70) = 6 cap prevents 100% monopolization


def test_sandbox_pool_rejects_unisolated_backend_mutation() -> None:
    """Pha 2.1 & 2.3 mutation check: SandboxPool MUST refuse any sandbox with isolates_network=False."""
    class UnisolatedFakeSandbox:
        name = "fake_unisolated"
        isolates_network = False
        isolates_filesystem = True

    with pytest.raises(SandboxUnavailable, match="network isolation"):
        SandboxPool(pool_size=2, sandbox_override=UnisolatedFakeSandbox())


def test_sandbox_and_selector_mutations() -> None:
    """Pha 2.3 mutation checks (a) network/fs blocked, (b) timeout kill, (c) output truncation, (d) syntax error skipped."""
    pool = SandboxPool(pool_size=2, timeout_s=2.0, max_output_bytes=16384)
    suite = ["assert solve(3) == 6\nassert solve(0) == 0"]

    # Candidate 0: SyntaxError (must be ignored by selector even though sample_index=0 and code is short)
    c_syntax = pool.evaluate_one(
        0, "ag-1", "m", "direct", 0.0, 10.0,
        "```python\ndef solve(x)\n    return x * 2\n```",
        suite, entry_point="solve",
    )
    assert not c_syntax.compile_ok and c_syntax.error_type == "SyntaxError"

    # Candidate 1: Attempts network socket connection -> blocked inside DockerSandbox
    c_net = pool.evaluate_one(
        1, "ag-1", "m", "edge", 0.5, 10.0,
        "```python\nimport socket\ndef solve(x):\n    s = socket.socket()\n    s.settimeout(1.0)\n    s.connect(('1.1.1.1', 80))\n    return x * 2\n```",
        suite, entry_point="solve",
    )
    assert c_net.compile_ok and c_net.passed_tests == 0

    # Candidate 2: Attempts to write to read-only rootfs -> blocked
    c_fs = pool.evaluate_one(
        2, "ag-1", "m", "stdlib", 0.6, 10.0,
        "```python\ndef solve(x):\n    open('/etc/pwn', 'w').write('1')\n    return x * 2\n```",
        suite, entry_point="solve",
    )
    assert c_fs.compile_ok and c_fs.passed_tests == 0

    # Candidate 3: Infinite loop -> killed at timeout_s=2.0
    c_inf = pool.evaluate_one(
        3, "ag-1", "m", "inv", 0.7, 10.0,
        "```python\ndef solve(x):\n    while True:\n        pass\n```",
        suite, entry_point="solve",
    )
    assert not c_inf.compile_ok and c_inf.error_type == "SandboxError" and "timed out" in c_inf.error

    # Candidate 4: Prints 100KB of stdout -> truncated to max_output_bytes // 2 (8192 bytes)
    c_flood = pool.evaluate_one(
        4, "ag-1", "m", "decomp", 0.8, 10.0,
        "```python\ndef solve(x):\n    print('A' * 100000)\n    return x * 2\n```",
        suite, entry_point="solve",
    )
    assert c_flood.output_truncated and len(c_flood.stdout) <= 8192

    # Candidate 5: Clean, correct implementation
    c_good = pool.evaluate_one(
        5, "ag-1", "m", "minimal", 0.9, 10.0,
        "```python\ndef solve(x: int) -> int:\n    return x * 2\n```",
        suite, entry_point="solve",
    )
    assert c_good.compile_ok and c_good.passed_tests == 1

    sel = select_best_candidate([c_syntax, c_net, c_fs, c_inf, c_good])
    assert sel.selected is not None
    assert sel.selected.sample_index == 5
    assert sel.selected.passed_tests == 1


def test_three_topology_modes_and_midflight_agent_drop_same_code() -> None:
    """Pha A Check Pass (a) & (b):
    Run the EXACT same BestOfNOrchestrator code across:
      1. 2 standalone compute agents (mode A/B)
      2. 1 merged group compute agent (mode C, RPC member hidden)
      3. 1 merged group + 1 standalone compute agent (hybrid)
      4. Mid-flight agent disappearance during sampling (automatic reassignment without failure)
    """
    state_lock = threading.Lock()
    current_agents: list[dict[str, Any]] = []
    drop_agent_after_first_call: str = ""
    agent_call_counts: dict[str, int] = {}

    class FakeGatewayHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/api/compute-agents"):
                with state_lock:
                    payload = json.dumps(current_agents).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/v1/chat/completions":
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                pinned = self.headers.get("X-AICF-Agent", "")
                with state_lock:
                    agent_call_counts[pinned] = agent_call_counts.get(pinned, 0) + 1
                    live_ids = {a["agent_id"] for a in current_agents if a["state"] == "running"}
                    if drop_agent_after_first_call and pinned == drop_agent_after_first_call and agent_call_counts[pinned] >= 1:
                        for a in current_agents:
                            if a["agent_id"] == pinned:
                                a["state"] = "offline"
                        live_ids.discard(pinned)

                if pinned not in live_ids:
                    err = json.dumps({"error": {"message": "pinned agent is offline", "code": 503}}).encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", "0.05")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                resp = {
                    "choices": [{"message": {"content": "```python\ndef double_val(x):\n    return x * 2\n```"}}],
                    "usage": {"prompt_tokens": 25, "completion_tokens": 12},
                    "timings": {"predicted_per_second": 100.0},
                }
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-AICF-Agent", pinned)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGatewayHandler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    try:
        orch = BestOfNOrchestrator(
            gateway_url=f"http://127.0.0.1:{port}",
            api_key="aicf_test_key",
            sandbox_pool_size=2,
            default_n=4,
        )
        scenarios = [
            (
                "2_standalone",
                [
                    {"agent_id": "pc-001", "kind": "single", "model": "Coder-30B", "ctx_per_slot": 8192, "slots": 1, "tok_s_ref": 46.2, "labels": ["code"], "state": "running", "busy": False},
                    {"agent_id": "pc-win-01", "kind": "single", "model": "Coder-30B", "ctx_per_slot": 8192, "slots": 1, "tok_s_ref": 52.2, "labels": ["code"], "state": "running", "busy": False},
                ],
            ),
            (
                "1_merged_group",
                [
                    {"agent_id": "pc-001", "kind": "group", "model": "Coder-30B", "ctx_per_slot": 32768, "slots": 1, "tok_s_ref": 126.7, "labels": ["code"], "state": "running", "busy": False},
                ],
            ),
            (
                "1_group_plus_1_standalone",
                [
                    {"agent_id": "pc-001", "kind": "group", "model": "Coder-30B", "ctx_per_slot": 32768, "slots": 1, "tok_s_ref": 126.7, "labels": ["code"], "state": "running", "busy": False},
                    {"agent_id": "pc-003", "kind": "single", "model": "Coder-30B", "ctx_per_slot": 8192, "slots": 2, "tok_s_ref": 63.2, "labels": ["code"], "state": "running", "busy": False},
                ],
            ),
        ]

        for name, agent_list in scenarios:
            with state_lock:
                current_agents[:] = [dict(a) for a in agent_list]
                drop_agent_after_first_call = ""
                agent_call_counts.clear()

            res = orch.run_best_of_n(
                messages=[{"role": "user", "content": "Write double_val(x) returning x * 2."}],
                model="bon:Coder-30B",
                n=4,
                tests=["assert double_val(4) == 8\nassert double_val(-3) == -6"],
                entry_point="double_val",
            )
            rep = res["bon_report"]
            assert rep["n_candidates"] == 4, name
            assert rep["n_all_tests_passed"] == 4, name
            assert "double_val" in res["choices"][0]["message"]["content"]

        # Scenario 4 (Check Pass b): mid-flight agent disappears after first sample -> reassigned to surviving agent
        with state_lock:
            current_agents[:] = [
                {"agent_id": "ag-survivor", "kind": "group", "model": "Coder-30B", "ctx_per_slot": 32768, "slots": 2, "tok_s_ref": 120.0, "labels": ["code"], "state": "running", "busy": False},
                {"agent_id": "ag-dropping", "kind": "single", "model": "Coder-30B", "ctx_per_slot": 8192, "slots": 1, "tok_s_ref": 80.0, "labels": ["code"], "state": "running", "busy": False},
            ]
            drop_agent_after_first_call = "ag-dropping"
            agent_call_counts.clear()

        res_drop = orch.run_best_of_n(
            messages=[{"role": "user", "content": "Write double_val(x) returning x * 2."}],
            model="bon:Coder-30B",
            n=6,
            tests=["assert double_val(5) == 10"],
            entry_point="double_val",
        )
        rep_drop = res_drop["bon_report"]
        assert rep_drop["n_candidates"] == 6
        assert rep_drop["n_all_tests_passed"] == 6
        assert len(rep_drop["sampling"]["reassignments"]) >= 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_verilog_eda_evaluation_and_selection() -> None:
    """Verify Best-of-N Verilog/SystemVerilog EDA evaluation (Verilator + Icarus Verilog + Yosys)."""
    pool = SandboxPool(pool_size=2, timeout_s=8.0)
    tb = """`timescale 1ns/1ps
module tb_pulse;
    reg [7:0] a, b;
    wire [8:0] sum;
    adder8 dut(.a(a), .b(b), .sum(sum));
    initial begin
        a = 8'd100; b = 8'd200; #1;
        if (sum !== 9'd300) begin
            $display("FAIL: got %0d", sum);
            $finish;
        end
        $display("PASS: sum=%0d", sum);
        $finish;
    end
endmodule
"""
    c_bad_syntax = pool.evaluate_one(
        0, "ag-1", "Qwen3.8-27B", "direct", 0.0, 12.0,
        "```verilog\nmodule adder8(input [7:0] a, b, output [8:0] sum)\n assign sum = a + b\nendmodule\n```",
        [tb], entry_point="adder8", language="verilog",
    )
    assert not c_bad_syntax.compile_ok

    c_wrong_logic = pool.evaluate_one(
        1, "ag-1", "Qwen3.8-27B", "edge", 0.4, 12.0,
        "```verilog\nmodule adder8(input [7:0] a, input [7:0] b, output [8:0] sum);\n  assign sum = a - b;\nendmodule\n```",
        [tb], entry_point="adder8", language="verilog",
    )
    assert c_wrong_logic.compile_ok and c_wrong_logic.passed_tests == 0

    c_good_rtl = pool.evaluate_one(
        2, "ag-1", "Qwen3.8-27B", "minimal", 0.6, 12.0,
        "```verilog\nmodule adder8(input [7:0] a, input [7:0] b, output [8:0] sum);\n  assign sum = a + b;\nendmodule\n```",
        [tb], entry_point="adder8", language="verilog",
    )
    assert c_good_rtl.compile_ok and c_good_rtl.passed_tests == 1
    assert "[Yosys Synthesis]" in c_good_rtl.stdout

    sel = select_best_candidate([c_bad_syntax, c_wrong_logic, c_good_rtl])
    assert sel.selected is not None
    assert sel.selected.sample_index == 2

