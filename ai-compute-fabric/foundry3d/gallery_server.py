"""Máy chủ XEM kết quả (chỉ đọc) cho trang web foundry3d.html: đọc kho hiện vật, KHÔNG chạy job, KHÔNG cần gateway.

Chạy riêng, mặc định chỉ lắng nghe 127.0.0.1. Dùng lại đúng trang/JS của site-gateway nên giao diện giống bản chính thức."""
from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from foundry3d.artifact_store import ArtifactNotFoundError, ArtifactSecurityError, ArtifactStore

WEB_DIR = Path(__file__).resolve().parent.parent / "apps" / "site-gateway" / "web"
STATIC_OK = {"foundry3d.html", "foundry3d.js", "common.js", "common.css"}
SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


def _read_json(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_interrupted(rec: dict[str, Any]) -> bool:
    """Job waiting for review with neither a QA report nor a gate error = the run was cut off, not judged."""
    return rec.get("status") == "needs_human_review" and not rec.get("qa_report") and not rec.get("error")


def job_record(store: ArtifactStore, tenant: str, job_id: str) -> dict[str, Any] | None:
    man = store.tenants_dir / tenant / job_id / "manifest.json"
    if not man.exists():
        return None
    arts = store.list_artifacts(tenant, job_id)
    names = {a.filename for a in arts}
    spec: dict[str, Any] = {}
    qa: dict[str, Any] = {}
    try:
        if "asset_spec.json" in names:
            spec = json.loads(store.get_artifact_path(tenant, job_id, "asset_spec.json")[0].read_text(encoding="utf-8"))
        if "qa_report.json" in names:
            qa = json.loads(store.get_artifact_path(tenant, job_id, "qa_report.json")[0].read_text(encoding="utf-8"))
    except (OSError, ValueError, ArtifactNotFoundError, ArtifactSecurityError):
        pass
    passed = bool((qa.get("geometry") or {}).get("passed")) if qa else False
    status = "completed" if passed else "needs_human_review"
    mt = man.stat().st_mtime
    reasons = (qa.get("geometry") or {}).get("failure_reasons") or []
    return {
        "job_id": job_id, "tenant_id": tenant, "status": status, "current_stage": status,
        "repair_count": int(qa.get("repair_iteration", 0)), "prompt": spec.get("prompt", job_id),
        "archetype": spec.get("archetype", "static_prop"), "target_engine": spec.get("target_engine", "godot4"),
        "mode": spec.get("mode", "M1"), "spec": spec or None, "qa_report": qa or None,
        "artifacts": [a.model_dump() for a in arts], "events": [], "error": "; ".join(reasons)[:600],
        "created_at": mt, "updated_at": mt,
    }


WEB_TENANT = "web-user"


class _Backend:
    """Dịch vụ dựng 3D thật chạy trong tiến trình, chỉ nghe 127.0.0.1 cổng tạm; gallery chuyển tiếp kèm secret nội bộ."""

    def __init__(self, store: ArtifactStore) -> None:
        from foundry3d.providers.llm_vlm_client import GatewayLlmVlmClient
        from foundry3d.service import FoundryServiceState, build_server
        from foundry3d.warm_blender import WarmBlenderPool

        self.secret = secrets.token_hex(24)
        self.state = FoundryServiceState(proxy_secret=self.secret, store=store, pool=WarmBlenderPool(),
                                         llm_client=GatewayLlmVlmClient(), max_active_per_tenant=1)
        self.srv = build_server("127.0.0.1", 0, self.state)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes, str]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=body or None, headers={
                "X-Aicf-Proxy-Secret": self.secret, "X-Aicf-Tenant": WEB_TENANT, "Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, r.read(), r.getheader("Content-Type", "application/json")
        finally:
            c.close()


def make_handler(store: ArtifactStore, tenant: str, backend: _Backend | None = None) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # giữ terminal sạch
            return

        def _json(self, code: int, obj: Any) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _forward(self, method: str) -> None:
            path = urlparse(self.path).path
            if backend is None or not path.startswith("/api/foundry3d/v1/jobs"):
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "máy chủ xem kết quả chỉ đọc (chạy với --enable-create để tạo job)"})
                return
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n) if 0 < n <= 1_048_576 else b""
            code, data, ctype = backend.call(method, path[len("/api/foundry3d"):], body)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            self._forward("POST")

        def do_DELETE(self) -> None:
            self._forward("DELETE")

        do_PUT = do_DELETE

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                path = "/foundry3d.html"
            name = path.lstrip("/")
            if name in STATIC_OK:
                data = (WEB_DIR / name).read_bytes()
                if name == "common.js":  # chỉ ẩn trong trang xem này; file dùng chung của gateway không bị sửa
                    data = data.replace('["connect.html", "Kích hoạt máy bạn", "🔌", "user"],'.encode(), b"")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", (mimetypes.guess_type(name)[0] or "text/plain") + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/whoami":
                self._json(HTTPStatus.OK, {"role": "viewer", "name": "gallery", "open": True,
                                           "can": {"view": True, "pause": False, "tenants": False}})
                return
            if path == "/api/foundry3d/v1/status":
                active = backend.state.status_summary(WEB_TENANT).get("tenant_active_jobs", 0) if backend else 0
                self._json(HTTPStatus.OK, {"mode": "M1", "tenant_active_jobs": active, "readonly_gallery": backend is None})
                return
            if path == "/api/foundry3d/v1/jobs":
                base = store.tenants_dir / tenant
                ids = sorted((d.name for d in base.iterdir() if d.is_dir() and SAFE.match(d.name)),
                             key=lambda i: (base / i / "manifest.json").stat().st_mtime if (base / i / "manifest.json").exists() else 0,
                             reverse=True) if base.exists() else []
                recs = [r for r in (job_record(store, tenant, i) for i in ids) if r and any(a["filename"] == "asset.glb" for a in r["artifacts"])]
                if backend is not None:
                    live = {j.job_id: j.model_dump() for j in backend.state.list_jobs(WEB_TENANT)}
                    recs = [r for r in recs if r["job_id"] not in live]
                    web_base = store.tenants_dir / WEB_TENANT
                    if web_base.exists():
                        for d in web_base.iterdir():
                            if d.is_dir() and SAFE.match(d.name) and d.name not in live:
                                r = job_record(store, WEB_TENANT, d.name)
                                if r and any(a["filename"] == "asset.glb" for a in r["artifacts"]):
                                    recs.append(r)
                    recs = list(live.values()) + recs
                    recs.sort(key=lambda r: r["created_at"], reverse=True)
                for r in recs:  # danh sách nhẹ: không kèm spec/qa
                    # Chạy bị ngắt giữa chừng (khởi động lại dịch vụ, mất kết nối LLM): đang chờ duyệt nhưng không có báo cáo QA
                    # và không có lỗi cổng nào -> không phải "chưa đạt" thật.
                    r["interrupted"] = is_interrupted(r)
                    r["spec"] = r["qa_report"] = None
                    r["events"] = []
                self._json(HTTPStatus.OK, {"jobs": recs})
                return
            m = re.match(r"^/api/foundry3d/v1/jobs/([^/]+)(?:/artifacts/([^/]+))?$", path)
            if m and SAFE.match(m.group(1)):
                tn = WEB_TENANT if (backend is not None and m.group(1).startswith("f3d_")) else tenant
                if backend is not None and tn == WEB_TENANT and m.group(2) is None:
                    code, data, ctype = backend.call("GET", path[len("/api/foundry3d"):])
                    if code == 200:
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                if m.group(2) is None:
                    rec = job_record(store, tn, m.group(1))
                    self._json(HTTPStatus.OK if rec else HTTPStatus.NOT_FOUND, rec or {"error": "job not found"})
                    return
                if SAFE.match(m.group(2)):
                    try:
                        p, art = store.get_artifact_path(tn, m.group(1), m.group(2))
                    except (ArtifactNotFoundError, ArtifactSecurityError):
                        self._json(HTTPStatus.NOT_FOUND, {"error": "artifact not found"})
                        return
                    data = p.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", art.mime_type or "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return H


def main() -> None:
    ap = argparse.ArgumentParser(description="Xem kết quả AICF 3D Foundry (chỉ đọc)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18130)
    ap.add_argument("--tenant", default="bench-m1")
    ap.add_argument("--enable-create", action="store_true", help="Cho phép nhập prompt và dựng job thật (dùng Blender + LLM)")
    a = ap.parse_args()
    store = ArtifactStore()
    backend = _Backend(store) if a.enable_create else None
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(store, a.tenant, backend))
    print(f"gallery: http://{a.host}:{a.port}/  (tenant={a.tenant}, {'TẠO JOB BẬT' if backend else 'chỉ đọc'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
