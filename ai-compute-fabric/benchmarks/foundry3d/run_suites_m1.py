"""Trình chạy Benchmark Pha 1 (20 đạo cụ tĩnh) & Pha 2 (12 đạo cụ khớp nối/modular) ở Mode M1."""
from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from foundry3d.artifact_store import ArtifactStore
from foundry3d.pipeline_graph import FoundryPipelineRunner
from foundry3d.reflexion import ExperienceLibrary
from foundry3d.providers.llm_vlm_client import GatewayLlmVlmClient
from foundry3d.schemas import AssetSpec
from foundry3d.warm_blender import LowMemoryQueuedError, WarmBlenderPool

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITES_DIR = Path(__file__).resolve().parent
REPORTS_DIR = REPO_ROOT / "benchmarks/reports"

KEEP_FINAL_ARTIFACTS = {
    "asset.glb",
    "asset.fbx",
    "source.blend",
    "qa_report.json",
    "asset_spec.json",
    "view_04_iso_pbr.png",
}


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return round(float(s[idx]), 2)


def _write_suite_report(
    out_report_path: Path,
    *,
    suite_name: str,
    max_repairs: int,
    bake_pbr: bool,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    pass1_cnt = sum(1 for r in results if r.get("pass_at_1"))
    pass_repaired_cnt = sum(1 for r in results if r.get("pass_at_repair_le_3"))
    validator_zero_cnt = sum(
        1 for r in results if r.get("pass_at_repair_le_3") and int(r.get("validator_errors", 0)) == 0
    )
    durations_s = [float(r.get("duration_s", 0.0)) for r in results if float(r.get("duration_s", 0.0)) > 0.0]
    max_rss_mb = max((float(r.get("peak_rss_mb", 0.0)) for r in results), default=0.0)
    total = max(1, len(results))
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "suite": suite_name,
        "mode": "M1",
        "max_repairs": max_repairs,
        "bake_pbr": bake_pbr,
        "total_items": len(results),
        "pass_at_1_count": pass1_cnt,
        "pass_at_1_rate": round(pass1_cnt / total, 4),
        "pass_at_repair_le_3_count": pass_repaired_cnt,
        "pass_at_repair_le_3_rate": round(pass_repaired_cnt / total, 4),
        "validator_zero_errors_on_passed": validator_zero_cnt == pass_repaired_cnt,
        "duration_p50_s": round(statistics.median(durations_s), 2) if durations_s else 0.0,
        "duration_p95_s": _pct(durations_s, 95.0),
        "peak_rss_mb": round(max_rss_mb, 2),
        "items": results,
    }
    out_report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_report_path.with_suffix(f"{out_report_path.suffix}.tmp")
    tmp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_report_path)
    return report


