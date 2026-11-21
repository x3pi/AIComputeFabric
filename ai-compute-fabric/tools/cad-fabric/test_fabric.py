#!/usr/bin/env python3
"""Offline tests (no gateway). Run with the venv: .venv/bin/python test_fabric.py"""
import json, math, os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from cq_backend import CadQueryBackend
import agent, tasks
import verifier as V

CYL = "result = cq.Workplane('XY').circle(20).extrude(50)"


class Backend(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.b = CadQueryBackend(timeout=20)
    @classmethod
    def tearDownClass(cls): cls.b.close()
    def setUp(self): self.b.reset()

    def test_measure_after_cell(self):
        r = self.b.execute(CYL)
        self.assertTrue(r.ok and r.changed); self.assertAlmostEqual(r.measure.volume, math.pi * 400 * 50, places=3)
        self.assertEqual((r.measure.faces, r.measure.solids, r.measure.valid), (3, 1, True)); self.assertAlmostEqual(r.measure.com[2], 25, places=6)

    def test_state_persists_and_error_does_not_corrupt(self):
        self.b.execute("w = cq.Workplane('XY').circle(20).extrude(50)\nresult = w")
        r = self.b.execute("result = w.faces('>Z').workplane().hole(10)\nraise ValueError('boom')")
        self.assertFalse(r.ok); self.assertIn("ValueError", r.error); self.assertIn("line 2", r.error)
        self.assertAlmostEqual(self.b.measure().volume, math.pi * 400 * 50, places=3, msg="a failed cell must not change the part")
        self.assertTrue(self.b.execute("result = w.faces('>Z').workplane().hole(10)").ok)
        self.assertAlmostEqual(self.b.measure().volume, math.pi * (400 * 50 - 25 * 50), places=3)

    def test_undo_replays_history(self):
        self.b.execute(CYL); self.b.execute("result = result.faces('>Z').workplane().hole(10)")
        self.assertTrue(self.b.undo()); self.assertAlmostEqual(self.b.measure().volume, math.pi * 400 * 50, places=3)
        self.assertTrue(self.b.undo()); self.assertIsNone(self.b.measure()); self.assertFalse(self.b.undo())

    def test_blocked_code(self):
        for bad in ("import os", "import subprocess", "open('/etc/passwd')", "__import__('os')", "().__class__.__bases__", "eval('1')", "import sys", "x = getattr(cq, 'Workplane')",
                    "from os import path", "exec('1')"):
            r = self.b.execute(bad); self.assertFalse(r.ok, bad)
        self.assertTrue(self.b.execute("import math\nimport cadquery as c2\nx = math.pi").ok)

    def test_timeout_restores_session(self):
        b = CadQueryBackend(timeout=4)
        try:
            b.execute(CYL); r = b.execute("while True:\n    pass")
            self.assertFalse(r.ok); self.assertIn("timed out", r.error)
            self.assertAlmostEqual(b.measure().volume, math.pi * 400 * 50, places=3)
        finally: b.close()

    def test_crash_restores_session(self):
        b = CadQueryBackend(timeout=20)
        try:
            b.execute(CYL); b.proc.kill(); b.proc.wait()
            r = b.execute("result = result.faces('>Z').workplane().hole(10)")
            self.assertTrue(r.ok or "restored" in r.error)
            if not r.ok: self.assertTrue(b.execute("result = result.faces('>Z').workplane().hole(10)").ok)
            self.assertAlmostEqual(b.measure().volume, math.pi * (400 * 50 - 25 * 50), places=3)
        finally: b.close()

    def test_step_export(self):
        import tempfile
        self.b.execute(CYL); p = os.path.join(tempfile.mkdtemp(), "a.step"); self.b.export_step(p)
        self.assertGreater(os.path.getsize(p), 500)

    def test_reference_solutions_match_closed_forms(self):
        for t, d in tasks.TASKS.items():
            e = tasks.exact(self.b, t)
            if d["analytic"] is not None: self.assertAlmostEqual(e / d["analytic"], 1, places=9, msg=t)
        self.assertAlmostEqual(tasks.exact(self.b, "cup_side_hole") / 19921.7514, 1, delta=5e-6)   # the SolidWorks kernel's number


class FakeLLM:
    """Scripted executor: each entry is a list of (tool, args) or a final string."""
    def __init__(self, script): self.script, self.i, self.seen = script, 0, []
    def chat(self, messages, tools=None, think=False, **kw):
        self.seen.append(messages[-1]["content"] if messages else "")
        s = self.script[min(self.i, len(self.script) - 1)]; self.i += 1
        if isinstance(s, str): return {"role": "assistant", "content": s, "_usage": {"completion_tokens": 1}}
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % self.i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for n, a in s],
                "_usage": {"completion_tokens": 1}}


PLAN = {"steps": [{"do": "cylinder", "volume_after": "pi*20**2*50"}, {"do": "hole", "volume_after": "pi*20**2*50 - pi*5**2*50"}], "final_bbox_size": ["40", "40", "50"]}
PLANNER = lambda r: (V.parse_spec(PLAN), "raw")
HOLE = "result = result.faces('>Z').workplane().hole(10)"


class Agent(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.b = CadQueryBackend(timeout=20)
    @classmethod
    def tearDownClass(cls): cls.b.close()

    def run_script(self, script, **kw):
        llm = FakeLLM(script); r = agent.run(llm, self.b, "req", planner=PLANNER, log=lambda s: None, **kw); return r, llm

    def test_good_run_has_no_warning(self):
        r, _ = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": HOLE})], "done"])
        self.assertEqual((r["warnings"], r["gates"], r["steps"]), (0, 0, 2)); self.assertAlmostEqual(r["measure"]["volume"], math.pi * (400 - 25) * 50, places=3)

    def test_wrong_volume_is_flagged_in_the_tool_result(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": "result = result.faces('>Z').workplane().hole(14)"})], "done"])
        self.assertGreaterEqual(r["warnings"], 1)
        self.assertTrue(any("HARNESS CHECK" in s and "Less material than planned" in s for s in llm.seen))

    def test_gate_sends_the_model_back_once_then_accepts(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], "done too early", [("execute_cadquery", {"code": HOLE})], "done"])
        self.assertEqual(r["gates"], 1); self.assertAlmostEqual(r["measure"]["volume"], math.pi * (400 - 25) * 50, places=3)
        self.assertTrue(any("FINAL CHECK" in s for s in llm.seen))

    def test_gate_is_bounded(self):
        r, _ = self.run_script([[("execute_cadquery", {"code": CYL})], "x"] * 6)
        self.assertEqual(r["gates"], agent.GATE_MAX); self.assertEqual(r["final"], "x")

    def test_plan_off_means_no_checks(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], "done"], plan_mode="off")
        self.assertEqual((r["checks"], r["gates"], r["plan"]), (0, 0, None))

    def test_no_change_warning_for_feature_cell(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": "c = cq.Workplane('XY').box(1,1,1).cut(cq.Workplane('XY').box(1,1,1))"})],
                                  "done"], plan_mode="think")
        self.assertTrue(any("did not change the volume" in s for s in llm.seen))

    def test_undo_tool(self):
        r, _ = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": HOLE})], [("undo", {})], [("execute_cadquery", {"code": HOLE})], "done"])
        self.assertAlmostEqual(r["measure"]["volume"], math.pi * (400 - 25) * 50, places=3); self.assertEqual(r["gates"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
