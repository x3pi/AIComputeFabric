#!/usr/bin/env python3
"""Build the architecture-document vs project comparison PDF (HTML -> Chrome headless).

Source document: AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx (25/10/2026).
Every status below was assigned by reading the repository (code, tests, reports) on 2026-11-07; the evidence
column names the file or measurement. Labels: [ĐO] measured / test run, [ĐỌC MÃ] read in code, [ƯỚC TÍNH] judgement.
Usage: python3 build_architecture_review_pdf.py [workdir]
"""
from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

HW = Path(__file__).resolve().parent
REP = HW.parents[0] / "reports"
OUT_PDF = REP / "DANH_GIA_SO_SANH_TAI_LIEU_KIEN_TRUC_VS_PROJECT.pdf"
WORK = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/aicf_arch_review")
WORK.mkdir(parents=True, exist_ok=True)

# status: D done, P partial, N not done, X achieved by a different route than the document proposed
LABEL = {"D": "Đã làm", "P": "Một phần", "N": "Chưa làm", "X": "Làm khác tài liệu"}

# (section, requirement in the document, status, evidence in the project)
ITEMS = [
    ("§2 Kiến trúc", "Site Gateway (Go + gRPC): registry, lease, link graph, policy, cache", "D",
     "apps/site-gateway: Register/Heartbeat, LeaseService, router WFQ + KV affinity + deadline, mTLS, RBAC, audit chuỗi băm SHA-256 [ĐỌC MÃ]; go test pass [ĐO]"),
    ("§2 Kiến trúc", "PC Worker Agent: profiling, quota, idle, sandbox, signed job, heartbeat (Windows/Linux)", "P",
     "apps/worker-guard: đọc VRAM thật, thu hồi GPU, heartbeat 6-bucket, Windows service. Chưa thấy signed job; quota CPU/RAM mới ở mức gắn nhân CPU [ĐỌC MÃ]"),
    ("§2 Kiến trúc", "Global Control Plane: chính sách, metering, federation", "P",
     "apps/global-control-plane: mTLS 1.3, thu hồi serial, DataClass, sổ cái bất biến; test đơn vị pass. Chưa chạy qua WAN/nhiều site thật [ĐỌC MÃ]"),
    ("§2 Kiến trúc", "Inference Runtime: llama.cpp là mặc định; Prima/MoLink đối chứng; KT/vLLM có điều kiện", "P",
     "llama.cpp chạy production, vLLM/SGLang chỉ để đối chứng (P10–P13). Prima/MoLink chưa tích hợp"),
    ("§2 Kiến trúc", "Agent Runtime: LangGraph + Fabric task API", "D",
     "agents/orchestrator.py (LangGraph), task_bus.py (NATS JetStream), dag_api.py"),
    ("§2 Kiến trúc", "Audio & Training: ASR/TTS, fine-tune, DiLoCo, đánh giá", "P",
     "Có ASR, LoRA, mô phỏng DiLoCo; TTS/enhancement/DDP chưa đúng như tài liệu (xem §7)"),
    ("§2 Kiến trúc", "Observability & Data: Postgres, object storage, NATS, OpenTelemetry", "P",
     "Postgres/pgvector + NATS có; /metrics và audit log có; chưa có OpenTelemetry, Grafana, object storage"),
    ("§2 Kiến trúc", "Ba mặt phẳng control / data / management (cert rotation, provisioning)", "P",
     "Control và data tách rõ; chứng chỉ do công cụ pki cấp tay, leaf hết hạn 25/01/2027, chưa tự xoay vòng"),
    ("§3 Luồng", "Luồng một yêu cầu Qwen: xác thực tenant, định tuyến, lease, stream, metrics, failover", "D",
     "/v1/chat/completions qua gateway → router → worker; test affinity, fairness, inference_load"),
    ("§3 Luồng", "Định danh model bằng hash weights + tokenizer + revision", "D",
     "ModelIdentity (proto), core/model_registry/provenance.py, scheduler từ chối MODEL_IDENTITY_MISMATCH"),
    ("§3 Luồng", "Workflow AI Agent → DAG task có kiểu → gộp kết quả", "D",
     "agents/ (15 typed task, validator), p5_agent_dag_e2e.json (DAG 14 bước)"),
    ("§3 Luồng", "Job fine-tune audio: manifest → kiểm quyền → tách speaker → train → cổng WER/CER → đăng ký artifact", "P",
     "audio/ingestion có tách speaker và manifest; cổng chất lượng và đăng ký model chưa thành quy trình khép kín"),
    ("§4 Engine", "LOCAL_REPLICA: Qwen nhỏ trên từng PC, nhiều PC phục vụ song song", "D",
     "Chế độ split: Qwen3-4B 554,9 tok/s tổng với 4 luồng/máy [ĐO]; 2 node chạy song song scale 96,4% [ĐO]"),
    ("§4 Engine", "LOCAL_HYBRID: GPU + RAM, expert nóng/lạnh cho MoE", "D",
     "smart_1gpu với -ncmoe; B06 trên RTX 3060; Qwen3-30B-A3B 44 tok/s ở 8k [ĐO]"),
    ("§4 Engine", "DISTRIBUTED_MODEL bằng Prima.cpp / MoLink", "N",
     "Chỉ có dòng trong upstream-manifest.yaml (license UNVERIFIED); không có adapter hay benchmark B04/B05"),
    ("§4 Engine", "DISTRIBUTED_MODEL: chạy được model vượt VRAM một máy bằng nhóm 2 node", "X",
     "Làm bằng GGML RPC của llama.cpp (chế độ merged): Qwen3-30B-A3B ~135 tok/s, gấp 2,1× (ctx 8k) và 2,7× (ctx 32k) RTX 5070 đơn lẻ [ĐO, README]. Tài liệu coi RPC chỉ là proof-of-concept"),
    ("§4 Engine", "Nhóm 3–4 PC cùng một model", "N",
     "Mới có 2 máy (RTX 3060 + RTX 5070); chưa thử 3–4 node"),
    ("§4 Engine", "SPECULATIVE decoding", "P",
     "B07: Qwen3-8B + draft 0,6B nhanh 1,185×, acceptance 0,49, nhưng đầu ra giống baseline chỉ 25% [ĐO]. Chưa đưa vào production"),
    ("§4 Engine", "Native MoE và distributed native MoE (bảo toàn phép tính)", "D",
     "Qwen3-30B-A3B và Coder-30B chạy native, merged 2 card; full-164 HumanEval 154/164 cho Coder-30B [ĐO]"),
    ("§4 Engine", "Agent-level experts (router chọn Agent theo loại việc)", "P",
     "Có Best-of-N cho code và Agent task; chưa có router chọn expert-agent đúng mô tả"),
    ("§4 Engine", "KTransformers (cổng ISA, backend riêng)", "P",
     "P8: đo baseline và micro-benchmark kt-kernel, cổng CPU ISA trong core/hardware_profiler; chưa tích hợp làm backend"),
    ("§5 Scheduler", "Lọc cứng + chọn argmin thời gian dự kiến (bộ nhớ, identity, residency, SLA)", "D",
     "core/adaptive_scheduler/scheduler.py (74 dòng, thuần logic), unit test pass"),
    ("§5 Scheduler", "Scheduler chi phí nói trên được cắm vào đường định tuyến thật", "P",
     "Chỉ test import module này. Gateway thật dùng WFQ + affinity + priority + deadline (router.go), không dùng hàm chi phí [ĐỌC MÃ]"),
    ("§5 Scheduler", "Đường cong benchmark theo (model, backend, phần cứng) + predictor học từ dữ liệu", "N",
     "Chưa có predictor; số đo nằm trong báo cáo, không nạp lại vào scheduler"),
    ("§5 Scheduler", "Lease có TTL, gia hạn bằng heartbeat; nhường PC cho người dùng", "D",
     "leases.go; thu hồi GPU trung vị 174 ms (Windows), 273 ms (Linux) < 2 s [ĐO]"),
    ("§5 Scheduler", "Chống quá tải: quota tenant, giới hạn đồng thời, backpressure", "D",
     "Tenant fairness benchmark, MaxQueue, max_inflight; test fair/leases"),
    ("§5 Scheduler", "3 mức ưu tiên: người dùng > doanh nghiệp > federation", "P",
     "Mức 1 làm tốt (thu hồi GPU); mức doanh nghiệp/federation có chính sách nhưng chưa đo dưới tải thật"),
    ("§6 Agent", "Graph Orchestrator (LangGraph) + checkpoint", "D", "agents/orchestrator.py, task_bus.py"),
    ("§6 Agent", "Task Planner tách mục tiêu thành DAG kiểu", "P", "Orchestrator dựng DAG; chưa có module planner riêng"),
    ("§6 Agent", "Tool Registry (allowlist có kiểu) và không có shell tổng quát", "D", "agents/tool_registry.py; test an ninh pass"),
    ("§6 Agent", "Task Executor + sandbox", "D", "agents/sandbox.py (bubblewrap/Docker); p7_pha0_sandbox_mutation.json"),
    ("§6 Agent", "Knowledge Service: chunking, pgvector, rerank, cô lập tenant", "D", "agents/knowledge.py; p5_rag_quality_small_vi.json"),
    ("§6 Agent", "Result Validator", "D", "agents/validator.py"),
    ("§6 Agent", "15 loại task chuẩn", "D", "tests/unit/test_15_typed_task_schemas.py"),
    ("§6 Agent", "Ví dụ phân tích 10.000 cuộc gọi chạy thật", "P",
     "Các mảnh (VAD, ASR, trích xuất, RAG) có; chưa có lần chạy quy mô lớn"),
    ("§7 Audio", "ASR bằng Qwen3-ASR", "D", "Qwen3-ASR-0.6B: WER 8,1% trên 100 clip FLEURS tiếng Việt [ĐO]"),
    ("§7 Audio", "TTS bằng Qwen3-TTS", "P", "Vòng TTS→ASR dùng mms-tts-vie, không phải Qwen3-TTS"),
    ("§7 Audio", "Enhancement bằng DeepFilterNet", "P", "Đang là cổng phổ numpy; WER nhiễu 13,6% → 12,9% [ĐO]"),
    ("§7 Audio", "Nhận diện sự kiện âm thanh (BEATs)", "N", "Không có"),
    ("§7 Training", "LoRA/PEFT một GPU", "D", "training/lora_trainer.py, QLoRA trong Studio, memory estimator đã khớp thực đo"),
    ("§7 Training", "Quy tắc gộp LoRA không lấy trung bình A và B tùy tiện", "D", "training/lora_merger.py: naive, exact, lora_fa, svd"),
    ("§7 Training", "DDP 2–4 GPU thật", "N", "Chỉ có mô phỏng: 4 worker chạy tuần tự trên một RTX 3060"),
    ("§7 Training", "DiLoCo thử nghiệm", "P",
     "p6_diloco_vs_ddp: 2 seed, lưới lr, chọn theo validation; nhưng chạy tuần tự một GPU, chưa đa máy"),
    ("§8 Hạ tầng", "PostgreSQL + pgvector", "D", "agents/knowledge.py (psycopg, pgvector)"),
    ("§8 Hạ tầng", "NATS JetStream (at-least-once, idempotent)", "D", "agents/task_bus.py, tests/integration/test_task_bus.py"),
    ("§8 Hạ tầng", "Object storage cho checkpoint / model shard", "N", "Dùng thư mục cục bộ; chưa có kho đối tượng"),
    ("§8 Hạ tầng", "Model Registry có artifact ký số", "P", "Hash, provenance, kiểm trust_remote_code có; ký model chưa có (cosign chỉ dùng cho SBOM)"),
    ("§8 Hạ tầng", "Metrics/traces: OpenTelemetry, Prometheus, Grafana", "P", "Có /metrics và /api/stats; không có OpenTelemetry/Grafana"),
    ("§9 WAN", "AdvertiseCapacity / OfferJob / AcceptJob / ReportUsage", "P", "apps/global-control-plane/plane.go + test; chưa có proto riêng"),
    ("§9 WAN", "mTLS liên site, thu hồi chứng chỉ, DataClass không rời site gốc", "D", "plane.go, policy.go; gcp_test, pki_test pass"),
    ("§9 WAN", "Pilot federation 2–3 site thật", "N", "Chưa có; chỉ test đơn vị"),
    ("§10 Monorepo", "upstream-manifest: commit ghim, SPDX, người phụ trách", "P",
     "Có file với SHA ứng viên; legal_reviewed=false cho mọi mục, 3 mục license UNVERIFIED"),
    ("§10 Monorepo", "NOTICE và SBOM ký cosign", "D", "NOTICE, Makefile verify, tests/security/test_sbom_signatures.py"),
    ("§10 Monorepo", "third_party/patches và pinned-forks", "N", "Thư mục chỉ có upstream-manifest.yaml"),
    ("§10 Monorepo", "Cấu trúc thư mục theo Phụ lục A", "P",
     "Có apps/core/inference/agents/audio/training/contracts/benchmarks; federation/, infrastructure/, inference/moe, speculative, audio/asr, tts rỗng"),
    ("§11 Contract", "Protobuf: WorkerCapacity, ModelIdentity, WorkloadJob, Lease; Fleet + Lease service", "D", "contracts/protobuf/aicf/v1 + golden test"),
    ("§11 Contract", "Service Placement, Inference, AgentJobs, Training, Federation (proto)", "N", "Chưa có proto; các API này tồn tại dưới dạng REST/Go struct"),
    ("§11 Contract", "OpenAPI v1 kiểm tự động theo route", "D", "contracts/openapi/aicf_v1.yaml"),
    ("§11 Contract", "Engine lifecycle 7 hàm (probe…stop) + adapter", "D", "inference/engine.py; adapter llama, vllm (sglang/prima/molink chưa có)"),
    ("§13 An ninh", "mTLS, RBAC, audit, tenant isolation", "D", "mtls.go, rbac.go, audit.go; tests/security 29 ca"),
    ("§13 An ninh", "Cấm generic shell, allowlist tool, sandbox, không trust_remote_code", "D", "tool_registry, sandbox, model_registry/security.py"),
    ("§13 An ninh", "Kiểm toán giấy phép model Qwen, dataset giọng nói, mã nguồn", "N", "legal_reviewed=false; chưa có kết quả rà soát"),
    ("§13 An ninh", "Không để LAN mặc định tin cậy / không lộ RPC", "P",
     "Mật khẩu admin mặc định 'Admin@AICF2026!' nằm trong deploy/demo/run_demo.sh; 'demo-admin-token' còn là giá trị dự phòng ở nhiều script; cổng GGML RPC không có xác thực. Đã dựng proxy lọc cho LAN nhưng chưa là cấu hình mặc định [ĐỌC MÃ]"),
    ("§15 Kiểm thử", "Bộ kiểm thử theo tầng (unit, integration, correctness, resilience, security)", "D",
     "356 pass, 16 skip, 1 xfail trên unit+security+correctness+resilience; go test pass 5 module [ĐO 07/11]"),
    ("§15 Kiểm thử", "Performance và audio-quality trong thư mục tests/", "P", "tests/performance và tests/audio_quality rỗng; benchmark nằm ở benchmarks/"),
    ("§15 Kiểm thử", "CI tự động", "D", ".github/workflows/ci.yml, Makefile verify-release-v1"),
    ("§14 Deliverable", "ADR cho từng quyết định lớn", "D", "docs/adr: 4 ADR (GPUStack, bus, serverless, web editor)"),
    ("§14 Deliverable", "Machine inventory + baseline vLLM TP/PP", "D", "p0_probe_*.json; P10–P13 (vLLM/SGLang PP, 2 GPU)"),
]

