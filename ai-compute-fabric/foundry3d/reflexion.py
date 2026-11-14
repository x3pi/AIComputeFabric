"""Tự học trong vòng lặp: chẩn đoán có số đo, phản tư nguyên nhân gốc, và thư viện kinh nghiệm bền vững.

Không có logic riêng cho từng bài: mọi thông tin đến từ báo cáo định lượng của evaluator_3d (scene_table + lý do cổng) và
từ các bài học được chắt lọc sau khi một lỗi đã được sửa THẬT (cổng 5A xanh)."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from foundry3d.schemas import AssetSpec

GATE_RE = re.compile(r"\[(G\d_[A-Za-z]+)\]")
PAIR_RE = re.compile(r"(\S+?)<->(\S+?)\s")


def gates_in(reasons: list[str]) -> list[str]:
    out: list[str] = []
    for r in reasons:
        m = GATE_RE.search(r)
        out.append(m.group(1) if m else "EXEC_ERROR")
    return sorted(set(out))


def _aabb_overlap(a: dict[str, Any], b: dict[str, Any]) -> list[float] | None:
    d = [min(a["bbox_max"][i], b["bbox_max"][i]) - max(a["bbox_min"][i], b["bbox_min"][i]) for i in range(3)]
    return [round(x, 4) for x in d] if all(x > 0 for x in d) else None


def build_diagnostics(geom: dict[str, Any], spec: AssetSpec) -> str:
    """Bảng cảnh thật + số đo cụ thể của từng lỗi (không đoán): bbox, tâm quay, độ sâu giao nhau theo từng trục."""
    table = {t["name"]: t for t in (geom.get("scene_table") or [])}
    lines: list[str] = []
    if table:
        lines.append("BẢNG CẢNH THỰC TẾ SAU KHI CHẠY (mét, tọa độ thế giới):")
        for t in table.values():
            size = [round(t["bbox_max"][i] - t["bbox_min"][i], 3) for i in range(3)]
            lines.append(f"  - {t['name']}: min={t['bbox_min']} max={t['bbox_max']} size={size} origin={t['origin']}")
    joints = {j.name: j for j in spec.joints}
    for j in joints.values():
        lines.append(f"KHỚP YÊU CẦU: {j.name} part={j.part_id} origin={list(j.origin)} axis={list(j.axis)} góc=[{j.min_angle_deg},{j.max_angle_deg}] trượt={j.max_slide_m}")
    for o in geom.get("illegal_overlaps") or []:
        a, b = table.get(o.get("part_a")), table.get(o.get("part_b"))
        ov = _aabb_overlap(a, b) if a and b else None
        lines.append(f"GIAO NHAU: {o.get('part_a')} x {o.get('part_b')} {o.get('overlap_pct')}%; hộp giao theo (x,y,z)={ov}")
    for f in geom.get("floating_parts") or []:
        lines.append(f"LƠ LỬNG: {json.dumps(f, ensure_ascii=False)[:200]}")
    for sf in geom.get("articulated_sweep_failures") or []:
        lines.append(f"QUÉT KHỚP: {json.dumps(sf, ensure_ascii=False)[:260]}")
    if geom.get("z_min_m") is not None and abs(float(geom["z_min_m"])) > 0.002:
        lines.append(f"CHẠM SÀN: z_min={geom['z_min_m']} m (yêu cầu 0.000 ± 0.002)")
    return "\n".join(lines)


class ExperienceLibrary:
    """Bài học chung, bền vững (JSONL). Chỉ ghi bài học đã được XÁC NHẬN bởi cổng 5A xanh sau khi sửa."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._mu = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def add(self, lesson: dict[str, Any]) -> bool:
        if self.read_only:
            return False
        rule = str(lesson.get("rule", "")).strip()
        if len(rule) < 20:
            return False
        with self._mu:
            cur = self._load()
            if any(c.get("rule", "").strip().lower() == rule.lower() for c in cur):
                for c in cur:
                    if c.get("rule", "").strip().lower() == rule.lower():
                        c["confirmed"] = int(c.get("confirmed", 1)) + 1
                self.path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cur) + "\n", encoding="utf-8")
                return True
            lesson = {**lesson, "confirmed": 1, "created": time.strftime("%Y-%m-%d")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(lesson, ensure_ascii=False) + "\n")
            return True

    def retrieve(self, spec: AssetSpec, gates: list[str] | None = None, k: int = 6) -> list[dict[str, Any]]:
        joint_types = {j.joint_type for j in spec.joints}
        want = set(gates or [])
        scored = []
        for c in self._load():
            sc = 0.0
            if c.get("archetype") == spec.archetype:
                sc += 1.0
            if joint_types & set(c.get("joint_types") or []):
                sc += 1.0
            sc += 2.0 * len(want & set(c.get("gates") or []))
            sc += 0.2 * min(int(c.get("confirmed", 1)), 5)
            if sc > 0.5:
                scored.append((sc, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]

    @staticmethod
    def render(lessons: list[dict[str, Any]]) -> str:
        if not lessons:
            return ""
        rows = [f"  * [{'/'.join(l.get('gates', []))}] {l['rule']}" for l in lessons]
        return "BÀI HỌC ĐÃ XÁC NHẬN TỪ CÁC LẦN TRƯỚC (áp dụng chung, không riêng một bài):\n" + "\n".join(rows)
