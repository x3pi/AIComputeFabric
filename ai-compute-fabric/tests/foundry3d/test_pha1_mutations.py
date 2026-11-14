"""Kiểm đột biến bắt buộc Pha 1 (8/8 đột biến phải ĐỎ khi hỏng/phá & XANH khi bật chốt, cộng chiều ngược ngoại lệ)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from foundry3d.artifact_store import ArtifactSecurityError, ArtifactStore
from foundry3d.schemas import AssetSpec
from foundry3d.warm_blender import LowMemoryQueuedError, WarmBlenderPool

DOCKER_OK = bool(shutil.which("docker")) and Path("/mnt/Data/aicf-3d-foundry/runtime/blender-4.2-lts/blender").exists()
pytestmark = pytest.mark.skipif(not DOCKER_OK, reason="Cần Docker + Blender 4.2 runtime trên /mnt/Data để chạy sandbox thật")


@pytest.fixture(scope="module")
def blender_pool():
    p = WarmBlenderPool()
    yield p
    p.shutdown()


def _base_table_bpy(z_offset: float = 0.0, top_gap: float = 0.0) -> str:
    return f"""
import bpy
m = bpy.data.materials.new("Wood"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.45, 0.28, 0.14, 1.0)
objs = []
def box(name, size, loc):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object; o.name = name; o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
    objs.append(o)
for nm, x, y in [("leg_fl", -0.4, -0.3), ("leg_fr", 0.4, -0.3), ("leg_bl", -0.4, 0.3), ("leg_br", 0.4, 0.3)]:
    box(nm, (0.06, 0.06, 0.70), (x, y, 0.35 + {z_offset}))
box("tabletop", (1.0, 0.8, 0.06), (0.0, 0.0, 0.73 + {z_offset} + {top_gap}))
for idx, o in enumerate(objs):
    for d in o.data.uv_layers.active.data:
        d.uv.x = 0.01 + idx * 0.19 + d.uv.x * 0.17
        d.uv.y = 0.05 + d.uv.y * 0.90
"""


def test_mut1_floating_object_zmin_12cm_blocked(blender_pool: WarmBlenderPool):
    """Đột biến 1: Vật thể đặt tại z_min = +0.12 m -> G2_TransformAndGround chặn ĐỎ; tắt G2 -> lọt lưới."""
    spec = AssetSpec(name="float_z12", prompt="table", dimensions_m=(1.0, 0.8, 0.76)).model_dump()
    code = _base_table_bpy(z_offset=0.12)
    resp, d, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": code, "skip_postproc": True, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d)
    assert resp["ok"] is True
    assert resp["geometry"]["passed"] is False
    assert any("G2_TransformAndGround" in r for r in resp["geometry"]["failure_reasons"])

    # Tắt G2 giả lập (mutation kill proof): nếu phá cổng thì lọt lưới
    resp_mut, d2, _ = blender_pool.run_task(
        "run_pipeline",
        {
            "spec": spec,
            "bpy_code": code,
            "skip_postproc": True,
            "render_6views": False,
            "export_bundle": False,
            "disabled_gates": ["G2_TransformAndGround"],
        },
    )
    blender_pool.cleanup_call_dir(d2)
    assert resp_mut["geometry"]["passed"] is True


def test_mut2_detached_tabletop_8cm_and_exception_reverse(blender_pool: WarmBlenderPool):
    """Đột biến 2: Mặt bàn tách khỏi 4 chân +0.08 m -> Floating Part Check chặn; và chiều ngược floating_ok."""
    from pydantic import ValidationError

    # Chiều 1: bật floating_ok nhưng thiếu exception_reasons -> Schema phải từ chối
    with pytest.raises(ValidationError):
        AssetSpec(name="float_no_reason", prompt="table", floating_ok=["tabletop"])

    # Chiều 2: miễn đúng bộ phận khác (decor_ring), mặt bàn tabletop vẫn bị chặn
    spec_dict = AssetSpec(
        name="float_top",
        prompt="table",
        dimensions_m=(1.0, 0.8, 0.76),
        floating_ok=["decor_ring"],
        exception_reasons={"floating_ok:decor_ring": "Vật trang trí bay giả lập — KHÔNG được miễn cho tabletop"},
    ).model_dump()
    code = _base_table_bpy(z_offset=0.0, top_gap=0.08)
    resp, d, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec_dict, "bpy_code": code, "skip_postproc": True, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d)
    assert resp["geometry"]["passed"] is False
    fps = resp["geometry"]["floating_parts"]
    assert any(fp["part_id"] == "tabletop" and 75.0 <= fp["gap_mm"] <= 85.0 for fp in fps)

    # Chiều 3: miễn chính tabletop với lý do kiểm toán đầy đủ -> Floating Part Check được miễn hợp lệ
    spec_exempt = AssetSpec(
        name="float_top_ok",
        prompt="floating magic tabletop",
        dimensions_m=(1.0, 0.8, 0.76),
        floating_ok=["tabletop"],
        exception_reasons={"floating_ok:tabletop": "Mặt bàn bay phép thuật theo mô tả cảnh"},
    ).model_dump()
    resp_ok, d_ok, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec_exempt, "bpy_code": code, "skip_postproc": True, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d_ok)
    assert resp_ok["geometry"]["passed"] is True


def test_mut3_illegal_overlap_35pct_blocked(blender_pool: WarmBlenderPool):
    """Đột biến 3: Hai khối đặc đâm xuyên ~35% thể tích trái phép -> G3_IllegalOverlap chặn; tắt G3 -> lọt lưới."""
    spec = AssetSpec(name="overlap35", prompt="two crates", dimensions_m=(1.0, 0.6, 0.6)).model_dump()
    code = """