# (id, tài liệu yêu cầu, trạng thái, thực tế)
BENCH = [
    ("B01", "1 PC, Qwen nhỏ: TTFT, TPOT, joule/token, VRAM", "D", "llama-bench Qwen3-4B/8B trên 3060 và 5070; joule/token đo ở full-164 (đối chiếu NVML sai −1,9% / +0,1%)"),
    ("B02", "1 PC, Qwen lớn + offload RAM", "D", "Qwen2.5-32B Q4 trên 3060 (p0_B02)"),
    ("B03", "3–4 PC, vLLM TP/PP baseline", "P", "Chỉ 2 GPU; vLLM/SGLang PP qua WSL2 (P10–P13); đa nút vLLM không chạy được qua NAT của Windows 10"),
    ("B04", "3–4 PC, Prima.cpp", "N", "Chưa làm"),
    ("B05", "3–4 PC, MoLink", "N", "Chưa làm"),
    ("B06", "Qwen MoE local / hybrid", "D", "p4_B06: Qwen3-30B-A3B với n-cpu-moe 48→0"),
    ("B07", "Speculative target/draft", "D", "p4_B07: 1,185×, acceptance 0,49; lưu ý đầu ra không giống baseline"),
    ("B08", "8–40 PC, Agent task độc lập", "P", "Mô phỏng worker (hiệu suất 85,5% / 74,1% / 56,8% ở 8 / 20 / 40) ghi rõ SIMULATED; thật mới 2 máy, scale 96,4%"),
    ("B09", "4–8 PC, audio training DDP/DiLoCo", "P", "Mô phỏng 4 worker tuần tự trên một GPU"),
]

