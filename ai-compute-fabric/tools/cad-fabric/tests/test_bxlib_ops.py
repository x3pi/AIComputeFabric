#!/usr/bin/env python3
"""Real-Blender tests of the tube and panel recipe ops (skipped without Blender)."""
import json, math, os, unittest
from cadfabric import viewers
from cadfabric import scene3d as S
from cadfabric.blender import bxlib
from tests.test_blender_joints import run, BL

TUBE = '{"op":"tube","points":[[0,0,0],[0,0,300],[200,0,500]],"r":12}'
PANEL = '{"op":"panel","sections":[{"x":0,"y":0,"z":800,"w":100,"h":60},{"x":300,"y":0,"z":860,"w":240,"h":140}],"arc":[0,180],"thick":3}'


class Validate(unittest.TestCase):
    def test_new_ops_validate(self):
        ok = {"ops": [{"op": "tube", "points": [[0, 0, 0], [0, 0, 5]], "r": 2}]}
        self.assertEqual(bxlib.validate(ok), [])
        self.assertIn("points", " ".join(bxlib.validate({"ops": [{"op": "tube", "points": [[0, 0, 0]], "r": 2}]})))
        self.assertIn("arc", " ".join(bxlib.validate({"ops": [{"op": "panel", "sections": [{"x": 0, "y": 0, "z": 0, "w": 1, "h": 1}, {"x": 1, "y": 0, "z": 0, "w": 1, "h": 1}], "arc": [90, 0]}]})))

    def test_the_director_prompt_documents_them(self):
        self.assertIn('"op":"tube"', S.SYSTEM); self.assertIn('"op":"panel"', S.SYSTEM)


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class Build(unittest.TestCase):
    def test_tube_follows_all_its_points_and_a_panel_stays_a_thin_sheet(self):
        t = run({"t": TUBE}, [])
        self.assertEqual(t["part_errors"], {})
        b = t["parts_bbox_mm"]["t"]; self.assertLess(b[0][2], 1); self.assertGreater(b[1][2], 495)       # spans the whole polyline, not just its last piece
        p = run({"p": PANEL}, [])
        self.assertEqual(p["part_errors"], {})
        b = p["parts_bbox_mm"]["p"]; self.assertAlmostEqual(b[0][1], -120, delta=1); self.assertAlmostEqual(b[1][2], 930, delta=4)     # spans the sections
        self.assertEqual(p["totals"]["vertices"], 52)                                                                        # 26 surface vertices, doubled by the thickness


class Shape(unittest.TestCase):
    SEC = [{"x": 0, "y": 0, "z": 0, "w": 100, "h": 40}, {"x": 100, "y": 0, "z": 10, "w": 200, "h": 80, "round_top": 6, "round_bottom": 2}, {"x": 200, "y": 0, "z": 0, "w": 100, "h": 40}]

    def test_smooth_passes_through_the_given_sections_and_adds_the_rest(self):
        out = bxlib._smooth_sections(self.SEC, 3)
        self.assertEqual(len(out), (len(self.SEC) - 1) * 4 + 1)
        for k, s in enumerate(self.SEC):
            self.assertAlmostEqual(out[k * 4]["x"], s["x"], places=6); self.assertAlmostEqual(out[k * 4]["w"], s["w"], places=6)
        self.assertEqual(bxlib._smooth_sections(self.SEC, 0), self.SEC)

    def test_top_and_bottom_halves_can_differ(self):
        s = self.SEC[1]
        yt, zt = bxlib._ring_pt(s, math.pi / 4, 2.5); yb, zb = bxlib._ring_pt(s, -math.pi / 4, 2.5)
        self.assertGreater(abs(yt), abs(yb))                                                       # flatter top reaches further out at 45 degrees
        self.assertAlmostEqual(bxlib._ring_pt(self.SEC[0], math.pi / 4, 2.5)[0], bxlib._ring_pt(self.SEC[0], -math.pi / 4, 2.5)[0])      # no per-half value: symmetric as before

    def test_validation_of_the_new_keys(self):
        ok = {"ops": [{"op": "loft", "sections": self.SEC, "smooth": 2}]}
        self.assertEqual(bxlib.validate(ok), [])
        self.assertIn("smooth", " ".join(bxlib.validate({"ops": [{"op": "loft", "sections": self.SEC, "smooth": 20}]})))
        bad = [dict(x) for x in self.SEC]; bad[1]["round_top"] = 0.2
        self.assertIn("round_top", " ".join(bxlib.validate({"ops": [{"op": "loft", "sections": bad}]})))
        self.assertIn("round_top", " ".join(bxlib.validate({"ops": [{"op": "panel", "sections": bad}]})))


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class ShapeBuild(unittest.TestCase):
    def test_smooth_loft_builds_and_keeps_its_extent(self):
        r = json.dumps({"op": "loft", "sections": Shape.SEC, "smooth": 3, "n": 20})
        t = run({"b": r}, []); self.assertEqual(t["part_errors"], {})
        b = t["parts_bbox_mm"]["b"]; self.assertAlmostEqual(b[0][0], 0, delta=1); self.assertAlmostEqual(b[1][0], 200, delta=1)


if __name__ == "__main__":
    unittest.main()
