#!/usr/bin/env python3
"""Offline tests for verifier.py and its wiring in sw_agent.py (no SolidWorks, no network). Run: python3 test_verifier.py"""
import json, math, os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import verifier as V

CUP = {"steps": [{"do": "new_part"}, {"do": "add_disc d40 h50", "volume_after": "pi*20**2*50"},
                 {"do": "add_shell 3 mm open +Z", "volume_after": "pi*20**2*50 - pi*17**2*47"},
                 {"do": "add_plane x=21"}, {"do": "add_sketch circle"},
                 {"do": "cut_sketch through the near wall", "volume_after": "pi*20**2*50 - pi*17**2*47 - pi*5**2*3", "approx": True}],
       "final_bbox_size": ["40", "40", "50"], "pitfalls": ["axis"]}
V1 = math.pi * 400 * 50; V2 = V1 - math.pi * 289 * 47; V3 = V2 - 237.8486  # exact hole through the curved wall


class Eval(unittest.TestCase):
    def test_ok(self):
        self.assertAlmostEqual(V.safe_eval("pi*20**2*50"), math.pi * 20000)
        self.assertAlmostEqual(V.safe_eval("sqrt(2)*-3 + radians(180)"), math.sqrt(2) * -3 + math.pi)
    def test_rejects(self):
        for bad in ("__import__('os').system('x')", "().__class__", "open('f')", "a+1", "1; 2", "[1]", "lambda: 1", "2**99999", "1/0", "x" * 400, "", "pi.real",
                    "sqrt(x=4)", "eval('1')", "'a'*3", "1e999*1e999"):
            with self.assertRaises(ValueError, msg=bad): V.safe_eval(bad)


class Spec(unittest.TestCase):
    def test_parse(self):
        sp = V.parse_spec(CUP)
        self.assertEqual([m["step"] for m in sp["milestones"]], [2, 3, 6]); self.assertAlmostEqual(sp["milestones"][0]["vol"], V1)
        self.assertTrue(sp["milestones"][2]["approx"]); self.assertEqual(sp["bbox"], [40.0, 40.0, 50.0]); self.assertEqual(sp["errors"], [])
        self.assertIn("expected volume after this step", V.describe(sp))
    def test_bad_formula_is_reported_not_trusted(self):
        sp = V.parse_spec({"steps": [{"do": "a", "volume_after": "__import__('os')"}, {"do": "b", "volume_after": "100"}]})
        self.assertEqual(len(sp["milestones"]), 1); self.assertEqual(len(sp["errors"]), 1); self.assertIn("not checked", V.describe(sp))
    def test_garbage(self):
        for p in (None, [], {"steps": "x"}, {"steps": [1, None, {"do": 5}]}): V.parse_spec(p)


class Track(unittest.TestCase):
    def setUp(self): self.sp = V.parse_spec(CUP); self.t = V.Tracker(self.sp)
    def test_happy_path(self):
        for tool, v in (("add_disc", V1), ("add_shell", V2), ("cut_sketch", V3)):
            r = self.t.observe(tool, v); self.assertEqual(r[0], "ok", (tool, r))
        self.assertEqual(self.t.final_verdict(V3, [40, 40, 50], self.sp), [])
    def test_missed_cut_is_caught_at_the_step(self):
        self.t.observe("add_disc", V1); self.t.observe("add_shell", V2)
        r = self.t.observe("cut_sketch", V2)                                    # the cut did nothing
        self.assertEqual(r[0], "warn"); self.assertIn("did not change the volume", r[1]); self.assertIn("removed", r[1])
    def test_cut_too_big_is_caught_with_numbers(self):
        self.t.observe("add_disc", V1); self.t.observe("add_shell", V2)
        r = self.t.observe("cut_sketch", V3 - 80.0)
        self.assertEqual(r[0], "warn"); self.assertIn("too much", r[1]); self.assertIn("Difference -82.2 mm3 (-0.41%)", r[1])
    def test_exact_milestone_rejects_small_real_error(self):
        r = self.t.observe("add_disc", V1 * 1.004); self.assertEqual(r[0], "warn")      # 0.4% on an exact step
    def test_approx_milestone_still_rejects_the_real_cup_errors(self):
        self.t.observe("add_disc", V1); self.t.observe("add_shell", V2)
        for extra in (71.5, 80.0, 237.8): self.assertEqual(V.Tracker(self.sp).observe("cut_sketch", V3 - extra)[0], "warn", extra)   # measured misses
    def test_approx_milestone_tolerates_curved_wall_simplification(self):
        self.t.observe("add_disc", V1); self.t.observe("add_shell", V2)
        self.assertEqual(self.t.observe("cut_sketch", V3)[0], "ok")                       # 1e-4 off the straight-cylinder formula
    def test_no_change_of_non_feature_tool_is_silent(self):
        self.t.observe("add_disc", V1); self.assertIsNone(self.t.observe("get_status", V1))
    def test_skipped_milestone_still_matches(self):
        self.assertEqual(self.t.observe("add_shell", V2)[0], "ok")                        # agent combined two planned steps
    def test_after_plan_complete_change_warns(self):
        for tool, v in (("add_disc", V1), ("add_shell", V2), ("cut_sketch", V3)): self.t.observe(tool, v)
        self.assertEqual(self.t.observe("add_fillet", V3 - 500)[0], "warn")
    def test_reset(self):
        self.t.observe("add_disc", V1); self.t.reset(); self.assertEqual(self.t.observe("add_disc", V1)[0], "ok")
    def test_final_verdict_reports_progress_and_bbox(self):
        self.t.observe("add_disc", V1); bad = self.t.final_verdict(V1, [40, 40, 60], self.sp)
        self.assertEqual(len(bad), 2); self.assertIn("1 of 3", bad[0]); self.assertIn("bounding box", bad[1])
    def test_no_milestones_means_no_checks(self):
        t = V.Tracker(V.parse_spec({"steps": [{"do": "x"}]})); self.assertIsNone(t.observe("add_box", 5.0))