GATES = [
    ("Distributed Qwen: giảm TPOT ≥30% so với PP baseline, hoặc chạy được model không vừa máy đơn",
     "Đạt theo vế thứ hai: Qwen3-30B-A3B (18,6 GB) chạy 100% trong VRAM hai card, 135 tok/s; Coder-30B 154/164 HumanEval. Chưa so TPOT với PP baseline cũ cùng điều kiện [ĐO]"),
    ("Task parallel: tổng throughput ≥80% tổng các node độc lập",
     "Đạt ở 2 node thật (96,4%); ở 40 worker mô phỏng chỉ 56,8%, chưa kiểm chứng ở quy mô 40+ thật [ĐO]"),
    ("Ưu tiên tài nguyên: không chiếm ngoài quota, thu hồi giữa task",
     "Đạt về thu hồi GPU (< 2 s, 10/10 lần trên Windows); chưa thử dưới tải người dùng thật kéo dài"),
    ("Audio training: thời gian đến target WER/CER không xấu hơn baseline",
     "Chưa đo trên đa máy; có so sánh DiLoCo với DDP theo perplexity trên một GPU"),
]

ROAD = [
    ("P0 Audit & baseline", "D", "Probe phần cứng, baseline llama.cpp và vLLM/SGLang đều có. Thiếu: rà soát license."),
    ("P1 Worker Fabric", "P", "Agent, Gateway, registry, lease, NATS, adapter llama.cpp có. Cổng '5–20–40 node ổn định' chưa đạt: mới 2 máy thật."),
    ("P2 Distributed Qwen", "P", "Topology planner + merged qua RPC có và đo tốt; Prima/MoLink không làm; so sánh 3–4 RTX 3060 chưa làm."),
    ("P3 MoE & AI Agent", "D", "MoE native/hybrid, LangGraph, RAG, ASR, tool, sandbox đều có test. Quota Agent mới ở mức tenant."),
    ("P4 Audio & DiLoCo", "P", "ASR, LoRA, DiLoCo mô phỏng. Chưa có TTS Qwen, DDP thật, báo cáo hội tụ đa máy."),
    ("P5 Pilot 200 PC / WAN", "N", "Chưa có pilot, chưa có federation 2–3 site, chưa review bảo mật độc lập."),
    ("P6 Production hardening", "P", "Release manifest, SBOM ký, runbook SRE, CI có. Còn: pin thật, legal, DR, đổi token mặc định."),
]

