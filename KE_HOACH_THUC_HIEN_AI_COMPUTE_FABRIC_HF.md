# Kế Hoạch Thực Hiện AI Compute Fabric (Bản Duy Nhất, Đã Rà Soát, Bổ Sung Chi Tiết WBS & Tiêu Chuẩn Viết Test / Check Pass)

> **Tài liệu tham chiếu gốc:**
> - `AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx` (25/10/2026) — gọi tắt **[KT]**. Các mục "Phụ lục A/B/C", "Mục 12.4 (`B01–B09`)", "Mục 15.1 (7 tầng kiểm thử)" trong kế hoạch này đều chiếu trực tiếp từ **[KT]**.
> - Thiết kế tổng thể `Distributed AI Compute Fabric — Qwen, MoE, AI Agents & Distributed Training (v1.0)`.
> - **Hướng kiến trúc đã chốt (xem Phần G):** **"Adopt có chọn lọc + Extend"** — tận dụng thư viện `gguf-parser-go` và học thiết kế GPUStack; **worker trên PC Windows do ta tự xây (`worker-guard` quản `llama-server`)** vì worker GPUStack chỉ hỗ trợ Linux ✅ (xem **§B.0b / ADR-001** và **Phần G**), kết hợp sâu **hệ sinh thái mã nguồn mở Hugging Face** cho định danh model/tokenizer, an toàn artifact, đánh giá toán học (correctness) và huấn luyện phân tán (PEFT/DiLoCo).
> - **Nguyên tắc Kiểm thử & Nghiệm thu (Test-First & Gate-Driven):** Không một module hay phase nào được phép đánh dấu hoàn thành (`DONE`) nếu chưa có mã kiểm thử tự động (`tests/`) và chưa chạy lệnh kiểm tra đạt ngưỡng định lượng (`Check Pass` với `exit code = 0`).
> - **Quy ước ký hiệu:** ✅ Đã kiểm chứng từ nguồn gốc · ⚠️ Cần đo/xác minh trên phần cứng thật tại P0 · Số liệu ví dụ là **minh họa công thức, không phải benchmark đã đo**.

---

## PHẦN A — NHẬT KÝ RÀ SOÁT & CẬP NHẬT KIỂM CHỨNG KỸ THUẬT

### A.1. Điểm mạnh cốt lõi được giữ vững
- Dùng **Hugging Face** (`huggingface_hub`, `safetensors`, `transformers`, `tokenizers`, `accelerate`, `peft`, `trl`, `datasets`, `evaluate`, `lighteval`) làm xương sống cho **định danh mô hình/tokenizer, an toàn trọng số, kiểm định toán học (model correctness) và huấn luyện phân tán**.
- Học và tận dụng **GPUStack** ở 2 điểm đắt giá nhất: **Bộ ước lượng VRAM trước khi nạp (`gguf-parser-go`)** và **Giao diện quản trị cụm trực quan (Live VRAM Fit Preview & Cluster Heatmap)**.
- Kế toán VRAM đa ngăn + Scheduler tôn trọng tuyệt đối nguyên tắc **"Người dùng PC là ưu tiên số 1"** ([KT] §5.3).

### A.2. Bảng xử lý 16 vấn đề từ bản nháp trước & Kết quả xác minh mới nhất

| # | Mức | Vấn đề trong bản nháp cũ | Cách xử lý triệt để trong kế hoạch hoàn thiện này |
|---|:-:|---|---|
| 1 | 🔴 | **§C (Kế hoạch P0–P8) mới dừng ở tóm tắt**, thiếu danh mục công việc (WBS), đường dẫn deliverable, yêu cầu viết test và lệnh check pass cụ thể. | **§C** được viết chi tiết toàn diện cho cả 9 giai đoạn (**P0 → P8**): gồm WBS theo nhóm kỹ thuật, Deliverables, **Danh sách bài Test bắt buộc phải viết** và **Tiêu chuẩn Check Pass định lượng (CLI + Ngưỡng)**. |
| 2 | 🔴 | **Ví dụ Qwen 32B sai số học** (42/64 layer không vừa 10 GB VRAM; 0–21/22–43/44–64 thành 65 layer). | Đã sửa chuẩn xác tại **§B.1**: Qwen 32B Q4_K_M (≈19,8 GB, 64 layer ≈ 0,31 GB/layer) trên 1 PC chỉ đặt được ~28–30 layer vào VRAM, ~34–36 layer ở RAM; trên 3 PC chia 0–21 / 22–42 / 43–63 (22+21+21 = 64 layer). |
| 3 | 🔴 | **Hứa "chính xác từng MB / sai số <5%" thiếu định nghĩa đo & hỗ trợ Qwen3.5 hybrid.** | ✅ **Đã xác minh:** `gpustack/gguf-parser-go` (`file_architecture.go`) đã có cờ `AttentionHybrid`, `AttentionRecurrent`, `FullAttentionInterval` (`<arch>.full_attention_interval`) cho kiến trúc lai Qwen3.5/Jamba. Tuy nhiên vẫn bắt buộc qua **vòng hiệu chuẩn với log thực của `llama.cpp` + NVML peak** (sai số $\le \max(5\%, 300\text{ MB})$ trên $\ge 95\%$ ma trận, xem **§B.1**). |
| 4 | 🔴 | **Kiểm toán VRAM bằng NVML per-process không khả thi trên Windows WDDM** và không tự tách được Weights/KV/Compute bên trong 1 process. | **§B.2** chuyển sang mô hình **6 ngăn VRAM**: Ngăn 1 (`OS & User`) = `NVML Total Used − Σ VRAM của tiến trình thuộc Fabric`; Ngăn 2–4 lấy từ `Estimator + Engine Metrics`; Ngăn 5 là `Headroom` tường minh; Ngăn 6 là `Allocatable`. |
| 5 | 🟠 | **Chưa chốt Build-vs-Adopt với GPUStack & rủi ro Worker trên Windows.** | ✅ **Đã xác minh (trang repo chính, qua tóm tắt tự động — kiểm lại ở P0 ngày 1–2):** `gpustack/gpustack` là **Apache-2.0**; trang ghi rõ **worker chỉ hỗ trợ Linux** (Windows qua WSL2); backend nêu là vLLM, SGLang, TensorRT-LLM (trang không nhắc llama-box/llama.cpp và không mô tả ước lượng VRAM). Fleet là PC Windows của nhân viên ⇒ **quyết định D01 (Phần G): tự xây `worker-guard` quản trực tiếp `llama-server`; GPUStack không vào production worker.** ADR-001 vẫn chạy cổng P0 để xác nhận lại. |
| 6 | 🟠 | **Cổng nghiệm thu (Gate) bị lệch so với [KT] §12.5 hoặc thiếu ngưỡng đo.** | **§C & §D** khôi phục chuẩn xác cổng [KT] §12.5 (ví dụ B04/B05: *"Giảm TPOT ≥30% so với PP baseline cũ **hoặc** chạy được model không vừa máy đơn"*) và bổ sung phương pháp đo + câu lệnh check pass cho mọi cổng. |
| 7 | 🟠 | **Lệch pha giữa P0–P8 và P0–P6 của [KT] §14; nhồi quá nhiều việc vào P1–P2.** | Đầu **§C** có bảng ánh xạ 1-1 giữa `P0–P8` và `[KT] P0–P6`; tách rõ MVP ở P2 và nâng cấp theo từng phase P3–P7. |
| 8 | 🟠 | **Thiếu hạng mục vận hành thực tế cho cụm 200 PC văn phòng.** | Bổ sung đầy đủ vào **P2, P7, P8 và §E**: Windows MSI installer ký số (Authenticode), triển khai GPO/Intune, auto-update theo vòng (Canary 5% → 25% → 100%), rollback tự động, giới hạn công suất GPU (Power Limit W) bảo vệ PSU văn phòng, quy trình opt-out/thông báo nhân viên. |
| 9 | 🟠 | **Một số thư viện HF trong bảng cũ không phù hợp (TGI, smolagents CodeAgent, accelerate offload).** | **§B.4**: Loại bỏ hoàn toàn **TGI** (đã vào maintenance mode ✅); `accelerate` chỉ làm **chuẩn đúng đắn toán học (correctness golden reference)** chứ không làm baseline tốc độ; bỏ `CodeAgent` của `smolagents` (chỉ giữ schema định nghĩa Typed Tool trong sandbox để tuân thủ [KT] §13). |
| 10 | 🟠 | **Ranh giới giữa "HF-first" và "Benchmark-first" chưa rõ.** | Chốt nguyên tắc **§B.0**: HF-first cho *Model Registry, Tokenizer, Safetensors, Evaluation, Training*; Benchmark-first cho *Inference Runtime*. |
| 11 | 🟡 | **Gọi header GGUF là "vài KB".** | Sửa chính xác: Header GGUF gồm cả từ điển tokenizer (~152k token của Qwen) nên nặng **~3–10 MB**; `gguf-parser-go` đọc theo chunk HTTP/file và được cache theo `gguf_sha256` tại Site Gateway. |
| 12 | 🟡 | **Thiếu chuỗi truy xuất nguồn gốc (provenance) từ HF Safetensors sang GGUF.** | **§B.5**: Mở rộng `ModelIdentity` Protobuf để lưu `source_repo`, `source_revision`, `converter` (`llama.cpp/convert_hf_to_gguf.py@commit`), `gguf_sha256`, `license_spdx`. |
| 13 | 🟡 | **Gọi SVD LoRA merge là "chính xác tuyệt đối".** | **§C (P6)**: Ghi rõ Truncated SVD về hạng $r$ chỉ là *xấp xỉ tốt nhất theo chuẩn Frobenius*; giải pháp bảo toàn toán học chính xác khi đồng bộ DiLoCo là **LoRA-FA** (đóng băng ma trận $A$, chỉ huấn luyện & lấy trung bình ma trận $B$: $\frac{1}{K}\sum B_k A = (\frac{1}{K}\sum B_k) A$) hoặc **Stack-Rank / Merge vào trọng số nền**. |
| 14 | 🟡 | **Giấy phép từng thành phần & thời điểm kiểm toán.** | ✅ **Đã xác minh:** `gpustack/gpustack` (Apache-2.0), `gpustack/gpustack-ui` (Apache-2.0), `gpustack/gguf-parser-go` (MIT). Đưa toàn bộ công tác kiểm toán giấy phép (source + model Qwen + dataset âm thanh) lên **cổng chặn P0**. |
| 15 | 🟡 | **Thiếu cấu hình kiểm chứng của `Qwen3-8B` và `Qwen3.5-35B-A3B`.** | ✅ **Đã xác minh trên Hugging Face:** `Qwen/Qwen3-8B` có `num_hidden_layers=36, num_key_value_heads=8, head_dim=128` $\implies$ KV Cache FP16 @ 8k ctx = $2 \times 36 \times 8 \times 128 \times 8192 \times 2 = 1,125\text{ GiB} \ (\approx 1,21\text{ GB})$. `Qwen/Qwen3.5-35B-A3B` có `text_config`: `num_hidden_layers=40, num_key_value_heads=2, head_dim=256`. |
| 16 | 🟡 | **Phân bổ màn hình Dashboard theo đúng thứ tự hoàn thiện backend.** | **§B.6**: Gán từng màn hình Dashboard vào đúng Phase có backend tương ứng (P2, P3, P5, P6, P7) kèm kiểm thử E2E Playwright. |

---

## PHẦN B — KIẾN TRÚC KỸ THUẬT ĐÃ HIỆU CHỈNH

### B.0. Bốn Nguyên Tắc Kiến Trúc Bất Biến
1. **HF-First cho Định danh, An toàn, Kiểm định & Huấn luyện; Benchmark-First cho Runtime Suy luận** ([KT] §1, §15.2).
2. **Một Chủ Sở Hữu Lập Lịch Duy Nhất (`Adaptive Compute Scheduler`):** `core/vram-estimator` chỉ là dịch vụ tính toán/dự báo thuần túy, không tự ý cấp phát tài nguyên.
3. **Adopt có chọn lọc + Extend:** Tái sử dụng `gguf-parser-go` (MIT, độc lập với GPUStack server) và học thiết kế/UX của GPUStack; **không** đặt GPUStack Server/Worker vào đường production trên PC Windows (worker chỉ Linux ✅). Chỉ xem xét dùng GPUStack cho nhóm máy chủ GPU Linux riêng (nếu có) qua API, không fork.
4. **Minh Bạch Độ Tin Cậy Của Số Liệu:** Mọi dự báo VRAM và độ trễ đều phải gắn nhãn `ESTIMATED` (công thức), `CALIBRATED` (đã nhân hệ số hiệu chỉnh), hoặc `MEASURED` (đã đo thực tế trên phần cứng đích) kèm biên sai số `%`.

### B.0b. ADR-001 — Chiến Lược "Adopt + Extend" Với GPUStack & Đường Lui Kiến Trúc

| Lớp chức năng | Thành phần tái sử dụng (Adopt) | Thành phần tự phát triển (Extend / First-Party) | Phương án dự phòng nếu trượt cổng P0 (Fallback) |
|---|---|---|---|
| **Ước lượng VRAM GGUF** | Thư viện Go `gpustack/gguf-parser-go` (MIT ✅) | Lớp hiệu chuẩn `core/vram-estimator` + Sidecar Python cho `safetensors`/MoE/Training | Thư viện `gguf-parser-go` độc lập hoàn toàn với GPUStack server nên luôn dùng được 100%. |
| **Quản lý Worker & Serving nội bộ 1 Site** | **Mặc định (D01): không dùng GPUStack Worker** (chỉ Linux ✅). Tùy chọn: GPUStack cho nhóm máy chủ GPU Linux riêng | Daemon `worker-guard` (Go) chạy native Windows/Linux: khởi chạy/quản vòng đời `llama-server`, giám sát người dùng, tính VRAM 6 ngăn, thu hồi GPU | Nếu fleet hóa ra ≥ 80 % Linux **và** GPUStack qua được điều kiện W2/W4 → đánh giá lại (ADR-001 bản 2). |
| **Giao diện Quản trị Cụm (`apps/dashboard`)** | Chỉ **học bố cục** từ `gpustack/gpustack-ui` (Apache-2.0 ✅); không phụ thuộc GPUStack UI vì nó nối với GPUStack Server | Viết mới toàn bộ: Heatmap 200 GPU, Preemption & Quota, Deploy + Live VRAM Fit, Agent DAG, Training, WAN/Metering | Nếu muốn tái dùng mã component → qua quy trình provenance ([KT] §10.3). |
| **Model Registry & Provenance** | `huggingface_hub`, `safetensors`, `tokenizers` | Chuỗi xác thực SHA256 (`HF Repo → Converter → GGUF`) & Local Site Mirror | Tự chủ 100% trong `core/model-registry`. |
| **Inference Phân tán (3–4 PC)** | So sánh đối chứng `llama-box` RPC của GPUStack | Sandbox cô lập `Prima.cpp` và `MoLink` (bọc mTLS) | Chỉ giữ lại duy nhất engine thắng bài benchmark `B04/B05`. |

