"""Runs inside Blender: blender -b <scene.blend> --python blender_video.py -- <out_dir> [n_frames]
Renders Workbench Studio frames across the animation range with a gentle camera orbit for H.264 MP4 export."""
import bpy, math, os, sys
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
out_dir = argv[0]
n_frames = max(2, int(argv[1])) if len(argv) > 1 else 60
os.makedirs(out_dir, exist_ok=True)

sc = bpy.context.scene
sc.render.engine = "BLENDER_WORKBENCH"
sc.render.resolution_x, sc.render.resolution_y = 800, 600
sc.render.resolution_percentage = 100
sc.render.image_settings.file_format = "PNG"

sh = sc.display.shading
sh.light = "STUDIO"
sh.color_type = "MATERIAL" if any(o.type == "MESH" and o.data.materials for o in bpy.data.objects) else "RANDOM"
sh.show_cavity = True

sc.world = sc.world or bpy.data.worlds.new("w")
sc.world.color = (0.93, 0.94, 0.96)

meshes = [o for o in bpy.data.objects if o.type == "MESH"]
if not meshes:
    sys.exit(0)

f0, f1 = sc.frame_start, sc.frame_end
lo_all = Vector((1e9, 1e9, 1e9))
hi_all = Vector((-1e9, -1e9, -1e9))
sample_frames = [f0] if f1 <= f0 else [int(round(f0 + (f1 - f0) * k / 8)) for k in range(9)]
for fr in sample_frames:
    sc.frame_set(fr)
    for o in meshes:
        for c in o.bound_box:
            p = o.matrix_world @ Vector(c)
            for ax in range(3):
                lo_all[ax] = min(lo_all[ax], p[ax])
                hi_all[ax] = max(hi_all[ax], p[ax])

center = (lo_all + hi_all) / 2
diag = (hi_all - lo_all).length or 1.0

cam = bpy.data.objects.new("vid_cam", bpy.data.cameras.new("vid_cam"))
sc.collection.objects.link(cam)
sc.camera = cam
cam.data.type = "ORTHO"
cam.data.ortho_scale = diag * 1.12
cam.data.clip_start, cam.data.clip_end = 0.001, diag * 12

for i in range(n_frames):
    u = i / max(1, n_frames - 1)
    if f1 > f0:
        sc.frame_set(int(round(f0 + (f1 - f0) * u)))
    az = math.radians(-38.0 + 56.0 * u)
    el = math.radians(22.0 + 6.0 * math.sin(u * math.pi))
    d = Vector((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))).normalized()
    cam.location = center + d * diag * 2.2
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    sc.render.filepath = os.path.join(out_dir, f"frame_{i:03d}.png")
    bpy.ops.render.render(write_still=True)

