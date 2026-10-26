"""Full fine-tuning: listed so the UI is honest about it, and refused with numbers rather than hidden."""
from __future__ import annotations

from pathlib import Path

from studio.config import Settings
from studio.registry import MethodInfo
from studio.web import Request, Router
from training.memory_estimator import ModelSpec

BYTES_PER_PARAM_ADAMW_MIXED = 16     # fp32 master 4 + bf16 weight 2 + grad 2 + Adam m,v 8  (a common rule of thumb, NOT measured here)


class FullFineTuneMethod:
    info = MethodInfo(
        id="full_ft", title="Fine-tune toàn bộ (Full FT)", status="unsupported",
        reason="Cần cập nhật mọi tham số nên bộ nhớ lớn gấp nhiều lần LoRA; không vừa GPU 12 GB của máy này với model có ý nghĩa. Dùng LoRA, hoặc chạy trên cụm GPU lớn.",
        summary="Huấn luyện lại toàn bộ trọng số model. Chất lượng cao nhất nhưng tốn GPU nhất, cần nhiều dữ liệu và dễ làm model quên kiến thức cũ.",
        when_to_use="Có hàng chục nghìn ví dụ chất lượng, có cụm GPU lớn, và LoRA đã được thử mà chưa đủ.",
        limits=("Không có job chạy được trên máy này.",), ui={"icon": "lock"})

    def __init__(self, settings: Settings):
        self.s = settings

    def mount(self, r: Router):
        r.add("GET", "/v1/full-ft/estimate", self.estimate)

    def estimate(self, req: Request):
        rows = []
        budget = self.s.gpu_total_mib - self.s.gpu_reserved_mib
        for name, d in sorted(self.s.base_models.items()):
            params = ModelSpec.from_config(Path(d) / "config.json").model_params()
            need = params * BYTES_PER_PARAM_ADAMW_MIXED / 2 ** 20
            rows.append({"base_model": name, "params_million": round(params / 1e6), "weights_grads_optimizer_mib": round(need), "budget_mib": budget, "state_fits_without_activations": need <= budget})
        return {"label": "ESTIMATE", "basis": f"{BYTES_PER_PARAM_ADAMW_MIXED} bytes/param (mixed precision AdamW), activations not included; not measured", "models": rows, "supported": False, "reason": self.info.reason}