import bpy
m = bpy.data.materials.new("Crate"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.5, 0.35, 0.2, 1.0)
for idx, (nm, x) in enumerate([("crate_a", -0.195), ("crate_b", 0.195)]):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, 0.0, 0.3))
    o = bpy.context.active_object; o.name = nm; o.scale = (0.6, 0.6, 0.6)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    o.data.materials.append(m)
    for d in o.data.uv_layers.active.data:
        d.uv.x = 0.02 + idx * 0.49 + d.uv.x * 0.45
        d.uv.y = 0.05 + d.uv.y * 0.90
"""
    resp, d, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": code, "skip_postproc": True, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d)
    assert resp["geometry"]["passed"] is False
    assert any("G3_IllegalOverlap" in r for r in resp["geometry"]["failure_reasons"])

    resp_mut, d2, _ = blender_pool.run_task(
        "run_pipeline",
        {
            "spec": spec,
            "bpy_code": code,
            "skip_postproc": True,
            "render_6views": False,
            "export_bundle": False,
            "disabled_gates": ["G3_IllegalOverlap"],
        },
    )
    blender_pool.cleanup_call_dir(d2)
    assert resp_mut["geometry"]["passed"] is True


def test_mut4_poly_budget_2x_blocked(blender_pool: WarmBlenderPool):
    """Đột biến 4: Lưới vượt 2x trần đa giác (budget=400, sinh ~1920 tris) -> G1_PolyBudget chặn."""
    spec = AssetSpec(name="high_poly_cube", prompt="sphere", poly_budget_lod0=400, dimensions_m=(0.8, 0.8, 0.8)).model_dump()
    code = """
import bpy
m = bpy.data.materials.new("Clay"); m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.6, 0.5, 0.4, 1.0)
bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.4, location=(0, 0, 0.4))
o = bpy.context.active_object; o.name = "orb"; o.data.materials.append(m)
"""
    resp, d, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": code, "skip_postproc": True, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d)
    assert resp["geometry"]["passed"] is False
    assert resp["geometry"]["tris_lod0"] > 800
    assert any("G1_PolyBudget" in r for r in resp["geometry"]["failure_reasons"])


def test_mut5_sandbox_blocks_network_and_host_files(blender_pool: WarmBlenderPool):
    """Đột biến 5: Mã bpy cố nối mạng socket & đọc /etc/shadow -> --network=none & user 65534 chặn cứng."""
    spec = AssetSpec(name="evil_net", prompt="evil", dimensions_m=(0.5, 0.5, 0.5)).model_dump()
    code = """
