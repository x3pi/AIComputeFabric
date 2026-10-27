# AI Compute Fabric (`ai-compute-fabric`)

Monorepo thực thi theo `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` (Phụ lục A `[KT]` & học hỏi kiến trúc GPUStack).

## 1. Các thành phần đã hiện thực & kiểm chứng tự động (`make verify-release-v1`)

| Thành phần | Mô tả & Trạng thái hiện tại |
|---|---|
| `core/vram-estimator` (Go) | Toán học thuần 6-Bucket VRAM bão hòa (`SatSub`/`SatAdd`), công thức KV cache chuẩn GQA/Hybrid, hiệu chuẩn `CalibratedLlamaCppVRAM`, tính `MaxGPULayers`, `SplitLayers` (`0-21, 22-42, 43-63`), dung sai `G-VRAM` (93.3% test coverage). |
| `core/vram_sidecar` (Python) | Dịch vụ đọc trực tiếp header `.gguf` trên đĩa (`/facts?file=`), nối với `POST /api/v1/models/estimate-vram` của `site-gateway`. |
| `apps/site-gateway` (Go) | Control Plane & OpenAI-compatible Data Plane (`/v1/chat/completions`), đăng ký/heartbeat gRPC (hỗ trợ mTLS X.509 qua `cmd/pki`), `LeaseService` gRPC (`leases.go`), RBAC 3 vai trò (`viewer`/`operator`/`admin` trong `rbac.go`), nhật ký kiểm toán dây chuyền băm SHA-256 (`audit.go`), định tuyến WFQ + KV Session Affinity + Priority (`interactive`/`batch`) + Deadline SLA (`X-Deadline-Ms`), tích hợp API Dự báo VRAM (`/api/models/catalog`, `/api/vram/estimate`, `/api/v1/models/estimate-vram`) và Telemetry toàn cụm (`/api/stats`, `/metrics`). |
| `apps/site-gateway/web` | Giao diện Quản trị Cụm 200-GPU & AI Studio chuẩn Production (5 màn hình `index.html`, `tenants.html`, `chat.html`, `agents.html`, `studio.html`): Dark/Light theme, không giới hạn chiều ngang (`width: 100%; max-width: none`, tương thích từ màn hình Ultrawide `2560px+` xuống Mobile `375px`), 4 KPI Cards, thanh 6-Bucket VRAM toàn cụm, Heatmap 200-GPU 1 Hz, Slide-over 6-Bucket Inspector Drawer, Bộ tính VRAM Fit & Chia Layer, Quản trị Multi-Tenant WFQ, AI Playground, Đồ thị DAG tác vụ Agent và AI Studio. |
| `apps/worker-guard` (Go) | Daemon bảo vệ ưu tiên người dùng trên từng máy trạm: đọc VRAM thực từ `nvidia-smi` + parse log bộ đệm `llama-server` (`model`, `KV`, `compute`), tự động thu hồi GPU (`SIGTERM` -> chờ giải phóng VRAM xác nhận thực tế `< 2s`), báo cáo gRPC Heartbeat 6-Bucket về Gateway, kèm giao diện cục bộ tại `:8099`. |
| `apps/global-control-plane` (Go) | Liên kết đa Site qua WAN (`plane.go`, `policy.go`, `metering.go`, `cmd/gcp-server/main.go`): mTLS 1.3 bắt buộc + thu hồi serial chứng chỉ trực tiếp, thực thi chính sách `DataClass` (`PUBLIC`, `INTERNAL`, `RESTRICTED` không bao giờ rời Site gốc), vận hành độc lập khi đứt cáp WAN và sổ cái đối soát bất biến có lưu đĩa (`OpenLedger`, `VerifyLedgerFile`). |
| `inference/adapters/{llama,vllm}` | Bộ điều hợp động cơ suy luận hợp nhất (`llama.cpp` và `vLLM`), cổng kiểm tra kiến trúc GPU (`gpu_class`, `cpu_isa`) và kiểm định Speculative Decoding. |
| `agents/` (Python) | Orchestrator dựa trên LangGraph + PostgreSQL (`pgvector`) + NATS JetStream (`task_bus.py`), 15 Typed Task Schemas (`tasks.py`), RAG đa bước (`knowledge.py`), trích xuất PDF/DOCX/OCR (`doc_executors.py`), hộp cát thực thi công cụ (`sandbox.py`), và API hiển thị đồ thị DAG không lộ nội dung (`dag_api.py`). |
| `deploy/demo/` | `run_demo.sh` dựng toàn bộ hệ thống trên 1 máy; `worker.sh list|status|start <model> [--lora <job-id>]|stop` chọn model GGUF cho worker (kèm ước tính VRAM), khởi chạy lại worker, và nạp adapter LoRA đã huấn luyện trong Studio (tự xuất GGUF, kiểm tra đúng model gốc). Thẻ "Model & Adapter đang nạp" trên trang Cụm GPU (`index.html`, admin) làm cùng việc đó từ giao diện, trạng thái cập nhật trực tiếp từ heartbeat: chọn model GGUF + adapter, worker-guard khởi động lại engine khi GPU rảnh (`--models-dir`/`--adapters-dir` bật `/api/engine`; chỉ nhận tên tệp trơn trong các thư mục đó). |
| `studio/` & `training/` & `audio/` | Dịch vụ AI Studio đa tenant (`studio/`), huấn luyện QLoRA + xuất `.gguf` + toán gộp LoRA (`naive`, `exact`, `lora_fa`, `svd`), và đường ống âm thanh tiếng Việt (`Qwen3-ASR-0.6B` + `mms-tts-vie` + WER/CER chuẩn hóa NFC). |
| `contracts/` & `docs/SRE_RUNBOOK.md` | Hợp đồng Protobuf v1 (`fabric.proto`, `gateway.proto`), đặc tả OpenAPI 3.0.3 (`contracts/openapi/aicf_v1.yaml` kiểm chứng tự động đối chiếu từng route Go), SBOM ký số `cosign` (`deploy/security/verify_sboms.sh`), `NOTICE`, `release-manifest-v1.0.yaml` và tài liệu vận hành `docs/SRE_RUNBOOK.md`. |

