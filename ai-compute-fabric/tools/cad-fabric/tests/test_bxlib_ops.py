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


class PurePythonGate(unittest.TestCase):
    def test_recipe_bbox_and_plan_gate(self):
        rot_box = {"ops": [{"op": "box", "center": [873.2, 0, 471.9], "size": [40, 160, 30], "rot": {"axis": [0, 1, 0], "deg": -25}}]}
        lo, hi = bxlib.recipe_bbox(rot_box)
        sz = [round(b - a, 1) for a, b in zip(lo, hi)]
        self.assertAlmostEqual(sz[0], 48.9, delta=0.5)
        self.assertAlmostEqual(sz[1], 160.0, delta=0.1)
        self.assertAlmostEqual(sz[2], 44.1, delta=0.5)

        ring_rec = {"ops": [{"op": "ring", "center": [0, 0, 200], "axis": "y", "r_out": 200, "r_in": 150, "width": 80}]}
        r_lo, r_hi = bxlib.recipe_bbox(ring_rec)
        self.assertEqual([round(b - a, 1) for a, b in zip(r_lo, r_hi)], [400.0, 80.0, 400.0])
        self.assertIsNone(bxlib.check_plan_part(ring_rec, {"center_mm": [0, 0, 200], "size_mm": [400, 80, 400], "shape": {"kind": "ring"}}))
        solid_box = {"ops": [{"op": "box", "center": [0, 0, 200], "size": [400, 80, 400]}]}
        self.assertIn("hollow ring", bxlib.check_plan_part(solid_box, {"center_mm": [0, 0, 200], "size_mm": [400, 80, 400], "shape": {"kind": "ring"}}))

        fork_good = {"ops": [{"op": "cylinder", "a": [1000, 75, 200], "b": [860.6, 75, 499], "r": 22}]}
        fork_bad = {"ops": [{"op": "cylinder", "a": [1000, 75, 200], "b": [1139.4, 75, 499], "r": 22}]}
        spec_part = {"center_mm": [930.3, 75, 349.5], "size_mm": [183.4, 44, 317.6], "shape": {"endpoints": [[1000, 75, 200], [860.6, 75, 499]]}}
        self.assertIsNone(bxlib.check_plan_part(fork_good, spec_part))
        self.assertIn("endpoint", bxlib.check_plan_part(fork_bad, {"shape": {"endpoints": [[1000, 75, 200], [860.6, 75, 499]]}}))

        for op in (json.loads(TUBE), json.loads(PANEL), {"op": "loft", "sections": Shape.SEC, "smooth": 2}):
            bb = bxlib.recipe_bbox({"ops": [op]})
            self.assertIsNotNone(bb)
            self.assertTrue(all(bb[1][i] >= bb[0][i] for i in range(3)))

    def test_monotonic_decreasing_sections_accepted_and_zigzag_rejected(self):
        rev_sec = list(reversed(Shape.SEC))
        rev_loft = {"ops": [{"op": "loft", "sections": rev_sec, "smooth": 2}]}
        rev_panel = {"ops": [{"op": "panel", "sections": rev_sec, "arc": [0, 180], "thick": 3}]}
        self.assertEqual(bxlib.validate(rev_loft), [])
        self.assertEqual(bxlib.validate(rev_panel), [])
        self.assertEqual(bxlib.recipe_bbox(rev_loft), bxlib.recipe_bbox({"ops": [{"op": "loft", "sections": Shape.SEC, "smooth": 2}]}))
        self.assertEqual(bxlib.recipe_bbox(rev_panel), bxlib.recipe_bbox({"ops": [{"op": "panel", "sections": Shape.SEC, "arc": [0, 180], "thick": 3}]}))
        zigzag = {"ops": [{"op": "loft", "sections": [{"x": 0, "y": 0, "z": 0, "w": 10, "h": 10}, {"x": 200, "y": 0, "z": 0, "w": 10, "h": 10}, {"x": 100, "y": 0, "z": 0, "w": 10, "h": 10}]}]}
        dup = {"ops": [{"op": "panel", "sections": [{"x": 0, "y": 0, "z": 0, "w": 10, "h": 10}, {"x": 100, "y": 0, "z": 0, "w": 10, "h": 10}, {"x": 100, "y": 0, "z": 0, "w": 10, "h": 10}]}]}
        self.assertIn("monotonic", " ".join(bxlib.validate(zigzag)))
        self.assertIn("monotonic", " ".join(bxlib.validate(dup)))
        if BL and os.path.isfile(BL):
            t = run({"rl": json.dumps(rev_loft["ops"][0]), "rp": json.dumps(rev_panel["ops"][0])}, [])
            self.assertEqual(t["part_errors"], {})
            b = t["parts_bbox_mm"]["rl"]
            self.assertAlmostEqual(b[0][0], 0, delta=1); self.assertAlmostEqual(b[1][0], 200, delta=1)


if __name__ == "__main__":
    unittest.main()
