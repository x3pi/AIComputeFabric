"""Pha 0 Probe: đo WarmBlenderPool 30 vòng, RSS đỉnh, render 6 góc 512x512 & đối chứng 10 mẫu (Tầng 5A + 5B VLM)."""
from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from foundry3d.providers.llm_vlm_client import GatewayLlmVlmClient
from foundry3d.schemas import AssetSpec
from foundry3d.warm_blender import WarmBlenderPool

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = REPO_ROOT / "benchmarks/reports/p_3d_0_probe.json"


def _df_kb(path: str) -> dict[str, int]:
    out = subprocess.check_output(["df", "-k", path], text=True).splitlines()[-1].split()
    return {"total_kb": int(out[1]), "used_kb": int(out[2]), "avail_kb": int(out[3])}


def _make_probe_cases() -> list[dict[str, Any]]:
    """5 mẫu chuẩn + 5 mẫu gài lỗi thật theo Check Pass Pha 0."""
    cases: list[dict[str, Any]] = []

    # 1. Valid wooden table (front -Y has drawer knob, z=0)
    cases.append({
        "id": "good_table",
        "expected_pass": True,
        "defect_type": "none",
        "spec": AssetSpec(
            name="good_table",
            prompt="Wooden dining table with 4 legs touching floor and a small front drawer handle facing -Y",
            dimensions_m=(1.2, 0.8, 0.75),
            parts=[
                {"id": "tabletop", "anchor_to": "leg_fl"},
                {"id": "leg_fl", "anchor_to": "ground"},
                {"id": "leg_fr", "anchor_to": "ground"},
                {"id": "leg_bl", "anchor_to": "ground"},
                {"id": "leg_br", "anchor_to": "ground"},
                {"id": "front_handle", "anchor_to": "tabletop"},
            ],
        ).model_dump(),
        "bpy_code": """
import bpy
def make_mat(name, rgba):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = 0.5
    return m
def box(name, size, loc, mat):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(mat)
    return o
wood = make_mat("Oak", (0.45, 0.27, 0.12, 1.0))
brass = make_mat("Brass", (0.8, 0.65, 0.25, 1.0))
box("tabletop", (1.2, 0.8, 0.06), (0.0, 0.0, 0.72), wood)
for nm, x, y in [("leg_fl", -0.5, -0.3), ("leg_fr", 0.5, -0.3), ("leg_bl", -0.5, 0.3), ("leg_br", 0.5, 0.3)]:
    box(nm, (0.07, 0.07, 0.69), (x, y, 0.345), wood)
box("front_handle", (0.18, 0.03, 0.03), (0.0, -0.415, 0.72), brass)
""",
    })

    # 2. Valid chair facing -Y (backrest on +Y)
    cases.append({
        "id": "good_chair",
        "expected_pass": True,
        "defect_type": "none",
        "spec": AssetSpec(
            name="good_chair",
            prompt="Wooden dining chair facing forward toward -Y camera (open seat in front -Y, tall backrest at back +Y)",
            dimensions_m=(0.5, 0.5, 0.9),
            parts=[
                {"id": "seat", "anchor_to": "leg_fl"},
                {"id": "backrest", "anchor_to": "seat"},
                {"id": "leg_fl", "anchor_to": "ground"},
                {"id": "leg_fr", "anchor_to": "ground"},
                {"id": "leg_bl", "anchor_to": "ground"},
                {"id": "leg_br", "anchor_to": "ground"},
            ],
        ).model_dump(),
        "bpy_code": """
import bpy
m = bpy.data.materials.new("ChairWood"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.42, 0.24, 0.11, 1.0)
def box(name, size, loc):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
box("seat", (0.5, 0.5, 0.05), (0.0, 0.0, 0.455))
box("backrest", (0.5, 0.05, 0.42), (0.0, 0.225, 0.69))
for nm, x, y in [("leg_fl", -0.21, -0.21), ("leg_fr", 0.21, -0.21), ("leg_bl", -0.21, 0.21), ("leg_br", 0.21, 0.21)]:
    box(nm, (0.05, 0.05, 0.43), (x, y, 0.215))
""",
    })

    # 3. Valid treasure chest with iron straps and back hinges
    cases.append({
        "id": "good_chest",
        "expected_pass": True,
        "defect_type": "none",
        "spec": AssetSpec(
            name="good_chest",
            prompt="Low-poly wooden treasure chest with two dark metal reinforcement straps, back hinges (+Y), and brass lock clasp on front -Y side",
            dimensions_m=(0.9, 0.55, 0.6),
            parts=[
                {"id": "chest_body", "anchor_to": "ground"},
                {"id": "chest_lid", "anchor_to": "chest_body"},
                {"id": "strap_left", "anchor_to": "chest_body"},
                {"id": "strap_right", "anchor_to": "chest_body"},
                {"id": "hinge_left", "anchor_to": "chest_body"},
                {"id": "hinge_right", "anchor_to": "chest_body"},
                {"id": "front_clasp", "anchor_to": "chest_body"},
            ],
        ).model_dump(),
        "bpy_code": """
import bpy
def mat(n, c, met=0.0, rough=0.6):
    m = bpy.data.materials.new(n); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = c
    b.inputs["Metallic"].default_value = met
    b.inputs["Roughness"].default_value = rough
    return m
wood = mat("ChestWood", (0.36, 0.19, 0.09, 1.0), 0.0, 0.72)
iron = mat("ChestIron", (0.22, 0.24, 0.27, 1.0), 0.8, 0.45)
gold = mat("GoldClasp", (0.85, 0.68, 0.20, 1.0), 0.9, 0.30)
def box(name, size, loc, mt):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(mt)
    return o
def u_strap(name, x):
    parts = [
        box(f"{name}_f", (0.07, 0.02, 0.59), (x, -0.285, 0.305), iron),
        box(f"{name}_b", (0.07, 0.02, 0.59), (x, 0.285, 0.305), iron),
        box(f"{name}_t", (0.07, 0.59, 0.02), (x, 0.0, 0.605), iron),
    ]
    bpy.ops.object.select_all(action="DESELECT")
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    parts[0].name = name
box("chest_body", (0.9, 0.55, 0.40), (0, 0, 0.20), wood)
box("chest_lid", (0.92, 0.57, 0.20), (0, 0, 0.50), wood)
u_strap("strap_left", -0.28)
u_strap("strap_right", 0.28)
box("hinge_left", (0.08, 0.04, 0.10), (-0.28, 0.285, 0.40), iron)
box("hinge_right", (0.08, 0.04, 0.10), (0.28, 0.285, 0.40), iron)
box("front_clasp", (0.14, 0.05, 0.14), (0, -0.295, 0.39), gold)
""",
    })

    # 4. Valid stone pillar
    cases.append({
        "id": "good_pillar",
        "expected_pass": True,
        "defect_type": "none",
        "spec": AssetSpec(
            name="good_pillar",
            prompt="Classical stone column pillar with plinth base, shaft, and capital resting on floor",
            dimensions_m=(0.6, 0.6, 2.2),
            parts=[
                {"id": "base_plinth", "anchor_to": "ground"},
                {"id": "shaft", "anchor_to": "base_plinth"},
                {"id": "capital", "anchor_to": "shaft"},
            ],
        ).model_dump(),
        "bpy_code": """
import bpy
st = bpy.data.materials.new("Stone"); st.use_nodes = True
st.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.68, 0.66, 0.62, 1.0)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.125))
b = bpy.context.active_object; b.name = "base_plinth"; b.scale = (0.6, 0.6, 0.25)
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True); b.data.materials.append(st)
bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=0.22, depth=1.70, location=(0, 0, 1.10))
s = bpy.context.active_object; s.name = "shaft"; s.data.materials.append(st)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 2.075))
c = bpy.context.active_object; c.name = "capital"; c.scale = (0.58, 0.58, 0.25)
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True); c.data.materials.append(st)
""",
    })

    # 5. Valid articulated 2-bone leg rig (smooth knee weights + forward foot facing -Y + frame-15 pose)
    cases.append({
        "id": "good_leg_rig",
        "expected_pass": True,
        "defect_type": "none",
        "spec": AssetSpec(
            name="good_leg_rig",
            prompt="Low-poly articulated two-bone bendable leg with thigh, knee joint, calf, and foot pointing forward (-Y) resting on Z=0",
            dimensions_m=(0.24, 0.34, 1.0),
            rig_type="mechanical",
            parts=[{"id": "leg_mesh", "anchor_to": "ground"}],
        ).model_dump(),
        "bpy_code": """
import bpy, bmesh, math
from mathutils import Vector
m = bpy.data.materials.new("ArmoredLeg"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.58, 0.52, 0.46, 1.0)
mesh = bpy.data.meshes.new("leg_mesh")
bm = bmesh.new()
# Create multi-ring cylinder-like octo leg from z=0 to z=1.0
rings = []
for zi in range(11):
    z = zi * 0.1
    r = 0.095 if zi >= 5 else (0.075 if zi >= 1 else 0.085)
    ring = []
    for k in range(8):
        ang = k * (2.0 * math.pi / 8.0)
        x = r * math.cos(ang)
        y = r * math.sin(ang)
        # Foot extension toward -Y near z=0..0.1
        if zi <= 1 and y < 0:
            y -= 0.14
        ring.append(bm.verts.new((x, y, z)))
    rings.append(ring)
for zi in range(10):
    r0, r1 = rings[zi], rings[zi+1]
    for k in range(8):
        bm.faces.new([r0[k], r0[(k+1)%8], r1[(k+1)%8], r1[k]])
bm.faces.new(list(reversed(rings[0])))
bm.faces.new(rings[-1])
bm.to_mesh(mesh); bm.free()
obj = bpy.data.objects.new("leg_mesh", mesh)
bpy.context.scene.collection.objects.link(obj)
obj.data.materials.append(m)

arm_d = bpy.data.armatures.new("LegArm")
arm = bpy.data.objects.new("LegArm", arm_d)
bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
b1 = arm_d.edit_bones.new("thigh"); b1.head = Vector((0,0,1.0)); b1.tail = Vector((0,0,0.5))
b2 = arm_d.edit_bones.new("calf"); b2.head = Vector((0,0,0.5)); b2.tail = Vector((0,0,0.0)); b2.parent = b1
bpy.ops.object.mode_set(mode="OBJECT")

vg1 = obj.vertex_groups.new(name="thigh")
vg2 = obj.vertex_groups.new(name="calf")
for v in obj.data.vertices:
    z = v.co.z
    if z >= 0.65:
        vg1.add([v.index], 1.0, "REPLACE")
    elif z <= 0.35:
        vg2.add([v.index], 1.0, "REPLACE")
    else:
        t = (z - 0.35) / 0.30
        vg1.add([v.index], t, "REPLACE")
        vg2.add([v.index], 1.0 - t, "REPLACE")
mod = obj.modifiers.new(name="Armature", type="ARMATURE"); mod.object = arm

# Keyframe frame 1 (rest) and frame 15 (bent knee 35 deg) so view_06 shows pose cleanly
pb = arm.pose.bones["calf"]
pb.rotation_mode = "XYZ"
bpy.context.scene.frame_set(1)
pb.rotation_euler = (0.0, 0.0, 0.0)
pb.keyframe_insert(data_path="rotation_euler", frame=1)
bpy.context.scene.frame_set(15)
pb.rotation_euler = (math.radians(35.0), 0.0, 0.0)
pb.keyframe_insert(data_path="rotation_euler", frame=15)
bpy.context.scene.frame_set(1)
""",
    })

    # 6. DEFECT 1: Floating leg +15cm (G2 Floating Part Check)
    cases.append({
        "id": "defect_floating_15cm",
        "expected_pass": False,
        "defect_type": "floating_part_15cm",
        "spec": AssetSpec(
            name="defect_floating_15cm",
            prompt="Chair where seat floats 15 cm above the legs",
            dimensions_m=(0.5, 0.5, 0.9),
            parts=[{"id": "seat", "anchor_to": "leg_fl"}, {"id": "leg_fl", "anchor_to": "ground"}],
        ).model_dump(),
        "bpy_code": """
import bpy
m = bpy.data.materials.new("Wood"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.45, 0.25, 0.1, 1.0)
def box(name, size, loc):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
for nm, x, y in [("leg_fl", -0.2, -0.2), ("leg_fr", 0.2, -0.2), ("leg_bl", -0.2, 0.2), ("leg_br", 0.2, 0.2)]:
    box(nm, (0.05, 0.05, 0.40), (x, y, 0.20))
# Seat floats +15cm above leg tops (0.40 -> 0.55 bottom)
box("seat", (0.5, 0.5, 0.05), (0.0, 0.0, 0.575))
""",
    })

    # 7. DEFECT 2: Two blocks overlapping ~40% illegally (G3 IllegalOverlap)
    cases.append({
        "id": "defect_overlap_40pct",
        "expected_pass": False,
        "defect_type": "illegal_overlap_40pct",
        "spec": AssetSpec(
            name="defect_overlap_40pct",
            prompt="Two crates illegally intersecting 40% volume",
            dimensions_m=(1.0, 0.6, 0.6),
            parts=[{"id": "crate_a", "anchor_to": "ground"}, {"id": "crate_b", "anchor_to": "ground"}],
        ).model_dump(),
        "bpy_code": """
import bpy
m = bpy.data.materials.new("Crate"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.55, 0.35, 0.18, 1.0)
def box(name, size, loc):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
box("crate_a", (0.6, 0.6, 0.6), (-0.18, 0.0, 0.30))
# Overlaps X by 0.24 / 0.60 = 40%
box("crate_b", (0.6, 0.6, 0.6), (0.18, 0.0, 0.30))
""",
    })

    # 8. DEFECT 3: Flipped Backwards Chair (faces +Y instead of -Y -> catches via VLM 6-View Critic!)
    cases.append({
        "id": "defect_flipped_backwards",
        "expected_pass": False,
        "defect_type": "flipped_front_back_axis",
        "spec": AssetSpec(
            name="defect_flipped_backwards",
            prompt="Dining chair facing FORWARD toward the camera (-Y front view MUST show the open front of the seat; backrest MUST be in the rear +Y, NOT blocking the front -Y view)",
            dimensions_m=(0.5, 0.5, 0.9),
            parts=[
                {"id": "seat", "anchor_to": "leg_fl"},
                {"id": "backrest", "anchor_to": "seat"},
                {"id": "leg_fl", "anchor_to": "ground"},
                {"id": "leg_fr", "anchor_to": "ground"},
                {"id": "leg_bl", "anchor_to": "ground"},
                {"id": "leg_br", "anchor_to": "ground"},
            ],
        ).model_dump(),
        "bpy_code": """
import bpy
m = bpy.data.materials.new("ChairWood"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.42, 0.24, 0.11, 1.0)
def box(name, size, loc):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
box("seat", (0.5, 0.5, 0.05), (0.0, 0.0, 0.455))
# BUG: Backrest placed at FRONT (-Y = -0.225) instead of BACK (+Y)!
box("backrest", (0.5, 0.05, 0.42), (0.0, -0.225, 0.69))
for nm, x, y in [("leg_fl", -0.21, -0.21), ("leg_fr", 0.21, -0.21), ("leg_bl", -0.21, 0.21), ("leg_br", 0.21, 0.21)]:
    box(nm, (0.05, 0.05, 0.43), (x, y, 0.215))
""",
    })

    # 9. DEFECT 4: Pitch-black BaseColor (G4_MeshAndUVHealth)
    cases.append({
        "id": "defect_black_material",
        "expected_pass": False,
        "defect_type": "all_black_basecolor",
        "spec": AssetSpec(
            name="defect_black_material",
            prompt="Wooden crate with brown plank texture (broken pitch-black basecolor)",
            dimensions_m=(0.8, 0.8, 0.8),
            parts=[{"id": "crate", "anchor_to": "ground"}],
        ).model_dump(),
        "bpy_code": """
import bpy
m = bpy.data.materials.new("BrokenBlack"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
bpy.ops.mesh.primitive_cube_add(size=0.8, location=(0.0, 0.0, 0.4))
o = bpy.context.active_object; o.name = "crate"; o.data.materials.append(m)
""",
    })

    # 10. DEFECT 5: Knee spike (>3.5x edge stretch at 60 deg bend, G5_RigDeformationStress)
    cases.append({
        "id": "defect_knee_spike",
        "expected_pass": False,
        "defect_type": "knee_weight_spike_60deg",
        "spec": AssetSpec(
            name="defect_knee_spike",
            prompt="Bendable leg rig with corrupted knee vertex weights causing spike",
            dimensions_m=(0.25, 0.25, 1.0),
            rig_type="mechanical",
            parts=[{"id": "leg_mesh", "anchor_to": "ground"}],
        ).model_dump(),
        "bpy_code": """
import bpy, bmesh
from mathutils import Vector
m = bpy.data.materials.new("Skin"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.72, 0.54, 0.42, 1.0)
mesh = bpy.data.meshes.new("leg_mesh")
bm = bmesh.new()
rings = []
for zi in range(11):
    z = zi * 0.1
    rings.append([bm.verts.new((-0.1,-0.1,z)), bm.verts.new((0.1,-0.1,z)), bm.verts.new((0.1,0.1,z)), bm.verts.new((-0.1,0.1,z))])
for zi in range(10):
    r0, r1 = rings[zi], rings[zi+1]
    for k in range(4):
        bm.faces.new([r0[k], r0[(k+1)%4], r1[(k+1)%4], r1[k]])
bm.faces.new(list(reversed(rings[0]))); bm.faces.new(rings[-1])
bm.to_mesh(mesh); bm.free()
obj = bpy.data.objects.new("leg_mesh", mesh)
bpy.context.scene.collection.objects.link(obj); obj.data.materials.append(m)

arm_d = bpy.data.armatures.new("LegArm")
arm = bpy.data.objects.new("LegArm", arm_d); bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
b1 = arm_d.edit_bones.new("thigh"); b1.head = Vector((0,0,1.0)); b1.tail = Vector((0,0,0.5))
b2 = arm_d.edit_bones.new("calf"); b2.head = Vector((0,0,0.5)); b2.tail = Vector((0,0,0.0)); b2.parent = b1
bpy.ops.object.mode_set(mode="OBJECT")

vg1 = obj.vertex_groups.new(name="thigh"); vg2 = obj.vertex_groups.new(name="calf")
for v in obj.data.vertices:
    # BUG: vertex near top stays pinned to calf while adjacent ring moves with thigh/rest -> extreme spike on 60 deg bend!
    if abs(v.co.z - 0.1) < 0.02 and v.co.x > 0:
        vg1.add([v.index], 1.0, "REPLACE")
    elif v.co.z >= 0.55:
        vg1.add([v.index], 1.0, "REPLACE")
    else:
        vg2.add([v.index], 1.0, "REPLACE")
mod = obj.modifiers.new(name="Armature", type="ARMATURE"); mod.object = arm
""",
    })

    return cases