EXTRAS = [
    ("Serverless GPU kiểu Modal", "serverless/ + agents/sl_*.py (~3,1 nghìn dòng Python), SDK Python (~1,5 nghìn dòng), 14 file test (184 ca), ADR-003"),
    ("AI Studio: RAG, QLoRA, ước tính full-FT, prompt/few-shot", "studio/ (~2,7 nghìn dòng), tests/studio 45 ca"),
    ("Best-of-N cho viết code + API cho IDE", "bestofn/ (~1,8 nghìn dòng), P7_BEST_OF_N.md"),
    ("Web IDE (CodeMirror) và dashboard 200-GPU", "apps/site-gateway/web (~8,8 nghìn dòng JS/HTML), ADR-004"),
    ("Giả lập Ollama + OpenAI Responses, trang kích hoạt một chạm, cầu nối Windows/Linux/macOS", "ollama_emulator.go (~1,3 nghìn dòng), bridge/*.tmpl, connect.html; thử thật với Codex CLI và GJC"),
    ("Windows Agent (điều khiển ứng dụng qua UIA)", "windows-agent/ (~1,5 nghìn dòng), so sánh với UFO2, goose, UI-TARS"),
    ("Benchmark HumanEval 164 bài × 23 cấu hình, gồm thống kê McNemar + Holm và đo điện", "benchmarks/reports/full164, 3 báo cáo + PDF tổng hợp"),
    ("Tối ưu CPU affinity / KV q8 / ncmoe, nghiên cứu nghẽn SGLang PP", "P9, P12, P13 và các kế hoạch KE_HOACH_*.md"),
]

