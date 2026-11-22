# Run INSIDE Blender:  blender --python blender_import.py -- FILE
# Replaces the default scene with the given mesh (millimetres -> metres so the part has its real size) and frames it.
import sys
import bpy

path = sys.argv[sys.argv.index("--") + 1]
ext = path.rsplit(".", 1)[-1].lower()
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
before = set(bpy.data.objects)
if ext == "stl":
    try:
        bpy.ops.wm.stl_import(filepath=path, global_scale=0.001)            # Blender 4.x
    except (AttributeError, TypeError):
        bpy.ops.import_mesh.stl(filepath=path, global_scale=0.001)          # Blender 2.8 - 3.x
elif ext == "obj":
    try:
        bpy.ops.wm.obj_import(filepath=path)
    except (AttributeError, TypeError):
        bpy.ops.import_scene.obj(filepath=path)
for o in set(bpy.data.objects) - before:
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    try:
        bpy.ops.object.shade_smooth()
    except Exception:
        pass
try:
    for area in bpy.context.window.screen.areas:
        if area.type == "VIEW_3D":
            with bpy.context.temp_override(area=area, region=[r for r in area.regions if r.type == "WINDOW"][0]):
                bpy.ops.view3d.view_selected()
except Exception:
    pass