class Sketch(unittest.TestCase):
    D = {"origin_mm": [0, 0, 25], "x_axis": [1, 0, 0], "y_axis": [0, 1, 0], "points_mm": [[-5, -5], [5, 5]]}
    def test_outside(self): self.assertIn("OUTSIDE", V.sketch_outside(self.D, ([-20, -20, -20], [20, 20, 20])))
    def test_inside(self): self.assertIsNone(V.sketch_outside(self.D, ([-20, -20, 0], [20, 20, 50])))
    def test_malformed(self): self.assertIsNone(V.sketch_outside({"ok": True}, ([0, 0, 0], [1, 1, 1])))


class Wiring(unittest.TestCase):
    """sw_agent.step_check / gate with a fake CAD kernel."""
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp(); os.environ["AICF_DATA_DIR"] = d; os.environ["ADVISOR_AUDIT"] = d + "/a.jsonl"
        import sw_agent as A; cls.A = A; A.EMIT = lambda *a, **k: None
    @classmethod
    def tearDownClass(cls): cls.A.srv.terminate()
    def setUp(self):
        A = self.A; A.ADVISOR_MODE = "qwen_think"; self.vol = [0.0]
        A.mcp_call = lambda n, a: (json.dumps({"mass_properties": {"volume_mm3": self.vol[0], "bounding_box_mm": {"size_mm": [40, 40, 50]}}}), [])
        A._ask_mode = lambda q, mode: (json.dumps(CUP) if "planning a CAD job" in q else "advice text", "")
        A.EMIT = lambda *a, **k: None
        self.msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "Make a cup with a hole in one wall, please do it carefully"}]
        A.start_request(self.msgs)
    def test_plan_is_evaluated_by_code_and_shown(self):
        self.assertIn("%.1f" % V1, self.msgs[-1]["content"]); self.assertEqual(len(self.A.SPEC["milestones"]), 3)
    def test_step_check_flags_missed_cut(self):
        A = self.A; self.vol[0] = V1; A.annotate("add_disc", {}, '{"ok": true}'); self.vol[0] = V2; A.annotate("add_shell", {}, '{"ok": true}')
        out = A.annotate("cut_sketch", {}, '{"ok": true}'); self.assertIn("did not change the volume", out)
    def test_failed_or_readonly_tools_are_not_measured(self):
        A = self.A; n = []; A.mcp_call = lambda nm, a: n.append(nm) or ("{}", [])
        self.assertEqual(A.annotate("add_disc", {}, '{"ok": false, "error": "x"}'), '{"ok": false, "error": "x"}')
        self.assertEqual(A.annotate("get_status", {}, '{"ok": true}'), '{"ok": true}'); A.annotate("add_plane", {}, '{"ok": true}'); self.assertEqual(n, [])
    def test_gate_rejects_wrong_final_and_passes_right_one(self):
        A = self.A; self.vol[0] = V3 - 80.0; g = A.gate("done"); self.assertIn("VERIFICATION FAILED", g or "")
        A.gates_used = 0; self.vol[0] = V3; self.assertIsNone(A.gate("done"))
    def test_gate_budget_and_off(self):
        A = self.A; self.vol[0] = V1; self.assertTrue(A.gate("a")); self.assertTrue(A.gate("b")); self.assertIsNone(A.gate("c"))
        A.gates_used = 0; A.ADVISOR_MODE = "off"; self.assertIsNone(A.gate("x"))
    def test_off_mode_has_no_plan_and_no_checks(self):
        A = self.A; A.ADVISOR_MODE = "off"; A.start_request(self.msgs + [{"role": "user", "content": "A completely new long request about something else entirely"}])
        self.assertIsNone(A.TRACK); self.assertEqual(A.annotate("add_disc", {}, '{"ok": true}'), '{"ok": true}')


if __name__ == "__main__":
    unittest.main(verbosity=1)