STRENGTHS = [
    "Giữ kỷ luật bằng chứng: tách rõ thiết kế với kết quả, ghi 'chưa phải benchmark', và đòi cùng model/tokenizer/quantization khi so sánh.",
    "Bốn nguyên tắc cốt lõi đã đứng vững qua thực nghiệm: phân tán chỉ để chạy được model lớn hoặc để nhanh hơn; MoE nên hybrid trước khi gọi expert từ xa; không đặt activation theo từng token qua WAN; LoRA không được lấy trung bình A và B.",
    "Phần cứng giả định (RTX 3060 12 GB, LAN 2–5 Gbps) khớp với thiết bị thật; con số suy hao (54,9 ms/token, 218 ms/token) giúp định hướng sớm.",
    "Có cổng nghiệm thu, ma trận B01–B09, Definition of Done cho mỗi engine, nên rất dễ đối chiếu như báo cáo này.",
    "Phụ lục C liệt kê đúng những câu hỏi hóa ra quyết định thành bại (3060 12 GB hay 8 GB, băng thông LAN theo từng PC, baseline TP/PP đã chạy).",
]

WEAK = [
    ("Chưa dự đoán được trở ngại Windows", "Tài liệu ngầm xem các PC như Linux. Thực tế vLLM/SGLang đa nút không chạy qua NAT của WSL2 trên Windows 10, Prima/MoLink chưa có hỗ trợ Windows đã xác minh. Đây là lý do chính hai engine này không vào được dự án."),
    ("Hướng 'GGML RPC chỉ là PoC' lệch với kết quả", "Cuối cùng RPC lại là đường phân tán duy nhất đạt mục tiêu (~135 tok/s, 2,1–2,7× RTX 5070 đơn lẻ). Tài liệu nên đổi thành 'RPC là đường mặc định cho nhóm 2 node trong LAN tin cậy, kèm điều kiện mạng và cách ly'."),
    ("Quá nhiều engine so với nhân lực", "Hơn 7 engine/backend ứng viên (llama.cpp, Prima, MoLink, vLLM, SGLang, KTransformers, Exo, Parallax) cho 3 mức phần cứng. Dự án thực tế chỉ cần llama.cpp là chính và đối chứng vLLM/SGLang."),
    ("Ngầm cho rằng sẽ có 200 PC", "Mọi KPI phân tán lớn (40+ node, pilot 200 PC) đều không kiểm chứng được trên hai máy; tài liệu nên có một bậc 'hai máy' làm đích rõ ràng cho giai đoạn đầu."),
    ("Thiếu các sản phẩm trọng yếu mà dự án phải làm", "Không đề cập serverless GPU, AI Studio, Best-of-N cho code, tích hợp IDE/CLI (Codex, GJC, Ollama). Tổng cộng đây là khoảng một nửa khối lượng mã thực tế."),
    ("API quá rộng so với giá trị", "Bảy service proto (Placement, Inference, AgentJobs, Training, Federation…) chưa cần khi mới 2 máy; dự án dùng REST + hai proto là đủ và đã chạy."),
    ("Lộ trình theo sprint không còn phù hợp", "Dự án tới trạng thái 'release v1.1' sau khoảng hai tuần (167 commit từ 25/10 đến 07/11), nhanh hơn nhiều so với ước tính 12–22 sprint cho P0–P5 (chưa tính P6), nhưng bỏ qua phần pilot và pháp lý. Cần viết lại lộ trình theo giá trị còn thiếu."),
    ("Chưa nói về vận hành và chi phí", "Không có phần về giá thành điện/GPU-giờ, SLO người dùng, cách phát hành cập nhật cho PC, hay mô hình đối soát tài nguyên, trong khi dự án đã đo joule/token."),
]