**5 Điều kiện Cổng P0 cho ADR-001 (Bắt buộc kiểm thử bằng script tự động):**
1. **Cổng W1 (Windows Native Stability):** `worker-guard` + `llama-server` (và GPUStack Worker qua WSL2 nếu muốn so sánh) chạy trên 5–10 PC Windows 10/11 thật (Core i5 + RTX 3060) liên tục 24h không treo driver WDDM; `worker-guard` thu hồi VRAM khi người dùng mở ứng dụng nặng đạt **p95 < 2,0 giây** qua $\ge 100$ lần đo.
2. **Cổng W2 (Zero-Fork Preemption):** Cơ chế tạm dừng nhận việc và giải phóng GPU theo khung giờ/người dùng hoạt động hoàn toàn qua API/sidecar mà không phải sửa mã nguồn lõi của GPUStack.
3. **Cổng W3 (Estimator Accuracy):** Sai số ước lượng VRAM sau hiệu chuẩn đạt $\le \max(5\%, 300\text{ MB})$ cho cả model Dense (`Qwen3-8B`, `Qwen2.5-32B`) và Hybrid MoE (`Qwen3.5-35B-A3B`).
4. **Cổng W4 (200-Node Scale Simulation):** Control plane chịu tải mượt khi giả lập 200 Worker gửi heartbeat 1 Hz đồng thời (độ trễ lập lịch p95 < 150 ms).
5. **Cổng W5 (License & Security):** Không phát sinh dependency GPL/AGPL nhiễm vào mã thương mại; không mở cổng RPC không xác thực trên mạng LAN doanh nghiệp.

---

### B.1. Thiết Kế Chi Tiết `core/vram-estimator` & Vòng Hiệu Chuẩn

- **Kiến trúc thực thi:**
  - **Đường GGUF (Go):** Nhúng `github.com/gpustack/gguf-parser-go` đọc chunk header `.gguf` (ngân sách đọc ~3–10 MB đầu file, lưu cache kết quả parse theo `gguf_sha256` vào PostgreSQL/Redis của Site Gateway).
  - **Đường Safetensors / PyTorch / Training (Python Sidecar):** Sử dụng `huggingface_hub` tải `config.json` + `model.safetensors.index.json` (chỉ vài chục KB) kết hợp `accelerate.commands.estimate` và công thức phân rã KV cache / LoRA gradients.
- **Công thức tổng quát trên từng card RTX 3060 thứ $i$:**
  $$\text{VRAM}_i = W_{\text{weights}, i} + \text{KV}_{\text{attn}, i}(C_{\text{ctx}}, B_{\text{par}}, D_{\text{kv}}) + S_{\text{recurrent}, i} + B_{\text{compute}, i}(n_{\text{ubatch}}, \text{FA}) + \Omega_{\text{CUDA\_ctx}} + H_{\text{calib}}(\text{engine}, \text{arch})$$
  - Trong đó với `Qwen/Qwen3-8B` (`36` layers, `8` KV heads, `head_dim = 128` ✅): tại $C_{\text{ctx}} = 8192, B_{\text{par}} = 1$, $\text{KV}_{\text{FP16}} = 2 \times 36 \times 8 \times 128 \times 8192 \times 2 \text{ bytes} = 1.125\text{ GiB} \ (\approx 1,21\text{ GB})$.
  - Với `Qwen/Qwen3.5-35B-A3B` (`40` layers, hybrid Gated DeltaNet + Full Attention ✅): `gguf-parser-go` đọc `FullAttentionInterval` để tính $\text{KV}_{\text{attn}}$ cho các lớp Full Attention và cộng $S_{\text{recurrent}}$ (được hiệu chuẩn từ log `recurrent_state` của `llama.cpp`) cho các lớp DeltaNet.

---

### B.2. Kế Toán VRAM 6 Ngăn Trên Từng PC (`6-Bucket VRAM Accounting`)

Để khắc phục hạn chế của NVML trên Windows WDDM (không báo cáo tin cậy VRAM của từng tiến trình đồ họa của Windows), `worker-guard` tính toán 6 ngăn bộ nhớ mỗi giây như sau:

| Ngăn (Bucket) | Mã trường Protobuf | Công thức / Nguồn thu thập chính xác | Màu chuẩn trên UI |
|---|---|---|:-:|
| **1. `OS & User`** | `os_user_bytes` | $\max(0,\ \text{NVML}_{\text{total\_used}} - (\text{weights} + \text{kv\_state} + \text{compute}))$, trong đó 3 số trừ do **engine tự báo** (không dùng NVML per-process của Fabric vì không tin cậy trên WDDM). Windows: đối chiếu thêm bộ đếm PDH `GPU Process Memory` (⚠️ xác minh ở P0). | Đỏ cam (`#F97316`) |
| **2. `Model Weights`** | `weights_bytes` | Giá trị `model buffer size` thực tế đọc từ stdout/metrics của engine (hoặc từ `VramEstimate` khi đang khởi tạo). | Xanh dương (`#3B82F6`) |
| **3. `KV / State Cache`** | `kv_state_bytes` | Giá trị `KV buffer size + recurrent buffer size` đã đặt chỗ theo `max_ctx × n_parallel`. | Tím (`#8B5CF6`) |
| **4. `Compute & Runtime`** | `compute_bytes` | Giá trị `compute buffer size` + CUDA context (~350–500 MB) + NCCL/gRPC buffer. | Vàng (`#EAB308`) |
| **5. `Safety Headroom`** | `headroom_bytes` | Ngưỡng đệm bảo vệ trải nghiệm người dùng (mặc định `1.536 MB` trong giờ hành chính, `512 MB` ngoài giờ). | Xám xanh (`#64748B`) |
| **6. `Allocatable Free`** | `allocatable_bytes` | $\max\left(0,\ \text{Total\_VRAM} - (\text{os\_user} + \text{headroom} + \text{weights} + \text{kv\_state} + \text{compute})\right)$. | Xanh lá (`#10B981`) |

---

### B.3. Mở Rộng Hợp Đồng Protobuf (`contracts/protobuf/aicf/v1/fabric.proto`)

```protobuf
syntax = "proto3";
package aicf.v1;

enum ConfidenceLevel {
  CONFIDENCE_ESTIMATED = 0;   // Tính thuần từ công thức parser
  CONFIDENCE_CALIBRATED = 1;  // Đã nhân hệ số hiệu chỉnh từ ma trận benchmark
  CONFIDENCE_MEASURED = 2;    // Đã đo thực nghiệm trực tiếp trên cấu hình đích
}

message ModelIdentity {
  string model_id = 1;
  string weight_sha256 = 2;
  string tokenizer_sha256 = 3;
  string revision = 4;
  string quantization = 5;
  string source_repo = 6;       // VD: "Qwen/Qwen3-8B"
  string source_revision = 7;   // Commit SHA40 trên Hugging Face Hub
  string converter = 8;         // VD: "llama.cpp/convert_hf_to_gguf@b4820"
  string gguf_sha256 = 9;       // SHA256 của file .gguf thành phẩm
  string license_spdx = 10;     // VD: "Apache-2.0"
  bool trust_remote_code = 11;  // Bắt buộc == false trong production
}

message VramBuckets {
  uint64 total_bytes = 1;
  uint64 os_user_bytes = 2;
  uint64 weights_bytes = 3;
  uint64 kv_state_bytes = 4;
  uint64 compute_bytes = 5;
  uint64 headroom_bytes = 6;
  uint64 allocatable_bytes = 7;
}

message GpuLayerEstimate {
  string worker_id = 1;
  uint32 gpu_index = 2;
  uint32 layer_start = 3;
  uint32 layer_end = 4;
  VramBuckets projected_buckets = 5;
}

message VramEstimate {
  string deployment_plan_id = 1;
  repeated GpuLayerEstimate per_gpu = 2;
  uint64 system_ram_bytes = 3;
  ConfidenceLevel confidence = 4;
  float error_margin_pct = 5;
  string estimator_version = 6;
  string engine_revision = 7;
  string fit_status = 8;        // "GREEN" (<=90%), "YELLOW" (90-100% or RAM offload), "RED" (>100%)
}
```

---

## PHẦN C — KẾ HOẠCH THỰC HIỆN CHI TIẾT P0 → P8 (KÈM YÊU CẦU VIẾT TEST & CHECK PASS RÕ RÀNG)

> **Quy ước tiến độ:** 1 Sprint = 2 tuần.
> **Bảng đối chiếu Phase với [KT] §14:**
> `P0` ↔ `[KT] P0 (Audit & Baseline)` · `P1` ↔ `Nền tảng Monorepo/Contract/Estimator (Tách riêng để giảm tải cho P2)` · `P2` ↔ `[KT] P1 (Worker Fabric)` · `P3` ↔ `[KT] P2 (Distributed Qwen)` · `P4` ↔ `[KT] P3a (MoE & Speculative)` · `P5` ↔ `[KT] P3b (AI Agent Platform)` · `P6` ↔ `[KT] P4 (Audio & DiLoCo)` · `P7` ↔ `[KT] P5 (200 PC / WAN Federation)` · `P8` ↔ `[KT] P6 (Production Hardening)`.

---

### BƯỚC 0 (P0): Khảo Sát Thực Tế, Kiểm Toán Giấy Phép, Baseline `B01–B03` & Kiểm Chứng ADR-001 (3 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P0.1 - Infra/SRE] Khảo sát 10 câu hỏi Phụ lục C [KT] & Đo mạng LAN:**
  - Viết công cụ `benchmarks/hardware/probe_node.py` & `benchmarks/network/probe_lan_matrix.py` chạy trên tập máy mẫu (tối thiểu 10 PC văn phòng thật).
  - Xác nhận chính xác: GPU là RTX 3060 **12 GB hay 8 GB**, WDDM/TCC mode, phiên bản CUDA Driver, mã CPU Core i5 (kiểm tra cờ `avx2`, `avx512f`, `avx512_vnni`, `amx`), RAM 32 GB (1 hay 2 kênh, đo băng thông thực bằng `mbw`/`stream`), RTT p50/p95 giữa các PC cùng switch và khác switch, băng thông uplink khi 10–20 máy truyền đồng thời.
- **[P0.2 - Legal/Arch] Kiểm toán Giấy phép & Khóa Nguồn (`third_party/upstream-manifest.yaml`):**
  - Lập manifest khóa commit SHA40 và giấy phép SPDX cho: `llama.cpp` (MIT), `gguf-parser-go` (MIT), `gpustack` (Apache-2.0), `Prima.cpp`, `MoLink`, `vLLM` (Apache-2.0), `LangGraph` (MIT), `prime-diloco`, cùng toàn bộ thư viện Hugging Face (`transformers`, `tokenizers`, `safetensors`, `accelerate`, `peft`, `trl`, `datasets`, `evaluate`).
  - Kiểm tra riêng giấy phép thương mại của từng model (`Qwen3-8B`, `Qwen2.5-32B`, `Qwen3.5-35B-A3B`, `Qwen3-ASR`, `Qwen3-TTS`) và bộ dữ liệu âm thanh nội bộ.
- **[P0.3 - Go/System] Thực nghiệm Cổng ADR-001 (kiểm lại GPUStack + PoC `worker-guard` trên 5–10 PC Windows thật):**
  - Ngày 1–2: đọc tài liệu chính thức GPUStack xác nhận lại OS worker/backend/license (ghi vào ADR-001). Chỉ nếu hỗ trợ Windows native hoặc WSL2 đạt yêu cầu mới cài thử Server + Worker để so sánh; nếu không, bỏ qua và dùng `worker-guard` làm đường chính.
  - Viết PoC `apps/worker-guard` (Go): đọc NVML 1 Hz + Windows `GetLastInputInfo` (phát hiện chuột/phím) + theo dõi VRAM tăng đột biến để gọi API dừng/thu hồi model đang chạy.
  - Chạy giả lập 200 Worker ảo gửi heartbeat về Server để đo độ trễ lập lịch.
- **[P0.4 - ML/Perf] Đo Baseline `B01`, `B02`, `B03` & Thu thập Dữ liệu Hiệu chuẩn VRAM:**
  - **`B01` (1 PC, Qwen vừa VRAM):** Đo `Qwen3-8B` (GGUF `Q4_K_M`, `Q8_0` trên `llama.cpp` và Safetensors `AWQ`/`FP16` trên `vLLM`).
  - **`B02` (1 PC, Qwen 32B + CPU RAM Offload):** Đo `Qwen2.5-32B-Instruct-GGUF` (`Q4_K_M`) trên `llama.cpp` (28 layer GPU + 36 layer CPU RAM); chạy thêm HF `accelerate` offload để lấy tập logprobs chuẩn vàng (golden reference).
  - **`B03` (3–4 PC, vLLM TP/PP Baseline):** Đo lại cấu hình vLLM TP/PP trên mạng LAN 2 Gbps và 5 Gbps với đúng ma trận prompt `512 / 4096` và output `128 / 512` ([KT] §12.4).

