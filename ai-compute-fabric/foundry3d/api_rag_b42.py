"""Chỉ mục tra cứu nhanh API chuẩn Blender 4.2 LTS (R4) & bài học prompt <<<PY ... >>> từ fi-ai03."""
from __future__ import annotations

from foundry3d.schemas import AssetSpec

BLENDER_42_QUICK_REFERENCE = """
QUY TẮC VIẾT MÃ PYTHON BLENDER 4.2 LTS (BẮT BUỘC TUÂN THỦ):
1. Đặt toàn bộ mã Python có thể thực thi vào giữa đúng 1 cặp thẻ:
<<<PY
import bpy, bmesh, math
...
PY>>>
Không dùng JSON tool-args để bọc mã Python dài. Không tạo file tạm ngoài cảnh Blender. Không xóa hay gọi bpy.ops.wm.quit_blender().
2. Hệ quy chiếu không gian nội bộ BẮT BUỘC (1 unit = 1.0 mét):
   - Trục đứng là +Z; sàn nằm tại Z = 0.000 (z_min của vật thể = 0.000).
   - Mặt trước (Front) hướng về -Y; phải là +X; tâm đáy hộp bao nằm tại (X=0, Y=0, Z=0).
   - Kích thước tổng khớp `dimensions_m = (width_x, depth_y, height_z)`.
3. Đặt tên đối tượng MESH khớp CHÍNH XÁC từng `part.id` trong AssetSpec (`obj.name = part_id`, `obj.type == 'MESH'`).
   - Mọi bộ phận nâng đỡ (anchor_to != 'ground') PHẢI chạm hoặc giao cắt nhẹ (>= 0.001 m, KHÔNG lơ lửng > 0.002 m) với bộ phận đỡ bên dưới; KHÔNG để hai khối rời đâm xuyên sâu (> 10% thể tích) nếu không cần thiết.
   - Với vòng đai/nẹp bao ngoài (hoop, strap, band, rim): phải dựng dạng ống rỗng (torus/hollow ring) hoặc các thanh nẹp mỏng ốp sát mặt ngoài, TUYỆT ĐỐI không đặt khối trụ/hộp đặc xuyên qua ruột vật thể chính.
   - Đảm bảo lưới đóng kín/sạch (không dùng boolean phức tạp dễ vỡ; ưu tiên bmesh hoặc nhiều khối lắp chạm tiếp xúc chính xác).
4. BREAKING CHANGES CỦA BLENDER 4.0–4.2 LTS (CẤM DÙNG API CŨ ĐÃ XÓA):
   - Principled BSDF trong Blender 4.0–4.2 KHÔNG còn các socket cũ:
     * Dùng `"Specular IOR Level"` thay cho `"Specular"`.
     * Dùng `"Transmission Weight"` thay cho `"Transmission"`.
     * Dùng `"Coat Weight"` thay cho `"Clearcoat"`.
     * Dùng `"Subsurface Weight"` thay cho `"Subsurface"`.
     * Dùng `"Emission Color"` + `"Emission Strength"` (mặc định Emission Strength = 0.0).
     * `"Metallic"`, `"Roughness"`, `"Base Color"`, `"IOR"`, `"Alpha"` giữ nguyên.
   - `mesh.use_auto_smooth` ĐÃ BỊ XÓA trong Blender 4.1/4.2! Nếu cần làm mượt cạnh, hãy đặt từng face `p.use_smooth = True` hoặc dùng modifier `EDGE_SPLIT` / `bpy.ops.object.shade_smooth_by_angle(angle=math.radians(35))` sau khi chọn object. KHÔNG truy cập `obj.data.use_auto_smooth`.
   - `bpy.ops.object.select_all(action='SELECT')` chỉ dùng khi ở OBJECT mode (`if bpy.context.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')`).
   - Khi tạo vật liệu: luôn gán `mat.use_nodes = True`, lấy `bsdf = mat.node_tree.nodes.get("Principled BSDF")`, đặt `Base Color` có độ sáng tự nhiên phù hợp (không để (0,0,0) đen xì trừ khi yêu cầu).
   - Không dùng thư viện ngoài (`numpy` có sẵn trong Blender nhưng ưu tiên `math`, `mathutils`, `bmesh`, `bpy`).
5. Nếu là `articulated_prop` có khớp xoay/trượt:
   - Đặt `obj.location` (tức Origin của bộ phận chuyển động) tại ĐÚNG tọa độ tâm bản lề / trục quay / điểm trượt khai báo trong `joints[]` và giữ các đỉnh của mesh đó ở tọa độ tương đối quanh Origin! Không đặt Origin về (0,0,0) cho phần chuyển động.
6. Tránh dùng `bpy.ops.object.join()` nhiều khối rời nếu có thể tách bộ phận theo `part.id`; nếu cần ghép khối phụ vào 1 mesh chính, PostProc sẽ tự trải lại UV sạch nhưng phải kiểm tra các phần ghép nối chạm kín không để khe hở lơ lửng > 2 mm.
""".strip()

