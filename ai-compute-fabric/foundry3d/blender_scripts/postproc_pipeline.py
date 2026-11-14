"""Chặng 3 — Hậu kỳ kỹ thuật tự động trong Blender 4.2: Z=0, Apply Scale, Clean Mesh, LOD0..2, UCX, UV & PBR Bake."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import bmesh
import bpy
from mathutils import Vector


def _ensure_collection(name: str, parent: bpy.types.Collection | None = None) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(col)
    return col


def _move_to_collection(obj: bpy.types.Object, col: bpy.types.Collection) -> None:
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    col.objects.link(obj)


def _apply_transforms_preserving_pivot(obj: bpy.types.Object, preserve_pivot: bool) -> None:
    """Áp scale/rotation về (1,1,1)/(0,0,0). Nếu preserve_pivot=True thì giữ nguyên Origin bản lề của khớp cơ học."""
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=not preserve_pivot, rotation=True, scale=True)


def _scene_bbox_world(objs: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    xs, ys, zs = [], [], []
    for o in objs:
        for corner in o.bound_box:
            wc = o.matrix_world @ Vector(corner)
            xs.append(wc.x)
            ys.append(wc.y)
            zs.append(wc.z)
    if not xs:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))


def _clean_single_mesh(obj: bpy.types.Object, keep_small: bool = False) -> None:
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=0.0001)
    # Xóa đỉnh/cạnh mồ côi
    loose_verts = [v for v in bm.verts if not v.link_faces]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
    loose_edges = [e for e in bm.edges if not e.link_faces]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
    # Vá lỗ thủng nhỏ (<= 16 cạnh)
    open_edges = [e for e in bm.edges if len(e.link_faces) == 1]
    if open_edges:
        try:
            bmesh.ops.holes_fill(bm, edges=open_edges, sides=16)
        except Exception:
            pass
    # Đồng bộ pháp tuyến hướng ra ngoài
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _mesh_tris(obj: bpy.types.Object) -> int:
    deps = bpy.context.evaluated_depsgraph_get()
    eo = obj.evaluated_get(deps)
    m = eo.to_mesh()
    m.calc_loop_triangles()
    n = len(m.loop_triangles)
    eo.to_mesh_clear()
    return n


def _mesh_volume(obj: bpy.types.Object) -> float:
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    vol = abs(bm.calc_volume(signed=False))
    bm.free()
    return max(float(vol), 1e-9)


def _decimate_object(obj: bpy.types.Object, ratio: float, organic: bool = False) -> None:
    """Rút gọn đa giác với fallback an toàn theo R7."""
    ratio = max(0.05, min(1.0, float(ratio)))
    if organic:
        orig_vol = _mesh_volume(obj)
        backup = obj.data.copy()
        try:
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            target_faces = max(100, int((_mesh_tris(obj) * ratio) // 2))
            bpy.ops.object.quadriflow_remesh(target_faces=target_faces)
            new_vol = _mesh_volume(obj)
            if abs(new_vol - orig_vol) / orig_vol <= 0.15:
                bpy.data.meshes.remove(backup)
                return
        except Exception:
            pass
        # Fallback R7 về Collapse Decimate nếu Quadriflow lệch > 15% thể tích hoặc lỗi
        old = obj.data
        obj.data = backup
        bpy.data.meshes.remove(old)

    # Planar (giữ phẳng cho hard-surface) + Collapse
    mod_p = obj.modifiers.new(name="FoundryPlanar", type="DECIMATE")
    mod_p.decimate_type = "DISSOLVE"
    mod_p.angle_limit = math.radians(5.0)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        bpy.ops.object.modifier_apply(modifier=mod_p.name)
    except Exception:
        obj.modifiers.remove(mod_p)

    # Nếu vẫn vượt quá tỷ lệ mong muốn mới dùng Collapse
    mod_c = obj.modifiers.new(name="FoundryCollapse", type="DECIMATE")
    mod_c.decimate_type = "COLLAPSE"
    mod_c.ratio = ratio
    try:
        bpy.ops.object.modifier_apply(modifier=mod_c.name)
    except Exception:
        if mod_c.name in obj.modifiers:
            obj.modifiers.remove(mod_c)


def _obj_uv_has_self_overlap(obj: bpy.types.Object) -> bool:
    """Kiểm tra nhanh trên lưới 48x48 xem UV nội bộ 1 mesh có tự đè chồng lên nhau (ví dụ sau object.join) không."""
    uv_layer_data = obj.data.uv_layers.active
    if uv_layer_data is None or len(uv_layer_data.data) == 0:
        return True
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    uvl = bm.loops.layers.uv.active
    if uvl is None:
        bm.free()
        return True
    res = 48
    grid = [-1] * (res * res)
    covered = 0
    overlap = 0
    for fid, f in enumerate(bm.faces):
        pts = [(lp[uvl].uv.x, lp[uvl].uv.y) for lp in f.loops]
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
            continue
        for py in range(miny, maxy + 1):
            cy = (py + 0.5) / res
            for px in range(minx, maxx + 1):
                cx = (px + 0.5) / res
                l1 = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / denom
                l2 = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / denom
                l3 = 1.0 - l1 - l2
                if l1 > 0.04 and l2 > 0.04 and l3 > 0.04:
                    idx = py * res + px
                    prev = grid[idx]
                    if prev == -1:
                        grid[idx] = fid
                        covered += 1
                    elif prev != fid and prev != -2:
                        grid[idx] = -2
                        overlap += 1
    bm.free()
    return covered == 0 or (overlap / covered) >= 0.015


def _ensure_smart_uv(objs: list[bpy.types.Object]) -> None:
    """Trải UV sạch trong [0, 1] bằng Smart UV Project (bắt buộc đảm bảo không chồng lấn trong từng mesh và liên mesh)."""
    for obj in objs:
        has_uv = len(obj.data.uv_layers) > 0
        need_unwrap = not has_uv
        if has_uv:
            uvs = obj.data.uv_layers.active.data
            if len(uvs) == 0:
                need_unwrap = True
            else:
                sample = [d.uv for d in uvs[:256]]
                xs = [u.x for u in sample]
                ys = [u.y for u in sample]
                if max(xs) - min(xs) < 1e-4 or max(xs) > 1.0001 or min(xs) < -0.0001 or max(ys) > 1.0001 or min(ys) < -0.0001:
                    need_unwrap = True
                elif _obj_uv_has_self_overlap(obj):
                    need_unwrap = True
        if need_unwrap:
            if not obj.data.uv_layers:
                obj.data.uv_layers.new(name="UVMap")
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02, area_weight=1.0, scale_to_bounds=True)
            bpy.ops.object.mode_set(mode="OBJECT")

    # Đảm bảo nhiều object cùng chia sẻ 1 atlas [0,1] mà không đè lên nhau (pack islands theo ô lưới)
    n = len(objs)
    if n > 1:
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        margin = 0.015
        for idx, obj in enumerate(objs):
            uv_layer = obj.data.uv_layers.active
            if uv_layer is None:
                continue
            c = idx % cols
            r = idx // cols
            cw = (1.0 - margin * (cols + 1)) / cols
            ch = (1.0 - margin * (rows + 1)) / rows
            ox = margin + c * (cw + margin)
            oy = margin + r * (ch + margin)
            for loop_uv in uv_layer.data:
                u = max(0.0, min(1.0, loop_uv.uv.x))
                v = max(0.0, min(1.0, loop_uv.uv.y))
                loop_uv.uv.x = ox + u * cw
                loop_uv.uv.y = oy + v * ch


def _create_convex_collider(lod0_objs: list[bpy.types.Object], asset_name: str, col: bpy.types.Collection) -> bpy.types.Object:
    bm = bmesh.new()
    for o in lod0_objs:
        tmp_bm = bmesh.new()
        tmp_bm.from_mesh(o.data)
        bmesh.ops.transform(tmp_bm, matrix=o.matrix_world, verts=tmp_bm.verts)
        vert_map = {}
        for v in tmp_bm.verts:
            vert_map[v] = bm.verts.new(v.co)
        tmp_bm.free()
    bmesh.ops.convex_hull(bm, input=bm.verts[:])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    mesh = bpy.data.meshes.new(f"UCX_{asset_name}")
    bm.to_mesh(mesh)
    bm.free()
    col_obj = bpy.data.objects.new(f"UCX_{asset_name}", mesh)
    col.objects.link(col_obj)
    # Giới hạn UCX_Collider <= 128 tris / <= 64 mặt
    cur_tris = _mesh_tris(col_obj)
    if cur_tris > 128:
        mod = col_obj.modifiers.new(name="ColliderCap", type="DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = max(0.08, 120.0 / cur_tris)
        bpy.ops.object.select_all(action="DESELECT")
        col_obj.select_set(True)
        bpy.context.view_layer.objects.active = col_obj
        bpy.ops.object.modifier_apply(modifier=mod.name)
    return col_obj


def _ensure_default_material(obj: bpy.types.Object) -> None:
    if len(obj.material_slots) == 0 or obj.material_slots[0].material is None:
        mat = bpy.data.materials.new(name=f"Mat_{obj.name}")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (0.55, 0.48, 0.42, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.55
        if len(obj.material_slots) == 0:
            obj.data.materials.append(mat)
        else:
            obj.material_slots[0].material = mat


def export_pbr_maps(
    lod0_objs: list[bpy.types.Object],
    out_dir: Path,
    resolution: int = 512,
    invert_normal_y: bool = False,
) -> dict[str, str]:
    """Sinh/nướng 5 bản đồ PBR (BaseColor, Normal, Roughness, Metallic, AO) + ORM gộp (R=AO, G=Roughness, B=Metallic)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for o in lod0_objs:
        _ensure_default_material(o)

    # Dùng Cycles Bake nếu GPU/CPU hợp lệ; tối ưu nhanh trên CPU bằng sample=4
    scene = bpy.context.scene
    prev_engine = scene.render.engine
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False

    paths: dict[str, str] = {}
    configs = [
        ("BaseColor", "DIFFUSE", {"COLOR"}, "sRGB"),
        ("Roughness", "ROUGHNESS", set(), "Non-Color"),
        ("Normal", "NORMAL", set(), "Non-Color"),
        ("AO", "AO", set(), "Non-Color"),
    ]
    bake_targets: list[bpy.types.Image] = []
    # Tạo ảnh dùng chung để bake lần lượt toàn bộ object vào cùng 1 UV atlas
    try:
        for key, bake_type, pass_filter, colorspace in configs:
            img = bpy.data.images.new(f"Bake_{key}", width=resolution, height=resolution, alpha=(key == "BaseColor"))
            img.colorspace_settings.name = colorspace
            bake_targets.append(img)
            temp_nodes: list[tuple[bpy.types.NodeTree, bpy.types.Node]] = []
            for o in lod0_objs:
                for ms in o.material_slots:
                    mat = ms.material
                    if not mat or not mat.use_nodes:
                        continue
                    nt = mat.node_tree
                    tn = nt.nodes.new("ShaderNodeTexImage")
                    tn.image = img
                    nt.nodes.active = tn
                    temp_nodes.append((nt, tn))

            bpy.ops.object.select_all(action="DESELECT")
            for o in lod0_objs:
                o.select_set(True)
            bpy.context.view_layer.objects.active = lod0_objs[0]
            scene.render.bake.use_clear = True
            scene.render.bake.margin = 4
            if pass_filter:
                bpy.ops.object.bake(type=bake_type, pass_filter=pass_filter)
            else:
                bpy.ops.object.bake(type=bake_type)

            if key == "Normal" and invert_normal_y:
                px = list(img.pixels[:])
                for i in range(1, len(px), 4):
                    px[i] = 1.0 - px[i]
                img.pixels.foreach_set(px)

            p = out_dir / f"{key}.png"
            img.filepath_raw = str(p)
            img.file_format = "PNG"
            img.save()
            paths[key] = str(p)

            for nt, tn in temp_nodes:
                try:
                    nt.nodes.remove(tn)
                except Exception:
                    pass

        # Sinh Metallic + ORM bằng cách đọc giá trị Metallic của từng vật liệu hoặc constant neutral
        # Nếu vật liệu có nhiều metallic khác nhau, lấy giá trị trung bình weighted hoặc 0.0
        met_vals = []
        for o in lod0_objs:
            for ms in o.material_slots:
                m = ms.material
                if m and m.use_nodes:
                    bsdf = m.node_tree.nodes.get("Principled BSDF")
                    if bsdf and "Metallic" in bsdf.inputs and not bsdf.inputs["Metallic"].is_linked:
                        met_vals.append(float(bsdf.inputs["Metallic"].default_value))
        met_val = sum(met_vals) / len(met_vals) if met_vals else 0.0

        met_img = bpy.data.images.new("Bake_Metallic", width=resolution, height=resolution, alpha=False)
        met_img.colorspace_settings.name = "Non-Color"
        met_px = [met_val, met_val, met_val, 1.0] * (resolution * resolution)
        met_img.pixels.foreach_set(met_px)
        p_met = out_dir / "Metallic.png"
        met_img.filepath_raw = str(p_met)
        met_img.file_format = "PNG"
        met_img.save()
        paths["Metallic"] = str(p_met)

        # Gộp ORM (R=AO, G=Roughness, B=Metallic) theo Phần 7.3.5
        ao_img = bpy.data.images.get("Bake_AO")
        rgh_img = bpy.data.images.get("Bake_Roughness")
        if ao_img and rgh_img:
            ao_px = list(ao_img.pixels[:])
            rgh_px = list(rgh_img.pixels[:])
            orm_px = [0.0] * len(ao_px)
            for i in range(0, len(orm_px), 4):
                orm_px[i] = ao_px[i]
                orm_px[i + 1] = rgh_px[i]
                orm_px[i + 2] = met_val
                orm_px[i + 3] = 1.0
            orm_img = bpy.data.images.new("Bake_ORM", width=resolution, height=resolution, alpha=False)
            orm_img.colorspace_settings.name = "Non-Color"
            orm_img.pixels.foreach_set(orm_px)
            p_orm = out_dir / "ORM.png"
            orm_img.filepath_raw = str(p_orm)
            orm_img.file_format = "PNG"
            orm_img.save()
            paths["ORM"] = str(p_orm)
            bpy.data.images.remove(orm_img)
        bpy.data.images.remove(met_img)
    finally:
        for img in bake_targets:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass
        scene.render.engine = prev_engine

    return paths