#### 2. Sản phẩm bàn giao (Deliverables)
- `benchmarks/reports/p0_hardware_network_inventory.json` (Báo cáo trả lời đủ 10 mục Phụ lục C).
- `third_party/upstream-manifest.yaml` & `docs/adr/ADR-001-gpustack-adopt-extend.md` (chốt trạng thái `ACCEPTED` hoặc `PARTIAL_FALLBACK`).
- `benchmarks/reports/p0_baselines_B01_B02_B03.json` & `benchmarks/reports/p0_vram_raw_calibration.csv`.

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/unit/test_manifest_schema.py`:** Kiểm tra mọi mục trong `upstream-manifest.yaml` đều có đủ trường `repo_url`, `commit_sha` (đúng định dạng 40 ký tự hex), `spdx_license`, `integration_strategy` (`PORT | VENDOR | DEPEND | RESEARCH_ONLY`), và cấm các license nằm trong danh sách đen (ví dụ AGPL chưa được duyệt) đi vào nhánh `production`.
2. **`tests/integration/test_p0_hardware_probe.py`:** Kiểm tra script khảo sát đọc được chính xác dung lượng VRAM, CPU ISA flags, RAM bandwidth và xuất đúng JSON schema trên cả Windows và Linux.
3. **`tests/performance/test_p0_preemption_poc.py`:** Script tự động hóa trên PC Windows thật: nạp model Qwen 8B chiếm ~7 GB VRAM, kích hoạt sự kiện giả lập người dùng thao tác + cấp phát DirectX/CUDA buffer 2 GB, đo thời gian từ lúc kích hoạt đến khi `worker-guard` giải phóng VRAM của AI xuống $< 500\text{ MB}$ (lặp lại $N = 100$ lần).
4. **`tests/performance/test_p0_baseline_harness.py`:** Kiểm tra bộ chạy benchmark `B01–B03` thực hiện đúng tối thiểu `3 warm runs + 5 measured runs` ([KT] §12.4) và tính khoảng tin cậy 95% CI cho `TTFT`, `TPOT`, `VRAM_peak`.

#### 4. Tiêu chuẩn Check Pass Cổng P0 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra tính hợp lệ của Upstream Manifest & License Gate
pytest tests/unit/test_manifest_schema.py -v
# PASS KHI: exit code = 0, 100% dependency nhóm production có commit_sha 40-hex và license hợp lệ.

# 2. Kiểm tra báo cáo phần cứng, mạng & kết quả baseline B01-B03
pytest tests/integration/test_p0_hardware_probe.py tests/performance/test_p0_baseline_harness.py -v
# PASS KHI:
# - File p0_baselines_B01_B02_B03.json có đủ dữ liệu cho prompt (512, 4096) x output (128, 512) x concurrency (1, 8, 32).
# - Độ lệch chuẩn tương đối (RSD) của TPOT qua 5 lần đo lặp lại < 8%.

# 3. Kiểm tra cổng ADR-001 (Thu hồi GPU trên Windows & Ước lượng VRAM ban đầu)
pytest tests/performance/test_p0_preemption_poc.py --runs=100
# PASS KHI:
# - Thời gian thu hồi VRAM p95 < 2000 ms (2,0 giây) và 0 lần làm treo màn hình / crash driver WDDM.
# - Sai số ước lượng VRAM ban đầu của gguf-parser-go so với log llama.cpp <= 10% trên >= 10 cấu hình thử nghiệm.
```

---

### BƯỚC 1 (P1): Khởi Tạo Monorepo, Unified Contracts, HF Model Registry & Hiệu Chuẩn `core/vram-estimator` (2 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P1.1 - Arch/SRE] Dựng Khung Monorepo `ai-compute-fabric` & CI/CD Pipeline:**
  - Tạo đầy đủ cây thư mục theo **Phụ lục A [KT]** + `core/vram-estimator/` + `apps/worker-guard/`.
  - Cấu hình `Makefile` / `Taskfile`, Go workspace (`go.work`), Python workspace (`pyproject.toml` với `uv`), linter (`golangci-lint`, `ruff`, `mypy`) và GitHub Actions / GitLab CI.
- **[P1.2 - Go/Python] Biên dịch & Kiểm tra Hợp đồng Giao tiếp (`contracts/`):**
  - Viết `contracts/protobuf/aicf/v1/fabric.proto` (gồm `WorkerCapacity`, `ModelIdentity` mở rộng §B.5, `VramBuckets` 6 ngăn, `VramEstimate`, `WorkloadJob`, `LeaseToken`) và sinh code tự động cho Go + Python (`buf generate`).
  - Định nghĩa Python `InferenceEngine` Protocol và `TrainingEngine` Protocol (`inference/engine-manager/protocol.py`).
- **[P1.3 - ML/HF] Xây dựng `core/model-registry` dựa trên Hugging Face Stack:**
  - Tích hợp `huggingface_hub` (`snapshot_download`) để đồng bộ model về kho đệm nội bộ của Site Gateway (LAN Mirror).
  - Xác thực chuỗi provenance: kiểm tra chữ ký `weight_sha256` (`.safetensors`), `gguf_sha256` (`.gguf`), khóa cứng `trust_remote_code=False` trong `transformers.AutoConfig`.
  - Tích hợp thư viện Rust `tokenizers` để tính `tokenizer_sha256` và cung cấp hàm chuẩn hóa `render_chat_template(messages, tools)` dùng chung cho mọi backend.
- **[P1.4 - Go/ML] Xây dựng & Hiệu chuẩn `core/vram-estimator`:**
  - Viết dịch vụ Go nhúng `gpustack/gguf-parser-go` kết hợp bảng hệ số hiệu chỉnh (`calibration_table.json` thu được từ P0) cho `llama.cpp` / `Prima.cpp`, và sidecar Python cho `safetensors` / `vLLM` / `MoLink`.
  - Chạy ma trận hiệu chuẩn 72 cấu hình: `{Qwen3-4B, Qwen3-8B, Qwen2.5-32B, Qwen3.5-35B-A3B} × {Q4_K_M, Q8_0, FP16} × {ctx 2k, 8k, 32k} × {parallel 1, 4} × {1–4 GPU}`.

#### 2. Sản phẩm bàn giao (Deliverables)
- `contracts/protobuf/aicf/v1/fabric.proto` & `contracts/openapi/aicf_v1.yaml`.
- `core/model-registry/` (Service + CLI đăng ký/xác thực model từ Hugging Face Hub).
- `core/vram-estimator/` (Go library + HTTP/gRPC endpoint `/api/v1/models/estimate-vram` + `calibration_table.json`).

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/unit/test_protobuf_contracts.py` & `core/vram-estimator/estimator_test.go`:**
   - Kiểm tra serialize/deserialize hai chiều giữa Go và Python cho mọi message Protobuf.
   - Kiểm tra công thức tính 6 ngăn VRAM (`os_user`, `weights`, `kv_state`, `compute`, `headroom`, `allocatable`) không bao giờ trả về tràn số nguyên âm (`uint64 underflow`) khi `os_user + headroom > total_vram`.
2. **`tests/unit/test_hf_model_registry_security.py`:**
   - Tạo model giả có: (a) sai lệch 1 byte trong file `.safetensors`/`.gguf` (sai SHA256), (b) chứa file `.bin` pickle không được phép, (c) `config.json` cài `"auto_map"` đòi `trust_remote_code=True`, (d) `tokenizer.json` bị sửa vocab $\implies$ Registry phải từ chối 100% với mã lỗi tường minh.
3. **`tests/correctness/test_tokenizer_determinism.py`:**
   - Kiểm tra cùng một danh sách hội thoại + định nghĩa tool, `core/model-registry` sinh chuỗi prompt và danh sách `input_ids` khớp 100% giữa Hugging Face `AutoTokenizer` và bộ nạp GGUF của `llama.cpp`.
4. **`tests/integration/test_vram_estimator_calibration.py`:**
   - Chạy so sánh kết quả dự báo của `core/vram-estimator` với số đo thực tế (`llama.cpp` buffer logs + NVML peak) trên toàn bộ 72 cấu hình của ma trận hiệu chuẩn.

#### 4. Tiêu chuẩn Check Pass Cổng P1 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra Go & Python Unit Tests cho Contracts và VRAM Estimator
go test ./core/vram-estimator/... -v -race -coverprofile=coverage.out
pytest tests/unit/test_protobuf_contracts.py tests/unit/test_hf_model_registry_security.py -v
# PASS KHI: exit code = 0, độ phủ lệnh (statement coverage) của core/vram-estimator và core/model-registry >= 85%,
#           100% mẫu model giả mạo/chứa pickle/trust_remote_code bị chặn.

# 2. Kiểm tra đồng nhất Tokenizer giữa HF và GGUF
pytest tests/correctness/test_tokenizer_determinism.py -v
# PASS KHI: 100% mẫu test (tiếng Việt, tiếng Anh, code, tool-call JSON) có token IDs trùng khớp tuyệt đối (exact match).

# 3. Kiểm tra độ chính xác của VRAM Estimator trên ma trận hiệu chuẩn 72 cấu hình
pytest tests/integration/test_vram_estimator_calibration.py --matrix=benchmarks/configs/vram_matrix_72.yaml
# PASS KHI: >= 95% cấu hình (tối thiểu 69/72 cấu hình) có sai số tuyệt đối:
#           |VRAM_estimated - VRAM_measured_peak| <= max(5% * VRAM_measured_peak, 300 MB).
```

---

### BƯỚC 2 (P2): Xây Dựng Fleet 200 PC — `worker-guard`, Site Gateway, Adaptive Scheduler & Dashboard MVP (4 Sprint — tăng từ 3 vì worker tự xây hoàn toàn, xem D08)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P2.1 - Go/System] Phát triển `apps/worker-guard` (hoặc `worker-agent`) Bản Production cho Windows & Linux:**
  - Đóng gói Windows Service (`MSI installer` có chữ ký số Authenticode, hỗ trợ cài đặt tĩnh qua Active Directory GPO / Intune) và Linux `systemd` service.
  - Thực thi bộ đếm **VRAM 6 ngăn** (1 Hz), giới hạn mức tiêu thụ điện năng GPU (`nvidia-smi -pl` hoặc NVML Power Management API) để bảo vệ bộ nguồn (PSU) máy văn phòng, và thang thu hồi 3 cấp (`Drain/Pause` $\rightarrow$ `Checkpoint` $\rightarrow$ `Kill process`) khi phát hiện nhân viên sử dụng máy.
  - Cơ chế tự cập nhật an toàn theo vòng (**Canary 5% $\rightarrow$ 25% $\rightarrow$ 100%**) kèm watchdog tự động rollback về phiên bản trước nếu service crash quá 3 lần trong 5 phút.
- **[P2.2 - Go/Infra] Phát triển `apps/site-gateway` (gRPC + mTLS + NATS JetStream + PostgreSQL):**
  - Quản lý danh bạ 200 Worker (`RegisterWorker`, `Heartbeat`), cấp phát chứng chỉ mTLS tự động xoay vòng, quản lý `LeaseService` (`RequestLease`, `ExtendLease`, `ReleaseLease` với TTL nghiêm ngặt) và hàng đợi tác vụ bền vững trên **NATS JetStream**.
  - Tích hợp `core/topology-graph`: định kỳ đo độ trễ RTT và băng thông khả dụng giữa các nhóm máy trong LAN.
- **[P2.3 - Python/Go] Phát triển `core/adaptive-scheduler`:**
  - Cài đặt thuật toán lọc ứng viên theo `VramEstimate` (chỉ chọn các máy có `allocatable_bytes >= required_vram`), kiểm tra `ModelIdentity` (`weight_sha256`, `tokenizer_sha256`), chính sách quyền dữ liệu của tenant, và chấm điểm theo hàm chi phí:
    $$T_{\text{expected}} = T_{\text{queue}} + T_{\text{cold\_load}} + T_{\text{prefill}} + T_{\text{decode}} + T_{\text{transfer}} + T_{\text{sync}} + T_{\text{failure\_penalty}}$$
- **[P2.4 - Frontend] Xây dựng `apps/dashboard` MVP (React + TypeScript + Vite + Ant Design + ECharts):**
  - Hoàn thiện **Màn hình 1 (Toàn cảnh cụm & Lưới Heatmap 200 GPU render bằng Canvas 1 Hz qua SSE)** và **Màn hình 3 (Bảng quản lý Worker, Thanh VRAM 6 ngăn, Cấu hình Quota theo phòng ban/khung giờ & Nhật ký Thu hồi GPU — Preemption Log)**.
  - Tích hợp phân quyền RBAC (`Admin`, `Operator`, `Viewer`), Audit Log và đa ngôn ngữ (Tiếng Việt / Tiếng Anh).

#### 2. Sản phẩm bàn giao (Deliverables)
- `apps/worker-guard/` (Binary + Bộ cài Windows `.msi` & Linux `.deb/.rpm`).
- `apps/site-gateway/` & `core/adaptive-scheduler/`.
- `apps/dashboard/` (Màn hình 1, Màn hình 3, RBAC, SSE real-time stream).

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/unit/test_adaptive_scheduler_decisions.py`:**
   - Kiểm tra tối thiểu 15 kịch bản ra quyết định của Scheduler: (a) chọn backend có `TPOT` thấp hơn trên cùng model, (b) từ chối worker lệch `tokenizer_sha256` hoặc `revision`, (c) từ chối worker không thuộc vùng dữ liệu của tenant, (d) từ chối worker thiếu `allocatable_bytes` dù tổng VRAM lý thuyết là 12 GB, (e) từ chối phương án vượt ngưỡng SLA latency, (f) ưu tiên PC đã nạp sẵn model trong VRAM (`T_cold_load = 0`).
2. **`tests/integration/test_lease_lifecycle_and_idempotency.py`:**
   - Kiểm tra quy trình `RequestLease -> Heartbeat Extend -> Expire TTL / ReleaseLease`: khi Worker ngừng gửi heartbeat quá `TTL = 10s`, Site Gateway tự động thu hồi lease, giải phóng chỉ số VRAM đặt chỗ trong DB và đẩy lại task vào NATS JetStream mà không bị nhân đôi kết quả (idempotency check).
3. **`tests/resilience/test_preemption_and_worker_churn.py`:**
   - Trên cụm 20–40 PC (hoặc môi trường giả lập phần cứng), kích hoạt ngẫu nhiên sự kiện người dùng mở ứng dụng chiếm GPU và rút cáp mạng 5 máy đang chạy inference $\implies$ kiểm tra hệ thống thu hồi GPU đúng hạn và tự động chuyển hướng request sang replica khác.
4. **`tests/e2e/test_dashboard_200_nodes.spec.ts` (Playwright):**
   - Giả lập luồng SSE của 200–1.000 Worker cập nhật trạng thái mỗi giây; kiểm tra Màn hình 1 (Heatmap 200 GPU) và Màn hình 3 hiển thị đúng 6 ngăn VRAM, thao tác chỉnh Quota phòng ban lưu thành công vào DB, tốc độ khung hình UI không tụt xuống dưới 30 FPS và không rò rỉ bộ nhớ trình duyệt.

#### 4. Tiêu chuẩn Check Pass Cổng P2 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra toàn bộ Unit & Integration Tests của Scheduler, Lease và Site Gateway
pytest tests/unit/test_adaptive_scheduler_decisions.py tests/integration/test_lease_lifecycle_and_idempotency.py -v
go test ./apps/site-gateway/... ./apps/worker-guard/... -v -race
# PASS KHI: 100% test cases pass (exit code = 0), không có race condition.

# 2. Kiểm tra E2E Dashboard với tải giả lập 200 - 1.000 Worker
npx playwright test tests/e2e/test_dashboard_200_nodes.spec.ts
# PASS KHI: Tất cả bài test UI + RBAC pass; thời gian render cập nhật lưới 200 GPU < 50 ms/frame.

# 3. Kiểm tra độ bền vận hành cụm thực tế (Soak & Preemption Test trên 5 -> 20 -> 40 PC)
pytest tests/resilience/test_preemption_and_worker_churn.py --nodes=40 --duration-hours=72
# PASS KHI:
# - Cụm 40 PC vận hành liên tục 72 giờ dưới tải hỗn hợp: 0 lần crash OS, 0 rò rỉ bộ nhớ (memory leak < 2%/24h).
# - Thời gian thu hồi GPU khi nhân viên dùng máy đạt p95 < 2,0 giây, p99 < 3,0 giây.
# - 0 trường hợp vượt hạn mức Quota CPU/VRAM đã cấu hình.
```

