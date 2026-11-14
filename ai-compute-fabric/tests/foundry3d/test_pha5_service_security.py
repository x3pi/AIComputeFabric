"""Kiểm thử bảo mật HTTP & cô lập tenant cho dịch vụ AICF 3D Foundry (Pha 5)."""
from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from foundry3d.artifact_store import ArtifactStore
from foundry3d.providers.llm_vlm_client import GatewayLlmVlmClient
from foundry3d.schemas import AssetSpec, JobCreateRequest, PartSpec, VLMVerdict
from foundry3d.service import FoundryServiceState, build_server
from foundry3d.warm_blender import WarmBlenderPool

PROXY_SECRET = "test-foundry3d-proxy-secret-0123456789"

DOCKER_AND_RUNTIME_OK = (
    shutil.which("docker") is not None
    and Path("/mnt/Data/aicf-3d-foundry/runtime/blender-4.2-lts/blender").exists()
)

BOX_BPY = """import bpy
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.5))
obj = bpy.context.active_object
obj.name = "crate"
mat = bpy.data.materials.new("CrateWood")
mat.use_nodes = True
bsdf = mat.node_tree.nodes.get("Principled BSDF")
if bsdf:
    bsdf.inputs["Base Color"].default_value = (0.42, 0.25, 0.12, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.72
obj.data.materials.append(mat)
"""


def _crate_spec() -> AssetSpec:
    return AssetSpec(
        name="wooden_crate",
        prompt="A simple wooden crate box",
        archetype="static_prop",
        target_engine="godot4",
        mode="M1",
        dimensions_m=(1.0, 1.0, 1.0),
        poly_budget_lod0=500,
        parts=[
            PartSpec(
                id="crate",
                role="main wooden crate",
                approx_size_m=(1.0, 1.0, 1.0),
                color_hint="warm brown wood",
            )
        ],
    )


class _StubLlmClient(GatewayLlmVlmClient):
    """LLM/VLM giả lập cục bộ để test HTTP service không chạm cổng live :18080."""

    def __init__(self) -> None:
        super().__init__(gateway_url="http://127.0.0.1:0", api_key="test-key")

    def plan_asset_spec(self, prompt: str, **kwargs: Any) -> AssetSpec:
        return _crate_spec()

    def generate_bpy_code(
        self, spec: AssetSpec, repair_feedback: str = "", previous_code: str = "", **kwargs: Any
    ) -> str:
        return BOX_BPY

    def judge_6views(
        self, spec: AssetSpec, view_paths: list[Path], geometry_summary: dict[str, Any] | None = None
    ) -> VLMVerdict:
        return VLMVerdict(
            **{
                "pass": True,
                "semantic_score": 5,
                "proportion_score": 5,
                "front_axis_correct": True,
                "texture_clean": True,
            }
        )


class _FakePool(WarmBlenderPool):
    """Pool Blender giả lập xuất GLB tối thiểu có magic b'glTF' để test nhanh HTTP/SSE."""

    def __init__(self, work_base: Path) -> None:
        super().__init__(work_base=work_base)
        self._counter = 0

    def run_task(
        self, op: str, payload: dict[str, Any], timeout_s: float | None = None
    ) -> tuple[dict[str, Any], Path, bool]:
        self._counter += 1
        call_id = f"call-fake-{self._counter}"
        call_dir = self.work_base / "calls" / call_id
        export_dir = call_dir / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        glb_path = export_dir / "asset.glb"
        # Header GLB v2 tối thiểu 20 byte: magic 'glTF' + version 2 + length 20
        glb_path.write_bytes(b"glTF\x02\x00\x00\x00\x14\x00\x00\x00\x00\x00\x00\x00JSON")
        resp = {
            "ok": True,
            "call_id": call_id,
            "peak_rss_mb": 64.0,
            "geometry": {
                "passed": True,
                "tris_lod0": 12,
                "tris_lod1": 12,
                "tris_lod2": 12,
                "tris_collider": 12,
                "dimensions_measured_m": [1.0, 1.0, 1.0],
                "z_min_m": 0.0,
                "gates": [{"gate": "G1", "passed": True, "summary": "ok"}],
                "failure_reasons": [],
            },
            "export": {
                "files": {"glb": f"/work/calls/{call_id}/export/asset.glb"},
                "gltf_validator": {"numErrors": 0, "numWarnings": 0},
            },
            "postproc": {"pbr_maps": {}},
            "views": {},
        }
        return resp, call_dir, False

    def cleanup_call_dir(self, call_work_dir: Path) -> None:
        shutil.rmtree(call_work_dir, ignore_errors=True)

    def shutdown(self) -> None:
        return None


