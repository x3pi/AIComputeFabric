"""Thực thi an toàn mã bpy do LLM sinh ra trong cảnh sạch Blender 4.2 (R4, D-3D-2)."""
from __future__ import annotations

import traceback
from typing import Any

import bpy
import mathutils


def reset_scene_empty() -> None:
    """Đưa Blender về cảnh rỗng hoàn toàn giữa các lệnh gọi trong warm worker."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for datablocks in (
        bpy.data.objects,
        bpy.data.meshes,
        bpy.data.materials,
        bpy.data.images,
        bpy.data.cameras,
        bpy.data.lights,
        bpy.data.armatures,
        bpy.data.actions,
        bpy.data.collections,
    ):
        for item in list(datablocks):
            try:
                datablocks.remove(item, do_unlink=True)
            except Exception:
                pass


def apply_b42_compatibility_shims() -> None:
    """Chặn nhẹ một số gọi hàm xóa ở Blender 4.2 nếu mã vẫn cố gọi, hoặc chuẩn hóa scene."""
    if bpy.context.scene.world is None:
        w = bpy.data.worlds.new("FoundryWorld")
        bpy.context.scene.world = w


def collect_scene_summary() -> dict[str, Any]:
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    parts = []
    min_pt = [1e9, 1e9, 1e9]
    max_pt = [-1e9, -1e9, -1e9]
    total_tris = 0
    for obj in meshes:
        deps = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(deps)
        mesh = eval_obj.to_mesh()
        mesh.calc_loop_triangles()
        tris = len(mesh.loop_triangles)
        total_tris += tris
        corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
        xs = [c.x for c in corners]
        ys = [c.y for c in corners]
        zs = [c.z for c in corners]
        for i, vals in enumerate((xs, ys, zs)):
            if vals:
                min_pt[i] = min(min_pt[i], min(vals))
                max_pt[i] = max(max_pt[i], max(vals))
        parts.append({
            "name": obj.name,
            "tris": tris,
            "bbox_min": [round(min(xs), 4), round(min(ys), 4), round(min(zs), 4)] if xs else [0, 0, 0],
            "bbox_max": [round(max(xs), 4), round(max(ys), 4), round(max(zs), 4)] if xs else [0, 0, 0],
        })
        eval_obj.to_mesh_clear()

    dims = (
        [round(max_pt[i] - min_pt[i], 4) for i in range(3)]
        if meshes and min_pt[0] < 1e8
        else [0.0, 0.0, 0.0]
    )
    return {
        "mesh_count": len(meshes),
        "armature_count": len(armatures),
        "total_tris": total_tris,
        "dimensions_m": dims,
        "parts": parts,
    }


def execute_generated_bpy(code: str) -> dict[str, Any]:
    """Biên dịch và chạy mã bpy trong namespace cô lập, trả về kết quả và danh sách đối tượng."""
    reset_scene_empty()
    apply_b42_compatibility_shims()
    if not code or not code.strip():
        return {"ok": False, "error": "Mã bpy rỗng", "scene": collect_scene_summary()}
    namespace: dict[str, Any] = {
        "__name__": "__main__",
        "bpy": bpy,
    }
    try:
        compiled = compile(code, "<generated_bpy>", "exec")
        exec(compiled, namespace, namespace)  # noqa: S102 — chạy bên trong Docker --network=none --cap-drop=ALL
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
            "scene": collect_scene_summary(),
        }

    # Đảm bảo thoát Edit mode nếu coder quên chuyển về OBJECT
    try:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

    summary = collect_scene_summary()
    if summary["mesh_count"] == 0 or summary["total_tris"] == 0:
        return {
            "ok": False,
            "error": "Mã bpy chạy không lỗi nhưng không sinh ra mesh nào có tam giác",
            "scene": summary,
        }
    return {"ok": True, "scene": summary}