---

### BƯỚC 3 (P3): Qwen Inference Core — Local, Hybrid & Đối Chứng Phân Tán `Prima.cpp` vs `MoLink` (3 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P3.1 - Python/C++] Hoàn thiện Adapter Local & Hybrid (`inference/adapters/llama`, `inference/adapters/vllm`):**
  - Chuẩn hóa adapter thực thi đúng 7 hàm của `InferenceEngine` (`probe`, `plan`, `prepare`, `start`, `infer`, `metrics`, `stop`).
  - Tự động ánh xạ kết quả từ `core/vram-estimator` thành tham số dòng lệnh chính xác (`--gpu-layers`, `--ctx-size`, `--parallel`, `--flash-attn` cho `llama.cpp`; `--gpu-memory-utilization` động theo `allocatable_bytes` cho `vLLM`).
- **[P3.2 - C++/CUDA] Đóng gói Cô lập `Prima.cpp` & `MoLink` cho Nhóm 3–4 RTX 3060:**
  - **`inference/adapters/prima`:** Đóng gói container/binary cô lập của `OpenCPIL/prima.cpp` (piped-ring parallelism với prefetching). Thiết lập đường hầm mTLS giữa các Worker trong nhóm 3–4 PC (cấm mở cổng RPC không xác thực).
  - **`inference/adapters/molink`:** Đóng gói container cô lập của `oldcpple/MoLink` trên nền `vLLM` (gRPC activation transport giữa các stage pipeline).
- **[P3.3 - ML/Benchmark] Thực thi Đối chứng `B04` (Prima.cpp) vs `B05` (MoLink) vs `B03` (vLLM PP/llama-box) & Kiểm định Toán học:**
  - Chạy benchmark đối chứng trên mô hình **Qwen 32B Dense** (`Qwen2.5-32B-Instruct` / `Qwen3-32B`) trên 3–4 PC RTX 3060 cùng switch LAN 2 Gbps và 5 Gbps.
  - Đo kiểm định toán học (Model Correctness) bằng `llama-perplexity --kl-divergence` và Hugging Face `lighteval` / `lm-evaluation-harness` so với chuẩn vàng HF `transformers` (`accelerate`).
- **[P3.4 - Frontend] Hoàn thiện Màn hình 2 (Live VRAM Fit & Layer-Split Visualizer) và Màn hình 5a (Chat Playground):**
  - Hiển thị trực quan sơ đồ chia layer trên 1–4 card RTX 3060 kèm nhãn độ tin cậy (`CALIBRATED` / `MEASURED`) và giao diện Chat thử nghiệm trực tiếp.

#### 2. Sản phẩm bàn giao (Deliverables)
- `inference/adapters/llama/`, `inference/adapters/vllm/`, `inference/adapters/prima/`, `inference/adapters/molink/`.
- `benchmarks/reports/p3_distributed_comparison_B03_B04_B05.json` & `docs/adr/ADR-004-distributed-engine-winner.md` (chốt duy nhất 1 backend phân tán thắng cuộc để đưa vào nhánh production).
- Màn hình 2 & Màn hình 5a trên `apps/dashboard`.

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/integration/test_engine_adapter_contract.py`:**
   - Bộ test tham số hóa (parametrized test) chạy cùng một kịch bản vòng đời (`probe -> plan -> prepare -> start -> infer stream 128 tokens -> metrics -> stop`) trên cả 4 adapter (`llama`, `vllm`, `prima`, `molink`). Kiểm tra sau khi `stop()`, VRAM của tất cả các Worker trong nhóm được giải phóng hoàn toàn về mức trước khi `start()`.
2. **`tests/correctness/test_distributed_qwen_correctness.py`:**
   - Kiểm tra trên tập 200 prompt chuẩn (Tiếng Việt, Toán GSM8K, Code HumanEval, Trích xuất JSON):
     - So sánh đầu ra giữa chạy **1 máy (`LOCAL_HYBRID`)** và chạy **phân tán 3–4 máy (`DISTRIBUTED_MODEL` trên `Prima.cpp` / `MoLink`)** với cùng một file trọng số và `temperature = 0`: yêu cầu độ lệch KL-divergence của phân phối logprobs top-50 nằm trong ngưỡng cho phép và không bị suy giảm điểm perplexity.
3. **`tests/performance/test_b04_b05_distributed_qwen.py`:**
   - Tự động chạy ma trận bài đo **`B04`** và **`B05`** trên 3–4 PC RTX 3060 qua mạng 2 Gbps và 5 Gbps (`3 warm + 5 measured runs`), xuất bảng so sánh `TTFT`, `TPOT`, `p95 inter-token latency`, `communication_share`.
4. **`tests/resilience/test_distributed_session_failover.py`:**
   - Đang trong lúc sinh token thứ 50 của Qwen 32B trên nhóm 3 PC, ngắt kết nối PC số 2 $\implies$ kiểm tra Engine Manager phát hiện lỗi trong $< 3\text{s}$, hủy lease nhóm hỏng, trả mã lỗi chuẩn hóa hoặc tự động khởi động lại phiên trên nhóm replica dự phòng mà không làm treo các tiến trình trên PC 1 và PC 3.

#### 4. Tiêu chuẩn Check Pass Cổng P3 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra Hợp đồng Vòng đời Adapter & Giải phóng VRAM sạch sẽ
pytest tests/integration/test_engine_adapter_contract.py -v
# PASS KHI: Cả 4 adapter thực thi đầy đủ lifecycle, VRAM rò rỉ sau khi gọi stop() < 50 MB.

# 2. Kiểm tra Tính Đúng Đắn Toán Học (Model Correctness Gate)
pytest tests/correctness/test_distributed_qwen_correctness.py -v
# PASS KHI:
# - Với cùng file GGUF (Single-node Hybrid vs 3-node Prima.cpp): KLD trung bình < 0.001, Perplexity lệch < 0.5%.
# - Với Safetensors FP16 vs GGUF Q4_K_M: Perplexity và điểm benchmark (MMLU/GSM8K subset) lệch < 2.0%.

# 3. Kiểm tra Hiệu năng Phân tán B04/B05 (Theo đúng tiêu chí [KT] §12.5)
pytest tests/performance/test_b04_b05_distributed_qwen.py --report=benchmarks/reports/p3_b04_b05.json
# PASS KHI (Đạt ít nhất 1 trong 2 điều kiện của [KT] §12.5 trên nhóm 3-4 RTX 3060):
# - Điều kiện A: Giảm TPOT >= 30% so với baseline vLLM PP cũ (B03) ở khoảng tin cậy 95% CI; HOẶC
# - Điều kiện B: Chạy thành công model Qwen 32B ở độ dài ngữ cảnh không thể nạp vừa 1 máy đơn và nhanh hơn LOCAL_HYBRID CPU offload (B02).
```

---

### BƯỚC 4 (P4): Tối Ưu Hóa Qwen MoE (`Qwen3.5-35B-A3B` / `Qwen3-30B-A3B`) & Speculative Decoding (2–4 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P4.1 - ML/C++] Triển khai Cấp 1 — Native MoE Local/Hybrid kèm Cổng Kiểm tra CPU ISA (`inference/moe/`):**
  - Cài đặt **CPU ISA Gate** trong `hardware-profiler`: tự động phát hiện mức hỗ trợ tập lệnh của CPU Core i5 (`AVX2` vs `AVX-512` / `AMX`).
  - Tích hợp backend `llama.cpp` MoE expert offload (`--override-tensor` giữ Attention/Shared layers + Hot Experts trên 12 GB VRAM của RTX 3060, đặt các Routed Experts ít dùng trên 32 GB RAM) và đối chứng với `KTransformers` (`KT-Kernel`) trên những máy đủ chuẩn ISA.
  - Bổ sung bộ quản lý **Expert Locality & Caching** (học từ `MoE-Infinity` trong `inference/moe/expert-cache/`) và cập nhật `core/vram-estimator` để hiển thị tách biệt thanh **VRAM (Shared + Hot Experts)** và **RAM (Cold Routed Experts)** trên Dashboard.
- **[P4.2 - ML/Agent] Triển khai Cấp 3 — Agent-Level Experts & Đánh giá Cấp 2 (Distributed MoE):**
  - Cài đặt bộ định tuyến cấp tác vụ (Task Router) chọn giữa các bản sao Qwen nhỏ chuyên biệt, gắn cờ rõ trong metadata response (`execution_equivalence = "AGENT_ENSEMBLE_NON_IDENTICAL"`).
- **[P4.3 - ML/Runtime] Triển khai Speculative Decoding (`inference/speculative/` — Bài đo `B07`):**
  - Khai thác chế độ **Native Speculative Decoding / Assisted Generation** của `llama.cpp` / `vLLM` / HF `transformers` (ví dụ: dùng `Qwen3-0.6B` làm draft model và `Qwen3-8B` / `32B` làm target model; kiểm tra bắt buộc hai model có cùng `tokenizer_sha256`).
  - Đối với kiến trúc lai `Qwen3.5` (Gated DeltaNet + Attention), viết bộ kiểm tra riêng cho cơ chế **rollback trạng thái đệ quy (`Recurrent / DeltaNet state rollback`)** khi draft token bị từ chối ([KT] §4.4).

#### 2. Sản phẩm bàn giao (Deliverables)
- `inference/moe/` (`isa_gate.py`, `expert_cache.py`, MoE placement logic trong `core/vram-estimator`).
- `inference/speculative/` (Draft-Target controller & DeltaNet state rollback verifier).
- `benchmarks/reports/p4_moe_B06_speculative_B07.json`.

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/unit/test_cpu_isa_gate.py`:**
   - Giả lập thông tin CPU flags (`avx2` only vs `avx512f + avx512_vnni` vs `amx_int8`): kiểm tra Scheduler tự động chặn `KT-Kernel` AVX-512 trên CPU Core i5 chỉ có AVX2 và chuyển mượt về `llama.cpp` AVX2 MoE offload mà không gây lỗi `SIGILL (Illegal Instruction)`.
2. **`tests/correctness/test_moe_expert_weighting_and_deltanet_rollback.py`:**
   - **Kiểm tra MoE:** So sánh trọng số chọn expert (`router_logits` top-$k$ experts và tổng có trọng số) của `Qwen3-30B-A3B` / `Qwen3.5-35B-A3B` giữa chế độ Hybrid CPU–GPU và chuẩn vàng HF `transformers` $\implies$ đảm bảo phép cộng kết quả expert không bị thay đổi.
   - **Kiểm tra Speculative Rollback trên Qwen3.5 Hybrid:** Chạy 100 chuỗi sinh văn bản với `temperature = 0` khi bật và tắt Speculative Decoding trên mô hình có lớp DeltaNet $\implies$ chuỗi token đầu ra phải **giống hệt nhau từng token (100% bit-exact / token-exact match)**, chứng minh khi draft token bị bác bỏ thì trạng thái DeltaNet được quay lui (rollback) chính xác.
3. **`tests/performance/test_b06_moe_and_b07_speculative.py`:**
   - Thực thi bài đo **`B06`** (MoE local/hybrid) và **`B07`** (Speculative decoding) theo chuẩn [KT] §12.4.

#### 4. Tiêu chuẩn Check Pass Cổng P4 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra CPU ISA Gate & Tính đúng đắn của MoE Router + Speculative Rollback
pytest tests/unit/test_cpu_isa_gate.py tests/correctness/test_moe_expert_weighting_and_deltanet_rollback.py -v
# PASS KHI:
# - 0 lỗi SIGILL trên CPU không có AVX-512.
# - Chuỗi token đầu ra khi bật Speculative Decoding (greedy temp=0) khớp 100% với khi tắt Speculative Decoding.

# 2. Kiểm tra Hiệu năng B06 (MoE) và B07 (Speculative)
pytest tests/performance/test_b06_moe_and_b07_speculative.py --report=benchmarks/reports/p4_b06_b07.json
# PASS KHI:
# - B06: Qwen MoE (30B-A3B / 35B-A3B) chạy trên 1 PC (12GB VRAM + 32GB RAM) đạt TPOT thấp hơn >= 40% so với Qwen 32B Dense offload (B02).
# - B07: Speculative Decoding chỉ được bật mặc định cho các workload có tỷ lệ chấp nhận token (acceptance_rate) >= 0.65 và speedup thực tế >= 1.25x.
```

---

