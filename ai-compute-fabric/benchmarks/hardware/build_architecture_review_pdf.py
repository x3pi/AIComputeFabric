#!/usr/bin/env python3
"""Build the architecture-document vs project comparison PDF and Markdown report.

Source document: AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx (25/10/2026).
Every status below was verified by reading the repository (code, tests, reports, diagrams)
and executing the test suites on 2026-11-08.
Labels: [ĐO] measured / test run, [ĐỌC MÃ] verified in source code, [ƯỚC TÍNH] engineering judgement.
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
OUT_MD = REP / "DANH_GIA_SO_SANH_TAI_LIEU_KIEN_TRUC_VS_PROJECT.md"
WORK = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/aicf_arch_review")
WORK.mkdir(parents=True, exist_ok=True)

# status: D done, P partial, N not done, X achieved by a different route than the document proposed
LABEL = {"D": "Đã làm", "P": "Một phần", "N": "Chưa làm", "X": "Làm khác tài liệu"}

# (section, requirement in the document, status, evidence in the project)
ITEMS = [
    ("§2 Kiến trúc", "Site Gateway (Go + gRPC): registry, lease, link graph, policy, cache", "D",
     "apps/site-gateway: Register/Heartbeat, LeaseService, router WFQ + KV affinity + cost-aware ReferenceTokS + deadline, mTLS hot-reload, RBAC, audit chuỗi băm SHA-256 [ĐỌC MÃ]; 109 Go tests pass [ĐO]"),
    ("§2 Kiến trúc", "PC Worker Agent: profiling, quota, idle, sandbox, signed job, heartbeat (Windows/Linux)", "D",
     "apps/worker-guard (NVML VRAM thật, thu hồi GPU <2s, heartbeat 6-bucket, Windows service, P-core pin) + agents/sl_security.py (HMAC-SHA256 signed job ServerlessSig, NonceCache chống replay) + agents/sandbox.py (bubblewrap/Docker --cpus, --memory, --pids-limit, AICF_VRAM_MB) [ĐỌC MÃ, ĐO]"),
    ("§2 Kiến trúc", "Global Control Plane: chính sách, metering, federation", "P",
     "apps/global-control-plane: mTLS 1.3, thu hồi serial, DataClass residency, sổ cái bất biến; 16 Go tests pass. Chưa chạy qua WAN/nhiều site vật lý thật [ĐỌC MÃ, ĐO]"),
    ("§2 Kiến trúc", "Inference Runtime: llama.cpp là mặc định; Prima/MoLink đối chứng; KT/vLLM có điều kiện", "P",
     "llama.cpp chạy production (split, smart_1gpu, merged RPC); vLLM/SGLang đã đo đối chứng kỹ (P10–P13); KTransformers đã đo micro-bench (P8). Prima/MoLink chưa tích hợp [ĐO]"),
    ("§2 Kiến trúc", "Agent Runtime: LangGraph + Fabric task API", "D",
     "agents/orchestrator.py (LangGraph DAG + checkpoint), task_bus.py (NATS JetStream), dag_api.py [ĐỌC MÃ, ĐO]"),
    ("§2 Kiến trúc", "Audio & Training: ASR/TTS, fine-tune, DiLoCo, đánh giá", "P",
     "Có Qwen3-ASR-0.6B (WER 8,11%), LoRA/QLoRA, LoRA merger (4 thuật toán), mô phỏng DiLoCo vs DDP; TTS dùng mms-tts-vie (round-trip WER 1,82%), enhancement dùng Wiener spectral gate numpy [ĐO]"),
    ("§2 Kiến trúc", "Observability & Data: Postgres, object storage, NATS, OpenTelemetry, Prometheus/Grafana", "P",
     "Postgres/pgvector + NATS JetStream + /metrics Prometheus + deploy/monitoring/ (prometheus.yml, 6 cảnh báo SRE, alertmanager.yml, grafana_aicf_dashboard.json 4 panel). Chưa có OpenTelemetry tracing và S3/MinIO object storage [ĐỌC MÃ]"),
    ("§2 Kiến trúc", "Ba mặt phẳng control / data / management (cert rotation, provisioning)", "D",
     "Control/data/management tách rõ; internal/pki hỗ trợ RotateLeaves + NeedsRotation (-rotate -renew-within-days 30) và mtls.go tự nạp nóng chứng chỉ mới (GetCertificate / GetClientCertificate) không cần khởi động lại [ĐỌC MÃ, ĐO]"),
    ("§3 Luồng", "Luồng một yêu cầu Qwen: xác thực tenant, định tuyến, lease, stream, metrics, failover", "D",
     "/v1/chat/completions qua gateway → router → worker; test affinity, fairness, cost-aware routing, inference_load [ĐO]"),
    ("§3 Luồng", "Định danh model bằng hash weights + tokenizer + revision", "D",
     "ModelIdentity (proto), core/model_registry/provenance.py, scheduler từ chối MODEL_IDENTITY_MISMATCH [ĐỌC MÃ, ĐO]"),
    ("§3 Luồng", "Workflow AI Agent → DAG task có kiểu → gộp kết quả", "D",
     "agents/ (15 typed task, validator), p5_agent_dag_e2e.json (DAG 14 bước) [ĐO]"),
    ("§3 Luồng", "Job fine-tune audio: manifest → kiểm quyền → tách speaker → train → cổng WER/CER → đăng ký artifact", "D",
     "audio/ingestion (manifest SHA-256 + consent_ref, speaker_disjoint_split 0% rò rỉ), training/coordinator.py (promote_if_passes cổng chất lượng + provenance registry), tests/audio_quality/ pass [ĐỌC MÃ, ĐO]"),
    ("§4 Engine", "LOCAL_REPLICA: Qwen nhỏ trên từng PC, nhiều PC phục vụ song song", "D",
     "Chế độ split: Qwen3-4B 554,9 tok/s tổng với 4 luồng/máy [ĐO]; 2 node chạy song song scale 96,4% [ĐO]"),
    ("§4 Engine", "LOCAL_HYBRID: GPU + RAM, expert nóng/lạnh cho MoE", "D",
     "smart_1gpu với -ncmoe; B06 trên RTX 3060 giảm 92% TPOT so với 32B dense offload; Qwen3-30B-A3B 49,9 tok/s (pp512/tg64) [ĐO]"),
    ("§4 Engine", "DISTRIBUTED_MODEL bằng Prima.cpp / MoLink", "N",
     "Chỉ có dòng trong upstream-manifest.yaml (license UNVERIFIED); không có adapter hay benchmark B04/B05 vì thiếu hỗ trợ Windows ổn định [ĐỌC MÃ]"),
    ("§4 Engine", "DISTRIBUTED_MODEL: chạy được model vượt VRAM một máy bằng nhóm 2 node", "X",
     "Làm bằng GGML RPC của llama.cpp (chế độ merged): Qwen3-30B-A3B ~135 tok/s, gấp 2,1× (ctx 8k) và 2,7× (ctx 32k) RTX 5070 đơn lẻ; Qwen3-Coder-30B-A3B đạt 154/164 HumanEval (93,9%) ở 124,7 tok/s [ĐO]. Tài liệu coi RPC chỉ là PoC"),
    ("§4 Engine", "Nhóm 3–4 PC cùng một model", "N",
     "Mới có 2 máy vật lý (RTX 3060 + RTX 5070); chưa có máy thứ 3–4 để đo thật"),
    ("§4 Engine", "SPECULATIVE decoding", "P",
     "B07: Qwen3-8B + draft 0,6B nhanh 1,185×, acceptance 0,49, nhưng đầu ra greedy giống baseline chỉ 25% [ĐO]. Giữ tắt ở production"),
    ("§4 Engine", "Native MoE và distributed native MoE (bảo toàn phép tính)", "D",
     "Qwen3-30B-A3B và Coder-30B chạy native, merged 2 card; full-164 HumanEval 154/164 cho Coder-30B [ĐO]"),
    ("§4 Engine", "Agent-level experts (router chọn Agent theo loại việc)", "D",
     "apps/site-gateway/compute_agents.go + router.go: nhãn năng lực (code, coder, reasoning, math, general), định tuyến theo X-AICF-Label / X-AICF-Agent + Best-of-N orchestrator [ĐỌC MÃ, ĐO]"),
    ("§4 Engine", "KTransformers (cổng ISA, backend riêng)", "P",
     "P8: đo baseline và micro-benchmark kt-kernel, cổng CPU ISA trong core/hardware_profiler; chưa tích hợp làm backend mặc định vì llama.cpp P-core + KV q8_0 đã đạt đích [ĐO]"),
    ("§5 Scheduler", "Lọc cứng + chọn argmin hàm chi phí (bộ nhớ, identity, residency, SLA, năng lượng, rủi ro)", "D",
     "core/adaptive_scheduler/scheduler.py: lọc cứng + expected_score(w, r) = T_expected + lambda*resource_cost + mu*risk_penalty (§5.2), 18 unit tests pass [ĐỌC MÃ, ĐO]"),
    ("§5 Scheduler", "Scheduler chi phí nói trên được cắm vào đường định tuyến thật", "D",
     "apps/site-gateway/router.go (pickPreferLocked chấm điểm (inflight+1)/ReferenceTokS kết hợp WFQ, KV affinity, priority, deadline); TestCostAwareRoutingPrefersFasterHardwareCurve pass [ĐỌC MÃ, ĐO]"),
    ("§5 Scheduler", "Đường cong benchmark theo (model, backend, phần cứng) + predictor học từ dữ liệu", "P",
     "Đã nạp bảng MEASURED_CURVES (B01, B02, B06, full164) vào scheduler.py và ReferenceTokS vào compute_agents.go; chưa có mô hình ML tự học online từ log [ĐỌC MÃ, ĐO]"),
    ("§5 Scheduler", "Lease có TTL, gia hạn bằng heartbeat; nhường PC cho người dùng", "D",
     "leases.go; thu hồi GPU trung vị 174 ms (Windows), 230 ms p50 / 257 ms p95 (Linux) < 2 s [ĐO]"),
    ("§5 Scheduler", "Chống quá tải: quota tenant, giới hạn đồng thời, backpressure", "D",
     "Tenant fairness benchmark, MaxQueue, max_inflight, token bucket; test fair/leases pass [ĐO]"),
    ("§5 Scheduler", "3 mức ưu tiên: người dùng > doanh nghiệp > federation", "P",
     "Mức 1 (thu hồi GPU cho chủ máy) và mức 2 (interactive > batch, WFQ) đã đo thật; mức 3 (federation) có chính sách trong global-control-plane nhưng chưa đo tải thật liên site [ĐO]"),
    ("§6 Agent", "Graph Orchestrator (LangGraph) + checkpoint", "D", "agents/orchestrator.py, task_bus.py [ĐỌC MÃ, ĐO]"),
    ("§6 Agent", "Task Planner tách mục tiêu thành DAG kiểu", "D", "bestofn/planner.py (8 chiến lược planner) + agents/orchestrator.py dựng DAG 14 bước có kiểu [ĐỌC MÃ, ĐO]"),
    ("§6 Agent", "Tool Registry (allowlist có kiểu) và không có shell tổng quát", "D", "agents/tool_registry.py; test an ninh pass [ĐO]"),
    ("§6 Agent", "Task Executor + sandbox", "D", "agents/sandbox.py (bubblewrap/Docker); p7_pha0_sandbox_mutation.json giết 100% mutant [ĐO]"),
    ("§6 Agent", "Knowledge Service: chunking, pgvector, rerank, cô lập tenant", "D", "agents/knowledge.py (RLS tenant_id, HNSW pgvector, rerank bge-reranker-v2-m3); p5_rag_quality_small_vi.json [ĐO]"),
    ("§6 Agent", "Result Validator", "D", "agents/validator.py [ĐỌC MÃ, ĐO]"),
    ("§6 Agent", "15 loại task chuẩn", "D", "tests/unit/test_15_typed_task_schemas.py [ĐO]"),
    ("§6 Agent", "Ví dụ phân tích 10.000 cuộc gọi chạy thật", "P",
     "Đã đo nạp streaming 10.000 file WAV (test_ten_thousand_files_stream_with_flat_memory, RSS tăng < 100 MB) và DAG E2E; chưa chạy 10.000 cuộc gọi qua ASR+LLM thật vì tốn ~30 giờ GPU [ĐO]"),
    ("§7 Audio", "ASR bằng Qwen3-ASR", "D", "Qwen3-ASR-0.6B: WER sạch 8,11% (corpus 7,90%), CER 4,70% trên 100 clip FLEURS tiếng Việt [ĐO]"),
    ("§7 Audio", "TTS bằng Qwen3-TTS", "P", "Vòng TTS→ASR dùng facebook/mms-tts-vie (WER vòng lặp 1,82%, gắn cờ CC-BY-NC-4.0 phi thương mại), chưa dùng Qwen3-TTS [ĐO]"),
    ("§7 Audio", "Enhancement bằng DeepFilterNet", "P", "Đang dùng cổng phổ Wiener/spectral-gate numpy; giảm WER nhiễu 10 dB SNR từ 13,57% xuống 12,93% [ĐO]"),
    ("§7 Audio", "Nhận diện sự kiện âm thanh (BEATs)", "N", "Không có trong phạm vi hiện tại"),
    ("§7 Training", "LoRA/PEFT một GPU", "D", "training/lora_trainer.py, QLoRA trong Studio, memory estimator sai số ≤ 9,5% so với thực đo [ĐO]"),
    ("§7 Training", "Quy tắc gộp LoRA không lấy trung bình A và B tùy tiện", "D", "training/lora_merger.py: hỗ trợ exact ΔW, svd, lora_fa; cảnh báo rõ độ lệch của naive [ĐỌC MÃ, ĐO]"),
    ("§7 Training", "DDP 2–4 GPU thật", "N", "Chỉ có đối chứng 4 worker tuần tự trên một RTX 3060 (p6_diloco_vs_ddp.json)"),
    ("§7 Training", "DiLoCo thử nghiệm", "P",
     "p6_diloco_vs_ddp.json: giảm 95–97,5% băng thông WAN (H=20..40) với val loss 1,105 vs DDP 1,098 (+0,6%), nhưng mới mô phỏng tuần tự trên 1 GPU [ĐO]"),
    ("§8 Hạ tầng", "PostgreSQL + pgvector", "D", "agents/knowledge.py, studio/ (psycopg, pgvector HNSW, RLS) [ĐO]"),
    ("§8 Hạ tầng", "NATS JetStream (at-least-once, idempotent)", "D", "agents/task_bus.py, tests/integration/test_task_bus.py [ĐO]"),
    ("§8 Hạ tầng", "Object storage cho checkpoint / model shard", "N", "Dùng thư mục cục bộ có kiểm tra SHA-256; chưa tích hợp S3/MinIO"),
    ("§8 Hạ tầng", "Model Registry có artifact ký số", "P", "Hash SHA-256 weights+tokenizer+revision, provenance, chặn trust_remote_code và pickle RCE đã có; cosign mới ký SBOM/release manifest, chưa ký từng file GGUF [ĐỌC MÃ, ĐO]"),
    ("§8 Hạ tầng", "Metrics/traces: OpenTelemetry, Prometheus, Grafana", "P", "Có /metrics Prometheus, /api/stats, deploy/monitoring/ (Grafana dashboard 4 panel + 6 Prometheus alert rules + Alertmanager). Chưa có OpenTelemetry tracing [ĐỌC MÃ]"),
    ("§9 WAN", "AdvertiseCapacity / OfferJob / AcceptJob / ReportUsage", "P", "apps/global-control-plane/plane.go + 16 unit tests pass; triển khai qua HTTP/JSON + mTLS, chưa tách file proto riêng [ĐỌC MÃ, ĐO]"),
    ("§9 WAN", "mTLS liên site, thu hồi chứng chỉ, DataClass không rời site gốc", "D", "plane.go, policy.go; gcp_test, pki_test pass [ĐO]"),
    ("§9 WAN", "Pilot federation 2–3 site thật", "N", "Chưa có 2–3 site vật lý; chỉ kiểm thử mức đơn vị và 1 site LAN (2 máy)"),
    ("§10 Monorepo", "upstream-manifest: commit ghim, SPDX, người phụ trách", "P",
     "third_party/upstream-manifest.yaml có đầy đủ commit/SPDX/owner; legal_reviewed=false đang chờ bộ phận Pháp chế ký duyệt (cổng xfail strict trong test_manifest_schema.py) [ĐỌC MÃ, ĐO]"),
    ("§10 Monorepo", "NOTICE và SBOM ký cosign", "D", "NOTICE (liệt kê rõ Apache-2.0, MIT, CC-BY-4.0, CC-BY-NC-4.0), Makefile verify-release-v1, tests/security/test_sbom_signatures.py [ĐO]"),
    ("§10 Monorepo", "third_party/patches và pinned-forks", "N", "Thư mục third_party chỉ dùng upstream-manifest.yaml, không giữ bản fork nội bộ"),
    ("§10 Monorepo", "Cấu trúc thư mục theo Phụ lục A", "P",
     "Có apps/core/inference/agents/audio/training/contracts/benchmarks/serverless/studio/bestofn; một số thư mục khung (federation/, infrastructure/) để rỗng vì mã đặt trong apps/ và deploy/ [ĐỌC MÃ]"),
    ("§11 Contract", "Protobuf: WorkerCapacity, ModelIdentity, WorkloadJob, Lease; Fleet + Lease service", "D", "contracts/protobuf/aicf/v1 + golden test pass [ĐO]"),
    ("§11 Contract", "Service Placement, Inference, AgentJobs, Training, Federation (proto)", "X", "Hiện thực đầy đủ qua OpenAPI v1 REST + Go/Python typed schema thay vì nhân bản thêm 5 file gRPC proto không cần thiết cho cụm 2 máy [ĐỌC MÃ]"),
    ("§11 Contract", "OpenAPI v1 kiểm tự động theo route", "D", "contracts/openapi/aicf_v1.yaml + test_openapi_routes.py [ĐO]"),
    ("§11 Contract", "Engine lifecycle 7 hàm (probe…stop) + adapter", "D", "inference/engine.py; adapter llama_cpp, vllm (kèm hợp đồng kiểm thử p3_vllm_adapter_contract.json) [ĐO]"),
    ("§13 An ninh", "mTLS, RBAC, audit, tenant isolation", "D", "mtls.go (tự nạp nóng cert), rbac.go, audit.go; rà soát OWASP ASVS L2 qua 14 bề mặt (S1–S14) với 0 lỗ hổng mở [ĐO]"),
    ("§13 An ninh", "Cấm generic shell, allowlist tool, sandbox, không trust_remote_code", "D", "tool_registry, sandbox, model_registry/security.py; mutation test giết 8/8 mutant (100%) [ĐO]"),
    ("§13 An ninh", "Kiểm toán giấy phép model Qwen, dataset giọng nói, mã nguồn", "P", "Đã kiểm toán kỹ thuật trong NOTICE, studio/audio_service.py (cảnh báo CC-BY-NC-4.0 cho mms-tts-vie) và test_manifest_schema.py; chờ chữ ký Pháp chế con người (legal_reviewed=false) [ĐỌC MÃ, ĐO]"),
    ("§13 An ninh", "Không để LAN mặc định tin cậy / không lộ RPC", "D",
     "run_demo.sh tự sinh mật khẩu & token ngẫu nhiên (0600) và tự xoay vòng chứng chỉ mTLS trước hạn 30 ngày; apps/worker-guard/rpc_proxy.go bọc GGML RPC bằng IP allowlist + giới hạn kết nối [ĐỌC MÃ, ĐO]"),
    ("§15 Kiểm thử", "Bộ kiểm thử theo tầng (unit, integration, correctness, resilience, security)", "D",
     "181 Go tests pass trên 5 module; 825+ pytest cases trên toàn repo (unit, security, correctness, resilience, performance, audio_quality pass 100%) [ĐO 08/11]"),
    ("§15 Kiểm thử", "Performance và audio-quality trong thư mục tests/", "D",
     "tests/performance/test_performance_gates.py (7 cổng B01/B06/B07/G-RECLAIM/DiLoCo/Prefix/Scheduler) và tests/audio_quality/test_audio_quality_gates.py (5 cổng WER/CER/speaker-split/SNR) pass 100% [ĐO 08/11]"),
    ("§15 Kiểm thử", "CI tự động", "D", ".github/workflows/ci.yml, Makefile verify-release-v1 [ĐỌC MÃ, ĐO]"),
    ("§14 Deliverable", "ADR cho từng quyết định lớn", "D", "docs/adr: 4 ADR (0001 GPUStack, 0002 Event Bus, 0003 Serverless GPU, 0004 Web Editor) [ĐỌC MÃ]"),
    ("§14 Deliverable", "Machine inventory + baseline vLLM TP/PP", "D", "p0_probe_*.json; P10–P13 (vLLM/SGLang PP, 2 GPU) [ĐO]"),
]

# (id, tài liệu yêu cầu, trạng thái, thực tế)
BENCH = [
    ("B01", "1 PC, Qwen nhỏ: TTFT, TPOT, joule/token, VRAM", "D", "llama-bench Qwen3-4B (69,5–122,9 tok/s) & Qwen3-8B (43,8–82,2 tok/s) trên RTX 3060 và RTX 5070; joule/token đo bằng NVML (sai số −1,9% / +0,1%)"),
    ("B02", "1 PC, Qwen lớn + offload RAM", "D", "Qwen2.5-32B Q4_K_M offload trên RTX 3060: 3,97 tok/s, TPOT 252 ms (p0_B02) — làm mốc chứng minh nghẽn PCIe"),
    ("B03", "3–4 PC, vLLM TP/PP baseline", "P", "Đo kỹ trên 2 GPU (P10–P13): vLLM/SGLang PP qua WSL2 bị nghẽn NAT/NCCL; chứng minh llama.cpp GGML RPC vượt trội trên cụm hỗn hợp Linux+Windows"),
    ("B04", "3–4 PC, Prima.cpp", "N", "Không triển khai vì thiếu hỗ trợ Windows ổn định và GGML RPC đã đạt 135 tok/s"),
    ("B05", "3–4 PC, MoLink", "N", "Không triển khai vì trùng mục tiêu với GGML RPC trên cụm 2 node"),
    ("B06", "Qwen MoE local / hybrid", "D", "p4_B06: Qwen3-30B-A3B với n-cpu-moe 48→18 trên 1× RTX 3060 đạt 49,87 tok/s (TPOT 20,1 ms, giảm 92,0% so với B02)"),
    ("B07", "Speculative target/draft", "D", "p4_B07: Qwen3-8B + draft Qwen3-0.6B đạt 75,29 tok/s (1,185×), acceptance 0,49; phát hiện độ lệch greedy 25% nên giữ tắt ở production"),
    ("B08", "8–40 PC, Agent task độc lập", "P", "Thật trên 2 máy đạt hiệu suất ghép 96,4%; mô phỏng 8 / 20 / 40 worker đạt 85,5% / 74,1% / 56,8% (ghi rõ nhãn SIMULATED)"),
    ("B09", "4–8 PC, audio training DDP/DiLoCo", "P", "p6_diloco_vs_ddp.json: đo 4 worker tuần tự trên 1 GPU, chứng minh DiLoCo giảm 95–97,5% băng thông WAN với độ lệch loss +0,6%"),
]

GATES = [
    ("Distributed Qwen: giảm TPOT ≥30% so với PP baseline, hoặc chạy được model không vừa máy đơn",
     "ĐẠT VƯỢT MỨC: Qwen3-30B-A3B (18,6 GB, vượt VRAM 12 GB của 1 card) chạy 100% trong VRAM 2 card (RTX 3060 + RTX 5070) đạt ~135 tok/s (8k) và 124,7 tok/s ở full-164 HumanEval (154/164 = 93,9%, nhanh gấp 3,16× chế độ 1 card 39,5 tok/s) [ĐO]"),
    ("MoE Hybrid (G-MOE-HYBRID): giảm TPOT ≥40% so với dense offload tương đương",
     "ĐẠT VƯỢT MỨC: B06 giảm 92,0% TPOT (20,1 ms vs 252,0 ms của B02) [ĐO, kiểm tự động trong tests/performance/]"),
    ("KV Prefix Affinity (G-PREFIX): giảm TTFT ≥30% cho hội thoại nhiều lượt",
     "ĐẠT VƯỢT MỨC: TTFT giảm từ 1.390 ms xuống 38 ms (giảm 97,3%) cho ngữ cảnh ~2.000 token nhờ định tuyến ưu tiên worker giữ KV cache [ĐO]"),
    ("Task parallel: tổng throughput ≥80% tổng các node độc lập",
     "ĐẠT Ở 2 NODE THẬT (96,4%, 554,9 tok/s tổng); ở 40 worker mô phỏng đạt 56,8% do nghẽn điều phối trung tâm [ĐO]"),
    ("Ưu tiên tài nguyên (G-RECLAIM): không chiếm ngoài quota, thu hồi GPU < 2,0 giây",
     "ĐẠT VƯỢT MỨC: Thu hồi GPU trung vị 174 ms trên Windows (10/10 lần) và p50 = 230 ms / p95 = 257,1 ms trên Linux (< 2.000 ms) [ĐO]"),
    ("Audio & Training (G-AUDIO / G-DILOCO): WER tiếng Việt ≤10%, DiLoCo giảm ≥95% băng thông WAN",
     "ĐẠT: Qwen3-ASR-0.6B đạt WER sạch 8,11% (corpus 7,90%) trên FLEURS vi_vn; TTS→ASR round-trip WER 1,82%; DiLoCo H=20..40 giảm 95–97,5% lưu lượng WAN [ĐO]"),
]

DIAGRAMS = [
    ("Hình 1 (Mục 2 — Kiến trúc tổng thể 5 tầng)",
     "Sơ đồ gồm 5 tầng từ trên xuống: (1) GLOBAL CONTROL PLANE (Policy · Model Registry · Metering · Cross-Site Federation) → (2) ADAPTIVE COMPUTE SCHEDULER (Latency/Throughput/Cost Predictor · Lease Manager · Topology Builder) → (3) ENTERPRISE SITE GATEWAY (Worker Registry · Task Queue · KV/Weight Cache · Local Policy Engine) → (4) 4 hộp Runtime (QWEN INFERENCE, NATIVE MoE, AI AGENTS, AUDIO + TRAINING) → (5) PC WORKER FABRIC · 200 máy / LAN (Windows/Linux Agent · Hardware Profiler · Quota Guard · Local Cache) + nhánh phải WAN Federation.",
     "Cả 5 tầng đều đã có thành phần chạy thật trong repo. Điểm cần lưu ý trong tài liệu gốc: chú thích dưới hình ghi 'Năm engine vận hành dưới một Fabric Scheduler' nhưng hình vẽ gộp Audio + Training vào hộp thứ 4 (tổng cộng 4 hộp runtime). Ngoài ra dự án thực tế đã mở rộng tầng thứ 4 thêm 3 khối lớn: Serverless GPU/Python, AI Studio và Best-of-N Code Orchestrator."),
    ("Hình 2 (Mục 3 — Luồng thực thi đầu-cuối: Online Serving vs Offline Training)",
     "Sơ đồ tách bạch 2 nhánh từ ENGINE / WORKERS: nhánh trên (Online) đi từ AI USER REQUEST → EXECUTION PLAN → ENGINE / WORKERS → RESULT + TELEMETRY; nhánh dưới (Offline) đi từ ENGINE / WORKERS → TRAINING PIPELINE (LoRA · local steps, DDP / DiLoCo · registry) → RESULT + TELEMETRY.",
     "Quy tắc cốt lõi của Hình 2 ('Training không nằm trong đường sinh token; chỉ phát hành model sau khi đánh giá chất lượng') được tuân thủ nghiêm ngặt trong studio/ và training/coordinator.py (hàm promote_if_passes kiểm tra cổng chất lượng trước khi xuất adapter GGUF sang llama-server). Phát hiện lỗi chế bản trong file .docx gốc: ảnh Hình 2 bị lỗi font chữ (hiện ký tự ô vuông ▯ tại 'LoRA ▯ local steps' và 'DDP / DiLoCo ▯ registry')."),
]

ECONOMICS = [
    ("Qwen3-Coder-30B-A3B (Ghép 2 GPU: RTX 3060 + RTX 5070, merged RPC)", "124,7 tok/s", "1,61 J/tok", "154/164 (93,9%)", "~1.340 VNĐ / 1M token", "Cấu hình chủ lực: chạy 100% trên VRAM 2 card, vừa nhanh gấp 3,16× vừa tiết kiệm 13,7% điện/token so với chạy 1 card."),
    ("Qwen3-Coder-30B-A3B (1 GPU: RTX 3060 smart_1gpu -ncmoe)", "39,5 tok/s", "1,86 J/tok", "154/164 (93,9%)", "~1.550 VNĐ / 1M token", "Giữ nguyên chất lượng 93,9% khi chỉ còn 1 máy rảnh, tốc độ chỉ bằng ~32% nhưng điện/token chỉ cao hơn ~16% (CPU gánh các tầng MoE lạnh)."),
    ("Qwen3-8B Q4_K_M (full-164, chế độ split)", "63,0 tok/s", "2,79 J/tok", "134/164 (81,7%)", "~2.325 VNĐ / 1M token", "Phù hợp chat tương tác và bước nháp; ghép 2 card chỉ nhanh hơn ~15% (72,2 tok/s, 3,01 J/tok)."),
    ("Qwen3-4B Q4_K_M (full-164, chế độ split)", "100,2 tok/s", "1,74 J/tok", "128/164 (78,0%)", "~1.450 VNĐ / 1M token", "Chi phí thấp nhất, phù hợp LOCAL_REPLICA."),
]

ROAD = [
    ("P0 Audit & baseline", "D", "Probe phần cứng, baseline llama.cpp và vLLM/SGLang, kiểm toán kỹ thuật giấy phép (NOTICE + upstream-manifest) đều hoàn tất."),
    ("P1 Worker Fabric", "P", "Agent, Gateway, registry, lease, NATS, adapter llama.cpp, mTLS xoay vòng tự động đều hoàn tất. Cổng '5–20–40 node vật lý' mới kiểm trên 2 máy thật + 40 worker mô phỏng."),
    ("P2 Distributed Qwen", "D", "Topology coordinator + chế độ merged qua GGML RPC (kèm rpc_proxy lọc IP) chạy ổn định ở 135 tok/s (8k) và 124,7 tok/s (full-164), vượt xa mục tiêu."),
    ("P3 MoE & AI Agent", "D", "MoE native/hybrid, LangGraph DAG, RAG pgvector RLS, ASR, tool allowlist, sandbox, định tuyến theo nhãn năng lực (X-AICF-Label) đều đạt."),
    ("P4 Audio & DiLoCo", "P", "ASR Qwen3 (WER 8,11%), LoRA/QLoRA, LoRA merger, cổng kiểm thử tests/audio_quality/ hoàn tất. DiLoCo mới dừng ở mô phỏng 1 GPU."),
    ("P5 Pilot 200 PC / WAN", "P", "Đã hoàn thành rà soát bảo mật nội bộ 2 vòng (OWASP ASVS L2, 14 bề mặt, 0 lỗ hổng mở, 8/8 mutant bị tiêu diệt). Chưa có dàn 200 PC và 2–3 site WAN vật lý."),
    ("P6 Production hardening", "D", "Release manifest v1.0/v1.1, SBOM ký cosign, runbook SRE, Prometheus/Grafana monitoring, tự xoay vòng mTLS, loại bỏ mật khẩu mặc định, CI tự động."),
]

EXTRAS = [
    ("Serverless GPU kiểu Modal", "serverless/ + agents/sl_*.py (~3,1 nghìn dòng Python), SDK Python (~1,5 nghìn dòng), ký HMAC-SHA256 + chống replay, 14 file test (184 ca), ADR-003"),
    ("AI Studio: RAG, QLoRA, ước tính full-FT, prompt/few-shot, Audio UI", "studio/ (~2,7 nghìn dòng), RLS multi-tenant, tests/studio 45 ca"),
    ("Best-of-N cho viết code + 8 chiến lược Planner + API cho IDE", "bestofn/ (~1,8 nghìn dòng), P7_BEST_OF_N.md, tăng điểm HumanEval lên 93,9%+"),
    ("Web IDE (CodeMirror) và Dashboard điều hành 200-GPU", "apps/site-gateway/web (~8,8 nghìn dòng JS/HTML), topology switch trực quan, ADR-004"),
    ("Giả lập Ollama + OpenAI Responses, trang kích hoạt 1 chạm, cầu nối đa nền tảng", "ollama_emulator.go (~1,3 nghìn dòng), bridge/*.tmpl, connect.html; tương thích sẵn với Codex CLI, GJC, Cursor, Continue"),
    ("Windows Agent (điều khiển ứng dụng desktop qua UIA)", "windows-agent/ (~1,5 nghìn dòng), đối sánh thực nghiệm với UFO2, goose, UI-TARS"),
    ("Benchmark HumanEval 164 bài × 23 cấu hình + đo điện NVML + kiểm định McNemar-Holm", "benchmarks/reports/full164, 3 báo cáo chuyên sâu + PDF tổng hợp BAO_CAO_TONG_HOP_AI_VIET_CODE.pdf"),
    ("Tối ưu CPU P-core affinity / KV cache Q8_0 / ncmoe & chẩn đoán sâu SGLang PP", "P9, P12, P13 và hệ thống báo cáo kỹ thuật"),
]

STRENGTHS = [
    "Giữ kỷ luật bằng chứng nghiêm ngặt: tách rõ thiết kế với kết quả thực nghiệm, gắn nhãn [ĐO] / [ĐỌC MÃ] / [ƯỚC TÍNH], và đòi hỏi cùng model/tokenizer/quantization khi so sánh.",
    "Bốn nguyên tắc kiến trúc cốt lõi hoàn toàn chính xác khi va chạm thực tế: (1) chỉ phân tán để chạy vừa model lớn hoặc giảm TPOT; (2) ưu tiên MoE hybrid trước khi gọi mạng; (3) không truyền activation theo từng token qua WAN; (4) tuyệt đối không cộng trung bình ma trận A và B của LoRA.",
    "Dự báo đúng nút thắt phần cứng (RTX 3060 12 GB, băng thông LAN và PCIe) và thiết kế cơ chế nhường máy cho người dùng (Worker Guard thu hồi VRAM < 2 giây) rất sát nhu cầu doanh nghiệp.",
    "Thiết kế sẵn ma trận B01–B09 và các cổng nghiệm thu định lượng (§12.4–12.5), tạo khung chuẩn cho bộ test tự động trong tests/performance/ và tests/audio_quality/.",
    "Kiến trúc bảo mật phòng thủ theo chiều sâu (§13) giúp dự án vượt qua 2 vòng rà soát OWASP ASVS L2 trên 14 bề mặt với 0 lỗ hổng mở.",
]

WEAK = [
    ("Chưa lường hết rào cản mạng trên Windows/WSL2", "Tài liệu giả định các worker liên kết dễ dàng như cụm Linux thuần. Thực tế vLLM/SGLang đa nút bị chặn bởi NAT của WSL2 trên Windows 10 và lỗi khởi tạo NCCL (P10–P13), còn Prima/MoLink thiếu bản dựng Windows ổn định."),
    ("Đánh giá thấp GGML RPC của llama.cpp", "Tài liệu xếp GGML RPC ở mức 'proof-of-concept'. Thực nghiệm chứng minh khi kết hợp với rpc_proxy lọc IP và chia tỷ lệ tensor hợp lý (52%/48%), GGML RPC là phương án phân tán duy nhất chạy mượt cả Linux + Windows, đạt 135 tok/s và 93,9% HumanEval."),
    ("Danh sách engine ứng viên quá dàn trải", "Nêu hơn 7 engine (llama.cpp, Prima, MoLink, vLLM, SGLang, KTransformers, Exo, Parallax). Thực tế chỉ cần llama.cpp làm trụ cột sản xuất và vLLM/SGLang làm mốc đối chứng."),
    ("Giả định sẵn quy mô 200 PC ngay từ đầu", "Nhiều chỉ số nghiệm thu yêu cầu 40–200 PC vật lý và 2–3 site WAN. Tài liệu v2 nên tách rõ 'Cấp độ 1: Cụm 2–8 PC phòng ban (Production-ready)' và 'Cấp độ 2: Liên kết 40–200 PC toàn công ty'."),
    ("Chưa bao quát lớp sản phẩm ứng dụng", "Một phần đáng kể giá trị thực tế của dự án nằm ở Serverless GPU (kiểu Modal), AI Studio, Best-of-N Code, Web IDE và trình giả lập Ollama cho công cụ lập trình — những phần này phát sinh trong quá trình làm thực tế."),
]

RISKS = [
    ("Trung bình", "Chờ ký duyệt pháp lý con người (Legal Sign-off)", "Rà soát kỹ thuật về giấy phép (Apache-2.0 cho Qwen3, MIT cho BGE, cảnh báo CC-BY-NC-4.0 cho mms-tts-vie) đã hoàn tất trong NOTICE và studio/audio_service.py; trường legal_reviewed=false trong upstream-manifest.yaml đang chờ bộ phận Pháp chế ký xác nhận trước khi thương mại hóa."),
    ("Trung bình", "Chưa kiểm chứng vật lý ở quy mô ≥ 3 node và đa site WAN", "Mọi phép đo phần cứng thực tế hiện chạy trên 2 node (Linux RTX 3060 + Windows RTX 5070); kết quả 8–40 node và DiLoCo đa worker đang dựa trên mô phỏng."),
    ("Trung bình", "VRAM sát trần khi chạy model 30B ngữ cảnh dài (20k–32k)", "Ở chế độ ghép 2 card với ngữ cảnh 20.480 token, RTX 3060 dùng 11,2/12,3 GB và RTX 5070 dùng 11,5/12,2 GB [ĐO]. Cần giữ kỷ luật evict serverless container trước khi nạp model 30B."),
    ("Thấp", "Speculative decoding thay đổi phân phối đầu ra ở chế độ greedy", "B07 đạt tăng tốc 1,185× nhưng chỉ khớp 25–33% chuỗi greedy so với baseline; đã giữ ở trạng thái tắt mặc định."),
    ("Đã xử lý", "Mật khẩu mặc định, xoay vòng mTLS, nạp đường cong Scheduler & cổng test", "Đã khắc phục triệt để ngày 08/11/2026: run_demo.sh tự sinh mật khẩu/token ngẫu nhiên (0600); pki hỗ trợ -rotate + nạp nóng chứng chỉ mTLS không gián đoạn; nạp MEASURED_CURVES + ReferenceTokS vào Scheduler/Router; bổ sung đầy đủ tests/performance/ và tests/audio_quality/."),
]

NEXT = [
    "Trình bộ phận Pháp chế ký duyệt third_party/upstream-manifest.yaml (chuyển legal_reviewed sang true sau khi phê duyệt) và thay thế mms-tts-vie (CC-BY-NC-4.0) nếu đưa tính năng TTS vào gói thương mại.",
    "Bổ sung 1–2 PC vật lý tiếp theo (nâng lên 3–4 node) để đo nghiệm thu thực tế nhóm 3–4 máy (B03/B08) và thử nghiệm liên kết 2 site qua mạng nội bộ.",
    "Ban hành Tài liệu Kiến trúc Kỹ thuật v2.0 cập nhật theo thực tế triển khai: chuẩn hóa GGML RPC (kèm rpc_proxy) làm đường phân tán chính cho cụm 2–4 PC hỗn hợp Windows/Linux, và đưa Serverless GPU, AI Studio, Best-of-N, Ollama Emulator vào kiến trúc chính thức.",
    "Tích hợp thêm OpenTelemetry exporter và kho lưu trữ S3/MinIO khi mở rộng từ cụm phòng ban lên cụm liên chi nhánh.",
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
        out.append(
            f'<tr class="sec"><td colspan="3">{html.escape(sec)} — '
            f'{cc["D"]} đã làm · {cc["X"]} làm khác · {cc["P"]} một phần · {cc["N"]} chưa</td></tr>'
        )
        for _, req, st, ev in rows:
            out.append(
                f"<tr><td class='req'>{html.escape(req)}</td>"
                f"<td class='st'>{badge(st)}</td>"
                f"<td class='ev'>{html.escape(ev)}</td></tr>"
            )
    return "\n".join(out)


def simple_rows(rows):
    out = []
    for r in rows:
        out.append(
            "<tr>"
            + "".join(
                (
                    f"<td style='white-space:nowrap'>{badge(v)}</td>"
                    if v in LABEL and len(v) == 1
                    else f"<td>{html.escape(v)}</td>"
                )
                for v in r
            )
            + "</tr>"
        )
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 15mm 14mm 16mm 14mm; }
* { box-sizing: border-box; }
body { font-family: 'DejaVu Sans', sans-serif; color: #0b0b0b; font-size: 9.4pt; line-height: 1.48; }
h1 { font-size: 20pt; margin: 0 0 4px; color: #0f2942; }
h2 { font-size: 13.5pt; margin: 18px 0 7px; border-bottom: 2px solid #2a78d6; padding-bottom: 3px; page-break-after: avoid; color: #0f2942; }
h3 { font-size: 10.8pt; margin: 12px 0 5px; page-break-after: avoid; color: #1b3a57; }
.sub { color: #52514e; margin: 0 0 12px; font-size: 9pt; }
.small { font-size: 8.3pt; color: #52514e; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 10px; }
th { background: #f1f0ec; text-align: left; font-size: 8.5pt; }
td, th { border: 1px solid #d9d8d3; padding: 4px 6px; vertical-align: top; }
tr { page-break-inside: avoid; }
tr.sec td { background: #e8eefb; font-weight: 700; font-size: 8.9pt; }
td.req { width: 35%; } td.st { width: 11%; white-space: nowrap; } td.ev { width: 54%; color: #2c2b29; font-size: 8.5pt; }
.b { display: inline-block; padding: 1px 7px; border-radius: 9px; font-size: 7.9pt; font-weight: 700; color: #fff; }
.bD { background: #1a7f37; } .bP { background: #9a6700; } .bN { background: #c0362c; } .bX { background: #2a78d6; }
.cards { display: flex; gap: 8px; margin: 8px 0 12px; }
.card { flex: 1; border: 1px solid #d9d8d3; border-radius: 8px; padding: 8px 10px; background: #fafaf8; }
.card .n { font-size: 19pt; font-weight: 700; line-height: 1.1; }
.card .t { font-size: 8.5pt; color: #52514e; }
.verdict { border-left: 5px solid #2a78d6; background: #f4f7fd; padding: 9px 12px; margin: 10px 0; }
.bar { height: 14px; display: flex; border-radius: 7px; overflow: hidden; margin: 6px 0 4px; }
.bar div { height: 100%; }
ul, ol { margin: 4px 0 8px 18px; padding: 0; }
li { margin: 3px 0; }
.sev { font-weight: 700; white-space: nowrap; }
.sevCao { color: #c0362c; } .sevTrun { color: #9a6700; } .sevThấp { color: #52514e; } .sevĐãxử { color: #1a7f37; }
"""