def _http_req(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(data))
    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def _wait_terminal(base_url: str, tenant: str, job_id: str, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    hdrs = {"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": tenant}
    while time.monotonic() < deadline:
        code, _, raw = _http_req(base_url, "GET", f"/v1/jobs/{job_id}", headers=hdrs)
        assert code == 200
        obj = json.loads(raw.decode("utf-8"))
        if obj["status"] in ("completed", "needs_human_review", "failed", "cancelled"):
            return obj
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} không hoàn tất trong {timeout_s}s")


@pytest.fixture()
def foundry_server(tmp_path: Path):
    store = ArtifactStore(root=tmp_path / "artifacts")
    pool = _FakePool(work_base=tmp_path / "work")
    state = FoundryServiceState(
        proxy_secret=PROXY_SECRET,
        store=store,
        pool=pool,
        llm_client=_StubLlmClient(),
        max_active_per_tenant=1,
    )
    srv = build_server("127.0.0.1", 0, state)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, state
    finally:
        srv.shutdown()
        srv.server_close()
        pool.shutdown()


def test_pha5_auth_headers_and_healthz(foundry_server: tuple[str, FoundryServiceState]) -> None:
    base_url, _ = foundry_server

    code, _, body = _http_req(base_url, "GET", "/healthz")
    assert code == 200
    assert json.loads(body)["status"] == "ok"

    # Thiếu hoặc sai X-Aicf-Proxy-Secret -> 401
    code, _, _ = _http_req(base_url, "GET", "/v1/status", headers={"X-Aicf-Tenant": "tenant-a"})
    assert code == 401
    code, _, _ = _http_req(
        base_url,
        "GET",
        "/v1/status",
        headers={"X-Aicf-Proxy-Secret": "wrong-secret-0123456789", "X-Aicf-Tenant": "tenant-a"},
    )
    assert code == 401

    # Thiếu hoặc sai X-Aicf-Tenant -> 400
    code, _, _ = _http_req(
        base_url,
        "GET",
        "/v1/status",
        headers={"X-Aicf-Proxy-Secret": PROXY_SECRET},
    )
    assert code == 400
    for bad_tenant in ("", "../etc", "a/b", ".hidden"):
        code, _, _ = _http_req(
            base_url,
            "GET",
            "/v1/status",
            headers={"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": bad_tenant},
        )
        assert code == 400

    # Hợp lệ -> 200
    code, _, body = _http_req(
        base_url,
        "GET",
        "/v1/status",
        headers={"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": "tenant-a"},
    )
    assert code == 200
    assert json.loads(body)["tenant_id"] == "tenant-a"


def test_pha5_cross_tenant_isolation_sse_and_artifacts(
    foundry_server: tuple[str, FoundryServiceState],
) -> None:
    base_url, _ = foundry_server
    hdr_a = {"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": "tenant-a"}
    hdr_b = {"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": "tenant-b"}

    code, _, raw = _http_req(
        base_url,
        "POST",
        "/v1/jobs",
        headers=hdr_a,
        json_body={
            "prompt": "A wooden crate box",
            "enable_vlm_critic": False,
            "max_repairs": 0,
        },
    )
    assert code == 201
    job_id = json.loads(raw)["job_id"]
    rec = _wait_terminal(base_url, "tenant-a", job_id)
    assert rec["status"] == "completed", rec

    # Tenant B bị cô lập hoàn toàn (404 và list rỗng)
    code, _, list_b = _http_req(base_url, "GET", "/v1/jobs", headers=hdr_b)
    assert code == 200
    assert json.loads(list_b)["jobs"] == []

    for method, path in (
        ("GET", f"/v1/jobs/{job_id}"),
        ("DELETE", f"/v1/jobs/{job_id}"),
        ("GET", f"/v1/jobs/{job_id}/events"),
        ("GET", f"/v1/jobs/{job_id}/artifacts/asset.glb"),
    ):
        c, _, _ = _http_req(base_url, method, path, headers=hdr_b)
        assert c == 404, f"Expected 404 for cross-tenant {method} {path}, got {c}"

    # Tenant A đọc được SSE chứa event done
    code, sse_hdrs, sse_body = _http_req(base_url, "GET", f"/v1/jobs/{job_id}/events", headers=hdr_a)
    assert code == 200
    assert "text/event-stream" in sse_hdrs.get("content-type", "")
    sse_txt = sse_body.decode("utf-8")
    assert "event: done" in sse_txt

    # Tenant A tải được asset.glb bắt đầu bằng magic GLB b'glTF'
    code, _, glb = _http_req(base_url, "GET", f"/v1/jobs/{job_id}/artifacts/asset.glb", headers=hdr_a)
    assert code == 200
    assert glb.startswith(b"glTF")

    # Chặn path traversal trên tên tệp hiện vật
    for bad_name in ("..%2Fsecret", "sub%5Casset.glb"):
        c, _, _ = _http_req(
            base_url,
            "GET",
            f"/v1/jobs/{job_id}/artifacts/{bad_name}",
            headers=hdr_a,
        )
        assert c in (403, 404)