### BƯỚC 5 (P5): Nền Tảng Distributed AI Agent (`LangGraph`), RAG & 15 Typed Tasks (3–4 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P5.1 - Agent/Python] Xây dựng `agents/orchestrator` & `agents/task-planner` với `LangGraph`:**
  - Triển khai đồ thị công việc (stateful DAG) trên `LangGraph` với lưu trữ checkpoint bền vững vào PostgreSQL (cho phép tạm dừng và khôi phục bước công việc trên máy khác nếu PC hiện tại bị nhân viên lấy lại GPU).
  - Cài đặt đủ **15 loại tác vụ có kiểu (Typed Tasks)** theo [KT] §6.2: `TEXT_GENERATE`, `OCR`, `DOCUMENT_PARSE`, `EMBEDDING`, `RERANK`, `RAG_RETRIEVE`, `AUDIO_VAD`, `AUDIO_ASR`, `AUDIO_TTS`, `AUDIO_ENHANCE`, `STRUCTURED_EXTRACTION`, `TOOL_EXECUTE`, `RESULT_VALIDATE`, `TRAIN_STEP`, `MODEL_EVALUATE`.
- **[P5.2 - Sec/Agent] Xây dựng `agents/tool-registry`, Constrained Decoding & Sandbox Thực thi:**
  - Mỗi công cụ trong `tool-registry` phải có JSON Schema đầu vào/đầu ra chặt chẽ, chữ ký số của quản trị viên và phạm vi quyền (`tenant_scope`).
  - Áp dụng **Constrained Decoding / Grammar** (`llama.cpp` JSON Schema grammar / `vLLM` guided decoding) cho tác vụ `STRUCTURED_EXTRACTION` và gọi công cụ.
  - **Cấm tuyệt đối công cụ thực thi lệnh shell tùy ý (`arbitrary shell`)**; mọi tool chạy trong tiến trình sandbox bị cô lập filesystem và mạng.
- **[P5.3 - ML/RAG] Xây dựng `agents/knowledge` với Hugging Face Embedding/Rerank + `pgvector`:**
  - Triển khai Worker chạy `sentence-transformers` hoặc Hugging Face `text-embeddings-inference` (TEI) với model `BAAI/bge-m3` / `Qwen3-Embedding` và `bge-reranker-v2-m3` kết hợp cơ sở dữ liệu `PostgreSQL + pgvector`.
  - Xây dựng `agents/result-validator` kiểm tra schema JSON, đối chiếu trích dẫn nguồn (citation check) và phát hiện ảo giác.
- **[P5.4 - Frontend] Bổ sung Màn hình 4a (Giám sát Đồ thị DAG của AI Agent) vào `apps/dashboard`.**

#### 2. Sản phẩm bàn giao (Deliverables)
- `agents/` (`orchestrator/`, `task-planner/`, `tool-registry/`, `task-executor/`, `knowledge/`, `result-validator/`).
- `benchmarks/reports/p5_agent_scale_B08.json` (Báo cáo chạy song song trên 8–40 PC).
- Màn hình 4a trên `apps/dashboard`.

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/unit/test_15_typed_task_schemas.py`:**
   - Kiểm tra cả 15 loại task đều có Pydantic/JSON Schema hợp lệ, kiểm tra `ResultValidator` từ chối mọi kết quả thiếu trích dẫn nguồn hoặc sai kiểu dữ liệu.
2. **`tests/security/test_agent_tool_sandbox_and_injection.py`:**
   - Bộ 50 kịch bản tấn công Prompt Injection (ví dụ: tài liệu OCR/âm thanh chứa chỉ dẫn ẩn yêu cầu LLM gọi `os.system`, đọc `/etc/passwd`, đọc khóa mTLS, gửi dữ liệu sang IP lạ, hoặc gọi tool không nằm trong allowlist của tenant) $\implies$ Hệ thống phải chặn 100% ở cả tầng `ToolRegistry` lẫn tầng `Sandbox`.
3. **`tests/resilience/test_langgraph_checkpoint_resume.py`:**
   - Chạy một DAG gồm 50 bước xử lý tài liệu; ở bước thứ 25, giả lập 3 PC Worker bị thu hồi GPU $\implies$ kiểm tra `LangGraph` + `NATS JetStream` khôi phục từ checkpoint và chỉ chạy lại đúng các task chưa hoàn tất trên Worker mới (không chạy lại 24 bước đã xong).
4. **`tests/performance/test_b08_agent_cluster_throughput.py`:**
   - Thực thi bài đo **`B08`** ([KT] §12.4 & §12.5): chạy kịch bản phân tích lô lớn (1.000 tài liệu hoặc 1.000 đoạn hội thoại) phân tán trên **8, 20 và 40 PC RTX 3060**, đo tổng thông lượng cụm so với tổng thông lượng các máy chạy độc lập.

#### 4. Tiêu chuẩn Check Pass Cổng P5 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra Schema 15 loại Task & Bảo mật Sandbox chống Prompt Injection / Arbitrary Shell
pytest tests/unit/test_15_typed_task_schemas.py tests/security/test_agent_tool_sandbox_and_injection.py -v
# PASS KHI: 100% kịch bản gọi tool trái phép, truy cập file ngoài sandbox hoặc thực thi shell bị chặn (0% bypass).

# 2. Kiểm tra Khôi phục Checkpoint DAG khi mất Worker
pytest tests/resilience/test_langgraph_checkpoint_resume.py -v
# PASS KHI: Workflow hoàn thành 100% kết quả đúng, số task phải thực thi lại <= số task đang in-flight trên các máy bị ngắt.

# 3. Kiểm tra Hiệu suất Phân tán Tác vụ B08 ([KT] §12.5)
pytest tests/performance/test_b08_agent_cluster_throughput.py --nodes=8,20,40 --report=benchmarks/reports/p5_b08.json
# PASS KHI:
# - Hiệu suất thông lượng cụm (throughput_efficiency = measured_total_tps / sum(isolated_node_tps)) >= 0.80 (80%).
# - Tỷ lệ hoàn thành đúng schema sau qua ResultValidator == 100%.
```

---

### BƯỚC 6 (P6): Audio AI Pipeline (`Qwen3-ASR/TTS`) & Distributed Training (`PEFT / DDP / DiLoCo`) (3–4 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P6.1 - Audio/HF] Xây dựng Pipeline Dữ liệu Âm thanh & Chuẩn hóa Tiếng Việt (`audio/ingestion`, `audio/evaluation`):**
  - Sử dụng Hugging Face `datasets` (streaming Apache Arrow/Parquet) để nạp và tiền xử lý lô 10.000+ bản ghi âm (`AUDIO_VAD`, resample 16 kHz, kiểm tra `consent_ref` và `checksum`).
  - Cài đặt thuật toán chia tập **`speaker-disjoint split`** (người nói trong tập `test`/`validation` tuyệt đối không xuất hiện trong tập `train`).
  - Xây dựng bộ chuẩn hóa văn bản tiếng Việt (`VietnameseTextNormalizer`: chuẩn hóa bảng mã Unicode dựng sẵn NFC, quy tắc bỏ dấu kiểu cũ/mới `òa/oà`, viết chữ số/ngày tháng/đơn vị tiền tệ) trước khi đưa vào `evaluate` (`jiwer`) tính **WER / CER**.
- **[P6.2 - Audio/Runtime] Tích hợp `Qwen3-ASR`, `Qwen3-TTS` & `DeepFilterNet` (`audio/asr`, `audio/tts`, `audio/enhancement`):**
  - Đóng gói inference và fine-tuning adapter cho `QwenLM/Qwen3-ASR` và `QwenLM/Qwen3-TTS` (0.6B/1.7B), tích hợp vào Màn hình 5b (Audio Playground).
- **[P6.3 - Training/HF] Xây dựng `training/` với HF `peft`, `accelerate`, `trl`, `bitsandbytes` & `prime-diloco`:**
  - Cập nhật `core/vram-estimator` để tính bộ nhớ huấn luyện (`Weights + LoRA Adapters + Gradients + 8-bit AdamW States + Activations`) đảm bảo vừa khít 12 GB VRAM của RTX 3060.
  - Hỗ trợ 3 chế độ huấn luyện ([KT] §7.3):
    1. **LoRA/QLoRA 1 GPU** (trên 1 RTX 3060 12 GB).
    2. **DDP 2–4 GPU** (qua `huggingface/accelerate` trên cùng switch LAN).
    3. **Periodic Local SGD / DiLoCo** (nhiều Worker huấn luyện $H$ bước cục bộ rồi đồng bộ vòng ngoài).
  - **Giải quyết triệt để bài toán gộp LoRA ([KT] §7.3):**
    - *Phương án mặc định chính xác toán học (**LoRA-FA / Frozen-A**):* Khởi tạo và đóng băng ma trận $A$ giống nhau trên mọi Worker ($\nabla_A = 0$), chỉ huấn luyện và đồng bộ ma trận $B_k$. Khi đó trung bình cập nhật trọng số thỏa mãn chính xác tuyệt đối:
      $$\frac{1}{K} \sum_{k=1}^{K} \Delta W_k = \frac{1}{K} \sum_{k=1}^{K} B_k A = \left(\frac{1}{K} \sum_{k=1}^{K} B_k\right) A = \bar{B} A$$
    - *Phương án khi cập nhật cả $A$ và $B$ (**Exact Delta-W Merge / SVD Re-factorization**):* Tính tổng trực tiếp $\Delta W_{\text{avg}} = \frac{1}{K}\sum_{k=1}^{K} \frac{\alpha}{r} B_k A_k$ để cộng vào trọng số nền (hoặc phân rã SVD kèm ghi nhận sai số cắt hạng Frobenius), **cấm tuyệt đối phép lấy trung bình ngây thơ $\bar{B}\cdot\bar{A}$**.

#### 2. Sản phẩm bàn giao (Deliverables)
- `audio/` (`ingestion/`, `vad/`, `asr/`, `tts/`, `enhancement/`, `evaluation/vn_normalizer.py`).
- `training/` (`peft/`, `ddp/`, `diloco/`, `lora_merger.py`, `checkpoint/`).
- `benchmarks/reports/p6_audio_training_B09.json` & Màn hình 4b + 5b trên `apps/dashboard`.

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/unit/test_speaker_disjoint_and_vn_wer.py`:**
   - Kiểm tra hàm chia dataset: giao của tập `set(train_speakers) & set(test_speakers)` phải bằng $\emptyset$ (rỗng 100%).
   - Kiểm tra bộ chuẩn hóa tiếng Việt: các cặp câu đồng nghĩa về âm đọc/chính tả (`"hòa bình"` vs `"hoà bình"`, `"25/10/2026"` vs `"hai mươi lăm tháng mười năm hai nghìn không trăm hai mươi sáu"`) được chuẩn hóa nhất quán trước khi tính WER/CER.
2. **`tests/correctness/test_lora_aggregation_math.py`:**
   - Sinh ngẫu nhiên $K = 4$ cặp ma trận LoRA $(A_k, B_k)$ từ 4 Worker:
     - Chứng minh bằng assertion rằng $\left\| \left(\frac{1}{K}\sum B_k\right)\left(\frac{1}{K}\sum A_k\right) - \frac{1}{K}\sum B_k A_k \right\|_F > 0$ (hàm gộp ngây thơ bị chặn).
     - Kiểm tra chế độ **LoRA-FA** đạt sai số $\left\| \bar{B} A - \frac{1}{K}\sum B_k A \right\|_F < 10^{-6}$ (chính xác tuyệt đối đến giới hạn dấu phẩy động FP32).
3. **`tests/resilience/test_training_stale_worker_and_checkpoint.py`:**
   - Trong phiên huấn luyện DiLoCo 4 Worker, giả lập Worker số 4 bị trễ mạng và gửi bản cập nhật của vòng `outer_step = t - 2` khi cụm đã ở vòng `t` $\implies$ Coordinator phải từ chối bản cập nhật cũ (`stale update rejection`), và kiểm tra khả năng khôi phục phiên huấn luyện từ file checkpoint `.safetensors` khi 1 Worker bị thu hồi GPU.
4. **`tests/performance/test_b09_audio_training_convergence.py`:**
   - Thực thi bài đo **`B09`** ([KT] §12.4 & §12.5) trên 4–8 PC RTX 3060: so sánh `LoRA 1 GPU`, `DDP 4 GPU`, và `DiLoCo 4–8 GPU` về `audio_hours/hour`, `% communication time`, `bytes synchronized`, `GPU kWh` và `time-to-target-WER`.

#### 4. Tiêu chuẩn Check Pass Cổng P6 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra Toán học Gộp LoRA, Chia tập Speaker-Disjoint & Chuẩn hóa WER Tiếng Việt
pytest tests/unit/test_speaker_disjoint_and_vn_wer.py tests/correctness/test_lora_aggregation_math.py -v
# PASS KHI:
# - Speaker overlap giữa train và validation/test == 0%.
# - Sai số Frobenius của phép gộp LoRA-FA / Exact Merge < 1e-6.

# 2. Kiểm tra Chống Stale Update & Khôi phục Checkpoint .safetensors
pytest tests/resilience/test_training_stale_worker_and_checkpoint.py -v
# PASS KHI: 100% stale updates bị chặn; checkpoint resume khớp chính xác trạng thái optimizer và step count.

# 3. Kiểm tra Hiệu năng & Hội tụ Huấn luyện B09 ([KT] §12.5)
pytest tests/performance/test_b09_audio_training_convergence.py --report=benchmarks/reports/p6_b09.json
# PASS KHI:
# - Model sau fine-tune đạt WER/CER thấp hơn model gốc trên tập test speaker-disjoint.
# - Time-to-target-WER của chế độ phân tán không xấu hơn baseline ngoài biên cho phép,
#   đồng thời DiLoCo giảm >= 70% dung lượng byte truyền qua mạng so với DDP thuần.
```

---

### BƯỚC 7 (P7): Liên Kết Đa Doanh Nghiệp Qua WAN Federation, Đa Tenant & Metering (3–5 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P7.1 - Go/Federation] Xây dựng `apps/global-control-plane` & `federation/`:**
  - Triển khai 4 API liên doanh nghiệp ([KT] §11.1): `AdvertiseCapacity` (chỉ báo cáo năng lực tổng hợp của Compute Island, không lộ danh sách IP từng PC nội bộ), `OfferJob`, `AcceptJob`, `ReportUsage`.
  - Bắt buộc xác thực **Mutual TLS (mTLS)** giữa các Site Gateway và Global Control Plane, kèm kiểm tra chính sách phân loại dữ liệu (`DATA_PUBLIC`, `DATA_INTERNAL`, `DATA_RESTRICTED`).