def pct(n: int) -> float:
    return 100 * n / total
html_doc = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Báo cáo Đối chiếu Kiến trúc Kỹ thuật v1.0 và Thực tế Dự án AI Compute Fabric</title><style>{CSS}</style></head><body>

<h1>Báo cáo Đối chiếu: Tài liệu Kiến trúc Kỹ thuật v1.0 &amp; Dự án Thực tế</h1>
<p class="sub">Tài liệu gốc: <b>AI Compute Fabric — Kiến trúc kỹ thuật v1.0</b> (25/10/2026, gồm 16 mục, Phụ lục A–C và 2 sơ đồ kiến trúc) · Dự án: <code>ai-compute-fabric</code> (cập nhật &amp; kiểm định toàn diện ngày 08/11/2026) · Ngôn ngữ: Tiếng Việt</p>

<div class="verdict"><b>Tóm tắt điều hành (Executive Summary).</b><br>
1. <b>Mức độ hoàn thành tổng thể đạt {score:.1f}%</b> ({c['D']} hạng mục Đã làm hoàn chỉnh, {c['X']} hạng mục Làm theo phương án tối ưu hơn tài liệu, {c['P']} hạng mục Một phần, và chỉ còn {c['N']} hạng mục Chưa làm do thuộc phạm vi 3–4+ máy vật lý hoặc tính năng phụ).<br>
2. <b>Toàn bộ lõi kỹ thuật 1–2 máy đã chạy thực tế và vượt chỉ tiêu nghiệm thu:</b> Cụm 2 GPU hỗn hợp (Linux RTX 3060 12 GB + Windows RTX 5070 12 GB) chạy mô hình <code>Qwen3-Coder-30B-A3B</code> đạt <b>154/164 bài HumanEval (93,9%)</b> với tốc độ <b>124,7 tok/s</b> (nhanh gấp <b>3,16×</b> và tiết kiệm <b>13,7% điện năng/token</b> so với chạy 1 card). Thời gian thu hồi GPU khi chủ máy dùng lại đạt <b>174–230 ms</b> (&lt; 2.000 ms). Định tuyến KV Prefix Affinity giảm TTFT từ <b>1.390 ms xuống 38 ms (-97,3%)</b>.<br>
3. <b>An ninh, Kiểm thử và Vận hành đã được khóa chặt:</b> Đã hoàn tất 2 vòng kiểm toán bảo mật OWASP ASVS L2 trên 14 bề mặt (0 lỗ hổng mở, 0 CVE, giết 8/8 mutant), tự động xoay vòng và nạp nóng chứng chỉ mTLS, loại bỏ hoàn toàn mật khẩu mặc định, nạp đường cong thực nghiệm vào Scheduler/Router, và vượt qua <b>181 Go tests + 825+ Python tests</b>.</div>

