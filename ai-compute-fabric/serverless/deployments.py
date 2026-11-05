"""Immutable versioned app deployments and rollback (Phase 5).

* `deploy` computes `version = sha256(canonical_json(functions + web_endpoints))[:16]` and stores up to `max_versions`
  versions per `(tenant, app_name)`.
* `rollback` switches `active_version` to the target (or previous) version without interrupting in-flight calls.
* Web endpoints (`/api/serverless/v1/web/{tenant}/{app}/{fn}`) resolve `(tenant, app_name, fn_id)` through `active_version`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


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


def _builtin_template_bundle(name: str) -> Optional[str]:
    fn = {"demo_v1": _sample_classify_v1, "demo_v2": _sample_classify_v2}.get(name)
    if fn is None:
        return None
    import sys
    import cloudpickle
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    return base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": fn})).decode("ascii")


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
            if not isinstance(f_copy.get("bundle_b64"), str) and isinstance(f_copy.get("builtin_template"), str):
                gen_b64 = _builtin_template_bundle(f_copy["builtin_template"])
                if gen_b64:
                    f_copy["bundle_b64"] = gen_b64
            if not isinstance(f_copy.get("bundle_b64"), str):
                raise DeploymentError(400, "bad_request", f"function {f['fn_id']!r} is missing bundle_b64")
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
        out = {
            "tenant": rec["tenant"],
            "app_name": rec["app_name"],
            "active_version": active,
            "versions": [v["version"] for v in rec["versions"]],
            "functions": sorted(vrec["functions"].keys()),
            "web_endpoints": sorted(vrec.get("web_endpoints", {}).keys()),
            "updated_at": rec.get("updated_at", vrec["created_at"]),
        }
        if previous_version is not None:
            out["previous_version"] = previous_version
        return out