- **[P7.2 - Core/Metering] Xây dựng Hệ thống Đối soát Tài nguyên (`core/resource-metering`) & Màn hình 5c (Metering UI):**
  - Ghi nhận bất biến (append-only audit log) số lượng `prompt_tokens`, `completion_tokens`, `gpu_seconds`, `vram_gb_hours`, `audio_minutes` và `estimated_kwh` theo từng `tenant_id`, `site_id` và `job_id`.
- **[P7.3 - Security] Kiểm thử Xâm nhập Liên Site (Cross-Site Security & Network Hardening):**
  - Đảm bảo không có bất kỳ cổng GGML RPC, Ray, NATS nội bộ hay PostgreSQL nào được mở trực tiếp ra WAN; mọi lưu lượng WAN chỉ đi qua cổng gRPC mTLS của `Site Gateway`.

#### 2. Sản phẩm bàn giao (Deliverables)
- `apps/global-control-plane/` & `federation/` (`site-discovery`, `policy`, `routing`, `secure-transport`).
- `core/resource-metering/` & Màn hình 5c (Multi-Tenant & WAN Metering Dashboard).

#### 3. Yêu cầu Viết Test Bắt Buộc (Mandatory Tests to Write)
1. **`tests/security/test_wan_tenant_isolation_and_data_residency.py`:**
   - Kiểm tra 20 kịch bản rò rỉ dữ liệu liên doanh nghiệp: gửi request hoặc tác vụ Audio/Agent gắn nhãn `DATA_RESTRICTED` (hoặc `site_allowlist = ["SITE_A"]`) khi `SITE_A` hết GPU và `SITE_B` đang rảnh 50 GPU $\implies$ Global Scheduler **bắt buộc phải xếp hàng đợi tại `SITE_A`**, tuyệt đối không được chuyển dữ liệu sang `SITE_B`.
2. **`tests/security/test_wan_mtls_and_port_exposure.py`:**
   - Kiểm tra kết nối tới `Site Gateway` bằng chứng chỉ hết hạn, chứng chỉ tự ký không thuộc CA liên minh, hoặc chứng chỉ đã bị thu hồi (CRL) $\implies$ từ chối 100% ở tầng TLS handshake.
   - Quét cổng tự động xác nhận các cổng RPC nội bộ (`llama-server`, `Prima.cpp`, `MoLink`, `NATS`) chỉ lắng nghe trên giao diện LAN/loopback được phép.
3. **`tests/resilience/test_wan_partition_and_autonomous_site.py`:**
   - Dùng `tc netem` cắt đứt hoàn toàn kết nối WAN giữa `Site A`, `Site B` và `Global Control Plane` trong 30 phút $\implies$ kiểm tra `Site A` và `Site B` vẫn tiếp tục phục vụ bình thường 100% các yêu cầu nội bộ của doanh nghiệp mình (**Autonomous Compute Island**), và tự động đồng bộ lại bản ghi `ReportUsage` ngay khi mạng WAN có lại.

#### 4. Tiêu chuẩn Check Pass Cổng P7 (Gate Verification & Pass Criteria)
```bash
# 1. Kiểm tra Cô lập Dữ liệu Tenant, Data Residency & Bảo mật mTLS qua WAN
pytest tests/security/test_wan_tenant_isolation_and_data_residency.py tests/security/test_wan_mtls_and_port_exposure.py -v
# PASS KHI: 0 trường hợp dữ liệu nhạy cảm đi sai site; 100% kết nối không có mTLS hợp lệ bị từ chối; 0 cổng RPC nội bộ lộ ra WAN.

# 2. Kiểm tra Tính Tự Chủ của Compute Island khi Đứt cáp WAN & Đối soát Metering
pytest tests/resilience/test_wan_partition_and_autonomous_site.py -v
# PASS KHI:
# - Tỷ lệ thành công của request nội bộ tại mỗi Site trong lúc mất WAN == 100%.
# - Sai lệch số liệu đối soát (ReportUsage) sau khi kết nối WAN phục hồi == 0%.
```

---

### BƯỚC 8 (P8): Hợp Nhất Sản Phẩm v1.0, Dọn Dẹp Mã Nguồn, Kiểm Thử 7 Tầng & Vận Hành Pilot 200 PC (2–4 Sprint)

#### 1. Danh mục công việc chi tiết (Work Breakdown)
- **[P8.1 - Arch/All] Thực thi Quy tắc Hợp nhất Sản phẩm (Consolidation Rule):**
  - Gỡ bỏ khỏi cấu hình build `production` mọi engine/fork không thắng cuộc trong `B01–B09` (chuyển sang nhánh lưu trữ `research/`), chỉ giữ lại các thành phần đã đạt đầy đủ **Definition of Done ([KT] §15.2)**.
- **[P8.2 - SRE/Sec] Đóng gói Bản Phát hành Ngoại tuyến (Offline Bundle), SBOM & Ký số Release:**
  - Xuất báo cáo **SBOM (CycloneDX / SPDX JSON)**, quét lỗ hổng bảo mật (`trivy` / `govulncheck` / `pip-audit`), đóng gói bộ cài đặt Offline Bundle (bao gồm binary đã ký số, container images, thư viện HF đã khóa phiên bản và bộ model chuẩn đã kiểm tra SHA256).
  - Hoàn thiện tài liệu vận hành **SRE Runbook** và kịch bản **Disaster Recovery (DR)** (sao lưu/khôi phục PostgreSQL, NATS JetStream và Model Registry).
- **[P8.3 - Ops/QA] Triển khai Pilot 200 PC theo 3 Vòng (Canary Rollout):**
  - Vòng 1: **10 PC (5%)** trong 3 ngày $\rightarrow$ Vòng 2: **50 PC (25%)** trong 4 ngày $\rightarrow$ Vòng 3: **200 PC (100%)** chạy liên tục $\ge 14$ ngày dưới sự giám sát của OpenTelemetry + Prometheus + Grafana + AI Compute Fabric Dashboard.

#### 2. Sản phẩm bàn giao (Deliverables)
- Bộ phát hành chính thức `AI Compute Fabric v1.0` (`release-manifest-v1.0.yaml`, `sbom-v1.0.cdx.json`, `NOTICE`).
- `docs/sre/SRE_RUNBOOK_v1.0.md` & Báo cáo nghiệm thu Pilot 200 PC (`benchmarks/reports/p8_pilot_200pc_signoff.pdf`).

#### 3. Yêu cầu Viết Test & Chạy Toàn Bộ 7 Tầng Kiểm Thử ([KT] §15.1)
Ở Bước 8, toàn bộ 7 tầng kiểm thử trong `tests/` phải được kết nối vào một lệnh cổng phát hành duy nhất (`make verify-release-v1`):
1. **Tầng 1 — `tests/unit/`:** Scheduler feasibility, placement cost model, 6-bucket VRAM math, tenant policy, tokenizer hash, lease TTL, LoRA merge math.
2. **Tầng 2 — `tests/integration/`:** End-to-end luồng `Gateway -> Scheduler -> Worker-Guard -> Engine Adapter -> Model Registry -> Telemetry/Metering`.
3. **Tầng 3 — `tests/correctness/`:** Kiểm định đầu ra Qwen Dense, Qwen MoE expert weighting, Speculative DeltaNet rollback, LoRA-FA aggregation so với chuẩn vàng Hugging Face.
4. **Tầng 4 — `tests/performance/`:** Bộ regression suite tự động cho `B01 → B09` (TTFT, TPOT, p95 ITL, throughput efficiency, Joule/token, VRAM peak).
5. **Tầng 5 — `tests/resilience/`:** Thu hồi GPU khi người dùng thao tác, rút cáp mạng Worker, trùng lặp tin nhắn hàng đợi, mất kết nối WAN, crash giữa chừng khi đang train.
6. **Tầng 6 — `tests/security/`:** Chống gọi tool trái phép, chống nạp model chứa pickle/`trust_remote_code`, chống rò rỉ dữ liệu liên tenant/site, kiểm tra hết hạn chứng chỉ mTLS, quét CVE.
7. **Tầng 7 — `tests/audio_quality/`:** Kiểm tra chia tập speaker-disjoint, WER/CER tiếng Việt & đa ngữ, độ tương đồng giọng nói (Speaker Similarity) và MOS cho TTS.

#### 4. Tiêu chuẩn Check Pass Cổng Phát Hành v1.0 (Final Release Gate)
```bash
# 1. Chạy trọn bộ 7 tầng kiểm thử tự động ([KT] §15.1)
make verify-release-v1
# (Tương đương chạy tuần tự: unit -> integration -> correctness -> security -> resilience -> audio_quality -> e2e)
# PASS KHI: 100% test suites trả về exit code = 0.

# 2. Kiểm tra Chuỗi Cung Ứng, SBOM & Quét Lỗ Hổng Bảo Mật
trivy fs --severity HIGH,CRITICAL --exit-code 1 .
govulncheck ./...
pip-audit --strict
# PASS KHI: 0 lỗ hổng mức CRITICAL/HIGH chưa được vá; đầy đủ SBOM và chữ ký số trên mọi binary/artifact.

# 3. Nghiệm thu Vận hành Thực tế Pilot 200 PC (14 ngày liên tục)
python3 benchmarks/verify_pilot_sla.py --window-days=14 --nodes=200
# PASS KHI:
# - Uptime của Site Gateway & Control Plane >= 99.9%.
# - 0 sự cố mức nghiêm trọng (Sev-1/Sev-2) làm ảnh hưởng đến công việc của nhân viên trên 200 PC.
# - Thời gian thu hồi GPU p95 < 2,0 giây trên toàn cụm 200 máy thực tế.
```

---

## PHẦN D — BẢNG TỔNG HỢP CỔNG NGHIỆM THU ĐỊNH LƯỢNG & LỆNH CHECK PASS

| Mã Cổng | Chỉ số Nghiệm thu (KPI / Gate) | Ngưỡng Đạt Bắt Buộc (Pass Threshold) | Công cụ / Lệnh Check Pass Tự Động |
|---|---|---|---|
| **G-VRAM** | Độ chính xác của `core/vram-estimator` | $\lvert \text{VRAM}_{\text{est}} - \text{VRAM}_{\text{peak}}\rvert \le \max(5\%, 300\text{ MB})$ trên $\ge 95\%$ ma trận 72 cấu hình | `pytest tests/integration/test_vram_estimator_calibration.py` |
| **G-USER** | Thời gian thu hồi GPU khi nhân viên dùng PC | **p95 < 2,0 giây**, **p99 < 3,0 giây** qua $\ge 100$ lần đo trên Windows thật; **0 lần** vượt Quota | `pytest tests/performance/test_p0_preemption_poc.py --runs=100` |
| **G-SEC** | An toàn Model Artifact & Tool Sandbox | **100%** model có pickle / `trust_remote_code=True` / sai SHA256 bị chặn; **0%** lệnh shell/tool trái phép lọt qua sandbox | `pytest tests/unit/test_hf_model_registry_security.py tests/security/` |
| **G-CORR** | Tính đúng đắn toán học (Model Correctness) | Tokenizer khớp **100%**; Speculative greedy khớp **100%**; KL-divergence phân tán vs máy đơn $< 0,001$ | `pytest tests/correctness/ -v` |
| **G-DIST** | Suy luận Qwen lớn phân tán (`B04` / `B05`) | Giảm **TPOT $\ge 30\%$** so với PP baseline cũ (`B03`) **HOẶC** chạy mượt model 32B không vừa 1 máy và nhanh hơn CPU offload (`B02`) | `pytest tests/performance/test_b04_b05_distributed_qwen.py` |
| **G-TASK** | Hiệu suất thông lượng cụm Agent (`B08`) | **Throughput Efficiency $\ge 80\%$** tổng công suất các máy chạy độc lập trên cụm 8–40 PC | `pytest tests/performance/test_b08_agent_cluster_throughput.py` |
| **G-TRAIN** | Huấn luyện Audio & DiLoCo (`B09`) | Sai số gộp LoRA-FA $< 10^{-6}$; Speaker overlap $= 0\%$; DiLoCo giảm $\ge 70\%$ băng thông mạng so với DDP và đạt target WER | `pytest tests/performance/test_b09_audio_training_convergence.py` |
| **G-WAN** | Tự chủ Compute Island & Cô lập Tenant | **100%** request nội bộ hoạt động khi đứt WAN; **0** byte dữ liệu `RESTRICTED` rời khỏi site gốc | `pytest tests/resilience/test_wan_partition_and_autonomous_site.py` |

---

## PHẦN E — TỔ CHỨC ĐỘI NGŨ (RACI), QUẢN TRỊ RỦI RO & TUÂN THỦ

### E.1. Phân công Trách nhiệm (RACI Matrix) & Định biên Nhân sự
- **Định biên khuyến nghị (10–14 FTE):**
  - **Nhóm Go / Distributed Systems (2–3 kỹ sư):** Phụ trách `worker-guard`, `site-gateway`, `lease-service`, `federation`, tích hợp `gguf-parser-go`.
  - **Nhóm ML Inference & Scheduler (2 kỹ sư):** Phụ trách `core/model-registry` (HF), `core/vram-estimator` calibration, `core/adaptive-scheduler`, MoE & Speculative (`P3, P4`).
  - **Nhóm C++ / CUDA / Runtime (1–2 kỹ sư):** Phụ trách đóng gói cô lập, vá lỗi và đo hiệu năng `llama.cpp`, `Prima.cpp`, `MoLink`, `KT-Kernel`.
  - **Nhóm AI Agent & RAG (1–2 kỹ sư):** Phụ trách `LangGraph` orchestrator, 15 typed tasks, `tool-registry` sandbox, HF `sentence-transformers`/TEI + `pgvector` (`P5`).
  - **Nhóm Audio AI & Distributed Training (1–2 kỹ sư):** Phụ trách HF `datasets` audio pipeline, `Qwen3-ASR/TTS`, `peft` (LoRA-FA), `accelerate`, `prime-diloco` (`P6`).
  - **Nhóm Frontend Dashboard (1 kỹ sư):** Phụ trách `apps/dashboard` (React + TS + Ant Design + ECharts) (`P2–P7`).
  - **Nhóm SRE / Security / QA (2 kỹ sư):** Phụ trách bộ kiểm thử 7 tầng, CI/CD, Windows MSI/GPO deployment, mTLS PKI, SBOM, vận hành Pilot 200 PC.

### E.2. Bảng Quản trị Rủi ro Trọng yếu