RISKS = [
    ("Cao", "Mặc định bảo mật yếu", "run_demo.sh sinh token ngẫu nhiên khi dùng giá trị mặc định, nhưng mật khẩu admin mặc định 'Admin@AICF2026!' nằm sẵn trong mã nguồn và 'demo-admin-token' còn là giá trị dự phòng ở nhiều script; GGML RPC không xác thực. Cần đổi trước khi mở rộng ngoài hai máy."),
    ("Cao", "Chưa rà soát giấy phép", "legal_reviewed=false cho mọi upstream và 3 mục UNVERIFIED (prime-diloco, prima.cpp, MoLink); chưa có kiểm toán license model Qwen và dữ liệu giọng nói."),
    ("Trung bình", "Scheduler chi phí chưa vào đường chạy", "Hàm argmin đã có test nhưng gateway thật không dùng; số đo không được nạp ngược để dự đoán."),
    ("Trung bình", "Chưa chứng minh ở quy mô nhiều node", "Mọi kết quả đa máy từ hai máy; số 40 worker là mô phỏng."),
    ("Trung bình", "Speculative decoding đổi đầu ra", "Chỉ 25–33% đầu ra giống baseline ở B07; chưa được bật production, cần giữ như vậy cho tới khi kiểm đúng phân phối."),
    ("Trung bình", "VRAM rất sát ở chế độ ghép 2 card", "Sau khi tăng ngữ cảnh lên 20.480 token: 3060 dùng 11,2/12,3 GB, 5070 dùng 11,5/12,2 GB [ĐO 07/11]. Còn ít dư cho tải khác trên máy Windows."),
    ("Thấp", "Dung lượng đĩa", "Ổ C: Windows từng đầy 100% (đã dọn 17 GB); đĩa dự án Linux gần đầy theo ghi nhận trước."),
]

NEXT = [
    "Bỏ mật khẩu admin mặc định trong run_demo.sh và các giá trị 'demo-admin-token' dự phòng; đặt proxy lọc làm cấu hình mặc định khi mở LAN; thêm xoay vòng chứng chỉ trước 25/01/2027.",
    "Rà soát giấy phép (upstream, model Qwen, dữ liệu giọng nói) và thay SHA 'ứng viên' bằng bản đã đo; chốt Prima/MoLink là 'bỏ' hoặc 'làm tiếp' theo kết luận Windows.",
    "Nối scheduler chi phí vào router (bắt đầu bằng chế độ shadow: chỉ ghi lại quyết định so với WFQ) và nạp số đo B01/B06/full-164 làm đường cong.",
    "Thêm máy thứ ba để kiểm chứng nhóm 3 node và B03/B08 thật; nếu chưa có máy, ghi rõ KPI nhiều node là 'chưa kiểm chứng'.",
    "Viết lại tài liệu kiến trúc v2: hai máy là đích đầu, RPC làm đường phân tán chính, đưa serverless/Studio/IDE vào phạm vi, rút gọn danh sách proto.",
    "Bổ sung quan sát: OpenTelemetry hoặc ít nhất Prometheus + dashboard, vì dự án đã có /metrics và số đo năng lượng.",
]


def badge(s: str) -> str:
    return f'<span class="b b{s}">{LABEL[s]}</span>'


def count(items):
    c = {k: 0 for k in LABEL}
    for it in items:
        c[it[2]] += 1
    return c


c = count(ITEMS)
total = len(ITEMS)
score = 100 * (c["D"] + c["X"] + 0.5 * c["P"]) / total
by_sec: dict[str, list] = {}
for it in ITEMS:
    by_sec.setdefault(it[0], []).append(it)


def sec_rows():
    out = []
    for sec, rows in by_sec.items():
        cc = count(rows)
        out.append(f'<tr class="sec"><td colspan="3">{html.escape(sec)} — {cc["D"]} đã làm · {cc["X"]} làm khác · {cc["P"]} một phần · {cc["N"]} chưa</td></tr>')
        for _, req, st, ev in rows:
            out.append(f"<tr><td class='req'>{html.escape(req)}</td><td class='st'>{badge(st)}</td><td class='ev'>{html.escape(ev)}</td></tr>")
    return "\n".join(out)


