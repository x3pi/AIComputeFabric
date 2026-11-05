"""Immutable versioned app deployments, custom Python source compilation, and rollback (Phase 5 & Web IDE).

* `deploy` computes `version = sha256(canonical_json(functions + web_endpoints))[:16]` and stores up to `max_versions`
  versions per `(tenant, app_name)`.
* Supports both pre-pickled `bundle_b64` (from Python SDK/CLI), `builtin_template` (`demo_v1`, `demo_v2`, `gpu_bench`, `volume_io`),
  and custom `source_code` + `entrypoint` (from the Web Code Editor or uploaded `.py` files).
* Host safety: `source_code` is ONLY syntax-checked via `ast.parse()` on the host; compilation/execution happens exclusively
  inside the isolated rootless Docker sandbox (`_warm_runner.py`).
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

TEMPLATE_SOURCES: dict[str, tuple[str, str]] = {
    "demo_v1": (
        "classify",
        '''def classify(payload: dict) -> dict:
    """Phân tích văn bản cơ bản (v1) — chạy trong container cô lập."""
    text = str((payload or {}).get("text", ""))
    words = text.split()
    print(f"[demo_v1] classified {len(words)} words")
    return {
        "release": "v1",
        "word_count": len(words),
        "char_count": len(text),
        "preview": text[:80],
    }
''',
    ),
    "demo_v2": (
        "classify",
        '''def classify(payload: dict) -> dict:
    """Phân tích văn bản nâng cao & trích xuất top token (v2)."""
    text = str((payload or {}).get("text", ""))
    words = text.split()
    upper_words = [w.upper() for w in words[:5]]
    print(f"[demo_v2] classified {len(words)} words -> top={upper_words}")
    return {
        "release": "v2",
        "word_count": len(words),
        "char_count": len(text),
        "top_tokens": upper_words,
        "preview": text[:80],
    }
''',
    ),
    "gpu_matmul": (
        "classify",
        '''import aicf_gpu

def classify(payload: dict) -> dict:
    """Thực thi Kernel CUDA PTX nhân ma trận 2D trực tiếp trên GPU NVIDIA RTX."""
    p = payload or {}
    a = p.get("matrix_a") or p.get("A") or [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    b = p.get("matrix_b") or p.get("B") or [[7.0, 8.0], [9.0, 1.0], [2.0, 3.0]]
    gpu_info = aicf_gpu.device_info()
    mm = aicf_gpu.matmul(a, b)
    sx = aicf_gpu.saxpy(float(p.get("alpha", 2.5)), [1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0])
    print(f"[gpu_matmul] device={gpu_info['device_name']} backend={mm['backend']} kernel={mm['gpu_kernel_ms']}ms")
    return {
        "release": "cuda-matmul-v1",
        "gpu": gpu_info,
        "gpu_device": gpu_info["device_name"],
        "cuda_available": gpu_info["cuda_available"],
        "vram_free_mib": gpu_info["free_vram_mib"],
        "vram_total_mib": gpu_info["total_vram_mib"],
        "lease_vram_cap_mib": gpu_info["lease_vram_cap_mib"],
        "matmul": mm,
        "saxpy": sx,
        "matmul_result": mm["matrix"],
        "shape": mm["shape"],
        "gpu_kernel_ms": mm["gpu_kernel_ms"],
        "backend": mm["backend"],
    }
''',
    ),
    "gpu_vector_search": (
        "classify",
        '''import aicf_gpu

def classify(payload: dict) -> dict:
    """Tìm kiếm tương đồng Vector (Cosine Similarity) tăng tốc bằng GPU CUDA."""
    p = payload or {}
    query = p.get("query_vector") or p.get("query_vec") or [0.9, 0.2, 0.1, 0.8]
    docs = p.get("doc_vectors") or p.get("doc_vecs") or [
        [0.88, 0.25, 0.12, 0.79],
        [0.10, 0.95, 0.80, 0.05],
        [0.50, 0.50, 0.50, 0.50],
    ]
    labels = p.get("doc_labels") or p.get("labels") or ["doc-gpu-serverless-cuda", "doc-network-routing", "doc-vram-lease-reclaim"]
    gpu_info = aicf_gpu.device_info()
    res = aicf_gpu.cosine_search(query, docs, labels)
    print(f"[gpu_vector_search] device={gpu_info['device_name']} top={res['matches'][0] if res['matches'] else None}")
    return {
        "release": "cuda-vecsearch-v1",
        "gpu": gpu_info,
        "gpu_device": gpu_info["device_name"],
        "cuda_available": gpu_info["cuda_available"],
        "vram_free_mib": gpu_info["free_vram_mib"],
        "search": res,
        "matches": res["matches"],
        "gpu_kernel_ms": res["gpu_kernel_ms"],
        "backend": res["backend"],
    }
''',
    ),
    "gpu_bench": (
        "gpu_compute",
        '''import os
import aicf_gpu

def gpu_compute(payload: dict) -> dict:
    """Cấp phát bộ nhớ VRAM thực trên GPU NVIDIA & chạy Kernel PTX SAXPY (y = a*x + y)."""
    alloc_mib = int((payload or {}).get("alloc_mib", 64))
    n_floats = int((payload or {}).get("size", 262144))
    bench = aicf_gpu.vram_stress(alloc_mib=alloc_mib, n_floats=n_floats)
    gpu = bench["gpu"]
    print(f"[gpu_bench] device={gpu['device_name']} alloc={bench['allocated_vram_mib']}MiB kernel={bench['saxpy_kernel_ms']}ms")
    return {
        "release": "gpu-cuda-v1",
        "gpu_device": gpu["device_name"],
        "cuda_available": gpu["cuda_available"],
        "driver_version": gpu["driver_version"],
        "vram_free_mib": gpu["free_vram_mib"],
        "vram_total_mib": gpu["total_vram_mib"],
        "allocated_vram_mib": bench["allocated_vram_mib"],
        "vram_alloc_memset_ms": bench["vram_alloc_memset_ms"],
        "saxpy_elements": bench["saxpy_elements"],
        "saxpy_kernel_ms": bench["saxpy_kernel_ms"],
        "sample_result": bench["sample_result"],
        "backend": bench["backend"],
        "sandbox_pid": os.getpid(),
    }
''',
    ),
    "volume_io": (
        "scratch_note",
        '''import json
import os
import time
import aicf_gpu

def scratch_note(payload: dict) -> dict:
    """Đọc/ghi dữ liệu trực tiếp vào Volume bền vững (/mnt/volumes/shared-artifacts) trong GPU Container."""
    note = str((payload or {}).get("text", "Hello from AICF GPU Container"))
    vol_dir = "/mnt/volumes/shared-artifacts" if os.path.isdir("/mnt/volumes") else "/tmp"
    os.makedirs(vol_dir, exist_ok=True)
    path = os.path.join(vol_dir, "gpu_run_artifact.json")
    gpu_info = aicf_gpu.device_info()
    record = {
        "saved_at": time.time(),
        "gpu_device": gpu_info["device_name"],
        "length": len(note),
        "note": note,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    print(f"[volume_io] persisted {len(note)} chars to {path} on {gpu_info['device_name']}")
    return {
        "release": "storage-v1",
        "file": path,
        "size_bytes": os.path.getsize(path),
        "gpu_device": gpu_info["device_name"],
        "data": loaded,
    }
''',
    ),
    "beta9_style": (
        "classify",
        '''from aicf import endpoint, Image, Volume
import aicf_gpu

@endpoint(
    name="text-analyzer",
    gpu="any",
    vram_mb=1024,
    memory=512,
    keep_warm_seconds=120,
    image=Image.debian_slim(python_version="3.12"),
    volumes=[Volume("shared-artifacts", mount_path="/mnt/volumes/shared-artifacts")],
)
def classify(**inputs) -> dict:
    """Phong cách Beam/Beta9 (@endpoint + **inputs): chạy trên GPU NVIDIA RTX."""
    text = str(inputs.get("text", ""))
    words = text.split()
    gpu = aicf_gpu.device_info()
    sx = aicf_gpu.saxpy(2.0, [float(len(w)) for w in words[:16]] or [1.0], [1.0] * max(1, min(len(words), 16)))
    print(f"[beta9_endpoint] gpu={gpu['device_name']} words={len(words)} backend={sx['backend']}")
    return {
        "sdk_style": "beam-beta9-compatible",
        "gpu_device": gpu["device_name"],
        "cuda_available": gpu["cuda_available"],
        "vram_free_mib": gpu["free_vram_mib"],
        "word_count": len(words),
        "top_words": words[:5],
        "gpu_token_scores": sx["output"][:5],
        "gpu_kernel_ms": sx["gpu_kernel_ms"],
        "preview": text[:80],
    }
''',
    ),
}


def _sample_classify_v1(payload: dict) -> dict:
    text = str((payload or {}).get("text", ""))
    words = text.split()
    print(f"[demo_v1] classified {len(words)} words")
    return {"release": "v1", "word_count": len(words), "char_count": len(text), "preview": text[:80]}


def _sample_classify_v2(payload: dict) -> dict:
    text = str((payload or {}).get("text", ""))
    words = text.split()
    upper_words = [w.upper() for w in words[:5]]
    print(f"[demo_v2] classified {len(words)} words -> top={upper_words}")
    return {"release": "v2", "word_count": len(words), "char_count": len(text), "top_tokens": upper_words, "preview": text[:80]}


def _ensure_sandbox_aicf_shim() -> None:
    """Provide lightweight decorator shims inside the sandbox container if `aicf` is imported in user scripts."""
    if "aicf" in sys.modules:
        return
    import types
    m = types.ModuleType("aicf")

    def _passthru_deco(*_a: Any, **_kw: Any) -> Any:
        def _wrap(f: Any) -> Any:
            return f
        return _wrap

    class _DummyObj:
        def __call__(self, *_a: Any, **_kw: Any) -> Any:
            return self
        def __getattr__(self, _name: str) -> Any:
            return self
        def function(self, *_a: Any, **_kw: Any) -> Any:
            return _passthru_deco()
        def web_endpoint(self, *_a: Any, **_kw: Any) -> Any:
            return _passthru_deco()
        def cls(self, *_a: Any, **_kw: Any) -> Any:
            return _passthru_deco()

    dummy = _DummyObj()
    setattr(m, "endpoint", _passthru_deco)
    setattr(m, "task_queue", _passthru_deco)
    setattr(m, "function", _passthru_deco)
    setattr(m, "enter", _passthru_deco)
    setattr(m, "exit", _passthru_deco)
    setattr(m, "method", _passthru_deco)
    setattr(m, "App", dummy)
    setattr(m, "Image", dummy)
    setattr(m, "Volume", dummy)
    setattr(m, "Secret", dummy)
    setattr(m, "ModelCache", dummy)
    setattr(m, "Sandbox", dummy)
    sys.modules["aicf"] = m


class _SandboxSourceRunner:
    """Callable pickled into the bundle. Executes user source ONLY inside `_warm_runner.py` in the isolated container."""

    def __init__(self, source_code: str, entrypoint: str, filename: str = "user_app.py"):
        self.source_code = source_code
        self.entrypoint = entrypoint
        self.filename = filename
        self._fn: Any = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._fn is None:
            _ensure_sandbox_aicf_shim()
            ns: dict[str, Any] = {"__name__": "__aicf_user_app__", "__file__": f"/tmp/aicf_files/{self.filename}"}
            code_obj = compile(self.source_code, ns["__file__"], "exec")
            exec(code_obj, ns)  # noqa: S102 — runs strictly inside isolated rootless container (_warm_runner.py)
            fn = ns.get(self.entrypoint)
            if hasattr(fn, "fn") and callable(getattr(fn, "fn")):
                fn = getattr(fn, "fn")
            if not callable(fn):
                raise RuntimeError(f"Entrypoint {self.entrypoint!r} is not callable in {self.filename}")
            self._fn = fn
        # Support both `def fn(payload: dict)` (Modal/AICF style) and `def fn(**inputs)` (Beam/Beta9 style)
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            import inspect
            try:
                sig = inspect.signature(self._fn)
                params = list(sig.parameters.values())
                has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
                pos_params = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
                if has_var_kw and len(pos_params) == 0:
                    return self._fn(**args[0])
            except Exception:
                pass
        return self._fn(*args, **kwargs)


def _builtin_template_bundle(name: str) -> Optional[tuple[str, str, str]]:
    """Return (bundle_b64, source_code, entrypoint) for a named template."""
    import cloudpickle
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    if name == "demo_v1":
        ep, src = TEMPLATE_SOURCES["demo_v1"]
        b64 = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": _sample_classify_v1})).decode("ascii")
        return b64, src, ep
    if name == "demo_v2":
        ep, src = TEMPLATE_SOURCES["demo_v2"]
        b64 = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": _sample_classify_v2})).decode("ascii")
        return b64, src, ep
    if name in TEMPLATE_SOURCES:
        ep, src = TEMPLATE_SOURCES[name]
        b64, resolved_ep, _ = compile_source_bundle(src, ep)
        return b64, src, resolved_ep
    return None


def compile_source_bundle(source_code: str, entrypoint: str = "", filename: str = "user_app.py") -> tuple[str, str, list[str]]:
    """Validate Python syntax via `ast.parse` (no host execution) and package into a sandbox bundle.

    Returns `(bundle_b64, resolved_entrypoint, all_function_names)`.
    """
    if not isinstance(source_code, str) or not source_code.strip():
        raise DeploymentError(400, "empty_source", "Mã nguồn Python không được để trống")
    if len(source_code.encode("utf-8")) > 512 * 1024:
        raise DeploymentError(413, "source_too_large", "Mã nguồn trực tiếp tối đa 512 KiB (hãy dùng Volume cho dữ liệu lớn)")
    try:
        tree = ast.parse(source_code, filename=filename)
    except SyntaxError as e:
        line_info = f"dòng {e.lineno}" + (f", cột {e.offset}" if e.offset else "")
        raise DeploymentError(400, "syntax_error", f"Lỗi cú pháp Python tại {line_info}: {e.msg}") from None

    fn_names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not fn_names:
        raise DeploymentError(
            400,
            "no_function_defined",
            "Mã nguồn phải định nghĩa ít nhất một hàm Python ở cấp cao nhất, ví dụ: def classify(payload: dict) -> dict:",
        )

    chosen = (entrypoint or "").strip()
    if not chosen or chosen == "auto":
        public_fns = [n for n in fn_names if not n.startswith("_")]
        chosen = public_fns[0] if public_fns else fn_names[0]
    elif chosen not in fn_names:
        raise DeploymentError(
            400,
            "entrypoint_not_found",
            f"Không tìm thấy hàm entrypoint '{chosen}' trong mã nguồn. Các hàm hiện có: {', '.join(fn_names)}",
        )

    import cloudpickle
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    runner = _SandboxSourceRunner(source_code=source_code, entrypoint=chosen, filename=filename)
    bundle_doc = {
        "kind": "function",
        "obj": runner,
        "files": {
            filename: base64.b64encode(source_code.encode("utf-8")).decode("ascii"),
        },
    }
    b64 = base64.b64encode(cloudpickle.dumps(bundle_doc)).decode("ascii")
    return b64, chosen, fn_names


class DeploymentError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


class DeploymentStore:
    def __init__(self, path: Optional[Path] = None, max_versions: int = 10):
        self.path = Path(path) if path else None
        self.max_versions = max_versions
        self._mu = threading.Lock()
        # (tenant, app_name) -> {"tenant": str, "app_name": str, "active_version": str, "versions": [dict]}
        self._apps: dict[tuple[str, str], dict[str, Any]] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            for item in raw.get("apps", []):
                self._apps[(item["tenant"], item["app_name"])] = item
        except Exception:
            pass

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"apps": list(self._apps.values())}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def deploy(self, tenant: str, app_name: str, functions: list[dict], web_endpoints: Optional[dict[str, dict]] = None) -> dict:
        if not IDENT.match(tenant or "") or not IDENT.match(app_name or ""):
            raise DeploymentError(400, "bad_request", "valid tenant and app_name are required")
        if not isinstance(functions, list) or not functions:
            raise DeploymentError(400, "bad_request", "deploy requires a non-empty functions list")
        fn_map: dict[str, dict] = {}
        for f in functions:
            if not isinstance(f, dict) or not isinstance(f.get("fn_id"), str) or not IDENT.match(f["fn_id"]):
                raise DeploymentError(400, "bad_request", "every function must have a valid fn_id")
            f_copy = dict(f)
            if not isinstance(f_copy.get("bundle_b64"), str):
                if isinstance(f_copy.get("source_code"), str) and f_copy["source_code"].strip():
                    b64, resolved_ep, _ = compile_source_bundle(
                        f_copy["source_code"],
                        str(f_copy.get("entrypoint") or f_copy["fn_id"]),
                        str(f_copy.get("filename") or f"{app_name}.py"),
                    )
                    f_copy["bundle_b64"] = b64
                    f_copy["entrypoint"] = resolved_ep
                elif isinstance(f_copy.get("builtin_template"), str):
                    tpl = _builtin_template_bundle(f_copy["builtin_template"])
                    if tpl is not None:
                        f_copy["bundle_b64"], f_copy["source_code"], f_copy["entrypoint"] = tpl
            if not isinstance(f_copy.get("bundle_b64"), str):
                raise DeploymentError(400, "bad_request", f"function {f['fn_id']!r} is missing bundle_b64 or source_code")
            fn_map[f["fn_id"]] = f_copy
        web_map: dict[str, dict] = {}
        for k, w in (web_endpoints or {}).items():
            if not IDENT.match(k) or k not in fn_map:
                raise DeploymentError(400, "bad_request", f"web_endpoint {k!r} must reference a deployed function")
            web_map[k] = {"requires_auth": bool((w or {}).get("requires_auth", True)), "cors_origins": list((w or {}).get("cors_origins", []))}

        canonical = json.dumps({"app_name": app_name, "functions": fn_map, "web_endpoints": web_map}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        version = "v-" + hashlib.sha256(canonical).hexdigest()[:16]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with self._mu:
            key = (tenant, app_name)
            rec = self._apps.get(key) or {"tenant": tenant, "app_name": app_name, "active_version": version, "versions": []}
            versions: list[dict] = [v for v in rec["versions"] if v["version"] != version]
            versions.append({
                "version": version,
                "created_at": now,
                "functions": fn_map,
                "web_endpoints": web_map,
            })
            if len(versions) > self.max_versions:
                versions = versions[-self.max_versions:]
            rec["versions"] = versions
            prev_version = rec.get("active_version")
            rec["active_version"] = version
            rec["updated_at"] = now
            self._apps[key] = rec
            self._save_locked()
            return self._summary(rec, previous_version=prev_version)

    def rollback(self, tenant: str, app_name: str, target_version: Optional[str] = None) -> dict:
        with self._mu:
            key = (tenant, app_name)
            rec = self._apps.get(key)
            if not rec or not rec.get("versions"):
                raise DeploymentError(404, "not_found", f"app {app_name!r} is not deployed")
            versions = rec["versions"]
            prev = rec["active_version"]
            if target_version:
                if not any(v["version"] == target_version for v in versions):
                    raise DeploymentError(404, "version_not_found", f"version {target_version!r} not found in {app_name!r}")
                chosen = target_version
            else:
                non_active = [v["version"] for v in versions if v["version"] != prev]
                if not non_active:
                    raise DeploymentError(409, "no_previous_version", f"app {app_name!r} has no previous version to roll back to")
                chosen = non_active[-1]
            rec["active_version"] = chosen
            rec["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._save_locked()
            return self._summary(rec, previous_version=prev)

    def stop_app(self, tenant: str, app_name: str) -> bool:
        with self._mu:
            removed = self._apps.pop((tenant, app_name), None) is not None
            if removed:
                self._save_locked()
            return removed

    def list_apps(self, tenant: Optional[str] = None) -> list[dict]:
        with self._mu:
            out = []
            for (t, _), rec in sorted(self._apps.items()):
                if tenant is None or t == tenant:
                    out.append(self._summary(rec))
            return out

    def resolve_function(self, tenant: str, app_name: str, fn_id: str, version: Optional[str] = None) -> tuple[dict, str, Optional[dict]]:
        with self._mu:
            rec = self._apps.get((tenant, app_name))
            if not rec:
                raise DeploymentError(404, "app_not_found", f"app {app_name!r} is not deployed for tenant {tenant!r}")
            ver = version or rec["active_version"]
            vrec = next((v for v in rec["versions"] if v["version"] == ver), None)
            if not vrec:
                raise DeploymentError(404, "version_not_found", f"version {ver!r} not found for app {app_name!r}")
            fndef = vrec["functions"].get(fn_id)
            if not fndef:
                raise DeploymentError(404, "fn_not_found", f"function {fn_id!r} not found in {app_name}@{ver}")
            web_meta = vrec.get("web_endpoints", {}).get(fn_id)
            return dict(fndef), ver, (dict(web_meta) if web_meta is not None else None)

    @staticmethod
    def _summary(rec: dict, previous_version: Optional[str] = None) -> dict:
        active = rec["active_version"]
        vrec = next((v for v in rec["versions"] if v["version"] == active), rec["versions"][-1])
        fn_details = {}
        for fn_id, fdef in vrec["functions"].items():
            fn_details[fn_id] = {
                "fn_id": fn_id,
                "entrypoint": fdef.get("entrypoint") or fn_id,
                "source_code": fdef.get("source_code") or "",
                "gpu": fdef.get("gpu"),
                "vram_mb": int(fdef.get("vram_mb") or 0),
                "memory_mb": int(fdef.get("memory_mb") or 512),
                "timeout_s": float(fdef.get("timeout_s") or 30),
                "keep_warm_s": int(fdef.get("keep_warm_s") or 60),
            }
        out = {
            "tenant": rec["tenant"],
            "app_name": rec["app_name"],
            "active_version": active,
            "versions": [v["version"] for v in rec["versions"]],
            "functions": sorted(vrec["functions"].keys()),
            "functions_detail": fn_details,
            "web_endpoints": sorted(vrec.get("web_endpoints", {}).keys()),
            "updated_at": rec.get("updated_at", vrec["created_at"]),
        }
        if previous_version is not None:
            out["previous_version"] = previous_version
        return out