| Rủi ro | Xác suất / Tác động | Biện pháp Giảm thiểu Kỹ thuật (Mitigation) | Chủ sở hữu |
|---|:-:|---|---|
| **1. `worker-guard` tự xây gặp lỗi trên Windows WDDM / PDH không cho số liệu cần** | TB / Cao | PoC ở P0 tuần 2–3; nếu PDH không đạt, dùng tổng `used` + headroom bảo thủ và kill-theo-thang; GPUStack không còn là đường lui vì worker chỉ Linux. | Lead Go / Architect |
| **2. Phần cứng thực tế không đồng nhất (trộn lẫn RTX 3060 8GB/12GB, i5 thiếu AVX-512)** | Cao / Cao | Chạy script `probe_node.py` ở ngay tuần đầu P0; `AdaptiveScheduler` và `ISA Gate` phân nhóm máy tự động theo năng lực thực đo. | Lead SRE / ML |
| **3. Sụt áp / Quá nhiệt PSU trên máy văn phòng khi chạy tải AI dài** | TB / Cao | `worker-guard` giới hạn Power Limit của RTX 3060 (ví dụ 130W thay vì 170W) và tự động tạm dừng nhận task nếu nhiệt độ GPU $> 78^\circ\text{C}$. | Lead SRE |
| **4. Phần mềm Antivirus / EDR doanh nghiệp chặn tiến trình AI hoặc Agent** | Cao / TB | Ký số toàn bộ binary `.exe`/`.msi` bằng chứng chỉ doanh nghiệp (Authenticode) và làm việc với IT Security để whitelist thư mục cache có kiểm tra SHA256. | Lead Security |
| **5. Nhân viên lo ngại PC bị chậm hoặc bị theo dõi** | TB / Cao | Minh bạch hóa trạng thái trên System Tray icon (cho phép nhân viên bấm nút *"Tạm dừng AI trong 4 giờ"*), chỉ đọc chỉ số phần cứng NVML/idle-time, không thu thập nội dung màn hình/phím bấm. | PM / Legal |

---

## PHẦN G — CÁC QUYẾT ĐỊNH ĐÃ CHỐT (MẶC ĐỊNH ĐỂ BẮT ĐẦU NGAY)

Mọi mục dưới đây là quyết định mặc định. Chỉ thay đổi khi đúng "điều kiện xem lại" xảy ra, và phải ghi lại bằng ADR (không đổi ngầm). Nếu một dòng mâu thuẫn với phần khác của tài liệu, **Phần G thắng**.

| ID | Chủ đề | Quyết định | Điều kiện xem lại / nơi xác minh |
|---|---|---|---|
| D01 | Nền worker | **Tự xây `worker-guard` (Go)** quản vòng đời `llama-server` (llama.cpp CUDA) trên Windows và Linux. GPUStack Worker **không** vào production (chỉ Linux ✅). | Fleet ≥ 80 % Linux **và** GPUStack qua W2/W4 → ADR bản 2 |
| D02 | GPUStack còn dùng ở đâu | `gguf-parser-go` (MIT) như thư viện; học UX; tùy chọn cho nhóm máy chủ GPU Linux riêng qua API | — |
| D03 | OS mặc định | Windows 10/11 x64 là mục tiêu chính; Linux (Ubuntu 22.04/24.04) hỗ trợ ngang hàng | P0 inventory |
| D04 | Engine | **llama.cpp (GGUF)** là đường mặc định trên mọi worker; vLLM/SGLang chỉ Linux/máy chủ; Prima.cpp & MoLink ở nhánh benchmark, chỉ vào production nếu thắng G-DIST **và** chạy được trên OS của fleet (⚠️ kiểm hỗ trợ Windows ở P0; không được thì dùng WSL2 hoặc giới hạn ở nhóm máy Linux) | P3 |
| D05 | Model khóa (ghim SHA ở P0) | Nhỏ: **Qwen3-4B, Qwen3-8B** · Dense lớn: **Qwen2.5-32B-Instruct** (thêm Qwen3-32B vào ma trận như phần mở rộng) · MoE: **Qwen3-30B-A3B** là mục tiêu chính của P4 (ít rủi ro hỗ trợ); **Qwen3.5-35B-A3B** ✅ (tồn tại, hybrid DeltaNet + MoE 256 expert, có GGUF cộng đồng) là mục tiêu mở rộng, chỉ đưa vào gate khi llama.cpp hỗ trợ đầy đủ DeltaNet GPU offload (⚠️ có báo cáo cần metadata riêng) · Embedding/rerank: `BAAI/bge-m3`, `bge-reranker-v2-m3` | P0 model-lock |
| D06 | Đo VRAM người dùng | Phần của Fabric lấy từ số engine tự báo; phần còn lại = tổng `used` − phần Fabric. Windows có thêm PDH `GPU Process Memory` để đối chiếu (⚠️ P0). | Nếu PDH không đạt: dùng tổng `used` + headroom bảo thủ |
| D07 | Headroom & quota | Headroom 1.536 MB giờ hành chính / 512 MB ngoài giờ (§B.2). Quota: giờ hành chính AI tối đa **80 % VRAM còn trống, 50 % luồng CPU**; sau 18:00 và cuối tuần **95 % VRAM, 80 % CPU**; sửa theo phòng ban trên dashboard. Power limit GPU mặc định 130 W, tạm dừng khi GPU > 78 °C (E.2). | Chỉnh sau P2 theo số đo |
| D08 | Sprint & tổng thời gian | Sprint 2 tuần. P0 3 · P1 2 · **P2 4** · P3 3 · P4 2–4 · P5 3–4 · P6 3–4 · P7 3–5 · P8 2–4 ⇒ **25–33 sprint nếu tuần tự; ≈ 20–26 sprint (40–52 tuần) khi chạy song song P3∥P5 và P4∥P6 sau P2.** Đây là ước lượng lập kế hoạch, không phải cam kết. | Sau P0 |
| D09 | Đội ngũ | Định biên theo E.1 (10–14 FTE). **P0–P2 chạy với 6 người** (Go 3, C++ 1, ML 1, SRE 1); các nhóm Agent, Audio/Training, Frontend đầy đủ vào từ P3 (Frontend 1 người vào từ P2). Thiếu người ⇒ kéo dài thời gian, **không cắt gate**. | PM |
| D10 | Dashboard | React + TS + Vite + Ant Design + ECharts; SSE 1 Hz; heatmap canvas; RBAC; Việt/Anh; viết mới (chỉ học bố cục GPUStack UI). | — |
| D11 | Hạ tầng | PostgreSQL + pgvector, NATS JetStream, MinIO, OpenTelemetry + Prometheus + Grafana. | — |
| D12 | Bảo mật | CA nội bộ `step-ca`, mTLS toàn tuyến, cert 30 ngày tự xoay; tool ký số + allowlist; không generic shell; `trust_remote_code=False`. | Security review cuối P2, P7 |
| D13 | Triển khai agent | MSI ký số Authenticode + Intune/GPO; `.deb` + systemd; vòng cập nhật 5 % → 25 % → 100 %; tự rollback nếu crash > 3 lần/5 phút (P2.1) hoặc tỉ lệ lỗi > 2 %/giờ. | — |
| D14 | Ngưỡng gate | Dùng các ngưỡng ở Phần D. Speculative: bật mặc định khi acceptance ≥ 0,65 **và** speedup ≥ 1,25× (theo P4). Cho phép **một lần** hiệu chỉnh ngưỡng p95 thu hồi GPU và tolerance estimator ở cuối P0 dựa trên số đo thật, sau đó đóng băng. | Cuối P0 |
| D15 | Đồng bộ LoRA | LoRA-FA (đóng băng A, đồng bộ B) là mặc định; SVD-merge chỉ là đối chứng xấp xỉ. DiLoCo là thử nghiệm, không mặc định. | P6 |
| D16 | Agent | LangGraph + typed tool của Fabric; không CodeAgent; bỏ TGI (maintenance mode ✅); embedding dùng `sentence-transformers` hoặc TEI, chọn một sau benchmark trên RTX 3060 ở P5. | P5 |
| D17 | WAN / pilot | Pilot **2 site**; chỉ chuyển request/AI task, không model-parallel qua WAN; dữ liệu `RESTRICTED` không rời site; tenant cô lập. | P7 |
| D18 | License | Cho phép Apache-2.0/MIT/BSD vào production; GPL/AGPL chỉ trong tiến trình tách biệt, không link; mỗi model Qwen và dataset kiểm riêng ở P0 (test `test_manifest_schema.py` chặn license cấm). | Legal duyệt P0 |
| D19 | Nhân viên & pháp lý | Chính sách minh bạch công bố trước khi vào máy thật; có nút "Tạm dừng AI 4 giờ" ở System Tray; chỉ đọc chỉ số phần cứng + idle-time; log thu hồi GPU lưu 90 ngày; **Legal/HR/IT duyệt và whitelist EDR là điều kiện chặn P2.** | Trước P2 |
| D20 | Tiếng Việt | `VietnameseTextNormalizer` trong repo (P6.1); dataset ứng viên Common Voice (vi), FLEURS (vi), VIVOS (⚠️ xác minh license/quy mô ở P0); dữ liệu nội bộ cần `consent_ref`. | P0 license |

---

## PHẦN F — DANH MỤC CẦN KHỞI ĐỘNG NGAY Ở TUẦN 1 CỦA BƯỚC 0 (P0 ACTION ITEMS)

1. **Cấp quyền truy cập cụm thử nghiệm 5–10 PC Windows/Linux thật** (Core i5 + RTX 3060 + kết nối cùng switch LAN) để chạy script khảo sát tự động `probe_node.py` và kiểm chứng 5 điều kiện của **ADR-001**.
2. **Ghim SHA các model đã chốt (D05) vào `upstream-manifest.yaml`:**
   - Nhóm Dense: `Qwen/Qwen3-4B`, `Qwen/Qwen3-8B`, `Qwen/Qwen2.5-32B-Instruct` (+ `Qwen3-32B` mở rộng).
   - Nhóm MoE: `Qwen/Qwen3-30B-A3B` (chính) và `Qwen/Qwen3.5-35B-A3B` (mở rộng, phụ thuộc hỗ trợ DeltaNet của llama.cpp).
   - Nhóm Audio & Embedding: `QwenLM/Qwen3-ASR`, `QwenLM/Qwen3-TTS`, `BAAI/bge-m3`.
3. **Khởi tạo ngay bộ khung Monorepo `ai-compute-fabric` kèm các bài test cổng P0–P1** (`tests/unit/test_manifest_schema.py`, `contracts/protobuf/aicf/v1/fabric.proto`, `core/vram-estimator/`) ngay trong thư mục dự án để đội ngũ bắt đầu làm việc trên cùng một chuẩn kiểm thử tự động.

---

## PHẦN H — CHECKLIST THEO DÕI TIẾN ĐỘ (CẬP NHẬT TRỰC TIẾP TRONG FILE NÀY)

**Cách dùng:** đổi `[ ]` thành `[~]` khi đang làm và `[x]` khi xong. **Chỉ đánh `[x]` khi có bằng chứng**: file deliverable tồn tại, hoặc lệnh test trả `exit code = 0` đạt ngưỡng ghi trong Phần C/D. Ghi bằng chứng (đường dẫn file / link CI / ngày chạy) vào cuối dòng sau dấu `→`. Một phase chỉ `DONE` khi **tất cả** mục của phase, gồm cả dòng "Gate", đều `[x]`. Mục bị thay đổi quyết định thì gạch bằng `~~...~~` và ghi ADR.

### H.0. Bảng tổng hợp (cập nhật tay khi đổi trạng thái)

| Phase | Nội dung | Số mục | Xong | Trạng thái |
|---|---|:-:|:-:|---|
| Chuẩn bị | Tài liệu & quyết định | 6 | 5 | Gần xong |
| P0 | Khảo sát, baseline, kiểm chứng ADR-001 | 19 | 0 | Chưa bắt đầu |
| P1 | Monorepo, contract, registry, estimator | 14 | 0 | Chưa bắt đầu |
| P2 | Fleet 200 PC, worker-guard, gateway, dashboard MVP | 21 | 0 | Chưa bắt đầu |
| P3 | Qwen phân tán + Deploy UI | 13 | 0 | Chưa bắt đầu |
| P4 | MoE & Speculative | 11 | 0 | Chưa bắt đầu |
| P5 | Agent, RAG, 15 typed tasks | 13 | 0 | Chưa bắt đầu |
| P6 | Audio & Training | 14 | 0 | Chưa bắt đầu |
| P7 | WAN, đa tenant, metering | 11 | 0 | Chưa bắt đầu |
| P8 | Hardening, release v1.0, pilot 200 PC | 14 | 0 | Chưa bắt đầu |

### H.1. Chuẩn bị (trước P0)
- [x] Đọc, đánh giá kế hoạch và tài liệu kiến trúc [KT]; sửa lỗi số học, công thức, gate → Phần A
- [x] Chốt hướng kiến trúc và 20 quyết định mặc định → Phần G
- [x] Kiểm chứng sơ bộ license/tính năng GPUStack, `gguf-parser-go`, TGI, Qwen3.5-35B-A3B → nguồn ở Phần A, G
- [x] Rà soát toàn bộ file, sửa lỗi chữ và mâu thuẫn
- [x] Định nghĩa gate định lượng + lệnh check pass → Phần C, D
- [ ] Nhận dữ liệu thực từ bên bạn: 5–10 PC thật, 10 câu Phụ lục C, đầu mối Legal/HR/IT (Phần F)