def main() -> None:
    df_before = {
        "root": _df_kb("/"),
        "workspace": _df_kb(str(REPO_ROOT)),
        "mnt_data": _df_kb("/mnt/Data"),
    }

    pool = WarmBlenderPool()
    llm = GatewayLlmVlmClient()
    live_model = llm.resolve_live_model()

    ping_ms_list: list[float] = []
    cold_start_ms = 0.0
    max_rss_mb = 0.0

    try:
        # 1. Đo 30 vòng khởi động lạnh vs gọi ấm
        for i in range(30):
            t0 = time.perf_counter()
            resp, call_dir, cold = pool.run_task("ping", {})
            pool.cleanup_call_dir(call_dir)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            max_rss_mb = max(max_rss_mb, float(resp.get("peak_rss_mb", 0.0)))
            if i == 0:
                cold_start_ms = dt_ms
            else:
                ping_ms_list.append(dt_ms)

        # 2. Chấm 10 mẫu đối chứng qua Tầng 5A + Tầng 5B VLM
        cases = _make_probe_cases()
        render_times: list[float] = []
        case_results: list[dict[str, Any]] = []
        correct_count = 0

        for idx, c in enumerate(cases):
            cid = c["id"]
            # Bỏ qua postproc cho lỗi hình học/đen/rig để giữ nguyên lỗi tiêm vào;
            # riêng defect_flipped_backwards vẫn chạy postproc UV để Tầng 5A XANH và buộc Tầng 5B VLM bắt lỗi quay lưng!
            skip_post = (not c["expected_pass"]) and cid != "defect_flipped_backwards"
            resp, call_dir, _ = pool.run_task(
                "run_pipeline",
                {
                    "spec": c["spec"],
                    "bpy_code": c["bpy_code"],
                    "skip_postproc": skip_post,
                    "bake_pbr": False,
                    "render_6views": True,
                    "render_res": 512,
                    "render_samples": 12,
                    "export_bundle": c["expected_pass"],
                },
            )
            try:
                if not resp.get("ok"):
                    print(f"[{idx+1}/10] {cid} DAEMON ERROR: {resp.get('error')}")
                geom = resp.get("geometry") or {}
                max_rss_mb = max(max_rss_mb, float(resp.get("peak_rss_mb", 0.0)))
                render_times.append(float(resp.get("render_6views_ms", 0.0)))
                pass_5a = bool(geom.get("passed", False))

                vlm_dict = None
                pass_5b = True
                # Gọi VLM khi 5A XANH (theo đúng D-3D-6 & 2.3 Chặng 5)
                if pass_5a:
                    view_paths = sorted((call_dir / "views").glob("view_*.png"))
                    verdict = llm.judge_6views(
                        AssetSpec(**c["spec"]),
                        view_paths,
                        geometry_summary={
                            "tris_lod0": geom.get("tris_lod0"),
                            "dimensions_measured_m": geom.get("dimensions_measured_m"),
                            "z_min_m": geom.get("z_min_m"),
                        },
                    )
                    vlm_dict = verdict.model_dump(by_alias=True)
                    pass_5b = bool(verdict.passed)

                predicted_pass = pass_5a and pass_5b
                is_correct = predicted_pass == c["expected_pass"]
                if is_correct:
                    correct_count += 1

                print(
                    f"[{idx+1}/10] {cid:24s} expected={str(c['expected_pass']):5s} "
                    f"5A={str(pass_5a):5s} 5B={str(pass_5b):5s} -> match={is_correct} "
                    f"(render={resp.get('render_6views_ms')}ms, rss={resp.get('peak_rss_mb')}MB)"
                )
                case_results.append({
                    "id": cid,
                    "expected_pass": c["expected_pass"],
                    "defect_type": c["defect_type"],
                    "pass_5a": pass_5a,
                    "pass_5b": pass_5b,
                    "predicted_pass": predicted_pass,
                    "correct": is_correct,
                    "failure_reasons_5a": geom.get("failure_reasons", []),
                    "vlm": vlm_dict,
                    "render_6views_ms": resp.get("render_6views_ms"),
                    "peak_rss_mb": resp.get("peak_rss_mb"),
                    "validator_errors": geom.get("validator_errors", 0),
                })
            finally:
                pool.cleanup_call_dir(call_dir)
    finally:
        pool.shutdown()

    df_after = {
        "root": _df_kb("/"),
        "workspace": _df_kb(str(REPO_ROOT)),
        "mnt_data": _df_kb("/mnt/Data"),
    }

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "live_vlm_model": live_model,
        "warm_probe_30_loops": {
            "cold_start_ms": round(cold_start_ms, 2),
            "warm_ping_p50_ms": round(statistics.median(ping_ms_list), 2),
            "warm_ping_p95_ms": round(sorted(ping_ms_list)[int(0.95 * len(ping_ms_list))], 2),
            "warm_ping_max_ms": round(max(ping_ms_list), 2),
            "render_6views_512_p50_ms": round(statistics.median(render_times), 2) if render_times else 0.0,
            "peak_rss_mb": round(max_rss_mb, 2),
        },
        "ten_case_confusion": {
            "total": len(case_results),
            "correct": correct_count,
            "accuracy": round(correct_count / max(1, len(case_results)), 3),
            "cases": case_results,
        },
        "disk_before_kb": df_before,
        "disk_after_kb": df_after,
        "check_pass": {
            "root_free_ge_1_8_gib": df_after["root"]["avail_kb"] >= 1_800_000,
            "workspace_free_ge_2_2_gib": df_after["workspace"]["avail_kb"] >= 2_200_000,
            "peak_rss_le_1536_mb": max_rss_mb <= 1536.0,
            "accuracy_ge_9_of_10": correct_count >= 9,
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nSUMMARY:", json.dumps(report["check_pass"], indent=2))
    print("WARM STATS:", json.dumps(report["warm_probe_30_loops"], indent=2))


if __name__ == "__main__":
    main()
