"""Bàn cân Toán học 3D Tầng 5A (evaluator_3d.py — chạy trong Blender 4.2, 0 token, cố định theo D-3D-6).

5+1 Cổng kiểm định cứng:
  - G1_PolyBudget: 0 < tris(LOD0) <= budget * 1.05; tris(UCX_Collider) <= 128
  - G2_TransformAndGround: scale == (1,1,1) ± 1e-4; |z_min| <= 0.002m; đồ thị phát hiện bộ phận lơ lửng (> 0.005m)
  - G3_IllegalOverlap: BVHTree + lấy mẫu thể tích giao cắt giữa các bộ phận rời (chặn > 15% trừ intentional_intersections)
  - G4_MeshAndUVHealth: 0 mặt suy biến; UV ngoài [0,1] == 0%; chồng lấn đảo UV < 2%; BaseColor mean_luminance > 0.02
  - G5_RigDeformationStress: unweighted_vertices == 0; <= 4 xương/đỉnh; xoay ±60° max_edge_stretch_ratio <= 3.5
  - G6_ArticulatedSweep: quét khớp cơ học 0..max_angle 5 bước kiểm tra lệch tâm quay & va chạm đâm xuyên
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any

import bmesh
import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

EDGE_STRETCH_LIMIT = 3.5
GROUND_EPS_M = 0.002
CONTACT_GAP_MAX_M = 0.005
ILLEGAL_OVERLAP_RATIO_MAX = 0.15


def _world_bmesh_and_bvh(obj: bpy.types.Object) -> tuple[bmesh.types.BMesh, BVHTree, list[Vector]]:
    deps = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(deps)
    mesh = eval_obj.to_mesh()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.transform(bm, matrix=obj.matrix_world, verts=bm.verts)
    bm.normal_update()
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    verts_world = [v.co.copy() for v in bm.verts]
    bvh = BVHTree.FromBMesh(bm)
    eval_obj.to_mesh_clear()
    return bm, bvh, verts_world


def _aabb_of_points(pts: list[Vector]) -> tuple[Vector, Vector]:
    if not pts:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))


def _aabb_gap(a_min: Vector, a_max: Vector, b_min: Vector, b_max: Vector) -> float:
    dx = max(0.0, a_min.x - b_max.x, b_min.x - a_max.x)
    dy = max(0.0, a_min.y - b_max.y, b_min.y - a_max.y)
    dz = max(0.0, a_min.z - b_max.z, b_min.z - a_max.z)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _min_surface_distance(
    bm_a: bmesh.types.BMesh,
    bvh_a: BVHTree,
    bm_b: bmesh.types.BMesh,
    bvh_b: BVHTree,
) -> float:
    if bvh_a.overlap(bvh_b):
        return 0.0
    best = 1e9
    step_a = max(1, len(bm_a.verts) // 256)
    for idx in range(0, len(bm_a.verts), step_a):
        _, _, _, dist = bvh_b.find_nearest(bm_a.verts[idx].co)
        if dist < best:
            best = dist
    step_fa = max(1, len(bm_a.faces) // 256)
    for idx in range(0, len(bm_a.faces), step_fa):
        _, _, _, dist = bvh_b.find_nearest(bm_a.faces[idx].calc_center_median())
        if dist < best:
            best = dist
    step_b = max(1, len(bm_b.verts) // 256)
    for idx in range(0, len(bm_b.verts), step_b):
        _, _, _, dist = bvh_a.find_nearest(bm_b.verts[idx].co)
        if dist < best:
            best = dist
    step_fb = max(1, len(bm_b.faces) // 256)
    for idx in range(0, len(bm_b.faces), step_fb):
        _, _, _, dist = bvh_a.find_nearest(bm_b.faces[idx].calc_center_median())
        if dist < best:
            best = dist
    return float(best)


def _point_inside_bvh(bvh: BVHTree, pt: Vector) -> bool:
    """Kiểm tra điểm nằm trong khối kín bằng ray_cast 3 trục + parity đa số."""
    hits_inside = 0
    dirs = (Vector((1.0, 0.13, 0.07)).normalized(), Vector((-0.11, 1.0, 0.17)).normalized(), Vector((0.09, -0.13, 1.0)).normalized())
    for d in dirs:
        count = 0
        cur = pt + d * 1e-5
        for _ in range(32):
            loc, _, _, _ = bvh.ray_cast(cur, d)
            if loc is None:
                break
            count += 1
            cur = loc + d * 1e-4
        if count % 2 == 1:
            hits_inside += 1
    return hits_inside >= 2


def _estimate_overlap_ratio(
    bm_a: bmesh.types.BMesh,
    bvh_a: BVHTree,
    a_min: Vector,
    a_max: Vector,
    bm_b: bmesh.types.BMesh,
    bvh_b: BVHTree,
    b_min: Vector,
    b_max: Vector,
) -> tuple[float, float]:
    """Trả về `(overlap_ratio_vs_smaller, overlap_vol_m3)` giữa hai mesh."""
    if not bvh_a.overlap(bvh_b):
        return 0.0, 0.0
    ix0, iy0, iz0 = max(a_min.x, b_min.x), max(a_min.y, b_min.y), max(a_min.z, b_min.z)
    ix1, iy1, iz1 = min(a_max.x, b_max.x), min(a_max.y, b_max.y), min(a_max.z, b_max.z)
    dx, dy, dz = ix1 - ix0, iy1 - iy0, iz1 - iz0
    if dx <= 1e-4 or dy <= 1e-4 or dz <= 1e-4:
        return 0.0, 0.0
    inter_box_vol = dx * dy * dz
    va = max(abs(bm_a.calc_volume(signed=False)), 1e-9)
    vb = max(abs(bm_b.calc_volume(signed=False)), 1e-9)
    n = 8
    in_both = 0
    total = n * n * n
    for i in range(n):
        x = ix0 + dx * (i + 0.5) / n
        for j in range(n):
            y = iy0 + dy * (j + 0.5) / n
            for k in range(n):
                z = iz0 + dz * (k + 0.5) / n
                pt = Vector((x, y, z))
                if _point_inside_bvh(bvh_a, pt) and _point_inside_bvh(bvh_b, pt):
                    in_both += 1
    overlap_vol = inter_box_vol * (in_both / total)
    ratio = overlap_vol / min(va, vb)
    return float(ratio), float(overlap_vol)


def _select_lod0_objects() -> list[bpy.types.Object]:
    """Lấy danh sách đối tượng MESH của tài sản chính (ưu tiên Collection LOD0 nếu có)."""
    lod0_col = bpy.data.collections.get("LOD0")
    if lod0_col is not None:
        objs = [o for o in lod0_col.all_objects if o.type == "MESH"]
        if objs:
            return objs
    excluded_prefixes = ("UCX_", "_Preview", "_Axis")
    excluded_cols = {"HighPoly", "LOD1", "LOD2", "UCX_Collider", "_PreviewHelpers"}
    objs = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if o.name.startswith(excluded_prefixes):
            continue
        if any(c.name in excluded_cols for c in o.users_collection):
            continue
        objs.append(o)
    return objs


def _rasterize_uv_stats(objs: list[bpy.types.Object]) -> tuple[float, float, int]:
    """Đo tỷ lệ UV ngoài [0,1], tỷ lệ chồng lấn đảo UV trên lưới 128x128, và số mặt suy biến."""
    res = 128
    grid = [-1] * (res * res)
    face_global_id = 0
    covered_cells = 0
    overlap_cells = 0
    total_uvs = 0
    oob_uvs = 0
    degenerate_faces = 0

    for obj in objs:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.transform(bm, matrix=obj.matrix_world, verts=bm.verts)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        uv_layer = bm.loops.layers.uv.active
        for f in bm.faces:
            if f.calc_area() <= 1e-10:
                degenerate_faces += 1
            if uv_layer is None:
                continue
            pts = []
            for loop in f.loops:
                u, v = loop[uv_layer].uv.x, loop[uv_layer].uv.y
                total_uvs += 1
                if u < -1e-4 or u > 1.0 + 1e-4 or v < -1e-4 or v > 1.0 + 1e-4:
                    oob_uvs += 1
                pts.append((u, v))
            if len(pts) != 3:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            minx = max(0, int(math.floor(min(xs) * res)))
            maxx = min(res - 1, int(math.floor(max(xs) * res)))
            miny = max(0, int(math.floor(min(ys) * res)))
            maxy = min(res - 1, int(math.floor(max(ys) * res)))
            (x0, y0), (x1, y1), (x2, y2) = pts
            denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
            if abs(denom) < 1e-10:
                face_global_id += 1
                continue
            for py in range(miny, maxy + 1):
                cy = (py + 0.5) / res
                for px in range(minx, maxx + 1):
                    cx = (px + 0.5) / res
                    l1 = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / denom
                    l2 = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / denom
                    l3 = 1.0 - l1 - l2
                    if l1 > 0.02 and l2 > 0.02 and l3 > 0.02:
                        idx = py * res + px
                        prev = grid[idx]
                        if prev == -1:
                            grid[idx] = face_global_id
                            covered_cells += 1
                        elif prev != face_global_id and prev != -2:
                            grid[idx] = -2
                            overlap_cells += 1
            face_global_id += 1
        bm.free()

    oob_ratio = (oob_uvs / total_uvs) if total_uvs > 0 else 0.0
    overlap_ratio = (overlap_cells / covered_cells) if covered_cells > 0 else 0.0
    return float(oob_ratio), float(overlap_ratio), degenerate_faces


def _measure_basecolor_luminance(objs: list[bpy.types.Object], baked_basecolor_path: str | None = None) -> tuple[float, float]:
    """Đo độ sáng trung bình và tỷ lệ texel đen trên BaseColor (từ ảnh bake hoặc Principled BSDF)."""
    if baked_basecolor_path:
        try:
            img = bpy.data.images.load(baked_basecolor_path, check_existing=True)
            px = list(img.pixels[:])
            if len(px) >= 4:
                step = max(1, (len(px) // 4) // 4096)
                lums = []
                black_cnt = 0
                all_lums = []
                all_black_cnt = 0
                for i in range(0, len(px), 4 * step):
                    lum = 0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]
                    all_lums.append(lum)
                    if lum < 0.015:
                        all_black_cnt += 1
                    if px[i + 3] >= 0.5:
                        lums.append(lum)
                        if lum < 0.015:
                            black_cnt += 1
                chosen = lums if lums else all_lums
                chosen_black = black_cnt if lums else all_black_cnt
                if chosen:
                    return float(sum(chosen) / len(chosen)), float(chosen_black / len(chosen))
        except Exception:
            pass

    lums = []
    for obj in objs:
        mats = [ms.material for ms in obj.material_slots if ms.material is not None]
        if not mats:
            lums.append(0.5)  # Default Blender grey ~0.5
            continue
        for mat in mats:
            if not mat.use_nodes or not mat.node_tree:
                c = mat.diffuse_color
                lums.append(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])
                continue
            bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is None:
                lums.append(0.5)
                continue
            sock = bsdf.inputs.get("Base Color")
            if sock is None:
                lums.append(0.5)
            elif sock.is_linked:
                # Nếu nối Image Texture, thử đọc pixel; nếu procedural node coi như hợp lệ > 0.2
                src_node = sock.links[0].from_node
                if src_node.type == "TEX_IMAGE" and src_node.image is not None and len(src_node.image.pixels) >= 4:
                    p = list(src_node.image.pixels[:])
                    step = max(1, (len(p) // 4) // 1024)
                    sample = [0.2126 * p[i] + 0.7152 * p[i + 1] + 0.0722 * p[i + 2] for i in range(0, len(p), 4 * step)]
                    lums.append(sum(sample) / len(sample))
                else:
                    lums.append(0.35)
            else:
                c = sock.default_value
                lums.append(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])
    mean_lum = sum(lums) / len(lums) if lums else 0.5
    black_ratio = 1.0 if mean_lum <= 0.015 else 0.0
    return float(mean_lum), float(black_ratio)


def _eval_g5_rig_stress(objs: list[bpy.types.Object], spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    arm_objs = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    rig_type = spec.get("rig_type") or ("static" if spec.get("archetype") in (None, "static_prop", "modular_kit") else "mechanical")
    if not arm_objs or rig_type == "static":
        return {
            "gate": "G5_RigDeformationStress",
            "passed": True,
            "severity": "hard",
            "summary": "Bỏ qua G5 (tài sản tĩnh không có Armature)",
            "metrics": {"unweighted_vertices": 0, "max_bones_per_vertex": 0, "max_edge_stretch_ratio": 1.0},
        }, {"unweighted_vertices": 0, "max_bones_per_vertex": 0, "max_edge_stretch_ratio": 1.0}

    arm = arm_objs[0]
    bone_names = {b.name for b in arm.data.bones}
    skinned_objs = [o for o in objs if any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers)]
    if not skinned_objs:
        skinned_objs = objs

    unweighted = 0
    max_bones_per_v = 0
    rest_edge_lens: dict[tuple[str, int], float] = {}
    deps = bpy.context.evaluated_depsgraph_get()
    for o in skinned_objs:
        vgroups = {vg.index: vg.name for vg in o.vertex_groups if vg.name in bone_names}
        eval_o = o.evaluated_get(deps)
        mesh_eval = eval_o.to_mesh()
        for idx, edge in enumerate(mesh_eval.edges):
            v0, v1 = mesh_eval.vertices[edge.vertices[0]].co, mesh_eval.vertices[edge.vertices[1]].co
            rest_edge_lens[(o.name, idx)] = (v1 - v0).length
        eval_o.to_mesh_clear()

        for v in o.data.vertices:
            bones = [g for g in v.groups if g.group in vgroups and g.weight > 1e-4]
            if not bones:
                unweighted += 1
            if len(bones) > max_bones_per_v:
                max_bones_per_v = len(bones)

    # Xoay thử từng pose bone +-60 độ (bỏ qua root)
    saved_rots = {pb.name: (pb.rotation_mode, pb.rotation_euler.copy(), pb.rotation_quaternion.copy()) for pb in arm.pose.bones}
    max_stretch = 1.0
    worst_bone = ""
    try:
        for pb in arm.pose.bones:
            if pb.name.lower() == "root":
                continue
            for sign in (1.0, -1.0):
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (math.radians(60.0) * sign, 0.0, 0.0)
                bpy.context.view_layer.update()
                deps = bpy.context.evaluated_depsgraph_get()
                for o in skinned_objs:
                    eval_o = o.evaluated_get(deps)
                    mesh_eval = eval_o.to_mesh()
                    for idx, edge in enumerate(mesh_eval.edges):
                        rest_len = rest_edge_lens.get((o.name, idx), 0.0)
                        if rest_len <= 1e-6:
                            continue
                        cur_len = (mesh_eval.vertices[edge.vertices[1]].co - mesh_eval.vertices[edge.vertices[0]].co).length
                        ratio = cur_len / rest_len
                        if ratio > max_stretch:
                            max_stretch = ratio
                            worst_bone = pb.name
                    eval_o.to_mesh_clear()
                pb.rotation_euler = (0.0, 0.0, 0.0)
    finally:
        for name, (mode, eul, quat) in saved_rots.items():
            pb = arm.pose.bones.get(name)
            if pb:
                pb.rotation_mode = mode
                pb.rotation_euler = eul
                pb.rotation_quaternion = quat
        bpy.context.view_layer.update()

    passed = unweighted == 0 and max_bones_per_v <= 4 and max_stretch <= EDGE_STRETCH_LIMIT
    summary = (
        f"unweighted={unweighted}, max_bones/v={max_bones_per_v}, max_edge_stretch={max_stretch:.2f} (bone={worst_bone or 'none'})"
    )
    return {
        "gate": "G5_RigDeformationStress",
        "passed": passed,
        "severity": "hard",
        "summary": summary,
        "metrics": {
            "unweighted_vertices": unweighted,
            "max_bones_per_vertex": max_bones_per_v,
            "max_edge_stretch_ratio": round(max_stretch, 4),
            "worst_bone": worst_bone,
        },
    }, {
        "unweighted_vertices": unweighted,
        "max_bones_per_vertex": max_bones_per_v,
        "max_edge_stretch_ratio": round(max_stretch, 4),
    }


def _eval_g6_articulated_sweep(objs: list[bpy.types.Object], spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    joints = spec.get("joints") or []
    if not joints:
        return {
            "gate": "G6_ArticulatedSweep",
            "passed": True,
            "severity": "hard",
            "summary": "Không có khớp cơ học cần quét động học",
            "metrics": {"joints_checked": 0},
        }, []

    allowed_pairs = {tuple(sorted((str(a), str(b)))) for a, b in (spec.get("intentional_intersections") or [])}
    failures: list[dict[str, Any]] = []
    obj_map = {o.name: o for o in objs}
    for j in joints:
        part_id = j["part_id"]
        obj = obj_map.get(part_id)
        if obj is None:
            failures.append({"joint": j["name"], "reason": f"Thiếu mesh bộ phận khớp {part_id!r}"})
            continue
        expected_origin = Vector(j.get("origin", (0.0, 0.0, 0.0)))
        world_origin = obj.matrix_world.translation
        origin_err = (world_origin - expected_origin).length
        if origin_err > 0.03:
            failures.append({
                "joint": j["name"],
                "part_id": part_id,
                "reason": f"Tâm quay Origin bị lệch {origin_err * 1000:.1f} mm so với khai báo {tuple(expected_origin)}",
                "origin_error_m": round(origin_err, 4),
            })
            continue

        # Quét 5 bước góc hoặc tịnh tiến, kiểm tra đỉnh không văng khỏi khung và không đâm xuyên vào parent/vật cản khác
        other_objs = [
            o for o in objs
            if o is not obj and tuple(sorted((obj.name, o.name))) not in allowed_pairs
        ]
        saved_mat = obj.matrix_world.copy()
        axis = Vector(j.get("axis", (0.0, 0.0, 1.0)))
        if axis.length < 1e-6:
            axis = Vector((0.0, 0.0, 1.0))
        axis.normalize()
        jtype = j.get("joint_type", "hinge")
        steps = 5
        try:
            hit_collision = False
            for step_i in range(steps + 1):
                t = step_i / steps
                if jtype == "slider":
                    slide = float(j.get("max_slide_m", 0.3)) * t
                    obj.matrix_world = Matrix.Translation(axis * slide) @ saved_mat
                else:
                    min_deg = float(j.get("min_angle_deg", 0.0))
                    max_deg = float(j.get("max_angle_deg", 90.0))
                    ang = math.radians(min_deg + (max_deg - min_deg) * t)
                    rot = Matrix.Rotation(ang, 4, axis)
                    pivot = saved_mat.translation
                    obj.matrix_world = Matrix.Translation(pivot) @ rot @ Matrix.Translation(-pivot) @ saved_mat
                bpy.context.view_layer.update()

                # Kiểm tra tâm quay không văng ra xa khỏi trục
                drift = (obj.matrix_world.translation - expected_origin).length
                if jtype != "slider" and drift > 0.03:
                    failures.append({
                        "joint": j["name"],
                        "step": step_i,
                        "reason": f"Khớp văng lệch tâm quay {drift * 1000:.1f} mm ở bước {step_i}",
                    })
                    break
                if step_i > 0 and other_objs:
                    bm_c, bvh_c, pts_c = _world_bmesh_and_bvh(obj)
                    try:
                        c_min, c_max = _aabb_of_points(pts_c)
                        for other_o in other_objs:
                            bm_p, bvh_p, pts_p = _world_bmesh_and_bvh(other_o)
                            try:
                                p_min, p_max = _aabb_of_points(pts_p)
                                if _aabb_gap(p_min, p_max, c_min, c_max) > 0.0:
                                    continue
                                ratio, _ = _estimate_overlap_ratio(bm_p, bvh_p, p_min, p_max, bm_c, bvh_c, c_min, c_max)
                                if ratio > ILLEGAL_OVERLAP_RATIO_MAX:
                                    failures.append({
                                        "joint": j["name"],
                                        "step": step_i,
                                        "collider": other_o.name,
                                        "overlap_ratio": round(ratio, 3),
                                        "reason": f"Khớp {j['name']} đâm xuyên {ratio * 100:.1f}% vào {other_o.name} ở góc bước {step_i}",
                                    })
                                    hit_collision = True
                                    break
                            finally:
                                bm_p.free()
                    finally:
                        bm_c.free()
                if hit_collision:
                    break
        finally:
            obj.matrix_world = saved_mat
            bpy.context.view_layer.update()

    passed = len(failures) == 0
    return {
        "gate": "G6_ArticulatedSweep",
        "passed": passed,
        "severity": "hard",
        "summary": "Khớp cơ học quét 5 bước sạch" if passed else failures[0]["reason"],
        "metrics": {"joints_checked": len(joints), "failures": failures},
    }, failures


def evaluate_scene(
    spec: dict[str, Any],
    *,
    baked_basecolor_path: str | None = None,
    disabled_gates: set[str] | None = None,
) -> dict[str, Any]:
    """Chấm điểm Bàn cân 3D Tầng 5A trên cảnh Blender hiện tại."""
    disabled = disabled_gates or set()
    objs = _select_lod0_objects()
    if not objs:
        return {
            "passed": False,
            "tris_lod0": 0,
            "failure_reasons": ["Không tìm thấy mesh nào trong LOD0"],
            "gates": [],
        }

    # Đếm tam giác LOD0 / LOD1 / LOD2 / UCX_Collider
    def _col_tris(col_name: str, fallback_objs: list[bpy.types.Object] | None = None) -> int:
        col = bpy.data.collections.get(col_name)
        target = [o for o in col.all_objects if o.type == "MESH"] if col else (fallback_objs or [])
        total = 0
        deps = bpy.context.evaluated_depsgraph_get()
        for o in target:
            eo = o.evaluated_get(deps)
            m = eo.to_mesh()
            m.calc_loop_triangles()
            total += len(m.loop_triangles)
            eo.to_mesh_clear()
        return total

    tris_lod0 = _col_tris("LOD0", objs)
    tris_lod1 = _col_tris("LOD1")
    tris_lod2 = _col_tris("LOD2")
    ucx_objs = [o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith("UCX_")]
    tris_collider = _col_tris("UCX_Collider", ucx_objs)

    budget = int(spec.get("poly_budget_lod0", 6000))
    # G1_PolyBudget
    g1_ok = (0 < tris_lod0 <= int(budget * 1.05)) and (tris_collider <= 128)
    if "G1_PolyBudget" in disabled:
        g1_ok = True
    g1 = {
        "gate": "G1_PolyBudget",
        "passed": g1_ok,
        "severity": "hard",
        "summary": f"LOD0={tris_lod0}/{budget} tris, Collider={tris_collider}/128 tris",
        "metrics": {"tris_lod0": tris_lod0, "budget": budget, "tris_collider": tris_collider},
    }

    # Thu thập BMesh + BVH + AABB từng phần trong LOD0
    part_data = []
    all_pts: list[Vector] = []
    scale_bad = []
    for o in objs:
        s = o.scale
        if abs(s.x - 1.0) > 1e-4 or abs(s.y - 1.0) > 1e-4 or abs(s.z - 1.0) > 1e-4:
            scale_bad.append({"part": o.name, "scale": [round(s.x, 4), round(s.y, 4), round(s.z, 4)]})
        bm, bvh, pts = _world_bmesh_and_bvh(o)
        p_min, p_max = _aabb_of_points(pts)
        all_pts.extend(pts)
        part_data.append({"obj": o, "name": o.name, "bm": bm, "bvh": bvh, "min": p_min, "max": p_max})

    try:
        scene_min, scene_max = _aabb_of_points(all_pts)
        dims_measured = (
            round(scene_max.x - scene_min.x, 4),
            round(scene_max.y - scene_min.y, 4),
            round(scene_max.z - scene_min.z, 4),
        )
        z_min = float(scene_min.z)

        # G2_TransformAndGround + Đồ thị Floating Part Check
        floating_ok_set = set(spec.get("floating_ok") or [])
        n_parts = len(part_data)
        adj: list[list[int]] = [[] for _ in range(n_parts)]
        min_gap_to_other = [1e9] * n_parts

        for i in range(n_parts):
            for j in range(i + 1, n_parts):
                box_gap = _aabb_gap(part_data[i]["min"], part_data[i]["max"], part_data[j]["min"], part_data[j]["max"])
                if box_gap > 0.5:
                    dist = box_gap
                else:
                    dist = _min_surface_distance(part_data[i]["bm"], part_data[i]["bvh"], part_data[j]["bm"], part_data[j]["bvh"])
                if dist < min_gap_to_other[i]:
                    min_gap_to_other[i] = dist
                if dist < min_gap_to_other[j]:
                    min_gap_to_other[j] = dist
                if dist <= CONTACT_GAP_MAX_M:
                    adj[i].append(j)
                    adj[j].append(i)

        grounded_indices = [i for i, pd in enumerate(part_data) if pd["min"].z <= CONTACT_GAP_MAX_M]
        visited = set(grounded_indices)
        q = deque(grounded_indices)
        while q:
            u = q.popleft()
            for v in adj[u]:
                if v not in visited:
                    visited.add(v)
                    q.append(v)

        floating_parts: list[dict[str, Any]] = []
        for i, pd in enumerate(part_data):
            if i not in visited and pd["name"] not in floating_ok_set:
                gap_to_floor = max(0.0, float(pd["min"].z))
                nearest_gap = min(gap_to_floor, min_gap_to_other[i]) if n_parts > 1 else gap_to_floor
                floating_parts.append({
                    "part_id": pd["name"],
                    "z_min_m": round(float(pd["min"].z), 4),
                    "gap_mm": round(nearest_gap * 1000.0, 2),
                })

        ground_ok = abs(z_min) <= GROUND_EPS_M
        g2_ok = ground_ok and (len(scale_bad) == 0) and (len(floating_parts) == 0)
        if "G2_TransformAndGround" in disabled:
            g2_ok = True
        g2_reasons = []
        if not ground_ok:
            g2_reasons.append(f"z_min={z_min:.4f}m (yêu cầu |z_min| <= {GROUND_EPS_M}m)")
        if scale_bad:
            g2_reasons.append(f"scale chưa apply: {scale_bad}")
        if floating_parts:
            fp_desc = ", ".join(f"{fp['part_id']} (hở {fp['gap_mm']}mm)" for fp in floating_parts)
            g2_reasons.append(f"Bộ phận bay lơ lửng: {fp_desc}")
        g2 = {
            "gate": "G2_TransformAndGround",
            "passed": g2_ok,
            "severity": "hard",
            "summary": "Chạm sàn Z=0, scale=1, liên thông vững" if g2_ok else "; ".join(g2_reasons),
            "metrics": {"z_min_m": round(z_min, 4), "floating_parts": floating_parts, "scale_bad": scale_bad},
        }

        # G3_IllegalOverlap (BVHTree + volumetric sampling)
        allowed_pairs = {tuple(sorted((str(a), str(b)))) for a, b in (spec.get("intentional_intersections") or [])}
        illegal_overlaps: list[dict[str, Any]] = []
        for i in range(n_parts):
            for j in range(i + 1, n_parts):
                pair_key = tuple(sorted((part_data[i]["name"], part_data[j]["name"])))
                if pair_key in allowed_pairs:
                    continue
                if _aabb_gap(part_data[i]["min"], part_data[i]["max"], part_data[j]["min"], part_data[j]["max"]) > 0.0:
                    continue
                ratio, vol = _estimate_overlap_ratio(
                    part_data[i]["bm"], part_data[i]["bvh"], part_data[i]["min"], part_data[i]["max"],
                    part_data[j]["bm"], part_data[j]["bvh"], part_data[j]["min"], part_data[j]["max"],
                )
                if ratio > ILLEGAL_OVERLAP_RATIO_MAX:
                    illegal_overlaps.append({
                        "part_a": part_data[i]["name"],
                        "part_b": part_data[j]["name"],
                        "overlap_ratio": round(ratio, 4),
                        "overlap_pct": round(ratio * 100.0, 1),
                        "overlap_vol_m3": round(vol, 6),
                    })

        g3_ok = len(illegal_overlaps) == 0
        if "G3_IllegalOverlap" in disabled:
            g3_ok = True
        g3 = {
            "gate": "G3_IllegalOverlap",
            "passed": g3_ok,
            "severity": "hard",
            "summary": (
                "Không có giao cắt thể tích trái phép"
                if g3_ok
                else f"Đâm xuyên {illegal_overlaps[0]['part_a']}<->{illegal_overlaps[0]['part_b']} ({illegal_overlaps[0]['overlap_pct']}%)"
            ),
            "metrics": {"illegal_overlaps": illegal_overlaps},
        }
    finally:
        for pd in part_data:
            pd["bm"].free()

    # G4_MeshAndUVHealth
    oob_ratio, uv_overlap, degen = _rasterize_uv_stats(objs)
    mean_lum, black_ratio = _measure_basecolor_luminance(objs, baked_basecolor_path)
    uv_mirror_ok = bool(spec.get("uv_mirror_overlap_ok", False))
    unlit_black_ok = bool(spec.get("unlit_black_ok", False))
    g4_reasons = []
    if degen > 0:
        g4_reasons.append(f"degenerate_faces={degen}")
    if oob_ratio > 0.0:
        g4_reasons.append(f"uv_oob={oob_ratio*100:.2f}%")
    if not uv_mirror_ok and uv_overlap >= 0.02:
        g4_reasons.append(f"uv_overlap={uv_overlap*100:.2f}% >= 2%")
    if not unlit_black_ok and mean_lum <= 0.02:
        g4_reasons.append(f"basecolor_mean_luminance={mean_lum:.4f} <= 0.02 (đen đặc)")
    g4_ok = len(g4_reasons) == 0
    if "G4_MeshAndUVHealth" in disabled:
        g4_ok = True
    g4 = {
        "gate": "G4_MeshAndUVHealth",
        "passed": g4_ok,
        "severity": "hard",
        "summary": "Lưới, UV và BaseColor đạt chuẩn" if g4_ok else "; ".join(g4_reasons),
        "metrics": {
            "degenerate_faces": degen,
            "uv_out_of_bounds_ratio": round(oob_ratio, 4),
            "uv_overlap_ratio": round(uv_overlap, 4),
            "basecolor_mean_luminance": round(mean_lum, 4),
            "basecolor_black_texel_ratio": round(black_ratio, 4),
        },
    }

    # G5_RigDeformationStress
    g5, g5_m = _eval_g5_rig_stress(objs, spec)
    if "G5_RigDeformationStress" in disabled:
        g5["passed"] = True

    # G6_ArticulatedSweep
    g6, sweep_failures = _eval_g6_articulated_sweep(objs, spec)
    if "G6_ArticulatedSweep" in disabled:
        g6["passed"] = True

    gates = [g1, g2, g3, g4, g5, g6]
    failure_reasons = [f"[{g['gate']}] {g['summary']}" for g in gates if not g["passed"]]
    overall = len(failure_reasons) == 0
    first_scale = objs[0].scale
    scene_table = []
    for _o in objs:
        _c = [_o.matrix_world @ Vector(_v) for _v in _o.bound_box]
        _mn = [round(min(c[i] for c in _c), 4) for i in range(3)]
        _mx = [round(max(c[i] for c in _c), 4) for i in range(3)]
        _w = _o.matrix_world.translation
        scene_table.append({"name": _o.name, "bbox_min": _mn, "bbox_max": _mx,
                            "origin": [round(_w.x, 4), round(_w.y, 4), round(_w.z, 4)]})
    return {
        "passed": overall,
        "scene_table": scene_table,
        "tris_lod0": tris_lod0,
        "tris_lod1": tris_lod1,
        "tris_lod2": tris_lod2,
        "tris_collider": tris_collider,
        "dimensions_measured_m": dims_measured,
        "z_min_m": round(z_min, 4),
        "scale_measured": (round(first_scale.x, 4), round(first_scale.y, 4), round(first_scale.z, 4)),
        "floating_parts": floating_parts,
        "illegal_overlaps": illegal_overlaps,
        "degenerate_faces": degen,
        "uv_out_of_bounds_ratio": round(oob_ratio, 4),
        "uv_overlap_ratio": round(uv_overlap, 4),
        "basecolor_mean_luminance": round(mean_lum, 4),
        "basecolor_black_texel_ratio": round(black_ratio, 4),
        "unweighted_vertices": g5_m["unweighted_vertices"],
        "max_bones_per_vertex": g5_m["max_bones_per_vertex"],
        "max_edge_stretch_ratio": g5_m["max_edge_stretch_ratio"],
        "articulated_sweep_failures": sweep_failures,
        "roundtrip_bbox_delta_m": 0.0,
        "validator_errors": 0,
        "validator_warnings": 0,
        "gates": gates,
        "failure_reasons": failure_reasons,
    }