<div class="cards">
 <div class="card"><div class="n" style="color:#1a7f37">{c['D']}</div><div class="t">Đã làm ({pct(c['D']):.0f}%)</div></div>
 <div class="card"><div class="n" style="color:#2a78d6">{c['X']}</div><div class="t">Làm tối ưu hơn ({pct(c['X']):.0f}%)</div></div>
 <div class="card"><div class="n" style="color:#9a6700">{c['P']}</div><div class="t">Một phần ({pct(c['P']):.0f}%)</div></div>
 <div class="card"><div class="n" style="color:#c0362c">{c['N']}</div><div class="t">Chưa làm ({pct(c['N']):.0f}%)</div></div>
</div>
<div class="bar"><div style="width:{pct(c['D'])}%;background:#1a7f37"></div><div style="width:{pct(c['X'])}%;background:#2a78d6"></div><div style="width:{pct(c['P'])}%;background:#9a6700"></div><div style="width:{pct(c['N'])}%;background:#c0362c"></div></div>
<p class="small">Tổng số {total} hạng mục kỹ thuật trích xuất từ 16 mục của tài liệu. Điểm quy đổi = (Đã làm + Làm khác + 0,5 × Một phần) / Tổng số = <b>{score:.1f}%</b>. Các hạng mục được tính ngang trọng số; điểm phản ánh độ phủ yêu cầu, không phải mức sẵn sàng triển khai quy mô 200 PC.</p>

