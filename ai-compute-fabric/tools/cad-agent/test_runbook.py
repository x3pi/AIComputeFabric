#!/usr/bin/env python3
"""Offline tests for runbook.py (no SolidWorks, no network). Run: python3 test_runbook.py"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runbook as RB

ERR = [("cut_sketch", '{"ok": false, "error": "FeatureCut4 failed (None). Is \'S1\' a closed profile that meets the part?"}')]
GOOD = {"tool": "cut_sketch", "pattern": r"FeatureCut4 failed.*closed profile that meets the part",
        "lesson": "This usually means the sketch does not touch the body. Compare the sketch plane origin and normal with the part bounding box and move the plane into the part."}


class T(unittest.TestCase):
    def setUp(self): self.p = os.path.join(tempfile.mkdtemp(), "runbook.json")

    def test_good_candidate_is_stored_pending_not_shown(self):
        added, rej = RB.add_candidates(self.p, [GOOD], ERR); self.assertEqual((len(added), rej), (1, []))
        self.assertEqual(RB.load(self.p)[0]["status"], "pending")
        self.assertEqual(RB.note_for(self.p, "cut_sketch", ERR[0][1]), "")                # pending lessons are never shown to the agent

    def test_approved_is_shown_and_counted(self):
        RB.add_candidates(self.p, [GOOD], ERR); RB.set_status(self.p, 1, "approved")
        n = RB.note_for(self.p, "cut_sketch", ERR[0][1]); self.assertIn("RUNBOOK", n); self.assertIn("bounding box", n)
        self.assertEqual(RB.load(self.p)[0]["hits"], 1)
        self.assertEqual(RB.note_for(self.p, "add_hole", ERR[0][1]), "")                  # other tool: silent
        self.assertEqual(RB.note_for(self.p, "cut_sketch", "some other error"), "")        # other error: silent

    def test_rejected_and_deleted_are_not_shown(self):
        RB.add_candidates(self.p, [GOOD], ERR); RB.set_status(self.p, 1, "approved"); RB.set_status(self.p, 1, "rejected")
        self.assertEqual(RB.note_for(self.p, "cut_sketch", ERR[0][1]), "")
        RB.set_status(self.p, 1, "deleted"); self.assertEqual(RB.load(self.p), [])

    def test_rejections(self):
        bad = [
            dict(GOOD, lesson="Make the hole 10 mm wide and 25 mm high."),                          # dimension
            dict(GOOD, lesson='Delete "HoleSketch" and draw it again.'),                           # a specific name
            dict(GOOD, pattern="("),                                                               # invalid regex
            dict(GOOD, pattern="zzz never seen in any error"),                                     # not keyed to a real error
            dict(GOOD, pattern=r".*ok.*"),                                                         # matches the error text AND ordinary success output
            dict(GOOD, lesson=""), dict(GOOD, lesson="x" * 700), {"tool": "t"}, "not a dict"]
        for c in bad:                                                                              # one at a time: add_candidates keeps at most 5 per call
            added, rej = RB.add_candidates(self.p, [c], ERR); self.assertEqual(added, [], c); self.assertEqual(len(rej), 1)
        self.assertEqual(RB.load(self.p), [])
        self.assertEqual(len(RB.add_candidates(self.p, [GOOD] * 9, ERR)[0]) + len(RB.add_candidates(self.p, [], ERR)[0]), 1)   # 9 copies: only 1 stored

    def test_duplicate_dropped(self):
        RB.add_candidates(self.p, [GOOD], ERR); added, rej = RB.add_candidates(self.p, [GOOD], ERR)
        self.assertEqual(added, []); self.assertEqual(rej[0][1], "duplicate")

    def test_tool_scoping_in_validation(self):
        c = dict(GOOD, tool="add_hole")                                                            # pattern only matches a cut_sketch error
        self.assertEqual(RB.add_candidates(self.p, [c], ERR)[0], [])

    def test_corrupt_file_is_ignored(self):
        open(self.p, "w").write("{not json"); self.assertEqual(RB.load(self.p), []); self.assertEqual(len(RB.add_candidates(self.p, [GOOD], ERR)[0]), 1)

    def test_unknown_status_and_id(self):
        RB.add_candidates(self.p, [GOOD], ERR); self.assertFalse(RB.set_status(self.p, 1, "hacked")); self.assertFalse(RB.set_status(self.p, 99, "approved"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