def run_postproc(spec: dict[str, Any], out_pbr_dir: Path | None = None, bake_pbr: bool = False, bake_res: int = 512) -> dict[str, Any]:
    """Chuẩn hóa không gian, vệ sinh lưới, tạo LOD0..2, UCX_Collider, Smart UV và (tùy chọn) PBR Bake."""
    raw_meshes = [o for o in bpy.data.objects if o.type == "MESH" and not o.name.startswith(("UCX_", "_Preview"))]
    if not raw_meshes:
        raise RuntimeError("PostProc nhận cảnh không có MESH")

    is_articulated = spec.get("archetype") == "articulated_prop" or bool(spec.get("joints"))
    joint_part_ids = {j["part_id"] for j in (spec.get("joints") or [])}

    # 1. Xác định Scale toàn cục để khớp dimensions_m (giữ tỷ lệ hình dáng hoặc khớp đúng bao tối đa nếu sai lệch lớn)
    for o in raw_meshes:
        _apply_transforms_preserving_pivot(o, preserve_pivot=o.name in joint_part_ids)

    bmin, bmax = _scene_bbox_world(raw_meshes)
    cur_dims = bmax - bmin
    target = Vector(spec.get("dimensions_m", (1.0, 1.0, 1.0)))
    # Nếu người sinh code lệch scale > 25% so với dimensions_m thì đồng co giãn tổng thể theo chiều cao / cạnh lớn
    if cur_dims.x > 1e-4 and cur_dims.y > 1e-4 and cur_dims.z > 1e-4:
        sx, sy, sz = target.x / cur_dims.x, target.y / cur_dims.y, target.z / cur_dims.z
        if max(abs(sx - 1.0), abs(sy - 1.0), abs(sz - 1.0)) > 0.25 and not is_articulated:
            for o in raw_meshes:
                o.location = Vector((o.location.x * sx, o.location.y * sy, o.location.z * sz))
                bm = bmesh.new()
                bm.from_mesh(o.data)
                bmesh.ops.scale(bm, vec=(sx, sy, sz), verts=bm.verts[:])
                bm.to_mesh(o.data)
                bm.free()

    # 2. Căn tâm đáy (X, Y) = (0, 0) và Z_min = 0.000
    bpy.context.view_layer.update()
    bmin, bmax = _scene_bbox_world(raw_meshes)
    shift = Vector((
        -((bmin.x + bmax.x) * 0.5),
        -((bmin.y + bmax.y) * 0.5),
        -bmin.z,
    ))
    if not is_articulated and shift.length > 1e-5:
        for o in raw_meshes:
            o.location += shift
            _apply_transforms_preserving_pivot(o, preserve_pivot=False)

    # 3. Vệ sinh lưới & tách Collection
    col_hp = _ensure_collection("HighPoly")
    col_lod0 = _ensure_collection("LOD0")
    col_lod1 = _ensure_collection("LOD1")
    col_lod2 = _ensure_collection("LOD2")
    col_ucx = _ensure_collection("UCX_Collider")

    organic = spec.get("archetype") in ("humanoid_char", "creature_char")
    budget = int(spec.get("poly_budget_lod0", 6000))

    # Lưu bản HighPoly (ẩn khi render/export GLB mặc định)
    for o in raw_meshes:
        _clean_single_mesh(o)
        hp = o.copy()
        hp.data = o.data.copy()
        hp.name = f"{o.name}_HighPoly"
        col_hp.objects.link(hp)
        hp.hide_render = True
        _move_to_collection(o, col_lod0)

    # Rút gọn LOD0 nếu vượt trần ngân sách
    total_lod0_tris = sum(_mesh_tris(o) for o in raw_meshes)
    if total_lod0_tris > budget:
        dec_ratio = (budget * 0.92) / max(1, total_lod0_tris)
        for o in raw_meshes:
            if _mesh_tris(o) > 24:
                _decimate_object(o, dec_ratio, organic=organic)

    # Trải UV sạch cho LOD0
    _ensure_smart_uv(raw_meshes)

    # Tạo LOD1 (50%) & LOD2 (25%)
    for suffix, factor, dst_col in (("_LOD1", 0.50, col_lod1), ("_LOD2", 0.25, col_lod2)):
        for o in raw_meshes:
            dup = o.copy()
            dup.data = o.data.copy()
            dup.name = f"{o.name}{suffix}"
            dst_col.objects.link(dup)
            if _mesh_tris(dup) > 24:
                _decimate_object(dup, factor, organic=False)
            dup.hide_render = True

    # Tạo khối va chạm lồi UCX_<asset_name>
    ucx = _create_convex_collider(raw_meshes, spec.get("name", "Asset"), col_ucx)
    ucx.hide_render = True

    pbr_paths: dict[str, str] = {}
    if bake_pbr and out_pbr_dir is not None:
        pbr_paths = export_pbr_maps(
            raw_meshes,
            out_pbr_dir,
            resolution=bake_res,
            invert_normal_y=(spec.get("target_engine") == "ue5"),
        )

    bpy.context.view_layer.update()
    return {
        "lod0_parts": [o.name for o in raw_meshes],
        "tris_lod0": sum(_mesh_tris(o) for o in raw_meshes),
        "pbr_maps": pbr_paths,
    }

