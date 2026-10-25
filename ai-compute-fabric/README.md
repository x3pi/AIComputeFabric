# ai-compute-fabric

Khung monorepo theo `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` (Phụ lục A [KT]).

## Hiện có (kiểm chứng được trên máy dev, không cần phần cứng)
| Thành phần | Trạng thái |
|---|---|
| `core/vram-estimator` (Go) | Toán học thuần: KV cache, 6 ngăn bão hòa, chia layer, dung sai G-VRAM. **Chưa** nhúng `gguf-parser-go`, **chưa** có endpoint |
| `core/model_registry/security.py` | Kiểm tra toàn vẹn thư mục model offline (SHA256, pickle, auto_map, tokenizer). **Chưa** có tải từ HF Hub/mirror |
| `core/adaptive_scheduler/scheduler.py` | Logic lọc cứng + chọn theo thời gian kỳ vọng. **Chưa** có gateway/lease/NATS |
| `training/lora_merger.py` | Toán gộp LoRA (naive vs exact vs LoRA-FA vs SVD) |
| `audio/` | Chuẩn hóa tiếng Việt, WER/CER tham chiếu, chia tập speaker-disjoint |
| `contracts/protobuf/aicf/v1/fabric.proto` | Hợp đồng v1 (biên dịch bằng `protoc`); **chưa** sinh code Go/Python |
| `third_party/upstream-manifest.yaml` | Khóa SHA ứng viên + license; 3 mục `UNVERIFIED` đang ở tier `research` |
| `benchmarks/hardware/probe_node.py` | Khảo sát phần cứng (Linux đầy đủ; Windows còn thiếu) |

## Đã đo thật (1 máy Linux, 1 × RTX 3060 12 GB — không phải fleet Windows)
- Estimator: `gguf-parser` thô đạt 20/28 cấu hình; sau quy tắc hiệu chuẩn 28/28 (8/8 ở nhóm kiểm độc lập). Báo cáo + log: `benchmarks/reports/`.
- B01 một phần: Qwen3-8B Q4_K_M decode 64,9 tok/s; Qwen3-4B 104,5 tok/s (`llama-bench`, concurrency 1).
- Harness: `benchmarks/hardware/calibrate_vram.py` (cần llama-server, gguf-parser và GPU NVIDIA).

## Chưa có (cần phần cứng/con người thật)
worker-guard, site-gateway, dashboard, mọi adapter engine, benchmark B01–B09, soak 72 h, pilot, WAN, Legal/HR duyệt.

## Chạy kiểm tra
```
python3 -m venv .venv && .venv/bin/pip install pytest numpy pyyaml protobuf
PY=.venv/bin/python make verify-dev
```