## 2. Đã đo kiểm thực nghiệm trên phần cứng thật

### 2.1. Cụm đa máy thật dị thể (Linux RTX 3060 + Windows 10 Pro RTX 5070 qua mTLS LAN 2.5 GbE)
- **Đo kiểm suy luận đa máy E2E (`p2_multinode_linux3060_win5070_e2e.json`):**
  - Node Linux (`pc-001` - RTX 3060 12GB): **180.4 tok/s**, TTFT p50 729 ms (46/46 request OK).
  - Node Windows (`pc-win-01` - RTX 5070 12GB): **166.9 tok/s**, TTFT p50 1,134 ms (44/44 request OK).
  - **Chạy song song cả 2 node:** **334.9 tok/s** (hiệu suất scale 96.4%, 90/90 request 100% thành công, 0 lỗi).
- **Điều khiển từ xa qua Gateway:** Lệnh `Pause`/`Resume` từ xa phản hồi dưới 2 giây; `Live Model Switch` chuyển đổi model `Qwen3-4B` <-> `Qwen3-8B` thành công qua API.

### 2.2. Đo kiểm chuyên sâu trên máy Windows 10 Pro (`fios02@192.168.1.49`, RTX 5070 12 GB, Ryzen 7 9700X)
- **Baseline B01 (`llama-bench`):** Qwen3-4B đạt `179.7 tok/s` (decode), Qwen3-8B đạt `115.65 tok/s` (decode, gấp 1.78x RTX 3060), RSD < 0.2%. Báo cáo: `p0_B01_llamabench_Qwen3-4B_win_rtx5070.json`, `p0_B01_llamabench_Qwen3-8B_win_rtx5070.json`.
- **Nghiệm thu cổng thu hồi GPU (`G-RECLAIM` trên Windows WDDM):** 10/10 lần đạt, min `62.5 ms`, trung vị `174.1 ms`, p95 `638.2 ms` (< 2.0 s). Báo cáo: `p2_gpu_reclaim_poc_win_rtx5070.json`.
- **Dịch vụ Windows `AICFWorkerGuard`:** Quản lý bởi Windows SCM; `Stop-Service` tắt sạch `llama-server.exe` trong `262 ms` và trả VRAM về `203 MiB` nền; `Start-Service` khởi động lại trong `264 ms`. Báo cáo: `p2_windows_service_lifecycle_rtx5070.json`.
- **Khảo sát WDDM & Đóng quyết định D06:** `nvidia-smi` theo tiến trình in `[N/A]`, nhưng bộ đếm PDH `\GPU Process Memory(*)\Dedicated Usage` đọc được VRAM từng PID chính xác (đo được 6.174.539.776 bytes khi nạp 8B, khớp với buffer size và tổng VRAM adapter). Báo cáo: `p0_probe_windows_rtx5070.json`, `p0_probe_windows_loaded_rtx5070.json`.

