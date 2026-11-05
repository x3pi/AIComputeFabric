"""Mutation harness (project rule 3): break ONE safety condition in the source, the selected tests must go RED (non-zero exit or
timeout), then the file is restored byte-for-byte and the same tests must be GREEN again. The report is written as JSON.

A Mutation is (id, file, old, new, command). `old` must occur exactly once in `file`; the harness refuses to run otherwise
(a mutation that does not apply proves nothing)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Mutation:
    id: str
    file: str
    old: str
    new: str
    cmd: list           # argv run from ROOT; exit 0 = tests green
    why: str = ""
    timeout_s: float = 300.0


def run(cmd: list, timeout_s: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout + p.stderr)[-600:]
    except subprocess.TimeoutExpired:
        return 124, "timeout (treated as RED)"


def run_all(mutations: list, report: Path, phase: str) -> int:
    results, bad = [], 0
    base_rc, base_tail = run(mutations[0].cmd, mutations[0].timeout_s)
    for m in mutations:
        path = ROOT / m.file
        original = path.read_bytes()
        text = original.decode()
        rec = {"id": m.id, "file": m.file, "why": m.why, "mutation": f"{m.old.strip()[:80]!r} -> {m.new.strip()[:80]!r}"}
        if text.count(m.old) != 1:
            rec.update(status="NOT-APPLICABLE", detail=f"old text occurs {text.count(m.old)} times")
            bad += 1
            results.append(rec)
            continue
        try:
            path.write_text(text.replace(m.old, m.new))
            t = time.time()
            rc, tail = run(m.cmd, m.timeout_s)
            rec.update(red_rc=rc, red_seconds=round(time.time() - t, 1), red_tail=tail[-300:])
        finally:
            path.write_bytes(original)
        assert path.read_bytes() == original
        t = time.time()
        rc2, tail2 = run(m.cmd, m.timeout_s)
        rec.update(green_rc=rc2, green_seconds=round(time.time() - t, 1))
        ok = rec["red_rc"] != 0 and rc2 == 0
        rec["status"] = "KILLED" if ok else ("SURVIVED" if rec["red_rc"] == 0 else "RESTORE-NOT-GREEN")
        bad += 0 if ok else 1
        results.append(rec)
        print(f"[{rec['status']}] {m.id}", flush=True)
    out = {"phase": phase, "baseline_rc": base_rc, "total": len(results), "killed": sum(r["status"] == "KILLED" for r in results),
           "failed": bad, "results": results, "label": "[ĐO]"}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"{out['killed']}/{out['total']} mutants killed -> {report}")
    return 1 if bad or base_rc != 0 else 0


if __name__ == "__main__":
    from importlib import import_module
    mod = import_module(f"tests.mutation.{sys.argv[1]}")
    sys.exit(run_all(mod.MUTATIONS, ROOT / f"benchmarks/reports/{mod.REPORT}", sys.argv[1]))
