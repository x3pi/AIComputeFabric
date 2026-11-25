"""Export an animated Blender scene (.blend) to binary glTF (.glb) for interactive Three.js preview."""
import os
import sys

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
out_glb = os.path.abspath(args[0])
os.makedirs(os.path.dirname(out_glb) or ".", exist_ok=True)
sc = bpy.context.scene
sc.frame_set(sc.frame_start)
bpy.ops.export_scene.gltf(filepath=out_glb, export_format="GLB", export_animations=True, export_yup=True)