SAFE_HELPERS_BPY = """import bpy, math

def _mat(name, rgb):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes.get("Principled BSDF")
    if b is not None:
        b.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
    return m

def _finish(o, name, mat):
    o.name = name
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    if mat is not None:
        o.data.materials.append(mat)
    return o

def box_world(name, size, center, mat=None):
    # size=(sx,sy,sz) mét, center=(x,y,z) tọa độ thế giới. Đúng 4 tham số (mat tùy chọn).
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    o = bpy.context.active_object
    o.scale = size
    return _finish(o, name, mat)

def cyl_world(name, radius, depth, center, axis="Z", mat=None, verts=16):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=depth, location=center)
    o = bpy.context.active_object
    if axis == "X":
        o.rotation_euler = (0.0, math.radians(90), 0.0)
    elif axis == "Y":
        o.rotation_euler = (math.radians(90), 0.0, 0.0)
    return _finish(o, name, mat)

def cone_world(name, r1, r2, depth, center, mat=None, verts=16):
    bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=r1, radius2=r2, depth=depth, location=center)
    return _finish(bpy.context.active_object, name, mat)

def set_pivot(o, pivot_world):
    # Đặt Origin của o tại pivot_world mà KHÔNG đổi hình dạng trong thế giới (dùng cho bản lề/trục quay).
    dx, dy, dz = (o.location.x - pivot_world[0], o.location.y - pivot_world[1], o.location.z - pivot_world[2])
    for v in o.data.vertices:
        v.co.x += dx; v.co.y += dy; v.co.z += dz
    o.location = pivot_world
"""