### H.2. P0 — Khảo sát, baseline, kiểm chứng ADR-001 (3 sprint)
**Chuẩn bị & truy cập**
- [ ] Có quyền truy cập cụm thử 5–10 PC (Windows/Linux) cùng switch
- [ ] Legal/HR/IT đã cử đầu mối; chính sách nhân viên (D19) đã gửi duyệt
**Công việc**
- [ ] P0.1 `benchmarks/hardware/probe_node.py` và `benchmarks/network/probe_lan_matrix.py` chạy xong trên ≥ 10 PC
- [ ] P0.1 Trả lời đủ 10 câu Phụ lục C → `p0_hardware_network_inventory.json`
- [ ] P0.2 `third_party/upstream-manifest.yaml` đủ commit SHA40 + SPDX cho mọi thành phần
- [ ] P0.2 Kiểm giấy phép từng model Qwen và dataset âm thanh; Legal ký
- [ ] P0.3 Ngày 1–2: kiểm lại tài liệu GPUStack (OS worker, backend, license), ghi vào ADR-001
- [ ] P0.3 PoC `worker-guard`: đọc NVML + PDH + `GetLastInputInfo`, pause/kill `llama-server`
- [ ] P0.3 Giả lập 200 worker, đo độ trễ lập lịch p95
- [ ] P0.3 Kiểm hỗ trợ Windows của Prima.cpp/MoLink; kiểm llama.cpp hỗ trợ DeltaNet GPU offload
- [ ] P0.4 Baseline B01 → `p0_baselines_B01_B02_B03.json`
- [ ] P0.4 Baseline B02 (kèm tập logprobs chuẩn vàng bằng `accelerate`)
- [ ] P0.4 Baseline B03 (vLLM TP/PP, LAN 2 và 5 Gbps)
- [ ] P0.4 `p0_vram_raw_calibration.csv`
**Deliverable & Gate**
- [ ] ADR-001 chuyển trạng thái `ACCEPTED` hoặc `PARTIAL_FALLBACK`; ADR-002/003 xong
- [ ] Gate: `pytest tests/unit/test_manifest_schema.py` exit 0
- [ ] Gate: `test_p0_hardware_probe.py` + `test_p0_baseline_harness.py` exit 0; RSD TPOT < 8 %
- [ ] Gate: `test_p0_preemption_poc.py --runs=100` đạt p95 < 2000 ms, 0 crash WDDM; sai số estimator ≤ 10 % trên ≥ 10 cấu hình
- [ ] Hiệu chỉnh ngưỡng D14 **một lần** và đóng băng → ghi vào Phần G

### H.3. P1 — Monorepo, contract, registry, estimator (2 sprint)
- [ ] P1.1 Khung monorepo theo Phụ lục A [KT] + `core/vram-estimator/` + `apps/worker-guard/`
- [ ] P1.1 CI/CD chạy build, lint (`golangci-lint`, `ruff`, `mypy`), test, SBOM
- [ ] P1.2 `fabric.proto` + `aicf_v1.yaml` + sinh code Go/Python (`buf generate`)
- [ ] P1.2 Protocol `InferenceEngine`, `TrainingEngine`
- [ ] P1.3 `core/model-registry`: snapshot HF, mirror LAN, xác thực SHA256, `trust_remote_code=False`
- [ ] P1.3 Chuỗi provenance HF → converter → GGUF; `tokenizer_sha256`; `render_chat_template`
- [ ] P1.4 `core/vram-estimator` (Go + sidecar Python) + endpoint `/api/v1/models/estimate-vram`
- [ ] P1.4 Ma trận hiệu chuẩn 72 cấu hình → `calibration_table.json`
- [ ] Test: `test_protobuf_contracts.py`, `estimator_test.go` (không underflow uint64)
- [ ] Test: `test_hf_model_registry_security.py` (chặn SHA sai, pickle, auto_map, tokenizer bị sửa)
- [ ] Test: `test_tokenizer_determinism.py`
- [ ] Gate: `go test ./core/vram-estimator/... -race`, độ phủ ≥ 85 %, 100 % mẫu giả mạo bị chặn
- [ ] Gate: tokenizer khớp 100 %
- [ ] Gate: ≥ 69/72 cấu hình đạt `max(5 %, 300 MB)`

### H.4. P2 — Fleet 200 PC (4 sprint)
**Điều kiện chặn**
- [ ] Legal/HR/IT duyệt chính sách nhân viên; EDR/antivirus whitelist (D19)
**Công việc**
- [ ] P2.1 `worker-guard` production: Windows service + systemd, MSI ký số, GPO/Intune
- [ ] P2.1 Kế toán VRAM 6 ngăn 1 Hz; power limit; thang thu hồi Drain → Checkpoint → Kill
- [ ] P2.1 Tự cập nhật vòng 5 → 25 → 100 % + watchdog rollback
- [ ] P2.1 System Tray: nút "Tạm dừng AI 4 giờ"
- [ ] P2.2 `site-gateway` (gRPC, mTLS, NATS JetStream, PostgreSQL): Register, Heartbeat, Lease
- [ ] P2.2 CA `step-ca`, cert tự xoay 30 ngày
- [ ] P2.2 `core/topology-graph` đo RTT/băng thông
- [ ] P2.3 `core/adaptive-scheduler` v1 (lọc VramEstimate, ModelIdentity, tenant, hàm chi phí)
- [ ] P2.4 Dashboard MVP: Màn 1 (heatmap 200 GPU) và Màn 3 (worker, quota, preemption log)
- [ ] P2.4 RBAC, audit log, Việt/Anh
- [ ] Test: `test_adaptive_scheduler_decisions.py` (≥ 15 kịch bản)
- [ ] Test: `test_lease_lifecycle_and_idempotency.py`
- [ ] Test: `test_preemption_and_worker_churn.py`
- [ ] Test: `test_dashboard_200_nodes.spec.ts` (Playwright, ≥ 30 FPS)
- [ ] Gate: unit/integration pass, không race
- [ ] Gate: Playwright pass, render lưới 200 GPU < 50 ms/frame
- [ ] Gate: soak 5 → 20 → 40 PC, 72 h; 0 crash OS; rò bộ nhớ < 2 %/24 h
- [ ] Gate: thu hồi GPU p95 < 2,0 s, p99 < 3,0 s; 0 lần vượt quota
- [ ] Security review nội bộ cuối P2
- [ ] Thử rollback agent thành công

### H.5. P3 — Qwen phân tán + Deploy UI (3 sprint)
- [ ] P3.1 Adapter `llama`, `vllm` đủ 7 hàm lifecycle
- [ ] P3.2 Adapter `prima`, `molink` cô lập, đường hầm mTLS (hoặc ghi rõ "không chạy được trên OS fleet" + phương án WSL2/Linux)
- [ ] P3.3 Chạy B03, B04, B05 trên 3–4 PC, LAN 2 và 5 Gbps → `p3_distributed_comparison_B03_B04_B05.json`
- [ ] P3.3 Đo KL-divergence/perplexity so với chuẩn vàng HF
- [ ] P3.4 Màn 2 (Live VRAM Fit + layer-split) và Màn 5a (Chat Playground)
- [ ] ADR-004: chốt duy nhất 1 backend phân tán thắng
- [ ] Test: `test_engine_adapter_contract.py`
- [ ] Test: `test_distributed_qwen_correctness.py` (200 prompt)
- [ ] Test: `test_b04_b05_distributed_qwen.py`
- [ ] Test: `test_distributed_session_failover.py`
- [ ] Gate: VRAM rò sau `stop()` < 50 MB trên mọi adapter
- [ ] Gate: KLD TB < 0,001 và perplexity lệch < 0,5 % (cùng GGUF)
- [ ] Gate: TPOT −30 % (95 % CI) **hoặc** chạy được 32B không vừa 1 máy và nhanh hơn B02

### H.6. P4 — MoE & Speculative (2–4 sprint)
- [ ] P4.1 CPU ISA gate trong `hardware-profiler`
- [ ] P4.1 MoE local/hybrid trên llama.cpp (`--override-tensor`); đối chứng KTransformers nơi đủ ISA
- [ ] P4.1 `inference/moe/expert-cache` + thanh VRAM/RAM cho MoE trên dashboard
- [ ] P4.2 Task Router Agent-level, gắn cờ `AGENT_ENSEMBLE_NON_IDENTICAL`
- [ ] P4.3 Speculative decoding (draft cùng `tokenizer_sha256`)
- [ ] P4.3 Kiểm riêng rollback trạng thái DeltaNet (chỉ khi D05 mở rộng Qwen3.5 được kích hoạt)
- [ ] Test: `test_cpu_isa_gate.py` (0 SIGILL)
- [ ] Test: `test_moe_expert_weighting_and_deltanet_rollback.py`
- [ ] Test: `test_b06_moe_and_b07_speculative.py`
- [ ] Gate: B06 TPOT thấp hơn ≥ 40 % so với B02
- [ ] Gate: B07 bật mặc định chỉ khi acceptance ≥ 0,65 và speedup ≥ 1,25×; đầu ra greedy khớp 100 %

### H.7. P5 — Agent, RAG, 15 typed tasks (3–4 sprint)
- [ ] P5.1 LangGraph orchestrator + checkpoint PostgreSQL
- [ ] P5.1 Đủ 15 typed task theo [KT] §6.2
- [ ] P5.2 Tool registry ký số, `tenant_scope`, constrained decoding, sandbox; không shell tùy ý
- [ ] P5.3 Worker embedding/rerank (chọn `sentence-transformers` hoặc TEI sau benchmark), pgvector
- [ ] P5.3 `result-validator` (schema, citation, ảo giác)
- [ ] P5.4 Màn 4a (Agent DAG)
- [ ] Test: `test_15_typed_task_schemas.py`
- [ ] Test: `test_agent_tool_sandbox_and_injection.py` (50 kịch bản)
- [ ] Test: `test_langgraph_checkpoint_resume.py`
- [ ] Test: `test_b08_agent_cluster_throughput.py` (8, 20, 40 PC)
- [ ] Gate: 0 % bypass sandbox
- [ ] Gate: chỉ chạy lại task đang in-flight khi mất worker
- [ ] Gate: throughput efficiency ≥ 0,80 và 100 % kết quả qua validator

### H.8. P6 — Audio & Training (3–4 sprint)
- [ ] P6.1 Pipeline `datasets` streaming 10.000+ file; VAD; resample; `consent_ref`; checksum
- [ ] P6.1 Chia speaker-disjoint; `VietnameseTextNormalizer`; WER/CER bằng `jiwer`
- [ ] P6.2 Adapter Qwen3-ASR, Qwen3-TTS, DeepFilterNet; Màn 5b
- [ ] P6.3 Estimator bộ nhớ huấn luyện (LoRA + gradient + AdamW 8-bit)
- [ ] P6.3 Ba chế độ: LoRA 1 GPU, DDP 2–4, DiLoCo/periodic
- [ ] P6.3 LoRA-FA mặc định; cấm trung bình ngây thơ B̄·Ā
- [ ] Test: `test_speaker_disjoint_and_vn_wer.py`
- [ ] Test: `test_lora_aggregation_math.py`
- [ ] Test: `test_training_stale_worker_and_checkpoint.py`
- [ ] Test: `test_b09_audio_training_convergence.py`
- [ ] Gate: speaker overlap = 0 %; sai số Frobenius < 1e-6
- [ ] Gate: chặn 100 % stale update; resume checkpoint khớp optimizer và step
- [ ] Gate: WER/CER sau fine-tune thấp hơn model gốc; DiLoCo giảm ≥ 70 % byte so với DDP; time-to-target-WER đạt biên
- [ ] Xác minh license/quy mô dataset tiếng Việt (D20)

### H.9. P7 — WAN, đa tenant, metering (3–5 sprint)
- [ ] P7.1 `global-control-plane` + 4 API federation + phân loại dữ liệu
- [ ] P7.1 mTLS liên site, CRL, job token có hạn
- [ ] P7.2 `core/resource-metering` (append-only) + Màn 5c
- [ ] P7.3 Rà soát cổng: không RPC/NATS/PostgreSQL lộ ra WAN; pen-test
- [ ] Chọn và chuẩn bị 2 site pilot
- [ ] Test: `test_wan_tenant_isolation_and_data_residency.py` (20 kịch bản)
- [ ] Test: `test_wan_mtls_and_port_exposure.py`
- [ ] Test: `test_wan_partition_and_autonomous_site.py` (cắt WAN 30 phút)
- [ ] Gate: 0 dữ liệu `RESTRICTED` sai site
- [ ] Gate: 100 % kết nối thiếu mTLS hợp lệ bị từ chối; 0 cổng nội bộ lộ ra WAN
- [ ] Gate: 100 % request nội bộ khi đứt WAN; sai lệch `ReportUsage` = 0 %

### H.10. P8 — Hardening, release v1.0, pilot 200 PC (2–4 sprint)
- [ ] P8.1 Gỡ khỏi build `production` mọi engine/fork không thắng; chuyển sang `research/`
- [ ] P8.2 SBOM (CycloneDX/SPDX), quét `trivy`, `govulncheck`, `pip-audit`
- [ ] P8.2 Offline bundle ký số (binary, container, thư viện HF pin, model đã kiểm SHA256)
- [ ] P8.2 SRE Runbook; DR (PostgreSQL, NATS JetStream, Model Registry) đã diễn tập
- [ ] P8.3 Pilot vòng 1: 10 PC × 3 ngày
- [ ] P8.3 Pilot vòng 2: 50 PC × 4 ngày
- [ ] P8.3 Pilot vòng 3: 200 PC liên tục ≥ 14 ngày
- [ ] `release-manifest-v1.0.yaml`, `sbom-v1.0.cdx.json`, `NOTICE`
- [ ] Báo cáo nghiệm thu pilot → `p8_pilot_200pc_signoff.pdf`
- [ ] Tầng 1–7 pass: unit, integration, correctness, performance, resilience, security, audio_quality
- [ ] Gate: `make verify-release-v1` exit 0
- [ ] Gate: `trivy` / `govulncheck` / `pip-audit`: 0 lỗ hổng HIGH/CRITICAL chưa vá
- [ ] Gate: `verify_pilot_sla.py --window-days=14 --nodes=200` — uptime ≥ 99,9 %, 0 sự cố Sev-1/Sev-2, thu hồi GPU p95 < 2,0 s
- [ ] DoD mỗi engine trong build production ([KT] §15.2)

### H.11. Theo dõi quyết định mở (xóa dòng khi đã xử lý)
- [ ] Phụ lục C #1–#10 trả lời đủ → nếu mâu thuẫn D03/D05, cập nhật Phần G bằng ADR
- [ ] Prima.cpp/MoLink chạy được trên OS fleet? (D04)
- [ ] PDH `GPU Process Memory` đủ tin cậy trên WDDM? (D06)
- [ ] llama.cpp hỗ trợ đầy đủ DeltaNet GPU offload cho Qwen3.5? (D05)
- [ ] `gguf-parser-go` hỗ trợ ước lượng hybrid DeltaNet đạt tolerance? (Phần A #3)
- [ ] License và quy mô dataset tiếng Việt (D20)
