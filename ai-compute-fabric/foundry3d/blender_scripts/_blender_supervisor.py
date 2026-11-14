"""Supervisor Python (-I -B) bên trong container: tạo thư mục tạm trong /work/.env_tmp và khởi chạy Blender 4.2 -b --factory-startup -Y."""
from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    work = Path("/work")
    env_dirs = [
        work / ".env_tmp",
        work / ".env_tmp/xdg_cfg",
        work / ".env_tmp/xdg_cache",
        work / ".env_tmp/xdg_data",
        work / ".env_tmp/blender_cfg",
        work / ".env_tmp/blender_scripts",
        work / ".env_tmp/mesa_cache",
        work / ".env_tmp/pycache",
        work / "calls",
    ]
    for d in env_dirs:
        d.mkdir(parents=True, exist_ok=True)
    os.execv(
        "/runtime/blender-4.2-lts/blender",
        [
            "/runtime/blender-4.2-lts/blender",
            "--background",
            "--factory-startup",
            "-Y",
            "--python-exit-code", "1",
            "--python", "/aicf_scripts/_blender_daemon.py",
        ],
    )


if __name__ == "__main__":
    main()

