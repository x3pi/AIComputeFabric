"""LoRA fine-tuning (PEFT): dataset upload and validation, memory gate, jobs, loss curve, adapter download."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

from studio.config import Settings
from studio.db import check_tenant
from studio.errors import bad, conflict, too_large, unavailable
from studio.jobs import JobKind, JobStore, Runner, tail
from studio.registry import MethodInfo
from studio.web import Raw, Request, Router
from training.memory_estimator import ModelSpec, estimate_mib

MIN_EXAMPLES = 20
PARAM_LIMITS = {"rank": (1, 64), "steps": (10, 5000), "batch": (1, 16), "seq": (32, 1024)}
ROOT = Path(__file__).resolve().parents[2]


def _pair(obj, n: int):
    """One record -> (question, answer) or an error string."""
    if not isinstance(obj, dict):
        return "not an object"
    if isinstance(obj.get("messages"), list):
        m = obj["messages"]
        users = [x.get("content") for x in m if isinstance(x, dict) and x.get("role") == "user"]
        asst = [x.get("content") for x in m if isinstance(x, dict) and x.get("role") == "assistant"]
        if len(users) != 1 or len(asst) != 1:
            return "'messages' must hold exactly one user and one assistant message"
        q, a = users[0], asst[0]
    else:
        q, a = obj.get("question"), obj.get("answer")
    if not isinstance(q, str) or not isinstance(a, str):
        return "needs string 'question' and 'answer'"
    return q.strip(), a.strip()


def parse_dataset(fmt: str, content: str, max_examples: int, max_chars: int) -> dict:
    """Validates and normalises a Q&A dataset. Bad rows are reported (with line numbers), never silently kept."""
    rows, errors = [], []
    if fmt == "csv":
        rd = csv.DictReader(io.StringIO(content))
        if not rd.fieldnames or not {"question", "answer"} <= set(rd.fieldnames):
            raise bad("CSV needs the header columns: question,answer", "bad_dataset")
        records = ((i, r) for i, r in enumerate(rd, 2))
    elif fmt == "jsonl":
        def gen():
            for i, line in enumerate(content.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    yield i, json.loads(line)
                except ValueError:
                    yield i, None
        records = gen()
    else:
        raise bad("format must be 'jsonl' or 'csv'", "bad_format")
    seen, dup = set(), 0
    for i, obj in records:
        if obj is None:
            errors.append({"line": i, "error": "not valid JSON"})
            continue
        p = _pair(obj, i)
        if isinstance(p, str):
            errors.append({"line": i, "error": p})
            continue
        q, a = p
        if not q or not a:
            errors.append({"line": i, "error": "empty question or answer"})
        elif len(q) > max_chars or len(a) > max_chars:
            errors.append({"line": i, "error": f"longer than {max_chars} characters"})
        elif (q, a) in seen:
            dup += 1
        else:
            seen.add((q, a))
            rows.append({"question": q, "answer": a})
        if len(rows) > max_examples:
            raise too_large(f"more than {max_examples} examples")
        if len(errors) > 1000:
            raise bad("too many invalid rows (over 1000); check the format", "bad_dataset")
    return {"rows": rows, "errors": errors[:50], "error_count": len(errors), "duplicates": dup}


def split(rows: list, val_fraction: float = 0.1):
    """Deterministic split by content hash: the same row is always on the same side, whatever the order or upload."""
    tr: list = []
    va: list = []
    for r in rows:
        h = int(hashlib.sha256((r["question"] + "\x00" + r["answer"]).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        (va if h < val_fraction else tr).append(r)
    if len(va) < 2 and len(rows) >= MIN_EXAMPLES:      # tiny sets: still keep something to measure on
        k = max(2, len(rows) // 10)
        va, tr = rows[:k], rows[k:]
    return tr, va


class LoraMethod:
    info = MethodInfo(
        id="lora", title="Huấn luyện LoRA (PEFT)", status="available",
        summary="Dạy model phong cách, định dạng hoặc kiến thức chuyên ngành bằng tập câu hỏi–trả lời của bạn. Chỉ huấn luyện một adapter nhỏ (vài chục MB), model gốc giữ nguyên. Chạy trên GPU của máy này, một job một lúc; nếu GPU đang bận hoặc thiếu bộ nhớ, job chờ.",
        when_to_use="Cần model trả lời theo văn phong/định dạng cố định hoặc dùng thuật ngữ riêng mà prompt và few-shot không đủ. Cần tối thiểu vài trăm ví dụ sạch để thấy hiệu quả.",
        limits=("Chỉ mô hình có sẵn trên máy, cỡ nhỏ (đo trên Qwen3-0.6B; 1.7B có thể chạy).", "Adapter không tự nạp vào llama-server: tải bản GGUF và chạy llama-server với --lora. Adapter nhớ máy móc dữ liệu huấn luyện, không phân biệt ngữ cảnh (đã đo).",
                "Không thay cho RAG khi cần dữ liệu luôn mới.", "Độ đo hiện tại là perplexity trên 10% dữ liệu giữ lại, chưa phải đánh giá chất lượng trả lời."),
        ui={"icon": "tune"})

    def __init__(self, settings: Settings, store: JobStore, runner: Runner):
        self.s, self.jobs, self.runner = settings, store, runner
        self._specs: dict = {}

    kind = "lora"

    def job_kind(self) -> JobKind:
        s = self.s

        def command(job, d):
            prefix = list(s.lora_runner) or [s.train_python, "-m", "studio.lora_run"]
            return prefix + [str(d)]
        return JobKind(self.kind, command, lambda params: params.get("estimated_mib"), env=lambda: {"PYTHONPATH": str(ROOT)}, cwd=ROOT)

    def mount(self, r: Router):
        b = "/v1/tenants/{tenant}/lora"
        r.add("GET", "/v1/lora/base-models", self.base_models)
        r.add("POST", b + "/datasets/preview", self.preview)
        r.add("POST", b + "/jobs", self.create)
        r.add("GET", b + "/jobs", self.list_items)
        r.add("GET", b + "/jobs/{job}", self.show)
        r.add("POST", b + "/jobs/{job}/cancel", self.cancel)
        r.add("GET", b + "/jobs/{job}/log", self.log)
        r.add("GET", b + "/jobs/{job}/adapter", self.adapter)
        r.add("DELETE", b + "/jobs/{job}", self.delete)

    # -- helpers --------------------------------------------------------------------------------
    def _spec(self, name: str) -> ModelSpec:
        if name not in self.s.base_models:
            raise bad(f"unknown base model {name!r}; available: {sorted(self.s.base_models)}", "unknown_base_model")
        if name not in self._specs:
            self._specs[name] = ModelSpec.from_config(Path(self.s.base_models[name]) / "config.json")
        return self._specs[name]

    def _params(self, req: Request) -> dict:
        b = req.body
        out = {"base_model": b.get("base_model")}
        if not isinstance(out["base_model"], str):
            raise bad("'base_model' is required", "bad_param")
        for k, (lo, hi) in PARAM_LIMITS.items():
            v = b.get(k, {"rank": 8, "steps": 200, "batch": 4, "seq": 256}[k])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise bad(f"'{k}' must be an integer in [{lo}, {hi}]", "bad_param")
            out[k] = v
        lr = b.get("lr", 2e-3)
        if isinstance(lr, bool) or not isinstance(lr, (int, float)) or not 1e-6 <= lr <= 1e-1:
            raise bad("'lr' must be a number in [1e-6, 0.1]", "bad_param")
        out["lr"] = float(lr)
        out["lora_fa"] = bool(b.get("lora_fa", True))
        return out

    def _dataset(self, req: Request) -> dict:
        content = req.body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise bad("'content' (the dataset text) is required", "bad_dataset")
        if len(content.encode()) > self.s.quotas.max_upload_bytes:
            raise too_large(f"dataset larger than {self.s.quotas.max_upload_bytes} bytes")
        q = self.s.quotas
        return parse_dataset(req.body.get("format", "jsonl"), content, q.max_examples, q.max_example_chars)

    def _estimate(self, p: dict) -> float:
        # checkpointing off and AdamW are what training.lora_trainer.LoraWorker does; +reserve = what the owner keeps
        return estimate_mib(self._spec(p["base_model"]), p["batch"], p["seq"], p["rank"], False, "adamw")

    # -- routes ---------------------------------------------------------------------------------
    def base_models(self, req: Request):
        out = []
        for name in sorted(self.s.base_models):
            spec = self._spec(name)
            out.append({"name": name, "layers": spec.layers, "hidden": spec.hidden, "params_million": round(spec.model_params() / 1e6)})
        return {"base_models": out, "trainer_ready": bool(self.s.train_python or self.s.lora_runner), "gguf_export": bool(self.s.llama_cpp_dir and self.s.train_python)}

    def preview(self, req: Request):
        check_tenant(req.params["tenant"])
        d = self._dataset(req)
        tr, va = split(d["rows"])
        return {"valid": len(d["rows"]), "train": len(tr), "validation": len(va), "duplicates_removed": d["duplicates"], "invalid": d["error_count"],
                "errors": d["errors"], "samples": d["rows"][:3], "enough": len(d["rows"]) >= MIN_EXAMPLES,
                "warning": None if len(d["rows"]) >= 300 else "dưới ~300 ví dụ, kết quả thường chưa rõ rệt"}

    def create(self, req: Request):
        req.need("operator")
        t = check_tenant(req.params["tenant"])
        if not (self.s.train_python or self.s.lora_runner):
            raise unavailable("no training environment configured (train_python)")
        p = self._params(req)
        d = self._dataset(req)
        if d["error_count"]:
            raise bad(f"{d['error_count']} invalid row(s); fix them first (use /datasets/preview)", "bad_dataset")
        if len(d["rows"]) < MIN_EXAMPLES:
            raise bad(f"need at least {MIN_EXAMPLES} examples, got {len(d['rows'])}", "too_few_examples")
        need = self._estimate(p)
        budget = self.s.gpu_total_mib - self.s.gpu_reserved_mib
        if need > budget:
            raise bad(f"estimated peak {need:.0f} MiB does not fit in {budget} MiB (GPU {self.s.gpu_total_mib} - reserved {self.s.gpu_reserved_mib}); "
                      "lower batch, seq or rank", "does_not_fit")
        tr, va = split(d["rows"])
        params = {**p, "estimated_mib": round(need), "train_examples": len(tr), "val_examples": len(va)}

        def prepare(jd: Path):
            (jd / "job.json").write_text(json.dumps({**p, "base_dir": self.s.base_models[p["base_model"]]}))
            for name, rows in (("train.jsonl", tr), ("val.jsonl", va)):
                (jd / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return 202, self.jobs.create(t, self.kind, params, req.user, prepare)

    def list_items(self, req: Request):
        t = check_tenant(req.params["tenant"])
        return {"jobs": [self.jobs.public(r) for r in self.jobs.list(t, self.kind)], "runner": self.runner.status()}

    def show(self, req: Request):
        t = check_tenant(req.params["tenant"])
        row = self.jobs.get(t, req.params["job"])
        out = self.jobs.public(row, with_progress=True)
        out["runner"] = self.runner.status() if row["status"] in ("queued", "running") else None
        return out

    def cancel(self, req: Request):
        req.need("operator")
        return self.jobs.cancel(check_tenant(req.params["tenant"]), req.params["job"])

    def log(self, req: Request):
        t = check_tenant(req.params["tenant"])
        self.jobs.get(t, req.params["job"])
        return {"log": tail(self.jobs.dir(req.params["job"]) / "log.txt")}

    def _gguf(self, row: dict, d: Path) -> Raw:
        """Adapter converted for llama-server (--lora file.gguf), cached next to the job. Needs the llama.cpp checkout and the training venv."""
        if not (self.s.llama_cpp_dir and self.s.train_python):
            raise unavailable("GGUF export is not configured (llama_cpp_dir and train_python)")
        script = Path(self.s.llama_cpp_dir) / "convert_lora_to_gguf.py"
        if not script.is_file():
            raise unavailable("convert_lora_to_gguf.py not found in llama_cpp_dir")
        out = d.parent / "adapter.gguf"
        if not out.is_file():
            base = json.loads((d.parent / "job.json").read_text())["base_dir"]
            tmp = d.parent / "adapter.gguf.tmp"
            try:
                r = subprocess.run([self.s.train_python, str(script), "--base", base, "--outtype", "f16", "--outfile", str(tmp), str(d)], capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                raise unavailable("GGUF conversion timed out") from None
            if r.returncode != 0 or not tmp.is_file():
                raise unavailable("GGUF conversion failed: " + (r.stderr or r.stdout)[-300:])
            tmp.replace(out)
        return Raw(out.read_bytes(), "application/octet-stream", f"adapter-{row['id'][:8]}.gguf")

    def adapter(self, req: Request):
        t = check_tenant(req.params["tenant"])
        row = self.jobs.get(t, req.params["job"])
        d = self.jobs.dir(row["id"]) / "adapter"
        if row["status"] != "succeeded" or not d.is_dir():
            raise conflict("the job has no adapter (it did not succeed)", "no_adapter")
        fmt = (req.query.get("format") or ["safetensors"])[0]
        if fmt == "gguf":
            return self._gguf(row, d)
        if fmt != "safetensors":
            raise bad("format must be 'safetensors' or 'gguf'", "bad_format")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(d).as_posix())
        return Raw(buf.getvalue(), "application/zip", f"adapter-{row['id'][:8]}.zip")

    def delete(self, req: Request):
        req.need("admin")
        self.jobs.delete(check_tenant(req.params["tenant"]), req.params["job"])
        return {"deleted": req.params["job"]}
