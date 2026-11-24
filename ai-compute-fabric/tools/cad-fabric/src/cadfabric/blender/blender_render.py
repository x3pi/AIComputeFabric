"""Runs inside Blender: blender -b <proj>/scene.blend --python blender_render.py -- <proj>
Renders a few small, fast (Workbench) pictures for the director's final review into <proj>/views/: iso and side and top at rest, and iso at the end of the motion."""
import bpy, os, sys
from mathutils import Vector

proj = sys.argv[sys.argv.index("--") + 1]
out = os.path.join(proj, "views")
os.makedirs(out, exist_ok=True)
sc = bpy.context.scene
sc.render.engine = "BLENDER_WORKBENCH"
sc.render.resolution_x, sc.render.resolution_y = 640, 480
sc.render.image_settings.file_format = "PNG"
sh = sc.display.shading
sh.light, sh.color_type, sh.show_cavity = "STUDIO", ("MATERIAL" if any(o.type == "MESH" and o.data.materials for o in bpy.data.objects) else "RANDOM"), True
sc.world = sc.world or bpy.data.worlds.new("w")
sc.world.color = (0.92, 0.93, 0.95)
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
if not meshes:
    sys.exit(0)


def bounds():
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
sc.collection.objects.link(cam)
sc.camera = cam
cam.data.type = "ORTHO"


def shot(name, direction, frame):
    sc.frame_set(frame)
    lo, hi = bounds()
    c, d = (lo + hi) / 2, Vector(direction).normalized()
    diag = (hi - lo).length or 1.0
    cam.location = c + d * diag * 2
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    cam.data.ortho_scale = diag * 1.05
    cam.data.clip_start, cam.data.clip_end = 0.001, diag * 10
    sc.render.filepath = os.path.join(out, name + ".png")
    bpy.ops.render.render(write_still=True)


f0, f1 = sc.frame_start, sc.frame_end
shot("iso", (1, -1, 0.7), f0)
shot("side", (0, -1, 0.05), f0)
shot("top", (0, 0.001, 1), f0)
if f1 > f0:
    shot("iso_end_of_motion", (1, -1, 0.7), f1)
if os.environ.get("BX_VIEWS") == "all":                    # extra angles for the appearance judge
    shot("iso_rear", (-1, 1, 0.6), f0)
    shot("front3q", (1, 0.8, 0.35), f0)
