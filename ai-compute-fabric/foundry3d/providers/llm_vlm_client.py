"""Khách gọi Gateway :18080/v1/chat/completions (X-Priority: batch, X-AICF-Thinking on/off, 6-View Vision base64)."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from foundry3d.api_rag_b42 import build_blender42_rag_context
from foundry3d.schemas import PART_ID_RE, AssetSpec, VLMVerdict

DEFAULT_GATEWAY_URL = os.environ.get("AICF_GATEWAY_URL", "http://127.0.0.1:18080")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_CANDIDATES = (
    REPO_ROOT / ".demo/blender_fi-ai03_key.txt",
    REPO_ROOT / ".demo/bestofn_api_key.txt",
    REPO_ROOT / ".demo/vscode_api_key.txt",
)

PY_FENCE_RE = re.compile(r"<<<PY\s*\n(.*?)PY>>>", re.DOTALL)
MD_PY_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def resolve_gateway_api_key(explicit_key: str = "") -> str:
    if explicit_key:
        return explicit_key.strip()
    env_k = os.environ.get("AICF_GATEWAY_API_KEY", "").strip()
    if env_k:
        return env_k
    for p in DEFAULT_KEY_CANDIDATES:
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt
    return ""


def strip_thinking_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def extract_bpy_code(raw_text: str) -> str:
    """Ưu tiên bóc cặp thẻ <<<PY ... PY>>> theo bài học fi-ai03; fallback về markdown ```python."""
    cleaned = strip_thinking_tags(raw_text)
    m = PY_FENCE_RE.search(cleaned)
    if m:
        return m.group(1).strip()
    blocks = MD_PY_RE.findall(cleaned)
    if blocks:
        return max(blocks, key=len).strip()
    return cleaned.strip()


def extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = strip_thinking_tags(raw_text)
    m = JSON_FENCE_RE.search(cleaned)
    candidate = m.group(1).strip() if m else cleaned
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        return json.loads(candidate[start : end + 1])
    return json.loads(candidate)


class GatewayLlmVlmClient:
    """Giao tiếp đồng bộ với Gateway :18080/v1/chat/completions."""

    def __init__(
        self,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        api_key: str = "",
        model: str = "qwen3.8-27b",
        timeout_s: float = 360.0,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = resolve_gateway_api_key(api_key)
        self.model = model
        self.timeout_s = timeout_s

    def _headers(self, thinking: str = "off") -> dict[str, str]:
        hdr = {
            "Content-Type": "application/json",
            "X-Priority": "batch",
            "X-AICF-Thinking": thinking,
        }
        if self.api_key:
            hdr["Authorization"] = f"Bearer {self.api_key}"
        return hdr

    def resolve_live_model(self) -> str:
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(f"{self.gateway_url}/v1/models", headers=self._headers("off"))
                if r.status_code == 200:
                    data = r.json().get("data") or []
                    if data and data[0].get("id"):
                        return str(data[0]["id"])
        except Exception:
            pass
        return self.model

    def chat_completions(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: str = "off",
        max_tokens: int = 2048,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model_id = self.resolve_live_model()
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(
                        f"{self.gateway_url}/v1/chat/completions",
                        headers=self._headers(thinking=thinking),
                        json=payload,
                    )
                    if resp.status_code in (502, 503, 504) and attempt < 2:
                        time.sleep(3.0)
                        continue
                    resp.raise_for_status()
                    return resp.json()
            except httpx.ReadTimeout:
                if attempt < 1:
                    time.sleep(3.0)
                    continue
                raise
        raise RuntimeError("Không thể gọi /v1/chat/completions sau 3 lần thử")

    def plan_asset_spec(
        self,
        prompt: str,
        *,
        archetype: str = "static_prop",
        dimensions_m: tuple[float, float, float] | None = None,
        poly_budget_lod0: int = 6000,
        target_engine: str = "godot4",
        mode: str = "M1",
    ) -> AssetSpec:
        dims_hint = f"Kích thước mong muốn (m): {dimensions_m}" if dimensions_m else "Chọn kích thước thực tế ngoài đời bằng mét."
        sys_prompt = (
            "Bạn là Chuyên gia Kiến trúc 3D. Phân rã yêu cầu thành JSON thuần khớp lược đồ AssetSpec:\n"
            "{\n"
            '  "name": "ten_khong_dau_snake_case",\n'
            '  "archetype": "static_prop|articulated_prop|modular_kit",\n'
            '  "dimensions_m": [width_x, depth_y, height_z],\n'
            '  "parts": [{"id": "ten_bo_phan", "description": "...", "method": "procedural_bpy", "anchor_to": "ground|<part_id>", "contact_face": "bottom|side|top", "material_hint": "..."}],\n'
            '  "joints": []\n'
            "}\n"
            "Nếu archetype == articulated_prop, khai báo joints[] gồm {name, part_id, parent_part_id, joint_type, origin:[x,y,z], axis:[x,y,z], min_angle_deg, max_angle_deg, max_slide_m}.\n"
            "Quy ước trục Blender: Z đứng lên (sàn Z=0), mặt trước hướng -Y, phải +X. Chỉ trả về đúng 1 JSON object."
        )
        user_msg = f"Yêu cầu: {prompt}\nArchetype: {archetype}\n{dims_hint}\nPoly budget LOD0: {poly_budget_lod0}"
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                raw = self.chat_completions(
                    [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
                    thinking="off",
                    max_tokens=1400,
                    temperature=0.1 if attempt == 0 else 0.0,
                )
                text = raw["choices"][0]["message"]["content"]
                data = extract_json_object(text)
                data["prompt"] = prompt
                data["archetype"] = archetype
                if dimensions_m is not None:
                    data["dimensions_m"] = list(dimensions_m)
                data["poly_budget_lod0"] = poly_budget_lod0
                data["target_engine"] = target_engine
                data["mode"] = mode
                if mode == "M1":
                    norm_parts = []
                    for idx_p, part in enumerate(data.get("parts") or [], start=1):
                        if not isinstance(part, dict):
                            continue
                        p_copy = dict(part)
                        p_copy["method"] = "procedural_bpy"
                        pid = str(p_copy.get("id") or f"part_{idx_p}").strip()
                        pid = re.sub(r"[^A-Za-z0-9_.-]+", "_", pid)
                        if not PART_ID_RE.match(pid):
                            pid = f"part_{pid or idx_p}"[:64]
                        p_copy["id"] = pid
                        norm_parts.append(p_copy)
                    data["parts"] = norm_parts
                reasons = data.get("exception_reasons")
                if not isinstance(reasons, dict) or not reasons:
                    data["floating_ok"] = []
                    data["intentional_intersections"] = []
                    data["uv_mirror_overlap_ok"] = False
                    data["unlit_black_ok"] = False
                    data["exception_reasons"] = {}
                return AssetSpec(**data)
            except httpx.HTTPError:
                raise
            except Exception as exc:
                last_exc = exc

        fallback_name = re.sub(r"[^a-z0-9_]+", "_", prompt.lower())[:40].strip("_") or "asset_m1"
        _ = last_exc
        return AssetSpec(
            name=fallback_name,
            prompt=prompt,
            archetype=archetype,  # type: ignore[arg-type]
            dimensions_m=dimensions_m or (1.0, 1.0, 1.0),
            poly_budget_lod0=poly_budget_lod0,
            target_engine=target_engine,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            parts=[],
            joints=[],
        )

    def generate_bpy_code(
        self,
        spec: AssetSpec,
        repair_feedback: str = "",
        previous_code: str = "",
        *,
        lessons_text: str = "",
        diagnostics: str = "",
        reflection: str = "",
        history_text: str = "",
    ) -> str:
        rag = build_blender42_rag_context(spec)
        sys_prompt = (
            "Bạn là Nghệ sĩ Kỹ thuật 3D viết mã Python sạch chạy trong Blender 4.2 LTS Headless.\n"
            "CHỈ viết mã Python đặt trong đúng cặp thẻ <<<PY ... PY>>>.\n"
            + rag
            + ("\n\n" + lessons_text if lessons_text else "")
        )
        user_parts = [f"Hãy dựng tài sản 3D '{spec.name}': {spec.prompt}"]
        if repair_feedback:
            user_parts.append(f"\nLẦN SỬA LỖI — BÀN CÂN 3D BÁO LỖI SAU:\n{repair_feedback}")
            if diagnostics:
                user_parts.append(f"\nSỐ ĐO THẬT TỪ LẦN CHẠY TRƯỚC:\n{diagnostics}")
            if history_text:
                user_parts.append(f"\nCÁC LẦN ĐÃ THỬ (đừng lặp lại hướng đã thất bại):\n{history_text}")
            if reflection:
                user_parts.append(f"\nPHÂN TÍCH NGUYÊN NHÂN GỐC & KẾ HOẠCH SỬA (hãy thực hiện đúng):\n{reflection}")
            if previous_code:
                user_parts.append(f"\nMã lần trước (viết lại TOÀN BỘ sau khi sửa, giữ phần đã đúng):\n<<<PY\n{previous_code[:16000]}\nPY>>>")
        raw = self.chat_completions(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "\n".join(user_parts)}],
            thinking="off",
            max_tokens=4000,
            temperature=0.15 if repair_feedback else 0.25,
        )
        text = raw["choices"][0]["message"]["content"]
        return extract_bpy_code(text)

    def reflect(
        self,
        spec: AssetSpec,
        code: str,
        feedback: str,
        diagnostics: str,
        history_text: str,
        lessons_text: str,
    ) -> str:
        """Phản tư: tìm NGUYÊN NHÂN GỐC từ số đo thật rồi đề ra danh sách sửa cụ thể bằng số (không viết lại mã)."""
        sys_prompt = (
            "Bạn là kỹ sư kiểm định 3D. Nhiệm vụ: đọc mã Blender và SỐ ĐO THẬT, xác định nguyên nhân gốc của từng lỗi, "
            "rồi đưa ra kế hoạch sửa tối thiểu có số cụ thể (tọa độ, kích thước, dấu của hướng quay). "
            "Kiểm tra bằng phép tính (ví dụ: quy tắc bàn tay phải axis x r, khoảng hở = min/max bbox), không đoán. "
            "Trả lời tối đa 12 dòng đánh số: 'Nguyên nhân: ... | Sửa: ...'. Không viết mã."
        )
        user = (
            f"Tài sản: {spec.prompt}\nLỗi:\n{feedback}\n\n{diagnostics}\n\nLịch sử:\n{history_text}\n\n{lessons_text}\n\n"
            f"Mã hiện tại:\n{code[:12000]}"
        )
        raw = self.chat_completions(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            thinking="on",
            max_tokens=2500,
            temperature=0.1,
        )
        return str(raw["choices"][0]["message"]["content"]).strip()[:3000]

    def distill_lesson(self, spec: AssetSpec, failing_feedback: str, plan: str) -> dict[str, Any] | None:
        """Chắt 1 quy tắc CHUNG (không nêu tên bài/bộ phận cụ thể) từ một lỗi đã được sửa thật."""
        sys_prompt = (
            "Từ một lỗi dựng 3D đã được sửa thành công, hãy viết ĐÚNG 1 quy tắc chung, ngắn (<= 40 từ), áp dụng được cho "
            'các tài sản khác cùng loại. TUYỆT ĐỐI không nêu tên tài sản/bộ phận cụ thể. Trả về JSON: {"rule": "...", "gates": ["G6_..."]}'
        )
        raw = self.chat_completions(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": f"Loại: {spec.archetype}\nLỗi gốc:\n{failing_feedback[:1500]}\nCách đã sửa:\n{plan[:1500]}"}],
            thinking="off", max_tokens=300, temperature=0.1,
        )
        text = str(raw["choices"][0]["message"]["content"])
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return None
        if not isinstance(d, dict) or not d.get("rule"):
            return None
        return {"rule": str(d["rule"]), "gates": [g for g in (d.get("gates") or []) if isinstance(g, str)]}

    def judge_6views(
        self,
        spec: AssetSpec,
        image_paths: list[Path],
        geometry_summary: dict[str, Any] | None = None,
    ) -> VLMVerdict:
        view_labels = {
            "view_01_front.png": "Ảnh 1 — view_01_front: camera đặt ở phía TRƯỚC (-Y) nhìn thẳng vào mặt trước của tài sản (trục Lam +Z hướng thẳng lên, Đỏ +X sang phải, Lục +Y hướng ra sau).",
            "view_02_right.png": "Ảnh 2 — view_02_right: camera đặt bên PHẢI (+X) nhìn sang trái (mặt trước -Y nằm bên TRÁI khung hình, mặt sau +Y nằm bên PHẢI khung hình).",
            "view_03_back.png": "Ảnh 3 — view_03_back: camera đặt phía SAU (+Y) nhìn vào mặt lưng tài sản.",
            "view_04_iso_pbr.png": "Ảnh 4 — view_04_iso_pbr: góc phối cảnh 3/4 nhìn từ phía trước-phải-trên (+X, -Y, +Z).",
            "view_05_iso_wireframe.png": "Ảnh 5 — view_05_iso_wireframe: góc phối cảnh 3/4 hiển thị lưới đa giác wireframe.",
            "view_06_pose_or_top.png": "Ảnh 6 — view_06_pose_or_top: góc nhìn từ trên cao (hoặc tại khung hình thử khớp động frame 15 nếu có xương).",
        }
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Bạn là Giám khảo QA tài sản 3D game low-poly/stylized (Mode M1, dựng thủ tục Blender).\n"
                    f"Tài sản mục tiêu: '{spec.prompt}' (archetype={spec.archetype}, kích thước mục tiêu={spec.dimensions_m}m).\n"
                    f"Thông số Tầng 5A đã ĐẠT: {json.dumps(geometry_summary or {}, ensure_ascii=False)[:800]}\n"
                    "QUY TẮC CHẤM ĐIỂM RÕ RÀNG:\n"
                    "1. Đây là tài sản game low-poly/stylized sạch ở Mode M1: KHÔNG đánh rớt chỉ vì ít đa giác hoặc vật liệu phẳng/stylized sạch nếu hình khối đúng công năng.\n"
                    "2. Trục chuẩn: trục Lam (+Z) hướng lên trên, trục Đỏ (+X) sang phải, mặt trước hướng về phía camera Ảnh 1 (-Y). "
                    "Kiểm tra kỹ Ảnh 1 (Front) và Ảnh 2 (Right): với ghế phải thấy mặt ngồi mở ở Ảnh 1 và tựa lưng ở phía sau (+Y / bên phải Ảnh 2); "
                    "với bàn/rương phải thấy tay nắm/khóa ở Ảnh 1 (-Y); với trụ đứng phải dựng thẳng dọc trục Lam (+Z). Nếu quay ngược lưng về -Y hoặc nằm ngang -> pass=false, front_axis_correct=false.\n"
                    "3. Nếu tài sản nhận diện đúng vật thể, đủ bộ phận chính, hướng trục đúng, bề mặt sạch và không biến dạng -> pass=true.\n"
                    "Trả về DUY NHẤT 1 JSON object:\n"
                    '{"pass": true/false, "semantic_score": 1..5, "proportion_score": 1..5, '
                    '"front_axis_correct": true/false, "texture_clean": true/false, '
                    '"failed_stage": null|1|2|3|4, "fix_instruction": "..."}'
                ),
            }
        ]
        for idx, p in enumerate(image_paths[:8], start=1):
            label = view_labels.get(p.name, f"Ảnh {idx} — {p.name}")
            content.append({"type": "text", "text": label})
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        text = ""
        obj: dict[str, Any] = {}
        for attempt in range(2):
            raw = self.chat_completions(
                [{"role": "user", "content": content}],
                thinking="off",
                max_tokens=512,
                temperature=0.0 if attempt == 0 else 0.1,
            )
            text = raw["choices"][0]["message"]["content"]
            try:
                obj = extract_json_object(text)
                break
            except Exception:
                if attempt == 1:
                    raise
        return VLMVerdict.model_validate(
            {
                "pass": bool(obj.get("pass", False)),
                "semantic_score": int(obj.get("semantic_score", 3)),
                "proportion_score": int(obj.get("proportion_score", 3)),
                "front_axis_correct": bool(obj.get("front_axis_correct", True)),
                "texture_clean": bool(obj.get("texture_clean", True)),
                "failed_stage": obj.get("failed_stage"),
                "fix_instruction": str(obj.get("fix_instruction", "")),
                "raw_response": text[:1500],
            }
        )
