"""Giao diện trừu tượng MeshProvider cho Chặng 2 (Mode M1 chạy procedural_bpy; M2 Hunyuan3D-2mini chờ Pha 3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from foundry3d.schemas import AssetSpec


@dataclass
class MeshSynthesisResult:
    method: str
    bpy_code: str = ""
    raw_glb_uri: str = ""
    notes: str = ""


class MeshProvider(Protocol):
    def supports_mode(self, mode: str) -> bool: ...


class ModeNotEnabledError(RuntimeError):
    """Từ chối fail-closed khi yêu cầu Mode M2/M3 chưa được mở khóa."""


class HybridMeshRouter:
    """Định tuyến tổng hợp hình học theo chế độ phần cứng."""

    def ensure_supported(self, spec: AssetSpec) -> None:
        if spec.mode != "M1":
            raise ModeNotEnabledError(
                f"Mode {spec.mode} (Hunyuan3D-2mini/GPU Lease) thuộc Pha 3–4, chưa kích hoạt trong phiên Mode M1 hiện tại."
            )
        for p in spec.parts:
            if p.method == "neural_mesh":
                raise ModeNotEnabledError(
                    f"Bộ phận {p.id!r} yêu cầu neural_mesh (Mode M2), không chạy được ở Mode M1 Zero-VRAM."
                )