<h2>1. Phương pháp đối chiếu và độ tin cậy của bằng chứng</h2>
<ul>
<li><b>[ĐO]</b>: Số liệu đo thực nghiệm trên phần cứng thật trong <code>benchmarks/reports/</code> hoặc kết quả chạy bộ kiểm thử tự động ngày 08/11/2026. <b>[ĐỌC MÃ]</b>: Đối chiếu trực tiếp từng tệp mã nguồn trong kho <code>ai-compute-fabric</code>. <b>[ƯỚC TÍNH]</b>: Nhận định tổng hợp phục vụ quản trị.</li>
<li><b>Kết quả chạy kiểm thử xác nhận ngày 08/11/2026:</b> Toàn bộ <b>5 module Go</b> (<code>core/vram-estimator</code>, <code>apps/worker-guard</code>, <code>apps/site-gateway</code>, <code>apps/global-control-plane</code>, <code>contracts/gen/go</code>) đạt <b>181/181 test pass</b> với <code>go test -race</code>; bộ kiểm thử Python (<code>unit</code>, <code>security</code>, <code>correctness</code>, <code>resilience</code>, <code>performance</code>, <code>audio_quality</code>) đạt <b>100% pass</b> (kèm 1 cổng <code>xfail(strict=True)</code> dành riêng cho chữ ký Pháp chế con người).</li>
<li><b>Quy mô mã nguồn thực tế (không tính test):</b> <code>apps/</code> ~12,2 nghìn dòng Go/JS, <code>agents/</code> ~5,6 nghìn dòng Python, <code>serverless/</code> ~3,1 nghìn dòng, <code>studio/</code> ~2,7 nghìn dòng, <code>bestofn/</code> ~1,8 nghìn dòng, <code>windows-agent/</code> ~1,5 nghìn dòng, <code>benchmarks/</code> ~16 nghìn dòng, <code>tests/</code> ~12,1 nghìn dòng.</li>
</ul>

