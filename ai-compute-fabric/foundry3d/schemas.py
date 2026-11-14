"""Lược đồ Pydantic cho AICF 3D Foundry (AssetSpec, Bàn cân 5A/5B, Artifact & Job)."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Archetype = Literal["static_prop", "articulated_prop", "modular_kit", "humanoid_char", "creature_char"]
TargetEngine = Literal["godot4", "unity", "ue5", "web_glb"]
HardwareMode = Literal["M1", "M2", "M3"]
RigType = Literal["static", "mechanical", "humanoid", "creature"]
JobStatus = Literal[
    "queued",
    "queued_low_mem",
    "running",
    "completed",
    "needs_human_review",
    "failed",
    "cancelled",
]

PART_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
SHA256_URI_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class PartSpec(BaseModel):
    """Mô tả một bộ phận cấu thành trong AssetSpec."""

    id: str
    description: str = ""
    method: Literal["procedural_bpy", "neural_mesh"] = "procedural_bpy"
    anchor_to: str = "ground"
    contact_face: str = "bottom"
    material_hint: str = ""
    expected_color_rgb: tuple[float, float, float] | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        s = v.strip()
        if not PART_ID_RE.match(s):
            raise ValueError(f"part.id không hợp lệ: {v!r}")
        return s


class JointSpec(BaseModel):
    """Đặc tả khớp cơ học dùng cho articulated_prop và G6_ArticulatedSweep."""

    name: str
    part_id: str
    parent_part_id: str = "root"
    joint_type: Literal["hinge", "wheel", "slider", "ball"] = "hinge"
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: tuple[float, float, float] = (1.0, 0.0, 0.0)
    min_angle_deg: float = 0.0
    max_angle_deg: float = 90.0
    max_slide_m: float = 0.0

    @field_validator("part_id", "name")
    @classmethod
    def _validate_names(cls, v: str) -> str:
        s = v.strip()
        if not PART_ID_RE.match(s):
            raise ValueError(f"joint identifier không hợp lệ: {v!r}")
        return s


class AssetSpec(BaseModel):
    """Đặc tả kỹ thuật thống nhất giữa Planner, Coder, PostProc và Bàn cân 3D."""

    name: str
    prompt: str
    archetype: Archetype = "static_prop"
    dimensions_m: tuple[float, float, float] = (1.0, 1.0, 1.0)
    poly_budget_lod0: int = Field(default=6000, ge=12, le=200_000)
    target_engine: TargetEngine = "godot4"
    mode: HardwareMode = "M1"
    rig_type: RigType | None = None
    parts: list[PartSpec] = Field(default_factory=list)
    joints: list[JointSpec] = Field(default_factory=list)
    # Cờ ngoại lệ kiểm soát (Phần 7.3.3 — phải khai báo kèm lý do, không tự miễn toàn cục)
    floating_ok: list[str] = Field(default_factory=list)
    intentional_intersections: list[tuple[str, str]] = Field(default_factory=list)
    uv_mirror_overlap_ok: bool = False
    unlit_black_ok: bool = False
    exception_reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("dimensions_m")
    @classmethod
    def _validate_dims(cls, v: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(x <= 0.0 or x > 100.0 for x in v):
            raise ValueError(f"dimensions_m phải nằm trong (0, 100] m, nhận được {v}")
        return v

    @model_validator(mode="after")
    def _validate_exceptions(self) -> AssetSpec:
        # Cờ ngoại lệ không được miễn trừ vô căn cứ; yêu cầu ghi rõ lý do trong exception_reasons nếu bật
        for part_id in self.floating_ok:
            if not (self.exception_reasons.get(f"floating_ok:{part_id}") or self.exception_reasons.get("floating_ok")):
                raise ValueError(f"Bật floating_ok cho '{part_id}' nhưng thiếu lý do trong exception_reasons")
        for a, b in self.intentional_intersections:
            reason = (
                self.exception_reasons.get(f"intentional_intersections:{a}:{b}")
                or self.exception_reasons.get(f"intentional_intersections:{b}:{a}")
                or self.exception_reasons.get("intentional_intersections")
            )
            if not reason:
                raise ValueError(f"Bật intentional_intersections ({a}, {b}) nhưng thiếu lý do trong exception_reasons")
        if self.uv_mirror_overlap_ok and not self.exception_reasons.get("uv_mirror_overlap_ok"):
            raise ValueError("Bật uv_mirror_overlap_ok nhưng thiếu lý do trong exception_reasons")
        if self.unlit_black_ok and not self.exception_reasons.get("unlit_black_ok"):
            raise ValueError("Bật unlit_black_ok nhưng thiếu lý do trong exception_reasons")

        if self.rig_type is None:
            if self.archetype == "articulated_prop":
                self.rig_type = "mechanical"
            elif self.archetype == "humanoid_char":
                self.rig_type = "humanoid"
            elif self.archetype == "creature_char":
                self.rig_type = "creature"
            else:
                self.rig_type = "static"
        return self


class StageArtifact(BaseModel):
    """Tham chiếu hiện vật băm SHA-256 trên /mnt/Data (D-3D-1)."""

    filename: str
    uri: str
    size_bytes: int = Field(ge=0)
    mime_type: str = "application/octet-stream"
    stage: str = ""

    @field_validator("uri")
    @classmethod
    def _validate_uri(cls, v: str) -> str:
        if not SHA256_URI_RE.match(v):
            raise ValueError(f"artifact_uri không hợp lệ: {v!r}")
        return v


class GateResult(BaseModel):
    """Kết quả từng cổng toán học ở Tầng 5A."""

    gate: str
    passed: bool
    severity: Literal["hard", "warn"] = "hard"
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class GeometryReport(BaseModel):
    """Báo cáo định lượng toàn phần của evaluator_3d.py (Tầng 5A)."""

    passed: bool
    scene_table: list[dict[str, Any]] = Field(default_factory=list)
    tris_lod0: int = 0
    tris_lod1: int = 0
    tris_lod2: int = 0
    tris_collider: int = 0
    dimensions_measured_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    z_min_m: float = 0.0
    scale_measured: tuple[float, float, float] = (1.0, 1.0, 1.0)
    floating_parts: list[dict[str, Any]] = Field(default_factory=list)
    illegal_overlaps: list[dict[str, Any]] = Field(default_factory=list)
    degenerate_faces: int = 0
    uv_out_of_bounds_ratio: float = 0.0
    uv_overlap_ratio: float = 0.0
    basecolor_mean_luminance: float = 0.5
    basecolor_black_texel_ratio: float = 0.0
    unweighted_vertices: int = 0
    max_bones_per_vertex: int = 0
    max_edge_stretch_ratio: float = 1.0
    articulated_sweep_failures: list[dict[str, Any]] = Field(default_factory=list)
    roundtrip_bbox_delta_m: float = 0.0
    validator_errors: int = 0
    validator_warnings: int = 0
    gates: list[GateResult] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)


class VLMVerdict(BaseModel):
    """Phán quyết cấu trúc JSON cứng của VLM 6-View Critic (Tầng 5B)."""

    passed: bool = Field(alias="pass", default=False)
    semantic_score: int = Field(ge=1, le=5, default=1)
    proportion_score: int = Field(ge=1, le=5, default=1)
    front_axis_correct: bool = False
    texture_clean: bool = False
    failed_stage: Literal[1, 2, 3, 4] | None = None
    fix_instruction: str = ""
    raw_response: str = ""

    model_config = {"populate_by_name": True}


class QAReport(BaseModel):
    """Biên bản kiểm định kép xuất kèm gói tài sản."""

    job_id: str
    repair_iteration: int = 0
    mode: HardwareMode = "M1"
    geometry: GeometryReport
    vlm: VLMVerdict | None = None
    gltf_validator: dict[str, Any] = Field(default_factory=dict)
    peak_rss_mb: float = 0.0
    duration_ms: float = 0.0
    energy_wh_est: float = 0.0


class JobCreateRequest(BaseModel):
    """Payload POST /api/foundry3d/jobs."""

    prompt: str = Field(min_length=3, max_length=4000)
    archetype: Archetype = "static_prop"
    dimensions_m: tuple[float, float, float] | None = None
    poly_budget_lod0: int = Field(default=6000, ge=100, le=100_000)
    target_engine: TargetEngine = "godot4"
    mode: HardwareMode = "M1"
    reference_image_b64: str | None = None
    enable_vlm_critic: bool = True
    bake_pbr: bool = False
    max_repairs: int = Field(default=3, ge=0, le=3)


class JobRecord(BaseModel):
    """Bản ghi trạng thái công việc 3D trả về qua HTTP & SSE."""

    job_id: str
    tenant_id: str
    status: JobStatus = "queued"
    current_stage: str = "queued"
    repair_count: int = 0
    prompt: str
    archetype: Archetype = "static_prop"
    target_engine: TargetEngine = "godot4"
    mode: HardwareMode = "M1"
    spec: AssetSpec | None = None
    qa_report: QAReport | None = None
    artifacts: list[StageArtifact] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_at: float
    updated_at: float
