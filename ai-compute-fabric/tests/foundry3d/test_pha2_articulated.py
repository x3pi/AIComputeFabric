"""Kiểm thử Pha 2 — Đạo cụ có khớp cơ học & Cổng G6_ArticulatedSweep (+ đột biến Origin lệch / cánh cửa đâm khung)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from foundry3d.schemas import AssetSpec, JointSpec
from foundry3d.warm_blender import WarmBlenderPool

DOCKER_OK = bool(shutil.which("docker")) and Path("/mnt/Data/aicf-3d-foundry/runtime/blender-4.2-lts/blender").exists()
pytestmark = pytest.mark.skipif(not DOCKER_OK, reason="Cần Docker + Blender 4.2 runtime")


@pytest.fixture(scope="module")
def pool2():
    p = WarmBlenderPool()
    yield p
    p.shutdown()


def _hinged_door_bpy(door_origin_x: float = -0.42, swing_collide: bool = False) -> str:
    # Khung cửa nằm ở X=[-0.50, +0.50], trừ ô rỗng ở giữa; làm 2 trụ đứng + xà ngang trên
    # Nếu swing_collide=True thì thêm khối cản đặt ở +Y (đường xoay +90 deg quanh +Z của cánh cửa)
    obstacle = """
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.35, 0.90))
obs = bpy.context.active_object; obs.name = "front_blocker"; obs.scale = (0.9, 0.45, 1.80)
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True); obs.data.materials.append(m)
""" if swing_collide else ""

    # Ở Origin (-0.42, 0, 1.0), cánh cửa rộng 0.80 nằm lệch sang +X [+0.02 .. +0.82] quanh Origin
    # Nếu cố tình đặt door_origin_x = 0.0 (sai tâm bản lề!) thì G6 phải bắt lỗi lệch Origin!
    return f"""
import bpy
m = bpy.data.materials.new("DoorWood"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.48, 0.30, 0.16, 1.0)

def box_world(name, size, center):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
    return o

# Frame = 1 mesh ghép 3 khối rời nhau bằng bmesh join
parts = [
    box_world("p_left", (0.08, 0.16, 2.0), (-0.46, 0.0, 1.0)),
    box_world("p_right", (0.08, 0.16, 2.0), (0.46, 0.0, 1.0)),
    box_world("p_top", (1.00, 0.16, 0.10), (0.0, 0.0, 2.05)),
]
bpy.ops.object.select_all(action="DESELECT")
for o in parts:
    o.select_set(True)
bpy.context.view_layer.objects.active = parts[0]
bpy.ops.object.join()
parts[0].name = "door_frame"
{obstacle}

# Panel với Origin đặt tại (door_origin_x, 0.0, 1.0) và đỉnh Z nằm trong [-0.99, +0.99] quanh Origin -> Z thế giới [0.01, 1.99], mép bản lề tiếp xúc 1mm với khung -0.42
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
panel = bpy.context.active_object
panel.name = "door_panel"
panel.scale = (0.838, 0.05, 1.98)
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
for v in panel.data.vertices:
    v.co.x -= {door_origin_x}