def test_pha5_per_tenant_concurrency_cap_429(
    foundry_server: tuple[str, FoundryServiceState],
) -> None:
    base_url, state = foundry_server
    gate = threading.Event()
    req = JobCreateRequest(
        prompt="Hold crate job",
        enable_vlm_critic=False,
        max_repairs=0,
    )
    first = state.create_job(
        "tenant-cap",
        req,
        preset_spec=_crate_spec(),
        preset_bpy_code=BOX_BPY,
        hold_hook=gate,
    )
    try:
        # Job thứ 2 cùng tenant phải bị chặn 429
        code, _, _ = _http_req(
            base_url,
            "POST",
            "/v1/jobs",
            headers={"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": "tenant-cap"},
            json_body={"prompt": "Second job same tenant", "enable_vlm_critic": False},
        )
        assert code == 429

        # Tenant khác vẫn tạo job bình thường (201)
        code_other, _, raw_other = _http_req(
            base_url,
            "POST",
            "/v1/jobs",
            headers={"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": "tenant-other"},
            json_body={"prompt": "Job for other tenant", "enable_vlm_critic": False},
        )
        assert code_other == 201
        other_id = json.loads(raw_other)["job_id"]
    finally:
        gate.set()

    rec1 = _wait_terminal(base_url, "tenant-cap", first.job_id)
    rec2 = _wait_terminal(base_url, "tenant-other", other_id)
    assert rec1["status"] == "completed", rec1.get("error")
    assert rec2["status"] == "completed", rec2.get("error")


@pytest.mark.skipif(not DOCKER_AND_RUNTIME_OK, reason="Cần Docker + runtime Blender 4.2 để test end-to-end")
def test_pha5_real_blender_sandbox_http_e2e(tmp_path: Path) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts")
    pool = WarmBlenderPool()
    state = FoundryServiceState(
        proxy_secret=PROXY_SECRET,
        store=store,
        pool=pool,
        llm_client=_StubLlmClient(),
        max_active_per_tenant=1,
    )
    srv = build_server("127.0.0.1", 0, state)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    hdrs = {"X-Aicf-Proxy-Secret": PROXY_SECRET, "X-Aicf-Tenant": "tenant-e2e"}
    try:
        code, _, raw = _http_req(
            base_url,
            "POST",
            "/v1/jobs",
            headers=hdrs,
            json_body={"prompt": "A simple wooden crate box", "enable_vlm_critic": False, "max_repairs": 0},
        )
        assert code == 201
        job_id = json.loads(raw)["job_id"]
        rec = _wait_terminal(base_url, "tenant-e2e", job_id, timeout_s=45.0)
        assert rec["status"] == "completed", rec.get("error")
        code, _, glb = _http_req(base_url, "GET", f"/v1/jobs/{job_id}/artifacts/asset.glb", headers=hdrs)
        assert code == 200
        assert glb.startswith(b"glTF")
        code, _, qa_raw = _http_req(base_url, "GET", f"/v1/jobs/{job_id}/artifacts/qa_report.json", headers=hdrs)
        assert code == 200
        qa = json.loads(qa_raw.decode("utf-8"))
        assert qa["geometry"]["passed"] is True
    finally:
        srv.shutdown()
        srv.server_close()
        pool.shutdown()

