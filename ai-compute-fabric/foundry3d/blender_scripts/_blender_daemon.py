"""Daemon chạy thường trú bên trong Blender 4.2 (--background --factory-startup -Y).

Giao thức nhị phân uint32 big-endian + JSON qua fd 0/1 đã được dup() riêng;
toàn bộ print() của Blender/Cycles bị hướng ra /dev/null hoặc bộ đệm stderr để không phá JSON frame.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Chắc chắn `/aicf_scripts` nằm trong sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

MAX_FRAME = int(os.environ.get("AICF_MAX_FRAME", str(32 * 1024 * 1024)))

# Tách riêng kênh frame trước khi Blender/script chạm fd 0/1
_in = os.fdopen(os.dup(0), "rb", buffering=0)
_out = os.fdopen(os.dup(1), "wb", buffering=0)
_null_r = os.open(os.devnull, os.O_RDONLY)
_null_w = os.open(os.devnull, os.O_WRONLY)
os.dup2(_null_r, 0)
os.dup2(_null_w, 1)

import json  # noqa: E402
import bpy  # noqa: E402

import evaluator_3d  # noqa: E402
import export_bundle  # noqa: E402
import postproc_pipeline  # noqa: E402
import render_6views  # noqa: E402
import rig_and_nla  # noqa: E402
import safe_exec_bpy  # noqa: E402


def _read_exact(n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = _in.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _recv() -> dict[str, Any] | None:
    hdr = _read_exact(4)
    if hdr is None:
        return None
    (n,) = struct.unpack(">I", hdr)
    if n > MAX_FRAME:
        raise ValueError(f"frame {n} > {MAX_FRAME}")
    raw = _read_exact(n)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def _send(obj: dict[str, Any]) -> None:
    b = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    _out.write(struct.pack(">I", len(b)) + b)
    _out.flush()


def _read_rss_mb() -> tuple[float, float]:
    rss_kb = 0
    hwm_kb = 0
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    rss_kb = int(ln.split()[1])
                elif ln.startswith("VmHWM:"):
                    hwm_kb = int(ln.split()[1])
    except OSError:
        pass
    return round(rss_kb / 1024.0, 2), round(hwm_kb / 1024.0, 2)


def _handle_run_pipeline(req: dict[str, Any]) -> dict[str, Any]:
    call_id = req["call_id"]
    call_dir = Path("/work/calls") / call_id
    call_dir.mkdir(parents=True, exist_ok=True)
    spec = req.get("spec") or {}
    code = req.get("bpy_code") or ""
    bake_pbr = bool(req.get("bake_pbr", False))
    bake_res = int(req.get("bake_res", 512))
    do_render = bool(req.get("render_6views", True))
    do_export = bool(req.get("export_bundle", True))
    skip_postproc = bool(req.get("skip_postproc", False))
    disabled_gates = set(req.get("disabled_gates") or [])

    exec_res = safe_exec_bpy.execute_generated_bpy(code)
    if not exec_res.get("ok"):
        return {
            "ok": False,
            "stage_failed": "exec_bpy",
            "error": exec_res.get("error", "exec failed"),
            "traceback": exec_res.get("traceback", ""),
            "scene": exec_res.get("scene", {}),
        }

    pbr_dir = call_dir / "pbr"
    post_res: dict[str, Any] = {}
    if not skip_postproc:
        post_res = postproc_pipeline.run_postproc(
            spec,
            out_pbr_dir=pbr_dir if bake_pbr else None,
            bake_pbr=bake_pbr,
            bake_res=bake_res,
        )

    rig_res = rig_and_nla.apply_articulated_rig_and_nla(spec)

    # Tầng 5A: Bàn cân toán học cố định
    baked_bc = (post_res.get("pbr_maps") or {}).get("BaseColor")
    geom = evaluator_3d.evaluate_scene(
        spec,
        baked_basecolor_path=baked_bc,
        disabled_gates=disabled_gates,
    )

    views_res: dict[str, str] = {}
    t_render_start = time.perf_counter()
    if do_render:
        views_res = render_6views.render_6views(
            call_dir / "views",
            resolution=int(req.get("render_res", 512)),
            samples=int(req.get("render_samples", 8)),
        )
    render_ms = round((time.perf_counter() - t_render_start) * 1000.0, 2)

    export_res: dict[str, Any] = {}
    if do_export:
        export_res = export_bundle.export_asset_bundle(
            call_dir / "export",
            spec,
            export_fbx=bool(req.get("export_fbx", True)),
            export_blend=bool(req.get("export_blend", True)),
        )
        geom["roundtrip_bbox_delta_m"] = export_res.get("roundtrip_bbox_delta_m", 0.0)
        val = export_res.get("gltf_validator") or {}
        geom["validator_errors"] = int(val.get("numErrors", 0))
        geom["validator_warnings"] = int(val.get("numWarnings", 0))
        if geom["validator_errors"] > 0:
            geom["passed"] = False
            geom["failure_reasons"].append(f"[glTF-Validator] {geom['validator_errors']} lỗi chuẩn Khronos")

    rss_mb, hwm_mb = _read_rss_mb()
    return {
        "ok": True,
        "call_id": call_id,
        "postproc": post_res,
        "rig": rig_res,
        "geometry": geom,
        "views": {k: f"views/{k}" for k in views_res},
        "render_6views_ms": render_ms,
        "export": export_res,
        "rss_mb": rss_mb,
        "peak_rss_mb": hwm_mb,
    }


def _handle_open_blend_check(req: dict[str, Any]) -> dict[str, Any]:
    """Mở một tệp .blend (có thể chứa autoexec script) để chứng minh cờ -Y chặn autoexec."""
    blend_rel = req["blend_rel_path"]
    safe_exec_bpy.reset_scene_empty()
    p = (Path("/work") / blend_rel).resolve()
    if not p.is_relative_to(Path("/work").resolve()):
        return {"ok": False, "error": "Đường dẫn .blend thoát /work"}
    bpy.ops.wm.open_mainfile(filepath=str(p), load_ui=False)
    return {
        "ok": True,
        "use_scripts_auto_execute": bool(bpy.context.preferences.filepaths.use_scripts_auto_execute),
        "autoexec_fail": bool(bpy.app.autoexec_fail),
        "autoexec_fail_message": str(bpy.app.autoexec_fail_message),
        "mesh_count": len([o for o in bpy.data.objects if o.type == "MESH"]),
    }


def main() -> None:
    rss_mb, hwm_mb = _read_rss_mb()
    _send({
        "ready": True,
        "blender_version": bpy.app.version_string,
        "autoexec_enabled": bool(bpy.context.preferences.filepaths.use_scripts_auto_execute),
        "rss_mb": rss_mb,
        "peak_rss_mb": hwm_mb,
    })

    while True:
        req = _recv()
        if req is None:
            break
        op = req.get("op")
        t0 = time.perf_counter()
        try:
            if op == "shutdown":
                shutil.rmtree("/work/calls", ignore_errors=True)
                shutil.rmtree("/work/.env_tmp", ignore_errors=True)
                _send({"ok": True, "shutdown": True})
                break
            elif op == "ping":
                rss_mb, hwm_mb = _read_rss_mb()
                _send({"ok": True, "exec_ms": round((time.perf_counter() - t0) * 1000.0, 2), "rss_mb": rss_mb, "peak_rss_mb": hwm_mb})
            elif op == "cleanup":
                call_id = req.get("call_id", "")
                if call_id:
                    shutil.rmtree(Path("/work/calls") / call_id, ignore_errors=True)
                _send({"ok": True})
            elif op == "run_pipeline":
                res = _handle_run_pipeline(req)
                res["exec_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                _send(res)
            elif op == "open_blend_check":
                res = _handle_open_blend_check(req)
                res["exec_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                _send(res)
            else:
                _send({"ok": False, "error": f"Op không hỗ trợ: {op!r}"})
        except Exception as exc:
            _send({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
            })


if __name__ == "__main__":
    main()