panel.location = ({door_origin_x}, 0.0, 1.0)
panel.data.materials.append(m)
"""


def test_pha2_articulated_hinged_door_valid_passes_and_exports_nla(pool2: WarmBlenderPool):
    """Cửa bản lề chuẩn đặt đúng Origin (-0.42, 0, 1.0), quay 90° sạch -> XANH + có NLA track + 0 errors."""
    spec = AssetSpec(
        name="hinged_door",
        prompt="Wooden door in frame",
        archetype="articulated_prop",
        dimensions_m=(1.0, 0.25, 2.1),
        parts=[{"id": "door_frame", "anchor_to": "ground"}, {"id": "door_panel", "anchor_to": "door_frame"}],
        joints=[
            JointSpec(
                name="hinge_door",
                part_id="door_panel",
                parent_part_id="door_frame",
                joint_type="hinge",
                origin=(-0.42, 0.0, 1.0),
                axis=(0.0, 0.0, 1.0),
                min_angle_deg=0.0,
                max_angle_deg=90.0,
            )
        ],
    ).model_dump()
    resp, d, _ = pool2.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": _hinged_door_bpy(door_origin_x=-0.42), "render_6views": False, "export_bundle": True},
    )
    pool2.cleanup_call_dir(d)
    assert resp["ok"] is True
    assert resp["geometry"]["passed"] is True, resp["geometry"]["failure_reasons"]
    assert resp["rig"]["rigged"] is True
    assert "root" in resp["rig"]["bones"]
    assert "OpenClose" in resp["rig"]["nla_tracks"]
    assert resp["geometry"]["validator_errors"] == 0


def test_pha2_mut_misplaced_origin_blocked(pool2: WarmBlenderPool):
    """Đột biến Pha 2: Origin cánh cửa đặt ở X=0 thay vì X=-0.42 -> G6_ArticulatedSweep chặn ĐỎ."""
    spec = AssetSpec(
        name="hinged_door_bad_origin",
        prompt="Wooden door",
        archetype="articulated_prop",
        dimensions_m=(1.0, 0.25, 2.1),
        parts=[{"id": "door_frame", "anchor_to": "ground"}, {"id": "door_panel", "anchor_to": "door_frame"}],
        joints=[
            JointSpec(
                name="hinge_door",
                part_id="door_panel",
                parent_part_id="door_frame",
                joint_type="hinge",
                origin=(-0.42, 0.0, 1.0),
                axis=(0.0, 0.0, 1.0),
                min_angle_deg=0.0,
                max_angle_deg=90.0,
            )
        ],
    ).model_dump()
    resp, d, _ = pool2.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": _hinged_door_bpy(door_origin_x=0.0), "render_6views": False, "export_bundle": False},
    )
    pool2.cleanup_call_dir(d)
    assert resp["ok"] is True
    assert resp["geometry"]["passed"] is False
    assert any("G6_ArticulatedSweep" in r for r in resp["geometry"]["failure_reasons"])


def test_pha2_mut_swing_collision_blocked(pool2: WarmBlenderPool):
    """Đột biến Pha 2: Cánh cửa xoay 90° va chạm vật cản front_blocker -> G6_ArticulatedSweep chặn ĐỎ."""
    spec = AssetSpec(
        name="hinged_door_collide",
        prompt="Wooden door colliding while swinging",
        archetype="articulated_prop",
        dimensions_m=(1.0, 0.7, 2.1),
        parts=[
            {"id": "door_frame", "anchor_to": "ground"},
            {"id": "front_blocker", "anchor_to": "ground"},
            {"id": "door_panel", "anchor_to": "door_frame"},
        ],
        joints=[
            JointSpec(
                name="hinge_door",
                part_id="door_panel",
                parent_part_id="door_frame",
                joint_type="hinge",
                origin=(-0.42, 0.0, 1.0),
                axis=(0.0, 0.0, 1.0),
                min_angle_deg=0.0,
                max_angle_deg=90.0,
            )
        ],
    ).model_dump()
    resp, d, _ = pool2.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": _hinged_door_bpy(door_origin_x=-0.42, swing_collide=True), "render_6views": False, "export_bundle": False},
    )
    pool2.cleanup_call_dir(d)
    assert resp["ok"] is True
    assert resp["geometry"]["passed"] is False
    assert any("G6_ArticulatedSweep" in r for r in resp["geometry"]["failure_reasons"])


def test_safe_helpers_run_in_real_blender(pool2: WarmBlenderPool):
    """Thư viện trợ giúp trong prompt (api_rag_b42.SAFE_HELPERS_BPY) chạy được trong Blender 4.2 thật và set_pivot giữ nguyên hình dạng."""
    from foundry3d.api_rag_b42 import SAFE_HELPERS_BPY

    code = SAFE_HELPERS_BPY + """
m = _mat("W", (0.5, 0.3, 0.2))
a = box_world("body", (0.4, 0.2, 0.8), (0.0, 0.0, 0.4), m)
b = box_world("flap", (0.3, 0.02, 0.2), (0.0, -0.11, 0.7), m)
set_pivot(b, (0.0, -0.11, 0.8))
cyl_world("post", 0.05, 0.5, (0.5, 0.0, 0.25), "Z", m)
cone_world("cap", 0.1, 0.0, 0.2, (0.5, 0.0, 0.6), m)
"""
    spec = AssetSpec(name="helpers_probe", prompt="probe", archetype="static_prop", dimensions_m=(1.2, 0.4, 0.8),
                     parts=[{"id": "body", "anchor_to": "ground"}]).model_dump()
    resp, d, _ = pool2.run_task("run_pipeline", {"spec": spec, "bpy_code": code, "render_6views": False, "export_bundle": False})
    pool2.cleanup_call_dir(d)
    assert resp["ok"] is True, resp
