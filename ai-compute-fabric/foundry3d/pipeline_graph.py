"""Đồ thị LangGraph 6 chặng cho AICF 3D Asset Foundry (Mode M1, max_repairs=3, chốt chống tụt lùi R9)."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from foundry3d.artifact_store import ArtifactStore
from foundry3d.providers.llm_vlm_client import GatewayLlmVlmClient
from foundry3d.providers.mesh_provider import HybridMeshRouter
from foundry3d.providers.rig_provider import HybridRigRouter
from foundry3d.reflexion import ExperienceLibrary, build_diagnostics, gates_in
from foundry3d.schemas import (
    AssetSpec,
    GeometryReport,
    HardwareMode,
    QAReport,
    StageArtifact,
    VLMVerdict,
)
from foundry3d.warm_blender import WarmBlenderPool

EventHook = Callable[[str, dict[str, Any]], None]


class FoundryState(TypedDict, total=False):
    job_id: str
    tenant_id: str
    prompt: str
    archetype: str
    dimensions_m: tuple[float, float, float] | None
    poly_budget_lod0: int
    target_engine: str
    mode: str
    enable_vlm_critic: bool
    bake_pbr: bool
    max_repairs: int
    repair_count: int
    spec: dict[str, Any]
    bpy_code: str
    repair_feedback: str
    attempt_log: list[dict[str, Any]]
    last_plan: str
    first_failure: str
    geometry: dict[str, Any]
    vlm: dict[str, Any] | None
    gltf_validator: dict[str, Any]
    artifacts: list[dict[str, Any]]
    best_snapshot: dict[str, Any] | None
    best_5a_error_count: int
    peak_rss_mb: float
    started_at: float
    status: str
    error: str


def _emit(hook: EventHook | None, stage: str, data: dict[str, Any]) -> None:
    if hook is not None:
        hook(stage, data)


class FoundryPipelineRunner:
    """Chạy StateGraph 6 chặng trên WarmBlenderPool + Gateway :18080 + ArtifactStore."""

    def __init__(
        self,
        pool: WarmBlenderPool,
        store: ArtifactStore,
        llm_client: GatewayLlmVlmClient | None = None,
        lessons: ExperienceLibrary | None = None,
    ) -> None:
        self.lessons = lessons
        self.pool = pool
        self.store = store
        self.llm = llm_client or GatewayLlmVlmClient()
        self.mesh_router = HybridMeshRouter()
        self.rig_router = HybridRigRouter()

    def build_graph(
        self,
        *,
        preset_spec: AssetSpec | None = None,
        preset_bpy_fn: Callable[[AssetSpec, int, str], str] | None = None,
        event_hook: EventHook | None = None,
    ):
        g = StateGraph(FoundryState)

        def node_spec(state: FoundryState) -> dict[str, Any]:
            _emit(event_hook, "stage_1_spec", {"repair_count": state.get("repair_count", 0)})
            if preset_spec is not None:
                spec = preset_spec
            else:
                spec = self.llm.plan_asset_spec(
                    state["prompt"],
                    archetype=state.get("archetype", "static_prop"),
                    dimensions_m=state.get("dimensions_m"),
                    poly_budget_lod0=state.get("poly_budget_lod0", 6000),
                    target_engine=state.get("target_engine", "godot4"),
                    mode=state.get("mode", "M1"),
                )
            self.mesh_router.ensure_supported(spec)
            self.rig_router.ensure_supported(spec)
            self.store.put_bytes(
                state["tenant_id"],
                state["job_id"],
                "asset_spec.json",
                spec.model_dump_json(indent=2).encode("utf-8"),
                mime_type="application/json",
                stage="stage_1_spec",
            )
            return {"spec": spec.model_dump(), "status": "running"}

        def node_synthesize_and_blender(state: FoundryState) -> dict[str, Any]:
            repair_count = int(state.get("repair_count", 0))
            spec = AssetSpec(**state["spec"])
            _emit(event_hook, "stage_2_synthesize", {"repair_count": repair_count})

            if preset_bpy_fn is not None:
                bpy_code = preset_bpy_fn(spec, repair_count, state.get("repair_feedback", ""))
            else:
                feedback = state.get("repair_feedback", "")
                prev_gates = gates_in([ln for ln in feedback.split("\n") if ln.strip()]) if feedback else []
                lessons_text = ExperienceLibrary.render(self.lessons.retrieve(spec, prev_gates)) if self.lessons else ""
                diagnostics = reflection = history_text = ""
                if feedback:
                    diagnostics = build_diagnostics(state.get("geometry") or {}, spec)
                    history_text = "\n".join(
                        f"  - lần {a['repair']}: {a['count']} lỗi: {'; '.join(a['reasons'])[:300]}" for a in state.get("attempt_log", [])
                    )
                    try:
                        reflection = self.llm.reflect(spec, state.get("bpy_code", ""), feedback, diagnostics, history_text, lessons_text)
                    except Exception as exc:  # phản tư lỗi không được làm hỏng vòng sửa
                        reflection = f"(phản tư không khả dụng: {type(exc).__name__})"
                    _emit(event_hook, "reflection", {"repair_count": repair_count, "plan": reflection[:500]})
                bpy_code = self.llm.generate_bpy_code(
                    spec,
                    repair_feedback=feedback,
                    previous_code=state.get("bpy_code", ""),
                    lessons_text=lessons_text,
                    diagnostics=diagnostics,
                    reflection=reflection,
                    history_text=history_text,
                )
                state = cast(FoundryState, {**state, "last_plan": reflection})

            self.store.put_bytes(
                state["tenant_id"],
                state["job_id"],
                f"gen_bpy_r{repair_count}.py",
                bpy_code.encode("utf-8"),
                mime_type="text/x-python",
                stage="stage_2_synthesize",
            )

            _emit(event_hook, "stage_3_blender_postproc", {"repair_count": repair_count})
            resp, call_dir, _cold = self.pool.run_task(
                "run_pipeline",
                {
                    "spec": spec.model_dump(),
                    "bpy_code": bpy_code,
                    "bake_pbr": bool(state.get("bake_pbr", False)),
                    "bake_res": 256 if state.get("mode", "M1") == "M1" else 512,
                    "render_6views": bool(state.get("enable_vlm_critic", True)),
                    "render_res": 512,
                    "render_samples": 8,
                    "export_bundle": True,
                    "export_fbx": True,
                    "export_blend": True,
                },
            )
            try:
                if not resp.get("ok"):
                    err_msg = f"Blender lỗi ở {resp.get('stage_failed')}: {resp.get('error')}"
                    return {
                        "bpy_code": bpy_code,
                        "geometry": {
                            "passed": False,
                            "failure_reasons": [err_msg],
                            "gates": [],
                        },
                        "vlm": None,
                        "gltf_validator": {},
                        "repair_feedback": err_msg,
                        "last_plan": state.get("last_plan", ""),
                        "first_failure": state.get("first_failure") or err_msg,
                        "attempt_log": list(state.get("attempt_log", [])) + [{"repair": repair_count, "count": 1, "reasons": [err_msg]}],
                    }

                geom = resp["geometry"]
                peak_rss = max(float(state.get("peak_rss_mb", 0.0)), float(resp.get("peak_rss_mb", 0.0)))

                # Thu hồi hiện vật an toàn (từ chối symlink) vào ArtifactStore
                collected: list[StageArtifact] = []
                export_files = (resp.get("export") or {}).get("files") or {}
                mime_map = {
                    ".glb": "model/gltf-binary",
                    ".fbx": "application/octet-stream",
                    ".blend": "application/x-blender",
                    ".png": "image/png",
                }
                for key_name, container_path in export_files.items():
                    rel = Path(container_path).relative_to(Path("/work/calls") / resp["call_id"])
                    host_p = call_dir / rel
                    if host_p.exists() or host_p.is_symlink():
                        art = self.store.put_file(
                            state["tenant_id"],
                            state["job_id"],
                            host_p,
                            work_root=call_dir,
                            filename=host_p.name,
                            mime_type=mime_map.get(host_p.suffix.lower(), "application/octet-stream"),
                            stage="stage_6_export",
                        )
                        collected.append(art)

                pbr_maps = (resp.get("postproc") or {}).get("pbr_maps") or {}
                for _map_name, container_path in pbr_maps.items():
                    rel = Path(container_path).relative_to(Path("/work/calls") / resp["call_id"])
                    host_p = call_dir / rel
                    if host_p.exists() or host_p.is_symlink():
                        art = self.store.put_file(
                            state["tenant_id"],
                            state["job_id"],
                            host_p,
                            work_root=call_dir,
                            filename=f"pbr_{host_p.name}",
                            mime_type="image/png",
                            stage="stage_3_pbr",
                        )
                        collected.append(art)

                view_host_paths: list[Path] = []
                for vname, rel_v in sorted((resp.get("views") or {}).items()):
                    host_v = call_dir / rel_v
                    if host_v.exists() or host_v.is_symlink():
                        art = self.store.put_file(
                            state["tenant_id"],
                            state["job_id"],
                            host_v,
                            work_root=call_dir,
                            filename=vname,
                            mime_type="image/png",
                            stage="stage_5b_views",
                        )
                        collected.append(art)
                        stored_p, _ = self.store.get_artifact_path(state["tenant_id"], state["job_id"], vname)
                        view_host_paths.append(stored_p)

                # Tầng 5B chỉ gọi khi Tầng 5A đã XANH 100%
                vlm_dict: dict[str, Any] | None = None
                if geom.get("passed") and state.get("enable_vlm_critic", True) and view_host_paths:
                    _emit(event_hook, "stage_5b_vlm_critic", {"repair_count": repair_count})
                    verdict = self.llm.judge_6views(
                        spec,
                        view_host_paths,
                        geometry_summary={
                            "tris_lod0": geom.get("tris_lod0"),
                            "dimensions_measured_m": geom.get("dimensions_measured_m"),
                            "z_min_m": geom.get("z_min_m"),
                        },
                    )
                    vlm_dict = verdict.model_dump(by_alias=True)

                # Quy tắc R9: chỉ chấp nhận snapshot mới nếu số lỗi Tầng 5A giảm hoặc bằng 0
                err_cnt = len(geom.get("failure_reasons") or [])
                prev_best_cnt = int(state.get("best_5a_error_count", 10**9))
                best_snapshot = state.get("best_snapshot")
                artifacts_dicts = [a.model_dump() for a in collected]
                if best_snapshot is None or err_cnt <= prev_best_cnt:
                    best_snapshot = {
                        "repair_count": repair_count,
                        "geometry": geom,
                        "vlm": vlm_dict,
                        "gltf_validator": (resp.get("export") or {}).get("gltf_validator") or {},
                        "artifacts": artifacts_dicts,
                        "bpy_code": bpy_code,
                    }
                    prev_best_cnt = min(prev_best_cnt, err_cnt)

                feedback_lines = list(geom.get("failure_reasons") or [])
                if vlm_dict and not vlm_dict.get("pass"):
                    feedback_lines.append(f"[VLM 6-View Critic] {vlm_dict.get('fix_instruction', 'Cải thiện hình dáng/tỷ lệ.')}")

                return {
                    "bpy_code": bpy_code,
                    "geometry": geom,
                    "vlm": vlm_dict,
                    "gltf_validator": (resp.get("export") or {}).get("gltf_validator") or {},
                    "artifacts": artifacts_dicts,
                    "best_snapshot": best_snapshot,
                    "best_5a_error_count": prev_best_cnt,
                    "peak_rss_mb": peak_rss,
                    "repair_feedback": "\n".join(feedback_lines),
                    "last_plan": state.get("last_plan", ""),
                    "first_failure": state.get("first_failure") or ("\n".join(feedback_lines) if feedback_lines else ""),
                    "attempt_log": list(state.get("attempt_log", [])) + [{"repair": repair_count, "count": len(feedback_lines), "reasons": feedback_lines}],
                }
            finally:
                self.pool.cleanup_call_dir(call_dir)

        def node_decide_or_finalize(state: FoundryState) -> dict[str, Any]:
            geom_ok = bool((state.get("geometry") or {}).get("passed", False))
            vlm = state.get("vlm")
            vlm_ok = True if (not state.get("enable_vlm_critic", True) or vlm is None) else bool(vlm.get("pass", False))
            passed_both = geom_ok and vlm_ok

            repair_count = int(state.get("repair_count", 0))
            max_repairs = int(state.get("max_repairs", 3))

            if passed_both:
                if repair_count > 0 and self.lessons and not self.lessons.read_only and state.get("first_failure"):
                    try:
                        les = self.llm.distill_lesson(AssetSpec(**state["spec"]), state["first_failure"], state.get("last_plan", ""))
                        if les:
                            sp = AssetSpec(**state["spec"])
                            self.lessons.add({**les, "archetype": sp.archetype,
                                              "joint_types": sorted({j.joint_type for j in sp.joints}),
                                              "gates": les.get("gates") or gates_in(state["first_failure"].split("\n"))})
                    except Exception:
                        pass
                snap = state.get("best_snapshot") or {}
                final_geom = snap.get("geometry") or state.get("geometry") or {"passed": False}
                final_vlm = snap.get("vlm") if snap else state.get("vlm")
                final_val = snap.get("gltf_validator") or state.get("gltf_validator") or {}
                dur_ms = round((time.perf_counter() - float(state.get("started_at", time.perf_counter()))) * 1000.0, 2)
                # Ước lượng điện năng Mode M1 CPU Blender + Inference (~180W cụm trung bình [SUY LUẬN])
                energy_wh = round((dur_ms / 3_600_000.0) * 180.0, 4)
                qa = QAReport(
                    job_id=state["job_id"],
                    repair_iteration=repair_count,
                    mode=cast(HardwareMode, state.get("mode", "M1")),
                    geometry=GeometryReport(**final_geom),
                    vlm=VLMVerdict(**final_vlm) if final_vlm else None,
                    gltf_validator=final_val,
                    peak_rss_mb=float(state.get("peak_rss_mb", 0.0)),
                    duration_ms=dur_ms,
                    energy_wh_est=energy_wh,
                )
                qa_art = self.store.put_bytes(
                    state["tenant_id"],
                    state["job_id"],
                    "qa_report.json",
                    qa.model_dump_json(indent=2).encode("utf-8"),
                    mime_type="application/json",
                    stage="stage_6_export",
                )
                _emit(event_hook, "completed", {"repair_count": repair_count, "qa_report": qa.model_dump()})
                all_arts = list(snap.get("artifacts") or state.get("artifacts") or []) + [qa_art.model_dump()]
                return {"status": "completed", "artifacts": all_arts}

            if repair_count < max_repairs:
                _emit(event_hook, "repair_retry", {
                    "next_repair": repair_count + 1,
                    "reason": state.get("repair_feedback", ""),
                })
                return {"repair_count": repair_count + 1}

            # Hết ngân sách sửa -> needs_human_review kèm snapshot tốt nhất
            snap = state.get("best_snapshot") or {}
            final_geom = snap.get("geometry") or state.get("geometry") or {"passed": False, "failure_reasons": ["unknown"]}
            dur_ms = round((time.perf_counter() - float(state.get("started_at", time.perf_counter()))) * 1000.0, 2)
            qa = QAReport(
                job_id=state["job_id"],
                repair_iteration=repair_count,
                mode=cast(HardwareMode, state.get("mode", "M1")),
                geometry=GeometryReport(**final_geom),
                vlm=VLMVerdict(**snap["vlm"]) if snap.get("vlm") else None,
                gltf_validator=snap.get("gltf_validator") or {},
                peak_rss_mb=float(state.get("peak_rss_mb", 0.0)),
                duration_ms=dur_ms,
                energy_wh_est=round((dur_ms / 3_600_000.0) * 180.0, 4),
            )
            qa_art = self.store.put_bytes(
                state["tenant_id"],
                state["job_id"],
                "qa_report.json",
                qa.model_dump_json(indent=2).encode("utf-8"),
                mime_type="application/json",
                stage="stage_5_needs_review",
            )
            _emit(event_hook, "needs_human_review", {"repair_count": repair_count, "reason": state.get("repair_feedback", "")})
            return {
                "status": "needs_human_review",
                "artifacts": list(snap.get("artifacts") or state.get("artifacts") or []) + [qa_art.model_dump()],
            }

        def route_after_decide(state: FoundryState) -> str:
            if state.get("status") in ("completed", "needs_human_review", "failed"):
                return "end"
            return "retry"

        g.add_node("spec", node_spec)
        g.add_node("synthesize", node_synthesize_and_blender)
        g.add_node("decide", node_decide_or_finalize)
        g.add_edge(START, "spec")
        g.add_edge("spec", "synthesize")
        g.add_edge("synthesize", "decide")
        g.add_conditional_edges("decide", route_after_decide, {"retry": "synthesize", "end": END})
        return g.compile()

    def run_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        prompt: str,
        archetype: str = "static_prop",
        dimensions_m: tuple[float, float, float] | None = None,
        poly_budget_lod0: int = 6000,
        target_engine: str = "godot4",
        mode: str = "M1",
        enable_vlm_critic: bool = True,
        bake_pbr: bool = False,
        max_repairs: int = 3,
        preset_spec: AssetSpec | None = None,
        preset_bpy_fn: Callable[[AssetSpec, int, str], str] | None = None,
        event_hook: EventHook | None = None,
    ) -> FoundryState:
        graph = self.build_graph(preset_spec=preset_spec, preset_bpy_fn=preset_bpy_fn, event_hook=event_hook)
        init: FoundryState = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "prompt": prompt,
            "archetype": archetype,
            "dimensions_m": dimensions_m,
            "poly_budget_lod0": poly_budget_lod0,
            "target_engine": target_engine,
            "mode": mode,
            "enable_vlm_critic": enable_vlm_critic,
            "bake_pbr": bake_pbr,
            "max_repairs": max_repairs,
            "repair_count": 0,
            "repair_feedback": "",
            "attempt_log": [],
            "last_plan": "",
            "first_failure": "",
            "artifacts": [],
            "best_snapshot": None,
            "best_5a_error_count": 10**9,
            "peak_rss_mb": 0.0,
            "started_at": time.perf_counter(),
            "status": "running",
        }
        return graph.invoke(init)