### 2.3. Đo kiểm trên máy Linux (Core Ultra 7 265K, 1 × RTX 3060 12 GB)
- **Hiệu chuẩn VRAM (`G-VRAM`):** Đạt **28/28** cấu hình `Q4_K_M`, **16/16** cấu hình `Q8_0`/`F16` (`Qwen3-4B` & `Qwen3-8B`), và **5/5** cấu hình `LOCAL_HYBRID` với `Qwen2.5-32B-Instruct Q4_K_M` (`ngl=24..36`). Báo cáo: `benchmarks/reports/p1_vram_calibration_rtx3060.json`, `p1_vram_calibration_q8_f16_rtx3060.json`, `p1_vram_hybrid_32b_rtx3060.json`.
- **Thu hồi GPU (`G-RECLAIM`):** Trung vị `273 ms`, p95 `283 ms`, p99 `284 ms` dưới áp lực cấp phát đột ngột 4 GiB CUDA (`benchmarks/reports/p2_gpu_reclaim_poc_rtx3060_v2.json`).
- **Định tuyến KV Session Affinity & WFQ:** Giảm TTFT lượt hội thoại thứ 2 từ `1.390 ms` xuống `38 ms` (`36,6×`, `benchmarks/reports/p2_session_affinity_rtx3060.json`).
- **Kiểm thử E2E trình duyệt Chrome thật (`Playwright`):** Đã kiểm chứng tự động tất cả giao diện:
  - `tests/e2e/dashboard_e2e.mjs` -> `benchmarks/reports/p2_dashboard_e2e.json` (18/18 PASS, gồm kiểm tra màn hình Ultrawide `2560x1440` và Mobile `375x812`).
  - `tests/e2e/roles_e2e.mjs` -> `benchmarks/reports/p2_roles_e2e.json` (RBAC 3 vai trò `viewer`/`operator`/`admin`).
  - `tests/e2e/agents_dag_e2e.mjs` -> `benchmarks/reports/p5_agent_dag_e2e.json` (Đồ thị DAG Agent 14 bước thời gian thực).
  - `tests/e2e/studio_e2e.mjs` -> `benchmarks/reports/p9_studio_evidence.json` (RAG, Prompts/Few-shot, QLoRA thật & Full-FT Estimate).

## 3. Chạy thử Demo một lệnh & Kiểm tra Phát hành
```bash
# Chạy toàn bộ cổng kiểm tra Release v1 (Go tests, Python tests, Proto, Cross-build Linux/Windows, Linters, Cosign SBOM)
make verify-release-v1

# Chạy Demo thực tế với GPU NVIDIA + llama-server
deploy/demo/run_demo.sh /đường/dẫn/llama-server /đường/dẫn/model.gguf
```
Xem thêm hướng dẫn tại `docs/TESTING.md` và cẩm nang vận hành SRE tại `docs/SRE_RUNBOOK.md`.
