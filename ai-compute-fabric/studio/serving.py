"""Choose which model the worker serves and which LoRA adapter is attached, from the web UI. The worker's own guard
(apps/worker-guard) does the switching and only accepts plain .gguf file names from its configured directories; this module
adds what the guard cannot know: which adapter belongs to which base model, and exporting a trained job to GGUF."""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from studio.config import Settings
from studio.db import check_tenant
from studio.errors import bad, conflict, unavailable
from studio.methods.lora import LoraMethod
from studio.web import Request, Router


def same_base(gguf_name: str, base_model: str) -> bool:
    """True if a GGUF file name is a build of the HF base model the adapter was trained on ("Qwen3-1.7B-Q4_K_M.gguf" for "Qwen3-1.7B").
    A name heuristic: it stops the usual mistake (adapter of one model on another) but is not a proof, llama-server has the final say."""
    stem, b = gguf_name.lower().removesuffix(".gguf"), base_model.lower()
    return stem == b or (stem.startswith(b) and stem[len(b)] in "-_.")


class Serving:
    def __init__(self, s: Settings, lora: LoraMethod):
        self.s, self.lora = s, lora

    def mount(self, r: Router):
        r.add("GET", "/v1/serving", self.get)
        r.add("POST", "/v1/serving", self.switch)

    def _worker(self, method: str, body: dict | None = None) -> dict:
        if not self.s.worker_url:
            raise unavailable("choosing the served model is not configured (--worker-url)")
        req = urllib.request.Request(self.s.worker_url.rstrip("/") + "/api/engine", method=method,
                                     data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.load(e).get("error", "")
            except Exception:
                pass
            if e.code == 400:
                raise bad(msg or "the worker refused the request", "worker_refused") from None
            raise unavailable(msg or f"worker answered {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise unavailable("the worker (worker-guard) cannot be reached") from None

    def get(self, req: Request):
        return {"configured": bool(self.s.worker_url), **self._worker("GET"), "base_models": sorted(self.s.base_models)}

    def switch(self, req: Request):
        """Body: {model: "<file>.gguf", adapter_file?: "<file>.gguf"} or {model, tenant, job} to export and attach a trained job's adapter."""
        req.need("admin")      # it changes what every user of this worker gets
        model = req.str_param("model", "")
        if not model:
            raise bad("'model' is required")
        adapter = req.str_param("adapter_file", "")
        job = req.str_param("job", "")
        if job:
            if not self.s.adapters_dir:
                raise unavailable("adapters_dir is not configured")
            tenant = check_tenant(req.str_param("tenant", ""))
            src, row = self.lora.adapter_gguf(tenant, job)
            base = (row.get("params") or {}).get("base_model", "")
            if not base or not same_base(model, base):
                raise conflict(f"the adapter was trained on {base!r} and cannot be attached to {model!r}", "adapter_base_mismatch")
            adapter = f"{tenant}-{row['id'][:8]}.gguf"
            dst = Path(self.s.adapters_dir) / adapter
            Path(self.s.adapters_dir).mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name("." + adapter + ".tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
        out = self._worker("POST", {"model": model, "lora": adapter})
        return {**out, "note": "the worker restarts the engine when the GPU is free; poll GET /v1/serving"}
