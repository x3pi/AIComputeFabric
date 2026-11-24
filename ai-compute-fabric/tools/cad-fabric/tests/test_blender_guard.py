#!/usr/bin/env python3
"""Real-Blender tests of the generic geometry guard (skipped when Blender is not installed)."""
import json, os, shutil, subprocess, tempfile, unittest
from cadfabric import viewers
BL = os.environ.get("BLENDER") or viewers.blender_path()
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cadfabric", "blender")


def smoke(code, pid="a"):
    d = tempfile.mkdtemp(prefix="guard-"); os.makedirs(d + "/parts")
    with open("%s/parts/%s.py" % (d, pid), "w") as f: f.write(code)
    with open(d + "/plan.json", "w") as f: json.dump({"parts": [{"id": pid, "spec": "s"}], "joints": []}, f)
    subprocess.run([BL, "-b", "--python", HERE + "/blender_build.py", "--", d, pid], capture_output=True, timeout=300)
    with open("%s/smoke_%s.json" % (d, pid)) as f: r = json.load(f)["error"]
    shutil.rmtree(d, ignore_errors=True)
    return r


@unittest.skipUnless(BL and os.path.isfile(BL), "Blender not installed")
class Guard(unittest.TestCase):
    OK = "def build(bpy, bmesh):\n    bm = bmesh.new()\n    bmesh.ops.create_cube(bm, size=0.1)\n    me = bpy.data.meshes.new('a'); bm.to_mesh(me); bm.free()\n    bpy.data.objects.new('a', me)\n"

    def test_a_normal_part_passes(self):
        self.assertIsNone(smoke(self.OK))

    def test_an_exploding_operator_is_stopped_whatever_hides_it(self):
        code = ("def build(bpy, bmesh):\n    bm = bmesh.new()\n    def grow():\n        bmesh.ops.bevel(bm, geom=list(bm.edges), offset=0.002, segments=2, affect='EDGES')\n"
                "    for k in range(9):\n        bmesh.ops.create_cube(bm, size=0.05)\n        grow()\n    bpy.data.objects.new('a', bpy.data.meshes.new('a'))\n")
        e = smoke(code); self.assertIsNotNone(e); self.assertIn("exploding", e)

    def test_another_operator_in_a_loop_is_stopped_too(self):
        code = ("def build(bpy, bmesh):\n    bm = bmesh.new()\n    bmesh.ops.create_cube(bm, size=0.1)\n    for k in range(12):\n"
                "        bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=3, use_grid_fill=True)\n    bpy.data.objects.new('a', bpy.data.meshes.new('a'))\n")
        self.assertIn("vertices", smoke(code) or "")

    def test_a_wrong_keyword_comes_back_with_the_real_signature(self):
        code = ("def build(bpy, bmesh):\n    bm = bmesh.new()\n    bmesh.ops.create_cube(bm, size=0.1)\n    bmesh.ops.solidify(bm, geom=bm.faces, thickness=0.01, use_even_offset=True)\n"
                "    bpy.data.objects.new('a', bpy.data.meshes.new('a'))\n")
        e = smoke(code); self.assertIn("REAL SIGNATURE", e); self.assertIn("thickness", e)


if __name__ == "__main__":
    unittest.main()
