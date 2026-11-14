"""Chặng 6 — Xuất gói tài sản chuẩn Game Engine (.glb / .fbx / .blend), kiểm tra round-trip bbox & Khronos glTF-Validator."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import bpy
from mathutils import Vector

DEFAULT_VALIDATOR_BIN = os.environ.get("AICF_GLTF_VALIDATOR", "/runtime/gltf_validator/gltf_validator")


def _export_objects_selection() -> list[bpy.types.Object]:
    lod0 = bpy.data.collections.get("LOD0")
    objs = [o for o in lod0.all_objects if o.type == "MESH"] if lod0 else [
        o for o in bpy.data.objects if o.type == "MESH" and not o.name.startswith(("UCX_", "_Preview", "_Axis"))
    ]
    arm_col = bpy.data.collections.get("Rig")
    arms = [o for o in arm_col.all_objects if o.type == "ARMATURE"] if arm_col else [
        o for o in bpy.data.objects if o.type == "ARMATURE"
    ]
    return objs + arms


def _bbox_dims_of_meshes(objs: list[bpy.types.Object]) -> tuple[float, float, float]:
    xs, ys, zs = [], [], []
    for o in objs:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            wc = o.matrix_world @ Vector(c)
            xs.append(wc.x)
            ys.append(wc.y)
            zs.append(wc.z)
    if not xs:
        return (0.0, 0.0, 0.0)
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _roundtrip_check_glb(glb_path: Path, expected_dims: tuple[float, float, float]) -> float:
    """Nạp ngược asset.glb vào scene tạm (hoặc khôi phục sau) để so sánh bbox với bản gốc (Phần 7.3.5)."""
    existing = set(bpy.data.objects.keys())
    try:
        bpy.ops.import_scene.gltf(filepath=str(glb_path))
        imported = [o for o in bpy.data.objects if o.name not in existing and o.type == "MESH"]
        if not imported:
            return 999.0
        # glTF là Y-up trong tệp, Blender glTF importer xoay lại về Z-up khi nạp vào Blender!
        imp_dims = _bbox_dims_of_meshes(imported)
        delta = max(abs(imp_dims[i] - expected_dims[i]) for i in range(3))
        return round(float(delta), 5)
    finally:
        for o in list(bpy.data.objects):
            if o.name not in existing:
                bpy.data.objects.remove(o, do_unlink=True)


def run_gltf_validator(glb_path: Path, validator_bin: str = DEFAULT_VALIDATOR_BIN) -> dict[str, Any]:
    """Chạy trình kiểm định Khronos glTF-Validator độc lập trên tệp asset.glb."""
    if not Path(validator_bin).exists():
        return {"ok": False, "error": f"Không tìm thấy gltf_validator tại {validator_bin}", "numErrors": 1, "numWarnings": 0}
    proc = subprocess.run(
        [validator_bin, "--stdout", "-r", str(glb_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        rep = json.loads(proc.stdout)
        issues = rep.get("issues", {})
        return {
            "ok": int(issues.get("numErrors", 1)) == 0,
            "numErrors": int(issues.get("numErrors", 0)),
            "numWarnings": int(issues.get("numWarnings", 0)),
            "numInfos": int(issues.get("numInfos", 0)),
            "messages": issues.get("messages", [])[:20],
            "validatorVersion": rep.get("validatorVersion", ""),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Không đọc được JSON của gltf_validator (rc={proc.returncode}): {exc} / {proc.stderr[-300:]}",
            "numErrors": 1,
            "numWarnings": 0,
        }


def export_asset_bundle(
    out_dir: Path,
    spec: dict[str, Any],
    *,
    export_fbx: bool = True,
    export_blend: bool = True,
) -> dict[str, Any]:
    """Xuất asset.glb, (tuỳ chọn) asset.fbx & source.blend, kiểm round-trip + gltf_validator."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sel_objs = _export_objects_selection()
    mesh_objs = [o for o in sel_objs if o.type == "MESH"]
    expected_dims = _bbox_dims_of_meshes(mesh_objs)

    bpy.ops.object.select_all(action="DESELECT")
    for o in sel_objs:
        o.hide_viewport = False
        o.select_set(True)
    if sel_objs:
        bpy.context.view_layer.objects.active = sel_objs[0]

    glb_path = out_dir / "asset.glb"
    bpy.ops.export_scene.gltf(
        filepath=str(glb_path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_animations=True,
        export_nla_strips=True,
    )

    out_files: dict[str, str] = {"glb": str(glb_path)}

    target_engine = spec.get("target_engine", "godot4")
    if export_fbx or target_engine in ("unity", "ue5"):
        fbx_path = out_dir / "asset.fbx"
        # Unity dùng -Z forward / Y up (mặc định mét); UE5 giữ Z up / -Y forward (mặc định cm *100 với FBX_SCALE_UNITS hoặc 1.0)
        if target_engine == "ue5":
            axis_fwd, axis_up, gscale = "-Y", "Z", 100.0
        else:
            axis_fwd, axis_up, gscale = "-Z", "Y", 1.0
        try:
            bpy.ops.export_scene.fbx(
                filepath=str(fbx_path),
                use_selection=True,
                global_scale=gscale,
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_ALL",
                axis_forward=axis_fwd,
                axis_up=axis_up,
                bake_anim=True,
                bake_anim_use_nla_strips=True,
            )
            out_files["fbx"] = str(fbx_path)
        except Exception:
            pass

    if export_blend:
        blend_path = out_dir / "source.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        out_files["blend"] = str(blend_path)

    roundtrip_delta = _roundtrip_check_glb(glb_path, expected_dims)
    val = run_gltf_validator(glb_path)

    return {
        "files": out_files,
        "roundtrip_bbox_delta_m": roundtrip_delta,
        "gltf_validator": val,
    }

