#!/usr/bin/env python3
"""Real-Blender regression for the motion verifier: nested joints and a tilted steering-style axis (skipped when Blender is not installed)."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from cadfabric import viewers
BL = os.environ.get("BLENDER") or viewers.blender_path()
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cadfabric", "blender")
BOX = "def build(bpy, bmesh):\n    import bxlib\n    return bxlib.build(bpy, bmesh, %r, {'ops': [%s]})\n"
D = [-0.4067, 0.0, 0.9135]


def run(parts, joints):
    proj = tempfile.mkdtemp(prefix="joints-"); os.makedirs(proj + "/parts")
    for pid, op in parts.items():
        with open("%s/parts/%s.py" % (proj, pid), "w") as f: f.write(BOX % (pid, op))
    with open(proj + "/plan.json", "w") as f: json.dump({"parts": [{"id": p, "spec": "s"} for p in parts], "joints": joints, "allowed_contacts": []}, f)
    subprocess.run([BL, "-b", "--python", HERE + "/blender_build.py", "--", proj], capture_output=True, timeout=300)
    with open(proj + "/report.json") as f: rep = json.load(f)
    shutil.rmtree(proj, ignore_errors=True)
    return rep


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class Joints(unittest.TestCase):
    P = {"arm": '{"op":"cylinder","a":[1000,0,200],"b":[1000,0,500],"r":20}', "wheel": '{"op":"ring","center":[1000,0,200],"axis":"y","r_out":150,"r_in":40,"width":60}',
         "base": '{"op":"box","center":[300,0,300],"size":[200,50,50]}'}

    def joints(self, off=0.0):
        return [{"name": "steer", "parent": None, "children": ["arm"], "origin_mm": [1000 + off, 0, 200], "axis": D, "span_rad": 0.5},
                {"name": "spin", "parent": "arm", "children": ["wheel"], "origin_mm": [1000, 0, 200], "axis": [0, 1, 0], "coaxial": ["wheel"], "span_rad": 12.566}]

    def kinds(self, rep): return sorted({i["kind"] for i in rep["issues"]} - {"floating_part"})      # these fixtures test motion, not support

    def test_nested_joint_on_a_tilted_axis_passes(self):
        self.assertEqual(self.kinds(run(self.P, self.joints())), [])

    def test_a_coaxial_part_off_its_axis_is_still_caught(self):
        parts = dict(self.P, wheel='{"op":"ring","center":[1012,0,200],"axis":"y","r_out":150,"r_in":40,"width":60}')
        self.assertIn("not_coaxial", self.kinds(run(parts, self.joints())))

    def test_motion_is_exactly_the_declared_one_so_pivot_errors_cannot_arise_from_the_wiring(self):
        rep = run(self.P, self.joints(off=30.0)); self.assertNotIn("not_rigid", self.kinds(rep))


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class Ground(unittest.TestCase):
    WHEEL = '{"op":"ring","center":[500,0,200],"axis":"y","r_out":200,"r_in":100,"width":60}'
    BASE = '{"op":"box","center":[0,0,500],"size":[100,100,50]}'

    def kinds(self, origin, axis):
        j = [{"name": "swing", "parent": None, "children": ["wheel"], "origin_mm": origin, "axis": axis, "span_rad": 0.4}]
        return sorted({i["kind"] for i in run({"base": self.BASE, "wheel": self.WHEEL}, j)["issues"]} - {"floating_part"})

    def test_a_grounded_part_swung_below_the_floor_is_reported(self):
        self.assertIn("ground_penetration", self.kinds([0, 0, 500], [0, 1, 0]))           # swings the wheel about a pivot far above it: it dips below z=0

    def test_a_grounded_part_that_only_spins_in_place_is_fine(self):
        self.assertNotIn("ground_penetration", self.kinds([500, 0, 200], [0, 1, 0]))


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class Floating(unittest.TestCase):
    A = '{"op":"box","center":[0,0,100],"size":[100,100,200]}'
    NEAR = '{"op":"box","center":[0,0,260],"size":[60,60,60]}'     # 0 mm gap above A (A top z=200, this bottom z=230 -> 30 mm gap)
    TOUCH = '{"op":"box","center":[0,0,225],"size":[60,60,50]}'
    FAR = '{"op":"box","center":[0,0,400],"size":[60,60,60]}'

    def floating(self, extra, free=()):
        parts = {"a": self.A, "b": extra}
        d = run(parts, [])
        return sorted(i["part"] for i in d["issues"] if i["kind"] == "floating_part")

    def test_a_part_far_from_everything_floats(self):
        self.assertEqual(self.floating(self.FAR), ["b"])

    def test_a_touching_part_does_not(self):
        self.assertEqual(self.floating(self.TOUCH), [])

    def test_a_wide_flat_part_close_under_a_narrow_one_is_supported(self):
        """a 300x120 box whose top face lies 5 mm below a 60 mm wide bar: its corners are 30 mm from the bar, but the surfaces are only 5 mm apart"""
        parts = {"post": '{"op":"box","center":[190,0,155],"size":[20,20,310]}', "bar": '{"op":"box","center":[470,0,330],"size":[560,60,50]}',
                 "pack": '{"op":"box","center":[480,0,250],"size":[300,120,100]}'}
        d = run(parts, [])
        self.assertEqual([i["part"] for i in d["issues"] if i["kind"] == "floating_part"], [])


if __name__ == "__main__":
    unittest.main()
