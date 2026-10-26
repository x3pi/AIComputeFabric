# ai-compute-fabric

Monorepo thực thi theo `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` (Phụ lục A `[KT]` & học hỏi kiến trúc GPUStack).

## Các thành phần đã hiện thực & kiểm chứng tự động (`make verify-dev`)
| Thành phần | Trạng thái hiện tại |
|---|---|
| `core/vram-estimator` (Go) | Toán học thuần 6-Bucket VRAM bão hòa (`SatSub`/`SatAdd`), công thức KV cache chuẩn GQA/Hybrid, hiệu chuẩn `CalibratedLlamaCppVRAM`, tính `MaxGPULayers`, `SplitLayers` (`0-21, 22-42, 43-63`), dung sai `G-VRAM` (93.3% test coverage). |
| `apps/site-gateway` (Go) | Control Plane & OpenAI-compatible Data Plane (`/v1/chat/completions`), đăng ký/heartbeat gRPC (hỗ trợ mTLS X.509 qua `cmd/pki`), định tuyến WFQ (Weighted Fair Queuing) + KV Session Affinity + Priority (`interactive`/`batch`) + Deadline SLA (`X-Deadline-Ms`), quản lý Tenant & API Key băm SHA-256 (`0600`), tích hợp trực tiếp API Dự báo VRAM (`/api/models/catalog`, `/api/vram/estimate`) và Telemetry toàn cụm (`/api/stats`, `/metrics`). |
| `apps/site-gateway/web` | Giao diện Quản trị Cụm 200-GPU & Dự báo VRAM chuẩn Production (lấy cảm hứng từ GPUStack): 4 KPI Cards thời gian thực, thanh 6-Bucket VRAM toàn cụm, Heatmap 200-GPU 1 Hz (`< 60 B/worker`), Slide-over Drawer soi chi tiết 6-Bucket từng máy, Bộ tính VRAM Fit (`GREEN`/`YELLOW`/`RED`) & Chia Layer (`LOCAL_FULL` / `LOCAL_HYBRID` / `DISTRIBUTED_MODEL`), Quản trị Multi-Tenant WFQ và AI Playground (Streaming + Burst 8 luồng). |
| `apps/worker-guard` (Go) | Daemon bảo vệ ưu tiên người dùng trên từng máy trạm: đọc VRAM thực từ `nvidia-smi` + parse log bộ đệm `llama-server` (`model`, `KV`, `compute`), tự động thu hồi GPU (`SIGTERM` -> chờ giải phóng VRAM xác nhận thực tế `< 2s`), báo cáo gRPC Heartbeat 6-Bucket về Gateway, kèm giao diện cục bộ tại `:8099`. |
| `contracts/protobuf/aicf/v1/` | Hợp đồng Protobuf v1 (`fabric.proto`, `gateway.proto`) đã biên dịch và sinh mã Go tại `contracts/gen/go`. |
| `core/model_registry/security.py` | Kiểm tra toàn vẹn thư mục model offline (SHA256, chặn `.pkl`/`.pickle`, chặn `auto_map` khi `trust_remote_code=False`, kiểm tra tokenizer). |
| `core/adaptive_scheduler/scheduler.py` | Bộ lập lịch lọc cứng (Hard Filters) + tối ưu thời gian hoàn thành kỳ vọng (`expected_completion_ms`). |
| `training/lora_merger.py` & `audio/` | Toán gộp LoRA (`naive`, `exact`, `lora_fa`, `svd`) và chuẩn hóa tiếng Việt + WER/CER + chia tập `speaker-disjoint`. |

## Đã đo kiểm thực nghiệm trên phần cứng thật (1 máy Linux, 1 × RTX 3060 12 GB)
- **Hiệu chuẩn VRAM (`G-VRAM`):** `gguf-parser` thô đạt 20/28 cấu hình; sau quy tắc hiệu chuẩn `CalibratedLlamaCppVRAM` đạt **28/28** cấu hình (`Qwen3-4B` và `Qwen3-8B`, sai số tối đa 197 MiB) và **5/5** cấu hình `LOCAL_HYBRID` với `Qwen2.5-32B-Instruct Q4_K_M` (`ngl=24..36`). Báo cáo: `benchmarks/reports/p1_vram_calibration_rtx3060.json` & `p1_vram_hybrid_32b_rtx3060.json`.
- **Thu hồi GPU (`G-RECLAIM`):** Đo thực tế với `Qwen3-8B` dưới tải suy luận khi cấp phát đột ngột 4 GiB CUDA: trung vị `273 ms`, p95 `283 ms`, p99 `284 ms`, giải phóng 100% về `1 MiB` (`benchmarks/reports/p1_reclaim_rtx3060.json`).
- **Định tuyến KV Session Affinity:** Giảm TTFT lượt hội thoại thứ 2 (prefix ~2.000 token) từ `1.390 ms` xuống `38 ms` (`36,6×`, `benchmarks/reports/p3_prefix_affinity_rtx3060.json`).
- **Kiểm thử E2E trình duyệt Chrome thật (`Playwright`):** Chạy tự động toàn bộ luồng Heatmap, 6-Bucket VRAM Estimator, Worker Drawer, Tạo Tenant/API Key một lần, Chat Streaming, Burst 8 yêu cầu song song và Pause/Resume worker (`benchmarks/reports/p2_dashboard_e2e.json`).

## Chạy thử Demo một lệnh (Linux + GPU NVIDIA)
```bash
deploy/demo/run_demo.sh /đường/dẫn/llama-server /đường/dẫn/model.gguf
```
Xem hướng dẫn chi tiết tại `docs/TESTING.md`.

## Hạng mục tiếp theo (cần cụm máy Windows vật lý & hạ tầng Production)
- Chạy nghiệm thu thực tế trên các máy trạm Windows 10/11 (WDDM, PDH counters, Windows Service, gaming trace).
- Tích hợp NATS JetStream, PostgreSQL state store, HA Gateway và ký duyệt Pháp lý/HR (`legal_reviewed=true`).

## Chạy toàn bộ bộ kiểm tra tự động
```bash
python3 -m venv .venv && .venv/bin/pip install pytest numpy pyyaml protobuf
PY=.venv/bin/python make verify-dev
```