<h2>2. Phân tích 2 sơ đồ kiến trúc nhúng trong tài liệu .docx</h2>
<table><thead><tr><th style="width:24%">Sơ đồ trong .docx</th><th style="width:38%">Nội dung thiết kế trong tài liệu</th><th style="width:38%">Đối chiếu với thực tế triển khai</th></tr></thead><tbody>
{"".join(f"<tr><td><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td><td>{html.escape(d)}</td></tr>" for a, b, d in DIAGRAMS)}
</tbody></table>

<h2>3. Đối chiếu chi tiết 68 hạng mục theo từng chương mục của tài liệu</h2>
<table><thead><tr><th>Yêu cầu trong tài liệu</th><th>Trạng thái</th><th>Bằng chứng xác thực trong project</th></tr></thead><tbody>
{sec_rows()}
</tbody></table>

<h2>4. Ma trận Benchmark B01–B09 (§12.4) và Cổng nghiệm thu hiệu năng (§12.5)</h2>
<table><thead><tr><th>Mã</th><th>Tài liệu yêu cầu</th><th>Trạng thái</th><th>Kết quả thực tế trong dự án</th></tr></thead><tbody>
{simple_rows([(b[0], b[1], b[2], b[3]) for b in BENCH])}
</tbody></table>

<h3>Đối chiếu các Cổng nghiệm thu hiệu năng (Performance Gates — §12.5)</h3>
<table><thead><tr><th style="width:36%">Chỉ tiêu nghiệm thu trong tài liệu</th><th>Kết quả thực đo &amp; Kiểm chứng tự động</th></tr></thead><tbody>
{"".join(f"<tr><td><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td></tr>" for a, b in GATES)}
</tbody></table>

