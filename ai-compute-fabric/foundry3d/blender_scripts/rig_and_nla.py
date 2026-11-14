"""Chặng 4 — Gắn khớp cơ học / Armature xương gốc `root`, khóa <= 4 xương/đỉnh và NLA Tracks (Pha 2 & D-3D-5)."""
from __future__ import annotations

import math
from typing import Any

import bpy
from mathutils import Vector


def _ensure_rig_collection() -> bpy.types.Collection:
    col = bpy.data.collections.get("Rig")
    if col is None:
        col = bpy.data.collections.new("Rig")
        bpy.context.scene.collection.children.link(col)
    return col


def _set_bone_limit_4_and_normalize(obj: bpy.types.Object) -> None:
    """Chuẩn hóa tối đa 4 xương/đỉnh và tổng trọng số = 1.0 theo chuẩn Game Engine."""
    if not obj.vertex_groups:
        return
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        bpy.ops.object.vertex_group_limit_total(limit=4)
        bpy.ops.object.vertex_group_normalize_all(lock_active=False)
    except Exception:
        # Fallback thủ công nếu không ở mode phù hợp
        for v in obj.data.vertices:
            weights = sorted([(g.group, g.weight) for g in v.groups if g.weight > 0.0], key=lambda x: -x[1])[:4]
            total = sum(w for _, w in weights)
            for vg in obj.vertex_groups:
                vg.remove([v.index])
            if total > 1e-8:
                for gi, w in weights:
                    obj.vertex_groups[gi].add([v.index], w / total, "REPLACE")


def apply_articulated_rig_and_nla(spec: dict[str, Any]) -> dict[str, Any]:
    """Tạo Armature có xương gốc `root` + NLA Track đóng/mở hoặc trượt cho `articulated_prop`."""
    joints = spec.get("joints") or []
    archetype = spec.get("archetype", "static_prop")
    if archetype == "static_prop" or not joints:
        return {"rigged": False, "nla_tracks": [], "bones": []}

    lod0_col = bpy.data.collections.get("LOD0")
    objs = {o.name: o for o in (lod0_col.all_objects if lod0_col else bpy.data.objects) if o.type == "MESH"}

    rig_col = _ensure_rig_collection()
    arm_data = bpy.data.armatures.new(f"{spec.get('name', 'Asset')}_Armature")
    arm_obj = bpy.data.objects.new(f"{spec.get('name', 'Asset')}_Rig", arm_data)
    rig_col.objects.link(arm_obj)

    bpy.ops.object.select_all(action="DESELECT")
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")

    # Xương gốc `root` bắt buộc theo Phần 7.3.5
    root_eb = arm_data.edit_bones.new("root")
    root_eb.head = Vector((0.0, 0.0, 0.0))
    root_eb.tail = Vector((0.0, 0.0, 0.25))

    created_bones = ["root"]
    joint_bones: list[tuple[dict[str, Any], str]] = []
    for j in joints:
        bname = f"joint_{j['name']}"
        eb = arm_data.edit_bones.new(bname)
        orig = Vector(j.get("origin", (0.0, 0.0, 0.0)))
        axis = Vector(j.get("axis", (0.0, 0.0, 1.0)))
        if axis.length < 1e-6:
            axis = Vector((0.0, 0.0, 1.0))
        axis.normalize()
        eb.head = orig
        eb.tail = orig + axis * 0.20
        eb.parent = root_eb
        created_bones.append(bname)
        joint_bones.append((j, bname))

    bpy.ops.object.mode_set(mode="OBJECT")

    # Gắn bộ phận tĩnh về `root` và bộ phận khớp về `joint_<name>` với trọng số 1.0 cứng (mechanical)
    skinned_part_names = set()
    for j, bname in joint_bones:
        pid = j["part_id"]
        obj = objs.get(pid)
        if obj is None:
            continue
        skinned_part_names.add(pid)
        vg = obj.vertex_groups.get(bname) or obj.vertex_groups.new(name=bname)
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        mod = obj.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = arm_obj
        _set_bone_limit_4_and_normalize(obj)

    for pid, obj in objs.items():
        if pid in skinned_part_names:
            continue
        vg = obj.vertex_groups.get("root") or obj.vertex_groups.new(name="root")
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        mod = obj.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = arm_obj
        _set_bone_limit_4_and_normalize(obj)

    # Tạo 1 Action + NLA Track kiểm thử đóng/mở ("OpenClose" / "Cycle")
    action = bpy.data.actions.new(name="Action_OpenClose")
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 30

    for j, bname in joint_bones:
        pb = arm_obj.pose.bones.get(bname)
        if pb is None:
            continue
        pb.rotation_mode = "XYZ"
        jtype = j.get("joint_type", "hinge")
        axis = Vector(j.get("axis", (0.0, 0.0, 1.0))).normalized()
        if jtype == "slider":
            slide = float(j.get("max_slide_m", 0.25))
            pb.location = Vector((0.0, 0.0, 0.0))
            pb.keyframe_insert(data_path="location", frame=1)
            # Đổi tọa độ sang local bone space gần đúng
            pb.location = Vector((0.0, slide, 0.0))
            pb.keyframe_insert(data_path="location", frame=15)
            pb.location = Vector((0.0, 0.0, 0.0))
            pb.keyframe_insert(data_path="location", frame=30)
        else:
            # Với bone tail dọc theo `axis`, trục Y local của xương trùng với trục quay `axis`!
            min_deg = float(j.get("min_angle_deg", 0.0))
            max_deg = float(j.get("max_angle_deg", 90.0))
            pb.rotation_euler = (0.0, math.radians(min_deg), 0.0)
            pb.keyframe_insert(data_path="rotation_euler", frame=1)
            pb.rotation_euler = (0.0, math.radians(max_deg), 0.0)
            pb.keyframe_insert(data_path="rotation_euler", frame=15)
            pb.rotation_euler = (0.0, math.radians(min_deg), 0.0)
            pb.keyframe_insert(data_path="rotation_euler", frame=30)

    # Đưa Action vào NLA Track để glTF exporter đóng gói clip
    track = arm_obj.animation_data.nla_tracks.new()
    track.name = "OpenClose"
    track.strips.new(name="OpenClose", start=1, action=action)
    arm_obj.animation_data.action = None

    # Khôi phục tư thế nghỉ Frame 1
    for pb in arm_obj.pose.bones:
        pb.location = Vector((0.0, 0.0, 0.0))
        pb.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()

    return {
        "rigged": True,
        "bones": created_bones,
        "nla_tracks": [t.name for t in arm_obj.animation_data.nla_tracks],
    }

