"""Giao diện trừu tượng RigProvider cho Chặng 4 (Mode M1 mechanical khớp bpy; M2 MIA/UniRig chờ Pha 4)."""
from __future__ import annotations

from foundry3d.providers.mesh_provider import ModeNotEnabledError
from foundry3d.schemas import AssetSpec


class HybridRigRouter:
    """Đảm bảo nhân vật hữu cơ MIA/UniRig chỉ chạy khi bật Mode M2."""

    def ensure_supported(self, spec: AssetSpec) -> None:
        if spec.archetype in ("humanoid_char", "creature_char") and spec.mode == "M1":
            raise ModeNotEnabledError(
                f"Archetype {spec.archetype!r} cần MIA/UniRig ở Mode M2 (Pha 4). Hiện chỉ hỗ trợ static_prop, articulated_prop, modular_kit ở Mode M1."
            )