def simple_rows(rows, cols=4):
    out = []
    for r in rows:
        out.append("<tr>" + "".join((f"<td style='white-space:nowrap'>{badge(v)}</td>" if v in LABEL and len(v) == 1 else f"<td>{html.escape(v)}</td>") for v in r) + "</tr>")
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm; }
* { box-sizing: border-box; }
body { font-family: 'DejaVu Sans', sans-serif; color: #0b0b0b; font-size: 9.6pt; line-height: 1.5; }
h1 { font-size: 22pt; margin: 0 0 4px; } h2 { font-size: 14pt; margin: 20px 0 8px; border-bottom: 2px solid #2a78d6; padding-bottom: 3px; page-break-after: avoid; }
h3 { font-size: 11pt; margin: 14px 0 6px; page-break-after: avoid; }
.sub { color: #52514e; margin: 0 0 14px; } .small { font-size: 8.4pt; color: #52514e; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 10px; } th { background: #f1f0ec; text-align: left; font-size: 8.6pt; }
td, th { border: 1px solid #d9d8d3; padding: 4px 6px; vertical-align: top; } tr { page-break-inside: avoid; }
tr.sec td { background: #e8eefb; font-weight: 700; font-size: 9pt; }
td.req { width: 36%; } td.st { width: 11%; white-space: nowrap; } td.ev { width: 53%; color: #2c2b29; font-size: 8.6pt; }
.b { display: inline-block; padding: 1px 7px; border-radius: 9px; font-size: 8pt; font-weight: 700; color: #fff; }
.bD { background: #1a7f37; } .bP { background: #9a6700; } .bN { background: #c0362c; } .bX { background: #2a78d6; }
.cards { display: flex; gap: 8px; margin: 8px 0 12px; } .card { flex: 1; border: 1px solid #d9d8d3; border-radius: 8px; padding: 8px 10px; }
.card .n { font-size: 20pt; font-weight: 700; line-height: 1.1; } .card .t { font-size: 8.6pt; color: #52514e; }
.verdict { border-left: 5px solid #2a78d6; background: #f4f7fd; padding: 8px 12px; margin: 10px 0; }
.bar { height: 14px; display: flex; border-radius: 7px; overflow: hidden; margin: 6px 0 4px; } .bar div { height: 100%; }
ul { margin: 4px 0 8px 18px; padding: 0; } li { margin: 3px 0; }

.sev { font-weight: 700; } .sevCao { color: #c0362c; } .sevTrung { color: #9a6700; } .sevThap { color: #52514e; }
"""

pct = lambda n: 100 * n / total
html_doc = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Đánh giá so sánh tài liệu kiến trúc với project AI Compute Fabric</title><style>{CSS}</style></head><body>

<h1>Đánh giá so sánh: tài liệu kiến trúc và project</h1>
<p class="sub">Tài liệu: <b>AI Compute Fabric — Kiến trúc kỹ thuật v1.0</b> (25/10/2026) · Project: <code>ai-compute-fabric</code>, trạng thái 07/11/2026 (167 commit) · Ngôn ngữ: tiếng Việt</p>

<div class="verdict"><b>Kết luận ngắn.</b> Phần lõi "một máy – hai máy" của tài liệu đã được hiện thực và đo thật, thường vượt kỳ vọng:
gateway, worker, lease, thu hồi GPU, ghép hai card qua RPC, MoE, Agent, RAG, ASR, LoRA. Phần "quy mô lớn" gần như chưa có bằng chứng:
nhóm 3–4 node, 40–200 PC, federation qua WAN thật, DDP thật, Prima.cpp và MoLink. Đồng thời dự án đã làm thêm một khối lớn không nằm trong tài liệu
(serverless GPU, AI Studio, Best-of-N, tích hợp IDE/CLI). Mức hoàn thành theo các hạng mục của tài liệu: khoảng <b>{score:.0f}%</b> [ƯỚC TÍNH, cách tính ở dưới].</div>

<div class="cards">
 <div class="card"><div class="n" style="color:#1a7f37">{c['D']}</div><div class="t">Đã làm ({pct(c['D']):.0f}%)</div></div>
 <div class="card"><div class="n" style="color:#2a78d6">{c['X']}</div><div class="t">Làm khác tài liệu ({pct(c['X']):.0f}%)</div></div>
 <div class="card"><div class="n" style="color:#9a6700">{c['P']}</div><div class="t">Một phần ({pct(c['P']):.0f}%)</div></div>
 <div class="card"><div class="n" style="color:#c0362c">{c['N']}</div><div class="t">Chưa làm ({pct(c['N']):.0f}%)</div></div>
</div>
<div class="bar"><div style="width:{pct(c['D'])}%;background:#1a7f37"></div><div style="width:{pct(c['X'])}%;background:#2a78d6"></div><div style="width:{pct(c['P'])}%;background:#9a6700"></div><div style="width:{pct(c['N'])}%;background:#c0362c"></div></div>
<p class="small">Tổng {total} hạng mục trích từ tài liệu. Điểm = (đã làm + làm khác + 0,5 × một phần) / tổng. Đây là phán đoán của người đánh giá, các hạng mục không cùng trọng số, nên chỉ dùng để thấy xu hướng.</p>

<h2>1. Cách đối chiếu và mức tin cậy</h2>
<ul>
<li><b>[ĐỌC MÃ]</b>: tôi đọc file trong repo. <b>[ĐO]</b>: số có sẵn trong <code>benchmarks/reports</code> hoặc lệnh chạy ngày 07/11/2026. <b>[ƯỚC TÍNH]</b>: phán đoán.</li>
<li>Kiểm thử chạy lại hôm nay: <code>go test</code> pass ở 5 module (site-gateway, worker-guard, global-control-plane, vram-estimator, contracts); <code>pytest</code> trên unit + security + correctness + resilience cho kết quả <b>356 pass, 16 skip, 1 xfail</b>. Bộ integration, studio và serverless cần dịch vụ chạy nền nên không chạy lại.</li>
<li>Quy mô mã không-test đếm bằng <code>wc</code>: apps ~12 nghìn dòng Go/Py, agents ~5,6 nghìn, benchmarks ~15,8 nghìn, tests ~11,8 nghìn, serverless ~3,1 nghìn, studio ~2,7 nghìn.</li>
<li>Không chạy lại các benchmark GPU cũ; các số trong báo cáo được trích nguyên văn từ file kết quả.</li>
</ul>

<h2>2. So sánh chi tiết theo từng phần của tài liệu</h2>
<table><thead><tr><th>Yêu cầu trong tài liệu</th><th>Trạng thái</th><th>Bằng chứng trong project</th></tr></thead><tbody>
{sec_rows()}
</tbody></table>

<h2>3. Ma trận benchmark B01–B09 (mục 12.4 của tài liệu)</h2>
<table><thead><tr><th>Mã</th><th>Tài liệu yêu cầu</th><th>Trạng thái</th><th>Thực tế</th></tr></thead><tbody>
{simple_rows([(b[0], b[1], b[2], b[3]) for b in BENCH])}
</tbody></table>
<p class="small">Tài liệu yêu cầu thêm: prompt 512/4096, concurrency 1/8/32, hai mức LAN, tối thiểu 3 lần warm + 5 lần đo, có tải nền và một worker rời mạng. Chuỗi full-164 và các benchmark P3 đã có lặp lại và thống kê, nhưng không phải mọi B-test đều đủ ma trận này.</p>

<h3>Cổng nghiệm thu hiệu năng (mục 12.5)</h3>
<table><thead><tr><th>Mục tiêu / chỉ số</th><th>Kết quả hiện có</th></tr></thead><tbody>
{"".join(f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td></tr>" for a, b in GATES)}
</tbody></table>

<h2>4. Lộ trình P0–P6 so với thực tế</h2>
<table><thead><tr><th>Giai đoạn</th><th>Trạng thái</th><th>Ghi chú</th></tr></thead><tbody>
{simple_rows(ROAD)}
</tbody></table>

<h2>5. Những gì project đã làm thêm ngoài tài liệu</h2>
<table><thead><tr><th>Hạng mục</th><th>Quy mô / bằng chứng</th></tr></thead><tbody>
{simple_rows(EXTRAS)}
</tbody></table>
<p>Nhóm này chiếm tỷ trọng lớn trong khối lượng mã. Ngược lại, tài liệu dành nhiều trang cho engine (Prima, MoLink, KTransformers, Exo, Parallax) mà project hầu như chưa dùng. Phạm vi thực tế đã dịch từ "hạ tầng tính toán phân tán" sang "nền tảng phục vụ AI cho nhóm nhỏ trên hai máy GPU".</p>

<h2>6. Đánh giá chung về tài liệu so với project</h2>
<h3>Điểm mạnh</h3><ul>{"".join(f"<li>{html.escape(s)}</li>" for s in STRENGTHS)}</ul>
<h3>Điểm yếu và chỗ lệch với thực tế</h3>
<table><thead><tr><th>Vấn đề</th><th>Nhận xét</th></tr></thead><tbody>
{"".join(f"<tr><td style='width:28%'><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td></tr>" for a, b in WEAK)}
</tbody></table>
<p><b>Đánh giá tổng quát [ƯỚC TÍNH].</b> Là bản thiết kế, tài liệu tốt về nguyên tắc và kỷ luật đo đạc, và những nguyên tắc đó đã thật sự dẫn đường cho project. Điểm yếu là phạm vi và giả định quy mô (200 PC, 7 engine, Linux đồng nhất) vượt xa nguồn lực và phần cứng hiện có. Tài liệu cần được cập nhật thành v2 theo thực tế hai máy, không phải bỏ đi.</p>

<h2>7. Rủi ro hiện tại của project</h2>
<table><thead><tr><th>Mức</th><th>Rủi ro</th><th>Chi tiết</th></tr></thead><tbody>
{"".join(f"<tr><td class='sev sev{a[:4]}'>{a}</td><td><b>{html.escape(b)}</b></td><td>{html.escape(d)}</td></tr>" for a, b, d in RISKS)}
</tbody></table>

<h2>8. Đề xuất ưu tiên</h2>
<ol>{"".join(f"<li>{html.escape(s)}</li>" for s in NEXT)}</ol>

<h2>Phụ lục. Giới hạn của báo cáo này</h2>
<ul>
<li>Tôi đọc kỹ docx (228 đoạn, bảng) và khảo sát repo bằng grep, đọc file và chạy test. Tôi không đọc từng dòng của toàn bộ ~50 nghìn dòng mã, nên "đã làm" nghĩa là có mã và bằng chứng đo hoặc test tương ứng, không phải xác nhận mọi nhánh.</li>
<li>Hình ảnh trong docx (2 ảnh) không được phân tích.</li>
<li>Các con số hiệu năng lấy từ báo cáo của project; không đối chiếu độc lập với tài liệu gốc ngoài các phép kiểm NVML đã làm trước đó.</li>
</ul>
</body></html>"""

(WORK / "review.html").write_text(html_doc, encoding="utf-8")
subprocess.run(["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={OUT_PDF}", f"file://{WORK / 'review.html'}"], check=True, capture_output=True, timeout=120)
print(f"OK {OUT_PDF} items={total} D={c['D']} X={c['X']} P={c['P']} N={c['N']} score={score:.1f}")
