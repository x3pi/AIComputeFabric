"""Open-source EDA toolchain runner (Verilator + Icarus Verilog + Yosys) for Best-of-N RTL design."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

EDA_DEFAULT_BIN = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work/eda/bin")


def resolve_eda_tool(name: str) -> str:
    """Locate an EDA binary (iverilog, vvp, verilator, yosys) on PATH or in .aicf-work/eda/bin."""
    candidate = EDA_DEFAULT_BIN / name
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin" / name
    if local_bin.exists():
        return str(local_bin)
    return name


def extract_verilog_block(raw_text: str, top_module: str = "") -> str:
    """Extract synthesizable Verilog/SystemVerilog code from LLM markdown output."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    blocks = re.findall(
        r"```(?:systemverilog|verilog|sv|v)?\s*\n(.*?)```",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if blocks:
        if top_module:
            for b in blocks:
                if re.search(rf"\bmodule\s+{re.escape(top_module)}\b", b):
                    return b.rstrip()
        # Concatenate or pick block containing `module `
        mod_blocks = [b.rstrip() for b in blocks if re.search(r"\bmodule\s+\w+", b)]
        if mod_blocks:
            return "\n\n".join(mod_blocks)
        return blocks[-1].rstrip()

    open_match = re.search(
        r"```(?:systemverilog|verilog|sv|v)?\s*\n(.*)$",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if open_match:
        return open_match.group(1).rstrip()
    return cleaned.rstrip()


def normalize_verilog_structure(code: str) -> str:
    """Strip comments and normalize whitespace for structural consensus hashing."""
    no_block = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    lines = []
    for ln in no_block.splitlines():
        s = re.sub(r"//.*$", "", ln).strip()
        if s:
            lines.append(re.sub(r"\s+", " ", s))
    return "\n".join(lines)


def infer_top_module(code: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    mods = re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)\b", code)
    non_tb = [m for m in mods if not m.lower().startswith("tb")]
    if non_tb:
        return non_tb[-1]
    return mods[0] if mods else "top"


def evaluate_verilog_candidate(
    candidate_code: str,
    test_suites: list[str],
    setup_code: str = "",
    top_module: str = "",
    timeout_s: float = 10.0,
    max_output_bytes: int = 32768,
    run_synthesis: bool = True,
) -> dict[str, Any]:
    """Run Verilator lint, Icarus Verilog simulation, and Yosys gate-level synthesis in an isolated temp dir."""
    t0 = time.perf_counter()
    full_rtl = (setup_code + "\n\n" + candidate_code).strip() if setup_code else candidate_code.strip()
    top = infer_top_module(full_rtl, top_module)

    iverilog_bin = resolve_eda_tool("iverilog")
    vvp_bin = resolve_eda_tool("vvp")
    verilator_bin = resolve_eda_tool("verilator")
    yosys_bin = resolve_eda_tool("yosys")

    if not full_rtl or "module" not in full_rtl:
        return {
            "compile_ok": False,
            "compile_error_type": "SyntaxError",
            "compile_error": "No valid Verilog `module ... endmodule` found in candidate output",
            "passed_tests": 0,
            "total_tests": len(test_suites),
            "suite_results": [],
            "pass_vector": "0" * len(test_suites),
            "exec_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "stdout": "",
            "stdout_bytes": 0,
            "output_truncated": False,
            "eda_metrics": {},
        }

    with tempfile.TemporaryDirectory(prefix="aicf_eda_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        rtl_file = tmp_path / f"{top}.sv"
        rtl_file.write_text(full_rtl + "\n", encoding="utf-8")

        # 1. Verilator static lint check
        lint_warnings: list[str] = []
        lint_cmd = [
            verilator_bin,
            "--lint-only",
            "-sv",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-MULTITOP",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "-Wno-PINCONNECTEMPTY",
            "--top-module",
            top,
            str(rtl_file),
        ]
        try:
            lint_proc = subprocess.run(
                lint_cmd,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=min(timeout_s, 10.0),
            )
            lint_out = (lint_proc.stdout + "\n" + lint_proc.stderr).strip()
            for ln in lint_out.splitlines():
                if "%Warning" in ln or "%Error" in ln:
                    lint_warnings.append(ln.strip())
        except Exception as exc:
            lint_warnings.append(f"verilator exception: {exc}")

        # 2. Compile RTL standalone with iverilog -g2012 to verify syntax
        syntax_vvp = tmp_path / "syntax_check.vvp"
        comp_cmd = [iverilog_bin, "-g2012", "-s", top, "-o", str(syntax_vvp), str(rtl_file)]
        try:
            comp_proc = subprocess.run(
                comp_cmd,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=min(timeout_s, 10.0),
            )
        except subprocess.TimeoutExpired:
            return {
                "compile_ok": False,
                "compile_error_type": "SandboxError",
                "compile_error": f"iverilog compile timed out after {timeout_s}s",
                "passed_tests": 0,
                "total_tests": len(test_suites),
                "suite_results": [],
                "pass_vector": "0" * len(test_suites),
                "exec_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "stdout": "",
                "stdout_bytes": 0,
                "output_truncated": False,
                "eda_metrics": {},
            }

        if comp_proc.returncode != 0:
            err_text = (comp_proc.stderr or comp_proc.stdout).strip()
            return {
                "compile_ok": False,
                "compile_error_type": "VerilogCompileError",
                "compile_error": err_text[:600],
                "passed_tests": 0,
                "total_tests": len(test_suites),
                "suite_results": [],
                "pass_vector": "0" * len(test_suites),
                "exec_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "stdout": err_text[:max_output_bytes],
                "stdout_bytes": len(err_text),
                "output_truncated": len(err_text) > max_output_bytes,
                "eda_metrics": {"lint_warnings": lint_warnings[:10]},
            }

        # 3. Run each Verilog testbench suite in test_suites
        suite_results: list[dict[str, Any]] = []
        passed_count = 0
        stdout_parts: list[str] = []

        for idx, tb_src in enumerate(test_suites):
            tb_file = tmp_path / f"tb_{idx}.sv"
            sim_vvp = tmp_path / f"sim_{idx}.vvp"
            tb_file.write_text(tb_src + "\n", encoding="utf-8")

            tb_mods = re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)\b", tb_src)
            tb_top = tb_mods[-1] if tb_mods else "tb"

            tb_comp = subprocess.run(
                [iverilog_bin, "-g2012", "-s", tb_top, "-o", str(sim_vvp), str(rtl_file), str(tb_file)],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=min(timeout_s, 10.0),
            )
            if tb_comp.returncode != 0:
                err_msg = (tb_comp.stderr or tb_comp.stdout).strip()
                suite_results.append({
                    "suite_index": idx,
                    "passed": False,
                    "error_type": "TestbenchCompileError",
                    "error": err_msg[:400],
                })
                stdout_parts.append(f"[Suite {idx} Compile Error]\n{err_msg}")
                continue

            try:
                sim_proc = subprocess.run(
                    [vvp_bin, str(sim_vvp)],
                    cwd=tmp_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                suite_results.append({
                    "suite_index": idx,
                    "passed": False,
                    "error_type": "SimulationTimeout",
                    "error": f"vvp simulation timed out after {timeout_s}s (possible infinite clock loop without $finish)",
                })
                continue

            sim_out = (sim_proc.stdout + "\n" + sim_proc.stderr).strip()
            stdout_parts.append(sim_out)

            has_fail = bool(re.search(r"\b(FAIL|FAILED|ERROR|FATAL)\b", sim_out, re.IGNORECASE))
            has_pass = bool(re.search(r"\b(PASS|PASSED|ALL_TESTS_PASSED)\b", sim_out, re.IGNORECASE))
            s_ok = (sim_proc.returncode == 0) and (not has_fail) and (has_pass or "FAIL" not in sim_out.upper())

            if s_ok:
                passed_count += 1
                suite_results.append({
                    "suite_index": idx,
                    "passed": True,
                    "error_type": "",
                    "error": "",
                })
            else:
                fail_lines = [
                    ln.strip()
                    for ln in sim_out.splitlines()
                    if any(k in ln.upper() for k in ("FAIL", "ERROR", "FATAL", "MISMATCH", "ASSERT"))
                ]
                err_detail = "; ".join(fail_lines[:4]) if fail_lines else sim_out[-300:]
                suite_results.append({
                    "suite_index": idx,
                    "passed": False,
                    "error_type": "SimulationMismatch",
                    "error": err_detail[:400],
                })

        # 4. Run Yosys synthesis to measure gate count & flip-flops
        eda_metrics: dict[str, Any] = {
            "top_module": top,
            "lint_warnings_count": len(lint_warnings),
            "lint_warnings": lint_warnings[:10],
            "synth_ok": False,
            "total_cells": 0,
            "flip_flops": 0,
            "wire_bits": 0,
        }
        if run_synthesis:
            stat_file = tmp_path / "stat.json"
            yosys_script = (
                f"read_verilog -sv {rtl_file.name}; hierarchy -check -top {top}; "
                f"proc; opt; memory; opt; techmap; opt; tee -q -o {stat_file.name} stat -json"
            )
            try:
                y_proc = subprocess.run(
                    [yosys_bin, "-q", "-p", yosys_script],
                    cwd=tmp_dir,
                    capture_output=True,
                    text=True,
                    timeout=min(timeout_s, 15.0),
                )
                if y_proc.returncode == 0 and stat_file.exists():
                    stat_json = json.loads(stat_file.read_text(encoding="utf-8"))
                    design_stat = stat_json.get("design", {})
                    cells_by_type = design_stat.get("num_cells_by_type", {})
                    dff_count = sum(
                        int(cnt)
                        for ctype, cnt in cells_by_type.items()
                        if "DFF" in ctype.upper()
                    )
                    latch_count = sum(
                        int(cnt)
                        for ctype, cnt in cells_by_type.items()
                        if "LATCH" in ctype.upper() or "DLATCH" in ctype.upper()
                    )
                    eda_metrics.update({
                        "synth_ok": True,
                        "total_cells": int(design_stat.get("num_cells", 0)),
                        "flip_flops": dff_count,
                        "inferred_latches": latch_count,
                        "wire_bits": int(design_stat.get("num_wire_bits", 0)),
                        "cells_by_type": cells_by_type,
                    })
                    stdout_parts.append(
                        f"[Yosys Synthesis] top={top} cells={eda_metrics['total_cells']} "
                        f"dff={dff_count} latches={latch_count} wire_bits={eda_metrics['wire_bits']}"
                    )
            except Exception as exc:
                eda_metrics["synth_error"] = str(exc)[:200]

        dt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        combined_stdout = "\n".join(p for p in stdout_parts if p).strip()
        raw_bytes = combined_stdout.encode("utf-8", errors="replace")
        truncated = len(raw_bytes) > max_output_bytes
        if truncated:
            raw_bytes = raw_bytes[:max_output_bytes]

        pass_vector = "".join("1" if r["passed"] else "0" for r in suite_results)
        return {
            "compile_ok": True,
            "compile_error_type": "",
            "compile_error": "",
            "passed_tests": passed_count,
            "total_tests": len(test_suites),
            "suite_results": suite_results,
            "pass_vector": pass_vector,
            "exec_ms": dt_ms,
            "stdout": raw_bytes.decode("utf-8", errors="replace"),
            "stdout_bytes": len(raw_bytes),
            "output_truncated": truncated,
            "eda_metrics": eda_metrics,
        }

