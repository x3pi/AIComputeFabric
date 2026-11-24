#!/usr/bin/env python3
"""Real-Blender tests of the vertex optimisation: fewer vertices, the geometry gate still satisfied (skipped without Blender)."""
import json, os, shutil, subprocess, tempfile, unittest
from cadfabric import viewers
BL = os.environ.get("BLENDER") or viewers.blender_path()
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cadfabric", "blender")
HEAVY = ("def build(bpy, bmesh):\n    bm = bmesh.new()\n    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=160, radius1=0.3, radius2=0.3, depth=0.2)\n"
         "    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=2, use_grid_fill=True)\n    me = bpy.data.meshes.new('t'); bm.to_mesh(me); bm.free(); bpy.data.objects.new('t', me)\n")
RECIPE = "def build(bpy, bmesh):\n    import bxlib\n    return bxlib.build(bpy, bmesh, 'r', {'ops': [%s]})\n"


def run(code, pid, plan_part=None, env=None):
    d = tempfile.mkdtemp(prefix="lod-"); os.makedirs(d + "/parts")
    with open("%s/parts/%s.py" % (d, pid), "w") as f: f.write(code)
    with open(d + "/plan.json", "w") as f: json.dump({"parts": [dict({"id": pid, "spec": "s"}, **(plan_part or {}))], "joints": []}, f)
    subprocess.run([BL, "-b", "--python", HERE + "/blender_build.py", "--", d], capture_output=True, timeout=300, env=dict(os.environ, **(env or {})))
    with open(d + "/report.json") as f: r = json.load(f)
    shutil.rmtree(d, ignore_errors=True)
    return r


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class Lod(unittest.TestCase):
    def test_heavy_raw_mesh_is_reduced_within_tolerance_and_still_passes_the_gate(self):
        r = run(HEAVY, "t", {"center_mm": [0, 0, 0], "size_mm": [600, 600, 200]})
        a, b = r["opt"]["t"]
        self.assertGreater(a, 1000); self.assertLess(b, a * 0.3)
        self.assertEqual([i for i in r["issues"] if i["kind"] in ("size_mismatch", "position_mismatch")], [])

    def test_optimisation_can_be_switched_off(self):
        r = run(HEAVY, "t", env={"BX_OPTIMIZE": "0"}); self.assertEqual(r["opt"], {})

    def test_adaptive_tessellation_uses_fewer_segments_for_small_radius(self):
        small = run(RECIPE % '{"op":"cylinder","a":[0,0,0],"b":[0,0,50],"r":8}', "r")["totals"]["vertices"]
        big = run(RECIPE % '{"op":"cylinder","a":[0,0,0],"b":[0,0,50],"r":300}', "r")["totals"]["vertices"]
        self.assertLess(small, big); self.assertLessEqual(small, 2 * 16 + 2)


if __name__ == "__main__":
    unittest.main()