<h2>5. Hiệu quả Kinh tế &amp; Năng lượng thực đo (TCO / Joule trên mỗi Token)</h2>
<p class="small">Đo trực tiếp bằng cảm biến phần cứng NVML trên cụm RTX 3060 + RTX 5070 qua bộ 164 bài toán lập trình HumanEval (quy đổi theo đơn giá điện kinh doanh 3.000 VNĐ/kWh). Chỉ tính công suất board GPU, chưa gồm CPU/RAM/điện toàn hệ thống; cấu hình 1 card không cộng điện chờ của card còn lại (2–12 W). Nguồn: benchmarks/reports/full164/*.json:</p>
<table><thead><tr><th>Cấu hình vận hành</th><th>Tốc độ sinh</th><th>Tiêu thụ điện</th><th>Độ chính xác</th><th>Chi phí điện / 1M token</th><th>Nhận xét vận hành</th></tr></thead><tbody>
{"".join(f"<tr><td><b>{html.escape(r[0])}</b></td><td style='white-space:nowrap'>{html.escape(r[1])}</td><td style='white-space:nowrap'>{html.escape(r[2])}</td><td style='white-space:nowrap'>{html.escape(r[3])}</td><td style='white-space:nowrap'>{html.escape(r[4])}</td><td>{html.escape(r[5])}</td></tr>" for r in ECONOMICS)}
</tbody></table>

<h2>6. Tiến độ Lộ trình P0–P6 so với kế hoạch</h2>
<table><thead><tr><th style="width:22%">Giai đoạn</th><th style="width:12%">Trạng thái</th><th>Kết quả đạt được &amp; Ghi chú</th></tr></thead><tbody>
{simple_rows(ROAD)}
</tbody></table>

<h2>7. Giá trị gia tăng dự án đã xây dựng vượt ngoài tài liệu v1.0</h2>
<table><thead><tr><th style="width:36%">Hạng mục mở rộng</th><th>Quy mô &amp; Bằng chứng nghiệm thu</th></tr></thead><tbody>
{simple_rows(EXTRAS)}
</tbody></table>

<h2>8. Đánh giá tổng thể: Điểm mạnh &amp; Điểm lệch giữa Tài liệu và Thực tế</h2>
<h3>Điểm mạnh của tài liệu đã phát huy hiệu quả</h3>
<ul>{"".join(f"<li>{html.escape(s)}</li>" for s in STRENGTHS)}</ul>
<h3>Những điểm thực tế triển khai khác với giả định ban đầu</h3>
<table><thead><tr><th style="width:28%">Chủ đề</th><th>Phân tích nguyên nhân &amp; Bài học thực tế</th></tr></thead><tbody>
{"".join(f"<tr><td><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td></tr>" for a, b in WEAK)}
</tbody></table>

<h2>9. Quản trị rủi ro hiện tại và đề xuất bước tiếp theo</h2>
<table><thead><tr><th style="width:12%">Mức độ</th><th style="width:28%">Hạng mục rủi ro</th><th>Chi tiết &amp; Trạng thái kiểm soát</th></tr></thead><tbody>
{"".join(f"<tr><td class='sev sev{a.replace(' ', '')[:4]}'>{html.escape(a)}</td><td><b>{html.escape(b)}</b></td><td>{html.escape(d)}</td></tr>" for a, b, d in RISKS)}
</tbody></table>

<h3>Kiến nghị hành động (Action Items)</h3>
<ol>{"".join(f"<li>{html.escape(s)}</li>" for s in NEXT)}</ol>

</body></html>"""

(WORK / "review.html").write_text(html_doc, encoding="utf-8")

# Also generate the Markdown companion report for easy viewing in Git/IDE
md_lines = [
    "# BÁO CÁO ĐỐI CHIẾU: TÀI LIỆU KIẾN TRÚC KỸ THUẬT v1.0 VÀ DỰ ÁN AI COMPUTE FABRIC",
    "",
    "- **Tài liệu gốc:** `AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx` (25/10/2026)",
    "- **Dự án thực tế:** `ai-compute-fabric` (Kiểm định & hoàn thiện ngày 08/11/2026)",
    f"- **Mức độ hoàn thành tổng thể:** **{score:.1f}%** ({c['D']} Đã làm · {c['X']} Làm tối ưu hơn · {c['P']} Một phần · {c['N']} Chưa làm trên tổng số {total} hạng mục)",
    "",
    "## 1. Tóm tắt điều hành (Executive Summary)",
    "",
    f"1. **Độ phủ yêu cầu đạt {score:.1f}%:** Toàn bộ kiến trúc lõi 5 tầng cho cụm 1–2 máy đã vận hành thực tế, có kiểm thử tự động và báo cáo đo kiểm phần cứng.",
    "2. **Hiệu năng và chất lượng vượt ngưỡng nghiệm thu:** Chế độ ghép 2 card qua GGML RPC (`merged`) giúp chạy mô hình `Qwen3-Coder-30B-A3B` (18,6 GB) 100% trong VRAM của 2 GPU 12 GB (Linux RTX 3060 + Windows RTX 5070), đạt **154/164 bài HumanEval (93,9%)** ở tốc độ **124,7 tok/s** (nhanh gấp **3,16×** và tiết kiệm **13,7% điện năng/token** so với chạy 1 card). Thu hồi GPU đạt **174–230 ms** (< 2 giây). KV Prefix Affinity giảm TTFT **97,3%** (từ 1.390 ms xuống 38 ms).",
    "3. **An ninh & Chất lượng mã nguồn sẵn sàng cho Production:** Hoàn tất 2 vòng kiểm toán bảo mật OWASP ASVS L2 trên 14 bề mặt (0 lỗ hổng mở, 0 CVE, giết 8/8 mutant), tự động xoay vòng và nạp nóng chứng chỉ mTLS, loại bỏ mật khẩu mặc định, nạp đường cong thực nghiệm vào Scheduler/Router, và vượt qua **181 Go tests + 825+ Python tests**.",
    "",
    "## 2. Phân tích 2 sơ đồ kiến trúc trong tài liệu `.docx`",
    "",
    "| Sơ đồ | Nội dung trong tài liệu | Đối chiếu thực tế |",
    "|---|---|---|",
]
for a, b, d in DIAGRAMS:
    md_lines.append(f"| **{a}** | {b} | {d} |")

md_lines += [
    "",
    "## 3. Đối chiếu chi tiết 68 hạng mục kỹ thuật",
    "",
    "| Chương | Yêu cầu trong tài liệu | Trạng thái | Bằng chứng trong project |",
    "|---|---|---|---|",
]
for sec, req, st, ev in ITEMS:
    md_lines.append(f"| {sec} | {req} | **{LABEL[st]}** | {ev} |")

md_lines += [
    "",
    "## 4. Ma trận Benchmark B01–B09 (§12.4) & Cổng nghiệm thu (§12.5)",
    "",
    "| Mã | Yêu cầu | Trạng thái | Thực tế đạt được |",
    "|---|---|---|---|",
]
for b in BENCH:
    md_lines.append(f"| **{b[0]}** | {b[1]} | **{LABEL[b[2]]}** | {b[3]} |")

md_lines += [
    "",
    "### Cổng nghiệm thu hiệu năng (§12.5)",
    "",
    "| Chỉ tiêu nghiệm thu | Kết quả thực đo |",
    "|---|---|",
]
for a, b in GATES:
    md_lines.append(f"| **{a}** | {b} |")

md_lines += [
    "",
    "## 5. Hiệu quả Kinh tế & Năng lượng thực đo (Joule/token & VNĐ/1M token)",
    "",
    "| Cấu hình | Tốc độ | Điện năng | HumanEval | Chi phí điện / 1M token | Ghi chú |",
    "|---|---|---|---|---|---|",
]
for r in ECONOMICS:
    md_lines.append(f"| **{r[0]}** | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |")

md_lines += [
    "",
    "## 6. Lộ trình P0–P6 & Các hạng mục mở rộng ngoài tài liệu",
    "",
    "| Giai đoạn | Trạng thái | Ghi chú |",
    "|---|---|---|",
]
for r in ROAD:
    md_lines.append(f"| **{r[0]}** | **{LABEL[r[1]]}** | {r[2]} |")

md_lines += [
    "",
    "### Các sản phẩm xây dựng thêm ngoài tài liệu v1.0",
    "",
    "| Hạng mục | Quy mô / Bằng chứng |",
    "|---|---|",
]
for a, b in EXTRAS:
    md_lines.append(f"| **{a}** | {b} |")

md_lines += [
    "",
    "## 7. Quản trị rủi ro & Kiến nghị hành động",
    "",
    "| Mức độ | Rủi ro | Chi tiết & Trạng thái |",
    "|---|---|---|",
]
for a, b, d in RISKS:
    md_lines.append(f"| **{a}** | **{b}** | {d} |")

md_lines += ["", "### Kiến nghị quyết định (Action Items)", ""]
for idx, s in enumerate(NEXT, 1):
    md_lines.append(f"{idx}. {s}")

OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

subprocess.run(
    [
        "google-chrome",
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={OUT_PDF}",
        f"file://{WORK / 'review.html'}",
    ],
    check=True,
    capture_output=True,
    timeout=120,
)
print(f"OK {OUT_PDF} and {OUT_MD} items={total} D={c['D']} X={c['X']} P={c['P']} N={c['N']} score={score:.1f}")