def run_suite(
    suite_path: Path,
    out_report_path: Path,
    *,
    bake_pbr: bool,
    bake_res: int = 256,
    max_repairs: int = 3,
    limit: int = 0,
    only: str = "",
    resume: bool = False,
    tenant_id: str = "bench-m1",
    lessons_path: str = "",
    lessons_readonly: bool = False,
) -> dict[str, Any]:
    data = json.loads(suite_path.read_text(encoding="utf-8"))
    suite_name = str(data.get("suite") or suite_path.stem)
    items = list(data.get("items") or [])
    if only:
        _ids = set(only.split(","))
        items = [i for i in items if i["id"] in _ids]
    if limit > 0:
        items = items[:limit]

    results: list[dict[str, Any]] = []
    done_ids: set[str] = set()
    if resume and out_report_path.exists():
        try:
            prev = json.loads(out_report_path.read_text(encoding="utf-8"))
            for prev_item in prev.get("items") or []:
                if prev_item.get("status") in ("completed", "needs_human_review"):
                    iid_prev = str(prev_item.get("id") or "")
                    if iid_prev and iid_prev not in done_ids:
                        done_ids.add(iid_prev)
                        results.append(prev_item)
        except Exception:
            results = []
            done_ids = set()

    if done_ids:
        print(f"[resume] Đã nạp {len(done_ids)} mẫu từ {out_report_path.name}, bỏ qua các mẫu đã hoàn tất.", flush=True)

    pool = WarmBlenderPool()
    store = ArtifactStore()
    llm = GatewayLlmVlmClient(timeout_s=360.0)
    runner = FoundryPipelineRunner(
        pool=pool, store=store, llm_client=llm,
        lessons=ExperienceLibrary(lessons_path, read_only=lessons_readonly) if lessons_path else None,
    )

    try:
        for idx, item in enumerate(items, start=1):
            iid = item["id"]
            if iid in done_ids:
                continue
            job_id = f"{data['suite']}_{iid}_{int(time.time())}"
            preset_spec: AssetSpec | None = None
            if item.get("joints") or item.get("parts"):
                preset_spec = AssetSpec(
                    name=iid,
                    prompt=item["prompt"],
                    archetype=item.get("archetype", "static_prop"),
                    dimensions_m=tuple(item.get("dimensions_m") or (1.0, 1.0, 1.0)),
                    poly_budget_lod0=int(item.get("poly_budget_lod0", 6000)),
                    target_engine=data.get("target_engine", "godot4"),
                    mode="M1",
                    parts=item.get("parts") or [],
                    joints=item.get("joints") or [],
                )

            t0 = time.perf_counter()
            state: dict[str, Any] | None = None
            for mem_wait in range(19):
                try:
                    state = runner.run_job(
                        job_id=job_id,
                        tenant_id=tenant_id,
                        prompt=item["prompt"],
                        archetype=item.get("archetype", "static_prop"),
                        dimensions_m=tuple(item["dimensions_m"]) if item.get("dimensions_m") else None,
                        poly_budget_lod0=int(item.get("poly_budget_lod0", 6000)),
                        target_engine=data.get("target_engine", "godot4"),
                        mode="M1",
                        enable_vlm_critic=True,
                        bake_pbr=bake_pbr if bake_res else False,
                        max_repairs=max_repairs,
                        preset_spec=preset_spec,
                    )
                    break
                except LowMemoryQueuedError as exc:
                    if mem_wait >= 18:
                        _write_suite_report(
                            out_report_path,
                            suite_name=suite_name,
                            max_repairs=max_repairs,
                            bake_pbr=bake_pbr,
                            results=results,
                        )
                        print(
                            f"[ABORT] Thiếu RAM liên tục ở [{idx}/{len(items)}] {iid}: {exc}. "
                            f"Đã lưu checkpoint {len(results)} mẫu vào {out_report_path}.",
                            flush=True,
                        )
                        sys.exit(2)
                    gc.collect()
                    print(
                        f"[queued_low_mem] Chờ 10s giải phóng RAM ({mem_wait + 1}/18) ở {iid}: {exc}",
                        flush=True,
                    )
                    time.sleep(10.0)
                except (httpx.HTTPError, OSError) as exc:
                    _write_suite_report(
                        out_report_path,
                        suite_name=suite_name,
                        max_repairs=max_repairs,
                        bake_pbr=bake_pbr,
                        results=results,
                    )
                    print(
                        f"[ABORT] Mất kết nối/timeout ở [{idx}/{len(items)}] {iid}: {exc}. "
                        f"Đã lưu checkpoint {len(results)} mẫu vào {out_report_path}.",
                        flush=True,
                    )
                    sys.exit(2)
            assert state is not None

            dt_s = round(time.perf_counter() - t0, 2)
            store.cleanup_intermediate(tenant_id, job_id, KEEP_FINAL_ARTIFACTS)

            completed = state.get("status") == "completed"
            repairs = int(state.get("repair_count", 0))
            val_errs = int((state.get("gltf_validator") or {}).get("numErrors", 0))
            rss = float(state.get("peak_rss_mb", 0.0))

            geom = state.get("geometry") or {}
            vlm = state.get("vlm") or {}
            print(
                f"[{idx}/{len(items)}] {iid:26s} status={state.get('status'):18s} "
                f"repairs={repairs} val_err={val_errs} dt={dt_s}s rss={rss}MB",
                flush=True,
            )
            results.append({
                "id": iid,
                "job_id": job_id,
                "status": state.get("status"),
                "repair_count": repairs,
                "pass_at_1": completed and repairs == 0,
                "pass_at_repair_le_3": completed,
                "duration_s": dt_s,
                "peak_rss_mb": rss,
                "validator_errors": val_errs,
                "tris_lod0": geom.get("tris_lod0", 0),
                "failure_reasons": geom.get("failure_reasons", []),
                "vlm_pass": vlm.get("pass", vlm.get("passed")) if vlm else None,
                "vlm_fix": vlm.get("fix_instruction") if vlm else "",
            })
            _write_suite_report(
                out_report_path,
                suite_name=suite_name,
                max_repairs=max_repairs,
                bake_pbr=bake_pbr,
                results=results,
            )
    finally:
        pool.shutdown()

    return _write_suite_report(
        out_report_path,
        suite_name=suite_name,
        max_repairs=max_repairs,
        bake_pbr=bake_pbr,
        results=results,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Chạy benchmark Pha 1 (prop20) & Pha 2 (articulated12) ở Mode M1")
    ap.add_argument("--suite", choices=["prop20", "articulated12", "articulated_train12", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="Giới hạn số mẫu để kiểm tra nhanh (0 = chạy đủ)")
    ap.add_argument("--max-repairs", type=int, default=3)
    ap.add_argument("--no-bake", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--lessons", default="", help="Đường dẫn JSONL thư viện kinh nghiệm (bật tự học)")
    ap.add_argument("--lessons-readonly", action="store_true", help="Chỉ đọc thư viện (chế độ đánh giá, không ghi bài học)")
    ap.add_argument("--only", default="", help="Danh sách id cách nhau bằng dấu phẩy")
    args = ap.parse_args()

    if args.suite in ("prop20", "both"):
        run_suite(
            SUITES_DIR / "prop20_suite.json",
            REPORTS_DIR / "p_3d_1_prop20.json",
            bake_pbr=not args.no_bake,
            max_repairs=args.max_repairs,
            limit=args.limit,
            only=args.only,
            lessons_path=args.lessons,
            lessons_readonly=args.lessons_readonly,
            resume=args.resume,
        )
    if args.suite == "articulated_train12":
        run_suite(
            SUITES_DIR / "articulated_train12_suite.json",
            REPORTS_DIR / "p_3d_2_articulated_train12.json",
            bake_pbr=False,
            max_repairs=args.max_repairs,
            limit=args.limit,
            only=args.only,
            lessons_path=args.lessons,
            lessons_readonly=args.lessons_readonly,
            resume=args.resume,
        )
    if args.suite in ("articulated12", "both"):
        run_suite(
            SUITES_DIR / "articulated12_suite.json",
            REPORTS_DIR / "p_3d_2_articulated12.json",
            bake_pbr=False,
            max_repairs=args.max_repairs,
            limit=args.limit,
            only=args.only,
            lessons_path=args.lessons,
            lessons_readonly=args.lessons_readonly,
            resume=args.resume,
        )


if __name__ == "__main__":
    main()

