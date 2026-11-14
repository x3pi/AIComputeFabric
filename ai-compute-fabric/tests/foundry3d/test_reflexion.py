"""Vòng tự học: chẩn đoán có số đo, phản tư, và thư viện kinh nghiệm (Blender thật + LLM giả có kịch bản)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from foundry3d.artifact_store import ArtifactStore
from foundry3d.pipeline_graph import FoundryPipelineRunner
from foundry3d.reflexion import ExperienceLibrary, build_diagnostics, gates_in
from foundry3d.schemas import AssetSpec
from foundry3d.warm_blender import WarmBlenderPool

DOCKER_OK = bool(shutil.which("docker")) and Path("/mnt/Data/aicf-3d-foundry/runtime/blender-4.2-lts/blender").exists()

SPEC = AssetSpec(name="stool", prompt="three part stool", archetype="static_prop", dimensions_m=(0.4, 0.4, 0.5),
                 parts=[{"id": "seat", "anchor_to": "ground"}, {"id": "leg", "anchor_to": "seat"}])

BAD = """
import bpy
for n, c, s in (("leg", (0, 0, 0.2), (0.05, 0.05, 0.4)), ("seat", (0, 0, 0.62), (0.4, 0.4, 0.05))):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=c)
    o = bpy.context.active_object; o.name = n; o.scale = s
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
"""
GOOD = BAD.replace("0.62", "0.425")


class _Llm:
    """LLM giả: lần 1 sinh mã lỗi (seat lơ lửng), sau đó mã đúng; ghi lại những gì pipeline đưa vào."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reflected = 0

    def generate_bpy_code(self, spec, repair_feedback="", previous_code="", **kw):
        self.calls.append({"feedback": repair_feedback, **kw})
        return GOOD if repair_feedback else BAD

    def reflect(self, *a, **k):
        self.reflected += 1
        return "1. Nguyên nhân: seat cách leg > 5mm | Sửa: hạ seat xuống z=0.425"

    def distill_lesson(self, spec, failing, plan):
        return {"rule": "Bộ phận đỡ phải chạm bộ phận bên dưới trong khe <= 0.004 m; tính tâm từ bbox, không đoán.", "gates": ["G2_TransformAndGround"]}

    def judge_6views(self, *a, **k):  # không dùng vì enable_vlm_critic=False
        raise AssertionError


def _run(tmp_path: Path, lib: ExperienceLibrary | None, llm: _Llm, max_repairs: int = 2, bpy: str | None = None):
    pool = WarmBlenderPool()
    try:
        runner = FoundryPipelineRunner(pool, ArtifactStore(root=tmp_path / "a"), llm_client=llm, lessons=lib)  # type: ignore[arg-type]
        return runner.run_job(job_id="j1", tenant_id="t1", prompt="three part stool", archetype="static_prop",
                              dimensions_m=(0.4, 0.4, 0.5), enable_vlm_critic=False, max_repairs=max_repairs, preset_spec=SPEC)
    finally:
        pool.shutdown()


def test_library_dedupe_readonly_and_retrieve(tmp_path: Path):
    lib = ExperienceLibrary(tmp_path / "l.jsonl")
    rule = "Chừa khe 0.003 m giữa phần quay và khối đứng yên để tránh đâm xuyên."
    assert lib.add({"rule": rule, "archetype": "articulated_prop", "joint_types": ["hinge"], "gates": ["G6_ArticulatedSweep"]})
    assert lib.add({"rule": rule, "archetype": "articulated_prop", "joint_types": ["hinge"], "gates": ["G6_ArticulatedSweep"]})
    assert len(lib._load()) == 1 and lib._load()[0]["confirmed"] == 2
    assert not lib.add({"rule": "ngắn"})
    ro = ExperienceLibrary(tmp_path / "l.jsonl", read_only=True)
    assert not ro.add({"rule": "Một quy tắc khác đủ dài để hợp lệ nhưng thư viện chỉ đọc."})
    assert len(ro._load()) == 1
    hinge = AssetSpec(name="d", prompt="p", archetype="articulated_prop", dimensions_m=(1, 1, 1),
                      parts=[{"id": "a", "anchor_to": "ground"}, {"id": "b", "anchor_to": "a"}],
                      joints=[{"name": "j", "part_id": "b", "parent_part_id": "a", "joint_type": "hinge", "origin": (0, 0, 0)}])
    assert ro.retrieve(hinge, ["G6_ArticulatedSweep"])
    assert not ro.retrieve(SPEC, [])  # không liên quan -> không nhồi bài học vào prompt


def test_diagnostics_have_numbers():
    geom = {"scene_table": [{"name": "a", "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1], "origin": [0, 0, 0]},
                            {"name": "b", "bbox_min": [0.9, 0, 0], "bbox_max": [2, 1, 1], "origin": [1, 0, 0]}],
            "illegal_overlaps": [{"part_a": "a", "part_b": "b", "overlap_pct": 40}], "z_min_m": -0.05}
    d = build_diagnostics(geom, SPEC)
    assert "0.1" in d and "CHẠM SÀN" in d and "size=" in d
    assert gates_in(["[G3_IllegalOverlap] x", "Blender lỗi ở exec_bpy: y"]) == ["EXEC_ERROR", "G3_IllegalOverlap"]


@pytest.mark.skipif(not DOCKER_OK, reason="Cần Docker + Blender")
def test_loop_reflects_with_real_numbers_and_learns_only_from_verified_fix(tmp_path: Path):
    lib = ExperienceLibrary(tmp_path / "l.jsonl")
    llm = _Llm()
    st = _run(tmp_path, lib, llm)
    assert st["status"] == "completed" and st["repair_count"] == 1
    assert llm.reflected == 1
    second = llm.calls[1]
    assert "BẢNG CẢNH THỰC TẾ" in second["diagnostics"] and "seat" in second["diagnostics"]
    assert second["reflection"].startswith("1.")
    assert len(lib._load()) == 1  # bài học được chắt lọc từ lỗi đã sửa THẬT


@pytest.mark.skipif(not DOCKER_OK, reason="Cần Docker + Blender")
def test_mutation_no_lesson_when_fix_not_verified_or_readonly(tmp_path: Path):
    class _Stubborn(_Llm):
        def generate_bpy_code(self, spec, repair_feedback="", previous_code="", **kw):
            return BAD  # không bao giờ sửa được

    lib = ExperienceLibrary(tmp_path / "l1.jsonl")
    st = _run(tmp_path, lib, _Stubborn(), max_repairs=1)
    assert st["status"] == "needs_human_review"
    assert lib._load() == []  # đột biến: lỗi chưa sửa được thì KHÔNG được ghi bài học
    ro = ExperienceLibrary(tmp_path / "l2.jsonl", read_only=True)
    st2 = _run(tmp_path, ro, _Llm())
    assert st2["status"] == "completed" and ro._load() == []  # chế độ đánh giá không làm nhiễm thư viện
