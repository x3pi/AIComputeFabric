"""Chặng 5B — Kết xuất 6 góc nhìn 512x512 kèm trục màu XYZ (+X Đỏ, +Y Lục, +Z Lam) trên CPU Cycles nhanh."""
from __future__ import annotations

from pathlib import Path

import bpy
from mathutils import Vector


def _target_meshes() -> list[bpy.types.Object]:
    lod0 = bpy.data.collections.get("LOD0")
    if lod0:
        objs = [o for o in lod0.all_objects if o.type == "MESH"]
        if objs:
            return objs
    return [
        o for o in bpy.data.objects
        if o.type == "MESH" and not o.name.startswith(("UCX_", "_Preview", "_Axis"))
    ]


def _world_bbox(objs: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    xs, ys, zs = [], [], []
    for o in objs:
        for c in o.bound_box:
            wc = o.matrix_world @ Vector(c)
            xs.append(wc.x)
            ys.append(wc.y)
            zs.append(wc.z)
    if not xs:
        return Vector((-0.5, -0.5, 0.0)), Vector((0.5, 0.5, 1.0))
    return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))


def _add_preview_lights_and_axes(center: Vector, diag: float) -> bpy.types.Collection:
    col = bpy.data.collections.new("_PreviewHelpers")
    bpy.context.scene.collection.children.link(col)

    # Thế giới sáng đều trung tính
    world = bpy.context.scene.world or bpy.data.worlds.new("_PreviewWorld")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.72, 0.74, 0.78, 1.0)
        bg.inputs["Strength"].default_value = 1.0

    # Đèn mặt trời chính + đèn phụ
    for lname, energy, pos in (
        ("_PreviewSunKey", 2.5, (diag * 2.0, -diag * 2.0, diag * 3.0)),
        ("_PreviewFill", 1.2, (-diag * 2.0, diag * 1.5, diag * 1.5)),
    ):
        ldata = bpy.data.lights.new(name=lname, type="SUN")
        ldata.energy = energy
        ldata.angle = 0.35
        lobj = bpy.data.objects.new(lname, ldata)
        lobj.location = center + Vector(pos)
        direction = (center - lobj.location).normalized()
        lobj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        col.objects.link(lobj)

    # Hệ trục màu XYZ nhỏ đặt ở góc sàn bên trái phía trước để VLM nhận biết hướng
    origin = Vector((center.x - diag * 0.45, center.y - diag * 0.45, 0.015))
    axis_len = max(0.08, diag * 0.16)
    axis_rad = axis_len * 0.055
    for name, vec, color in (
        ("_Axis_X_Red", Vector((1, 0, 0)), (0.95, 0.1, 0.1, 1.0)),
        ("_Axis_Y_Green", Vector((0, 1, 0)), (0.1, 0.88, 0.15, 1.0)),
        ("_Axis_Z_Blue", Vector((0, 0, 1)), (0.12, 0.35, 0.96, 1.0)),
    ):
        bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=axis_rad, depth=axis_len, location=(0, 0, 0))
        ax = bpy.context.active_object
        ax.name = name
        for c in list(ax.users_collection):
            c.objects.unlink(ax)
        col.objects.link(ax)
        ax.rotation_euler = vec.to_track_quat("Z", "X").to_euler()
        ax.location = origin + vec * (axis_len * 0.5)
        mat = bpy.data.materials.new(f"Mat_{name}")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = color
            bsdf.inputs["Emission Color"].default_value = color
            bsdf.inputs["Emission Strength"].default_value = 0.8
        ax.data.materials.append(mat)

    return col