import bpy, socket
# Thử kết nối ra ngoài
s = socket.create_connection(("1.1.1.1", 80), timeout=1.0)
"""
    resp, d, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": code, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d)
    assert resp["ok"] is False
    assert resp["stage_failed"] == "exec_bpy"

    code_read = """
import bpy
open("/etc/shadow").read()
"""
    resp2, d2, _ = blender_pool.run_task(
        "run_pipeline",
        {"spec": spec, "bpy_code": code_read, "render_6views": False, "export_bundle": False},
    )
    blender_pool.cleanup_call_dir(d2)
    assert resp2["ok"] is False
    assert "PermissionError" in resp2["error"] or "FileNotFoundError" in resp2["error"]


def test_mut6_blend_register_autoexec_blocked_by_dash_Y(blender_pool: WarmBlenderPool):
    """Đột biến 6: Tệp .blend nhúng Text Register tự chạy lệnh tạo tệp -> cờ -Y chặn cứng không cho thực thi."""
    marker = "/work/autoexec_pwned.txt"
    create_evil_blend = f"""
import bpy
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0,0,0.25))
txt = bpy.data.texts.new("evil_register.py")
txt.from_string("open({marker!r}, 'w').write('PWNED')\\n")
txt.use_module = True
bpy.ops.wm.save_as_mainfile(filepath="/work/evil_test.blend")
"""
    # Chạy sinh .blend trong /work
    resp1, d1, _ = blender_pool.run_task(
        "run_pipeline",
        {
            "spec": AssetSpec(name="mk_evil", prompt="cube").model_dump(),
            "bpy_code": create_evil_blend,
            "skip_postproc": True,
            "render_6views": False,
            "export_bundle": False,
        },
    )
    assert resp1["ok"] is True

    # Mở chính tệp .blend vừa tạo qua wm.open_mainfile bên trong daemon đang chạy với -Y
    resp2, _d2, _ = blender_pool.run_task(
        "open_blend_check",
        {"blend_rel_path": "evil_test.blend"},
    )
    pool_work_root = d1.parent.parent
    assert resp2["ok"] is True
    assert resp2["use_scripts_auto_execute"] is False
    assert resp2["autoexec_fail"] is True
    assert not (pool_work_root / "autoexec_pwned.txt").exists()
    blender_pool.cleanup_call_dir(d1)


def test_mut7_artifact_store_rejects_symlink_escape(tmp_path: Path):
    """Đột biến 7: Tạo symlink /work/asset.glb -> /etc/hostname -> ArtifactStore từ chối thu hồi."""
    work = tmp_path / "work"
    work.mkdir()
    evil_link = work / "asset.glb"
    evil_link.symlink_to("/etc/hostname")

    store = ArtifactStore(root=tmp_path / "artifacts")
    with pytest.raises(ArtifactSecurityError):
        store.put_file("tenant-a", "job-1", evil_link, work_root=work)

    # Đối chứng khôi phục: tệp thật bên trong /work thu hồi thành công
    real_file = work / "real_asset.glb"
    real_file.write_bytes(b"glTF-real-bytes")
    art = store.put_file("tenant-a", "job-1", real_file, work_root=work)
    assert art.uri.startswith("sha256:")


def test_mut8_low_mem_available_blocks_blender_spawn(tmp_path: Path):
    """Đột biến 8: MemAvailable giả lập 1,500 MiB < 2,048 MiB -> pool từ chối chạy Blender (queued_low_mem)."""
    fake_mem = 1500
    pool = WarmBlenderPool(
        work_base=tmp_path / "wb",
        mem_reader=lambda: fake_mem,
        min_mem_available_mb=2048,
    )
    with pytest.raises(LowMemoryQueuedError):
        pool.run_task("ping", {})
    assert len(pool._workers) == 0