QUY_TAC_HELPER = """
7. DÙNG LẠI NGUYÊN VĂN các hàm trợ giúp đã kiểm chứng bên dưới (box_world/cyl_world/cone_world/set_pivot) thay vì tự viết hàm tạo khối mới. KHÔNG tự viết lại hàm có tên add_box/create_cone/make_*; KHÔNG dùng bmesh.ops.create_cone với tham số kiểu int (cap_ends/cap_tris là bool); KHÔNG gọi bm.faces.new với đỉnh trùng hoặc mặt đã tồn tại.
8. Bộ phận có khớp: dựng khối bằng box_world ở TỌA ĐỘ THẾ GIỚI cuối cùng rồi gọi set_pivot(obj, origin_trong_joints). Chừa khe >= 0.005 m giữa phần quay và bộ phận đứng yên ở phía nó quét tới (cánh cửa quay ra phía -Y thì khung/cột phải nằm ngoài vùng quét). Mọi khối phải có z_min >= 0.000 (không chìm dưới sàn).
9. QUY TẮC HƯỚNG QUAY (nguyên nhân số 1 gây đâm xuyên khi quét khớp): mặt phẳng quét dùng quy tắc bàn tay phải. Điểm của bộ phận chuyển động nằm lệch r so với tâm quay sẽ chuyển động theo hướng (axis x r). BẮT BUỘC chọn phía bộ phận chuyển động nằm so với tâm quay sao cho (axis x r) hướng RA XA khối đứng yên (lên trên / ra ngoài), không hướng vào nó. Ví dụ: nắp rương, trục X, tâm quay ở mép sau trên: nắp phải nằm về phía +Y so với tâm (r=+Y) khi axis=(+X)... hãy tự kiểm tra dấu bằng tích có hướng trước khi dựng, và nếu prompt nói "mở về phía -Y" mà axis/góc ra +Y thì ƯU TIÊN axis/góc của joints[] (cổng quét dùng đúng axis và min..max góc đó).
10. CÔNG THỨC DỰNG KHỚP ĐÃ KIỂM CHỨNG: (a) khung/bệ/thân đứng yên dựng trước, không chiếm vùng quét của bộ phận quay; (b) bộ phận quay cách bộ phận đỡ 0.003 m ở phía tiếp xúc (khe nhỏ hơn 0.005 để không bị coi là lơ lửng, lớn hơn 0 để không đâm xuyên); (c) cánh cửa bản lề đứng: trụ khung phía bản lề phải có mặt trong cách tâm quay >= (độ dày cánh / 2 + 0.003) về phía sau tâm quay, cánh bắt đầu cách tâm quay 0.003 m ở phía quét; thanh ngang trên khung nằm CAO HƠN đỉnh cánh >= 0.005 m; (d) không có khối nào chìm dưới sàn: sau khi dựng mọi khối phải có z_min >= 0.0; (e) trượt (slider): thân giữ nguyên, ngăn kéo trượt dọc axis và phải có khoảng trống đủ max_slide_m trong thân/khung phía sau (không để ngăn kéo đâm vào tấm lưng).
11. KHÔNG tạo thêm khối dư thừa chồng lên nhau (ví dụ cùng một thân tủ dựng 2 lần, cột chồng lên tường): mỗi khối có vùng riêng; tường, cột, sàn của bộ mô-đun đặt cạnh nhau (chạm mặt, không giao thể tích > 2 mm).
"""


def build_blender42_rag_context(spec: AssetSpec) -> str:
    """Trả về ngữ cảnh API Blender 4.2 + danh sách bộ phận/khớp của AssetSpec cho Coder Agent."""
    parts_lines = []
    for p in spec.parts:
        parts_lines.append(
            f"  - id={p.id!r}, anchor_to={p.anchor_to!r}, contact_face={p.contact_face!r}, "
            f"material={p.material_hint!r}, desc={p.description!r}"
        )
    joints_lines = []
    for j in spec.joints:
        joints_lines.append(
            f"  - joint={j.name!r}, part_id={j.part_id!r}, parent={j.parent_part_id!r}, "
            f"type={j.joint_type}, origin={j.origin}, axis={j.axis}, "
            f"angle=[{j.min_angle_deg}, {j.max_angle_deg}], slide={j.max_slide_m}"
        )
    parts_txt = "\n".join(parts_lines) if parts_lines else "  - (Tự phân rã tối thiểu 2 bộ phận có tên rõ ràng)"
    joints_txt = ("\nCÁC KHỚP CƠ HỌC PHẢI LẮP ĐÚNG TÂM ORIGIN:\n" + "\n".join(joints_lines)) if joints_lines else ""
    return (
        f"{BLENDER_42_QUICK_REFERENCE}\n{QUY_TAC_HELPER}\nMÃ TRỢ GIÚP (chép nguyên văn vào đầu <<<PY):\n{SAFE_HELPERS_BPY}\n\n"
        f"MỤC TIÊU CỤ THỂ:\n"
        f"- Asset: {spec.name} ({spec.archetype})\n"
        f"- Kích thước mục tiêu (X=rộng, Y=sâu, Z=cao, mét): {spec.dimensions_m}\n"
        f"- Ngân sách tam giác LOD0 tối đa: {spec.poly_budget_lod0}\n"
        f"- Danh sách bộ phận BẮT BUỘC đặt đúng obj.name:\n{parts_txt}"
        f"{joints_txt}"
    )