def _make_wireframe_material() -> bpy.types.Material:
    mat = bpy.data.materials.new("_PreviewWireframeMat")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    mix = nt.nodes.new("ShaderNodeMixShader")
    wf = nt.nodes.new("ShaderNodeWireframe")
    wf.inputs["Size"].default_value = 0.008
    bsdf_clay = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf_clay.inputs["Base Color"].default_value = (0.78, 0.80, 0.84, 1.0)
    bsdf_line = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf_line.inputs["Base Color"].default_value = (0.04, 0.05, 0.08, 1.0)
    nt.links.new(wf.outputs["Fac"], mix.inputs["Fac"])
    nt.links.new(bsdf_clay.outputs["BSDF"], mix.inputs[1])
    nt.links.new(bsdf_line.outputs["BSDF"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    return mat


def render_6views(
    out_dir: Path,
    *,
    resolution: int = 512,
    samples: int = 8,
    pose_frame: int = 15,
) -> dict[str, str]:
    """Kết xuất 6 ảnh 512x512 (Front, Right, Back, Iso PBR, Iso Wireframe, Top/Mid-Pose)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    objs = _target_meshes()
    bmin, bmax = _world_bbox(objs)
    center = (bmin + bmax) * 0.5
    dims = bmax - bmin
    diag = max(0.3, dims.length)
    dist = diag * 1.45

    helpers_col = _add_preview_lights_and_axes(center, diag)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = max(int(samples), 12)
    scene.cycles.max_bounces = 1
    scene.cycles.diffuse_bounces = 0
    scene.cycles.glossy_bounces = 1
    scene.cycles.sample_clamp_indirect = 2.0
    scene.cycles.use_denoising = False
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.image_settings.file_format = "PNG"

    cam_data = bpy.data.cameras.new("_PreviewCam")
    cam_data.lens = 45.0
    cam_obj = bpy.data.objects.new("_PreviewCam", cam_data)
    helpers_col.objects.link(cam_obj)
    scene.camera = cam_obj

    # Tạm thời đảm bảo LOD0 hiện khi render còn LOD1/LOD2/HighPoly/UCX ẩn
    saved_hide = {}
    for o in bpy.data.objects:
        if o.type == "MESH":
            saved_hide[o.name] = o.hide_render
            o.hide_render = (o not in objs) and not o.name.startswith("_Axis_")

    views: list[tuple[str, Vector, bool, bool]] = [
        ("view_01_front.png", Vector((0.0, -dist, 0.24 * dist)), False, False),
        ("view_02_right.png", Vector((dist, 0.0, 0.24 * dist)), False, False),
        ("view_03_back.png", Vector((0.0, dist, 0.24 * dist)), False, False),
        ("view_04_iso_pbr.png", Vector((dist * 0.72, -dist * 0.72, dist * 0.55)), False, False),
        ("view_05_iso_wireframe.png", Vector((dist * 0.72, -dist * 0.72, dist * 0.55)), True, False),
        ("view_06_pose_or_top.png", Vector((dist * 0.55, -dist * 0.55, dist * 1.10)), False, True),
    ]

    wf_mat = _make_wireframe_material()
    saved_mats = {o.name: list(o.data.materials) for o in objs}
    arm_objs = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    rendered: dict[str, str] = {}

    try:
        for fname, offset, is_wire, use_pose in views:
            if is_wire:
                for o in objs:
                    o.data.materials.clear()
                    o.data.materials.append(wf_mat)
            else:
                for o in objs:
                    o.data.materials.clear()
                    for m in saved_mats[o.name]:
                        o.data.materials.append(m)

            if use_pose and arm_objs:
                scene.frame_set(pose_frame)
            else:
                scene.frame_set(1)

            cam_obj.location = center + offset
            direction = (center - cam_obj.location).normalized()
            up_local = "Y" if abs(direction.z) < 0.98 else "X"
            cam_obj.rotation_euler = direction.to_track_quat("-Z", up_local).to_euler()

            out_path = out_dir / fname
            scene.render.filepath = str(out_path)
            bpy.ops.render.render(write_still=True)
            rendered[fname] = str(out_path)
    finally:
        for o in objs:
            o.data.materials.clear()
            for m in saved_mats[o.name]:
                o.data.materials.append(m)
        for name, hv in saved_hide.items():
            obj = bpy.data.objects.get(name)
            if obj:
                obj.hide_render = hv
        scene.frame_set(1)
        for o in list(helpers_col.objects):
            bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.collections.remove(helpers_col)
        bpy.data.materials.remove(wf_mat)

    return rendered
