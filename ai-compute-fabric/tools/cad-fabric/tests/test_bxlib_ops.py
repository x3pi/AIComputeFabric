#!/usr/bin/env python3
"""Real-Blender tests of the tube and panel recipe ops (skipped without Blender)."""
import os, unittest
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


if __name__ == "__main__":
    unittest.main()
