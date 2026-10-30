# BÁO CÁO P8: KHẢO SÁT KHẢ THI & THỬ NGHIỆM KTRANSFORMERS (`kvcache-ai/ktransformers`)

- **Ngày thực hiện:** `2026-10-30`
- **Trạng thái hiện tại:** **Hoàn thành Pha 0 (Khảo sát khả thi & Cổng Check Pass)** — Đang chờ chủ dự án duyệt phương án thực thi Pha 1 & Pha 2A/2B. **Bản chốt 2026-10-30** (đã rà soát lại, xem mục 6).

### Tóm tắt điều hành
- **Chưa đo hiệu năng nào.** Báo cáo này chỉ trả lời "có chạy được không và với chi phí nào".
- **Khả thi duy nhất trên máy này:** RTX 3060 + Core Ultra 7 265K (AVX2/AVX-VNNI, không AVX-512/AMX), Linux. RTX 5070/Windows **không hỗ trợ** (chỉ có wheel Linux, `sm_120` không có sẵn).
- **Toàn trình `sglang-kt` với trọng số BF16 không vừa đĩa** (56,87 GiB > 40 GiB trống). Hai đường còn lại: (1C) đo riêng nhân CPU trên GGUF có sẵn, 0 GB tải; (1B) GPTQ-Int4 15,77 GiB, cần tải và cần giải phóng RAM.
- **Rào cản lớn nhất là RAM của `pc-001`**: OOM-killer đã giết `worker-guard` lúc 02:04:49 ngày 30/10, dịch vụ Coder đang ngừng. Mọi phép đo cần RAM `available` ≥ 8 GiB.
- **Cần chủ dự án quyết:** (a) khôi phục dịch vụ Coder trước hay đo ngay, (b) cho chiếm GPU/RAM khung giờ nào, (c) có đóng bớt ứng dụng nặng không, (d) có duyệt đường 1B (tải 16,93 GB) không.
- **Kế hoạch gốc:** [`KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md`](../../../KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md)
- **Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật · `[ĐỌC MÃ]` đã đọc trong mã nguồn repo/wheel · `[TÀI LIỆU]` theo README/tài liệu KTransformers · `[SUY LUẬN]` tính toán/ước lượng · `[CHƯA XÁC MINH]` phải thử.

---

## 1. Kết quả Pha 0.1 — Đọc mã nguồn & tài liệu (`ktransformers==0.7.2`, `kt-kernel==0.7.2`, `sglang-kt==0.7.2`)

### 1.1 (a) Định dạng trọng số GPU và dung lượng thật trên đĩa
- **Cách nạp trọng số trong `sglang-kt==0.7.2` (`[ĐỌC MÃ]` `sglang/srt/models/qwen3_moe.py:1081-1160`, `sglang/srt/layers/moe/kt_ep_wrapper.py:5208-5447`):**
  - Khi chạy `python -m sglang.launch_server --model <THƯ_MỤC_HF> --kt-weight-path <ĐƯỜNG_DẪN_KT> --kt-method <BACKEND>`, SGLang nạp toàn bộ các tầng không phải chuyên gia (`model.embed_tokens`, `self_attn.qkv_proj`, `self_attn.o_proj`, `self_attn.q_norm`, `self_attn.k_norm`, `input_layernorm`, `post_attention_layernorm`, `mlp.gate`, `lm_head` ≈ **1,75 tỷ tham số**, ~`3,5 GB` ở BF16) cộng với `num_gpu_experts` mỗi tầng từ thư mục `--model` lên GPU.
  - Phần chuyên gia còn lại (`128 - num_gpu_experts` chuyên gia/tầng × 48 tầng) được lớp `KTMoEWrapper` nạp lên CPU từ `--kt-weight-path`.
  - Vì trong kho Hugging Face, các tensor `self_attn.*` và `mlp.experts.{0..127}.*` nằm xen kẽ trong từng file `.safetensors`, thư mục `--model` **bắt buộc phải có đầy đủ các shard `.safetensors`**.
- **Dung lượng thật của các kho `Qwen3-30B-A3B` trên Hugging Face (`[ĐO]` qua API `https://huggingface.co/api/models/...?blobs=true` ngày `2026-10-30`):**

| Kho mô hình Hugging Face | Định dạng | Số file `.safetensors` | Tổng dung lượng (`GB` thập phân) | Tổng dung lượng (`GiB` nhị phân) | So với `40 GiB` trống trên ổ dữ liệu `pc-001` |
|---|---|---:|---:|---:|---|
| `Qwen/Qwen3-30B-A3B` | BF16 gốc | 16 | `61,07 GB` `[ĐO]` | `56,87 GiB` `[ĐO]` | **KHÔNG VỪA** (`56,87 GiB > 40 GiB`) |
| `Qwen/Qwen3-Coder-30B-A3B-Instruct` | BF16 gốc | 16 | `61,07 GB` `[ĐO]` | `56,87 GiB` `[ĐO]` | **KHÔNG VỪA** (`56,87 GiB > 40 GiB`) |
| `Qwen/Qwen3-30B-A3B-FP8` | FP8 | 7 | `32,45 GB` `[ĐO]` | `30,22 GiB` `[ĐO]` | Cộng venv (`~6 GiB`) = `~36,2 GiB / 40 GiB` (quá sát ngưỡng đầy đĩa; RTX 3060 `sm_86` không có FP8 phần cứng) |
| `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` | FP8 | 4 | `31,20 GB` `[ĐO]` | `29,05 GiB` `[ĐO]` | Cộng venv (`~6 GiB`) = `~35,1 GiB / 40 GiB` (quá sát ngưỡng đầy đĩa; `sm_86` không có FP8 phần cứng) |
| `Qwen/Qwen3-30B-A3B-GPTQ-Int4` | GPTQ-Int4 | 1 | `16,93 GB` `[ĐO]` | `15,77 GiB` `[ĐO]` | **VỪA** (`15,77 GiB` + venv `~6 GiB` ≈ `21,8 GiB / 40 GiB`, dùng chung 1 thư mục cho cả `--model` và `--kt-weight-path` với `--kt-method GPTQ_INT4` `[ĐỌC MÃ]` `doc/en/kt-kernel/AVX2-Tutorial.md`) |
| File có sẵn `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` | GGUF `Q4_K_M` | 1 file `.gguf` | `18,56 GB` `[ĐO]` | `17,28 GiB` `[ĐO]` | **ĐÃ CÓ SẴN TRÊN ĐĨA (`0 GB` tải thêm)** — nạp trực tiếp được bằng `KTMoEWrapper(backend="LLAMAFILE")` (`kt-kernel/python/experts.py`) `[ĐỌC MÃ]` |

### 1.2 (b) Ý nghĩa tham số `--kt-num-gpu-experts` và ánh xạ sang `llama.cpp -ncmoe N`
- Trong `sglang/srt/layers/moe/kt_ep_wrapper.py` (dòng `4606–4613`) `[ĐỌC MÃ]`:
  ```python
  if server_args.kt_gpu_experts_ratio is not None:
      num_gpu_experts = int(total_experts * server_args.kt_gpu_experts_ratio)
  else:
      num_gpu_experts = server_args.kt_num_gpu_experts * num_moe_layers
  ```
- Như vậy `--kt-num-gpu-experts K` là **số chuyên gia giữ trên GPU của mỗi tầng MoE** (trong tổng số `128` chuyên gia/tầng của `Qwen3-30B-A3B`, nhân lên trên `48` tầng MoE = `6.144` chuyên gia tổng cộng `[ĐỌC MÃ]` `.aicf-work/qwen3-tok/config.json`).
- Bảng quy đổi tương đương về tổng tỷ lệ chuyên gia trên GPU/CPU với `llama.cpp -ncmoe N` (vốn đẩy toàn bộ 128 chuyên gia của `N` tầng đầu lên CPU và giữ `48 - N` tầng trên GPU):

| Cấu hình `llama.cpp` | Số tầng MoE trên GPU (`llama.cpp`) | Tỷ lệ chuyên gia trên GPU | Tham số `--kt-num-gpu-experts K` tương đương | Số chuyên gia GPU / CPU mỗi tầng (`KTransformers`) | Tổng chuyên gia trên CPU (`48` tầng) |
|---|---:|---:|---:|---|---:|
| `-ncmoe 20` | `28 / 48` | `58,33%` | `--kt-num-gpu-experts 75` | `75 GPU / 53 CPU` (`58,59%` GPU) | `2.544 / 6.144` |
| `-ncmoe 26` | `22 / 48` | `45,83%` | `--kt-num-gpu-experts 59` | `59 GPU / 69 CPU` (`46,09%` GPU) | `3.312 / 6.144` |
| `-ncmoe 30` | `18 / 48` | `37,50%` | `--kt-num-gpu-experts 48` | `48 GPU / 80 CPU` (`37,50%` GPU) | `3.840 / 6.144` |
| `-ncmoe 36` | `12 / 48` | `25,00%` | `--kt-num-gpu-experts 32` | `32 GPU / 96 CPU` (`25,00%` GPU) | `4.608 / 6.144` |

### 1.3 (c) Các nhân CPU trong `kt-kernel==0.7.2` và hành vi bộ nhớ `LLAMAFILE`
- Kiểm tra mã nguồn `kt-kernel/python/utils/amx.py` (dòng `62–235`), `kt-kernel/operators/avx2/` và `kt-kernel/operators/llamafile/moe.hpp` `[ĐỌC MÃ]`:
  1. **Nhân `LLAMAFILE` (`CPUInfer` + `LLAMA_MOE_TP`):**
     - Đọc trực tiếp tensor `blk.{i}.ffn_{gate,up,down}_exps.weight` từ file `.gguf` (`Q4_K_M`).
     - Đặc biệt, tại `kt-kernel/operators/llamafile/moe.hpp` dòng `113–116` và `283–320` `[ĐỌC MÃ]`, khi chạy trên máy **1 NUMA node (`tp_count == 1`)** như `pc-001`, hàm `LLAMA_MOE_TP::load_weights()` kích hoạt đường **Single-NUMA fast path (`m_weights_aliased_ = true`)**: trỏ thẳng vào vùng nhớ `mmap` của file GGUF và gọi `madvise(..., MADV_WILLNEED)` thay vì cấp phát lại 18 GB trên heap.
     - Lưu ý kỹ thuật khi viết script đo `[ĐỌC MÃ]`: `kt-kernel/python/utils/llamafile.py` giữ đối tượng `np.memmap` trong `self.gguf_loader.file_data_map`; cần giữ tham chiếu `gguf_loader` sống suốt phiên chạy để con trỏ `mmap` không bị thu hồi.
  2. **Nhóm nhân AVX2 / AVX-VNNI mới (`kt-kernel/python/utils/amx.py:62-235`):**
     - Ngoài `LLAMAFILE`, `kt-kernel==0.7.2` đã bổ sung các nhân không cần AVX-512/AMX: `AVX2BF16_MOE`, `AVX2FP8_MOE`, `AVX2GPTQInt4_MOE`, `AVXVNNI256GPTQInt4_MOE`, **`AVXVNNI256GPTQInt4Packed_MOE`** (giữ trọng số INT4 nén `~0,56 byte/trọng số` trong RAM và dùng lệnh `avx_vnni` có trên `Core Ultra 7 265K`), và `AVXVNNI256RawInt4_MOE` `[ĐỌC MÃ]`.

### 1.4 (d) Yêu cầu gói Python / PyTorch / CUDA và kiến trúc GPU (`sm_86` vs `sm_120`)
- Kiểm tra metadata wheel trên PyPI (`kt_kernel-0.7.2-cp312-cp312-manylinux_2_35_x86_64.whl` nặng `101,5 MB`; `sglang_kt-0.7.2-py3-none-manylinux_2_35_x86_64.whl` nặng `101,4 MB`) `[ĐỌC MÃ]`:
  - **Hệ điều hành & glibc:** `Operating System :: POSIX :: Linux`, `manylinux_2_35_x86_64` (khớp Ubuntu 24.04 `glibc 2.39` và `Python 3.12.3` trên `pc-001` `[ĐO]`). **Không có wheel cho Windows.**
  - **Phụ thuộc bắt buộc của `sglang-kt==0.7.2`:** `torch==2.9.1`, `flashinfer_python==0.6.3`, `flashinfer_cubin==0.6.3`, `cuda-python<13.3,>=13.1`, `apache-tvm-ffi==0.1.11`, `accelerate-kt==1.14.0.post4`, `gguf>=0.17.0`, `safetensors>=0.4.0`, `triton>=2.0.0` `[ĐỌC MÃ]`.
  - **Kiến trúc GPU (`kt-kernel/CMakeLists.txt:341`):** Mặc định biên dịch `CMAKE_CUDA_ARCHITECTURES "80;86;89;90"` `[ĐỌC MÃ]`.
    - **RTX 3060 (`sm_86`, Ampere):** **Hỗ trợ sẵn** trong wheel.
    - **RTX 5070 (`sm_120`, Blackwell):** **Không có** trong wheel dựng sẵn.
- **API chuẩn OpenAI (e):** `sglang.launch_server` hỗ trợ đầy đủ `/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/health` `[ĐỌC MÃ]`.

---

## 2. Kết quả Pha 0.2 — Kiểm tra thực tế phần cứng 2 máy (`[ĐO]`)

### 2.1 Máy Linux `pc-001` (`192.168.1.40` / `10.42.0.1`)
- **CPU (`lscpu`, `lscpu -e`):**
  - Model: `Intel(R) Core(TM) Ultra 7 265K`, 1 socket, **1 NUMA node (`node0: CPU 0..19`)**, **20 nhân vật lý / 20 luồng** (`Thread(s) per core: 1`) `[ĐO]`.
  - Cấu trúc nhân lai (`lscpu -e`):
    - **8 nhân P (`CPU 0..7`):** xung tối đa `5500–5600 MHz`, L2 `3 MiB`/nhân `[ĐO]`.
    - **12 nhân E (`CPU 8..19`):** xung tối đa `4600 MHz`, L2 `4 MiB`/cụm 4 nhân `[ĐO]`.
  - Tập lệnh vector: `avx`, `avx2`, `fma`, `f16c`, **`avx_vnni`**, `bmi1`, `bmi2`; **không có `avx512*`, không có `amx*`** `[ĐO]`.
- **GPU (`nvidia-smi`, `nvcc --version`):**
  - `NVIDIA GeForce RTX 3060`, VRAM `12288 MiB`, compute capability `8.6` (`sm_86`), Driver `580.178.04` (CUDA Driver API `13.0`), bộ biên dịch `nvcc` trên máy `V12.0.140` `[ĐO]`.
  - Mức dùng VRAM hiện tại: **`15 MiB / 12288 MiB`** `[ĐO]` vì `worker-guard` trên `pc-001` đã dừng lúc `2026-10-30 02:04:49–50` (`.demo/logs/worker.log`). **Nguyên nhân `[ĐO]` (`journalctl -k`): kernel OOM-killer** (`teamviewerd invoked oom-killer` lúc 02:04:49), không phải lỗi của worker-guard. `site-gateway` vẫn chạy và báo `pc-001` offline: **dịch vụ Qwen3-Coder gộp đang ngừng**.
- **RAM & Swap (`free -h`):**
  - Tổng RAM: `30 GiB` (`32.104 MB`). Đang dùng: **`24 GiB`** (chủ yếu do các tiến trình Chrome và Antigravity IDE trên máy trạm), `free = 1,7 GiB`, `buff/cache = 7,0 GiB`, **RAM khả dụng (`available`) = `6,0 GiB`** `[ĐO]`.
  - Swap: tổng `31 GiB`, đang dùng **`10 GiB`**, còn `21 GiB` `[ĐO]`.
- **Ổ đĩa (`df -h`):**
  - Ổ hệ thống `/` (`/dev/nvme0n1p5`): tổng `457 GiB`, đã dùng `422 GiB` (**`97%`**), còn trống **`12 GiB`** `[ĐO]`.
  - Ổ dữ liệu `/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d` (`/dev/nvme0n1p1`): tổng `358 GiB`, đã dùng `300 GiB` (**`88%`**), còn trống **`40 GiB`** `[ĐO]`.

### 2.2 Máy Windows `pc-win-01` (`192.168.1.49` / `10.42.0.225`)
- Theo dữ liệu probe đã xác thực tại [`benchmarks/reports/p0_probe_node_windows_rtx5070.json`](./p0_probe_node_windows_rtx5070.json) `[ĐO]`:
  - **Hệ điều hành:** `Microsoft Windows 10 Pro (Build 19041)` `[ĐO]`.
  - **CPU:** `AMD Ryzen 7 9700X 8-Core Processor` (`8 nhân vật lý / 16 luồng`), `avx2: true`, **`avx512f: true`**, `avx512_vnni: false`, `amx: false` `[ĐO]`.
  - **GPU:** `NVIDIA GeForce RTX 5070` (`12227 MiB` VRAM, Blackwell `sm_120`, Driver `617.42`) `[ĐO]`.
  - **RAM:** `64 GB` `[ĐO]`. **Đĩa:** file probe (ghi 2026-10-27) nêu ổ `C:\` trống `~399 GB`, **lỗi thời/không khớp**; đo lại 2026-10-30 qua SSH: ổ `C:` dung lượng `249,4 GB`, **trống `94,9 GB` (88,4 GiB)** `[ĐO]`.

---

## 3. Bảng Yêu Cầu So Với Máy & Cổng Quyết Định Pha 0 (Check Pass)

| Ứng viên / Đường thử nghiệm | Yêu cầu của KTransformers (`0.7.2`) `[ĐỌC MÃ]` | Thực tế máy chủ `[ĐO]` | Đạt / Không đạt | Lý do & Quyết định tại Cổng Pha 0 |
|---|---|---|---|---|
| **Ứng viên 1A — Linux `pc-001` (RTX 3060): Toàn trình `sglang-kt` + `--kt-method LLAMAFILE` + `--model Qwen/Qwen3-30B-A3B` (BF16)** | - Linux x86-64, GPU `sm_86`, CPU `avx2`<br>- Đĩa cho `--model` BF16: **`56,87 GiB`** + venv `~6 GiB` = **`~63 GiB`**<br>- RAM khả dụng: **`~18–22 GiB`** | - Ubuntu 24.04, RTX 3060 (`sm_86`), Core Ultra 7 265K (`avx2`)<br>- Đĩa trống ổ dữ liệu: **`40 GiB`**<br>- RAM khả dụng hiện tại: **`6,0 GiB`** | **KHÔNG ĐẠT (Hết đĩa & thiếu RAM khả dụng)** | **DỪNG ĐƯỜNG 1A:** Trọng số BF16 (`56,87 GiB`) lớn hơn toàn bộ dung lượng trống trên ổ dữ liệu (`40 GiB`). |
| **Ứng viên 1B — Linux `pc-001` (RTX 3060): Toàn trình `sglang-kt` + `--kt-method GPTQ_INT4` (`Qwen/Qwen3-30B-A3B-GPTQ-Int4`)** | - Linux x86-64, GPU `sm_86`, CPU `avx_vnni` (nhân `AVXVNNI256GPTQInt4Packed_MOE`)<br>- Đĩa: model **`15,77 GiB`** + venv `~6 GiB` = **`~21,8 GiB`**<br>- RAM khả dụng: **`~16–20 GiB`** | - Có `sm_86` và `avx_vnni`<br>- Đĩa trống: **`40 GiB`** (còn dư `~18 GiB` sau khi cài/tải)<br>- RAM khả dụng hiện tại: **`6,0 GiB`** (`24 GiB` đang bị Chrome/IDE chiếm) | **ĐẠT CÓ ĐIỀU KIỆN (Cần hỏi chủ dự án)** | **CHỜ CHỦ DỰ ÁN DUYỆT:** Vừa ổ đĩa `40 GiB`, nhưng (1) phải tải mới `16,93 GB` qua Wi-Fi (~7 phút đến vài giờ), (2) dùng lượng tử `GPTQ-Int4` (khác `GGUF Q4_K_M` của `llama.cpp`), và (3) cần giải phóng RAM trên `pc-001` lên `≥ 18 GiB` để tránh đảo trang Swap. |
| **Ứng viên 1C — Linux `pc-001` (RTX 3060): Pha 2A Micro-benchmark trực tiếp `kt-kernel==0.7.2` (`KTMoEWrapper` `LLAMAFILE`) trên file `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` có sẵn** | - Chỉ cài wheel `kt-kernel==0.7.2` (`101,5 MB`) + `gguf`<br>- Trọng số tải thêm: **`0 GB`** (dùng thẳng file `.gguf` `18 GB` có sẵn trên đĩa, `tp_count==1` alias `mmap`)<br>- Đo trực tiếp tốc độ 48 tầng MoE CPU (`LLAMAFILE` AVX2) ở `num_gpu_experts = 75, 59, 48, 32` | - File `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` đã có sẵn<br>- Không tốn băng thông tải model, tốn `< 1 GiB` đĩa nếu dùng chung PyTorch hoặc `~3 GiB` nếu tạo venv tối giản | **ĐẠT (Khuyến nghị chạy trước làm cổng lọc sớm)** | **KHUYẾN NGHỊ CHẠY TRƯỚC (Pha 1 + Pha 2A):** Trả lời ngay câu hỏi cốt lõi trên cùng file `Q4_K_M.gguf`: *Nhân CPU `LLAMAFILE` (AVX2) của `kt-kernel` trên `Core Ultra 7 265K` (`8P + 12E` hoặc `8P`) có nhanh hơn nhân GGML MoE CPU của `llama.cpp -ncmoe` hay không?* Nếu không thắng đủ ≥ 15%, có thể kết luận **DỪNG** ngay mà không cần tải 17 GB! |
| **Ứng viên 2 — Windows `pc-win-01` (RTX 5070 + Ryzen 9700X AVX-512)** | - Hệ điều hành: **Linux x86-64** (`manylinux_2_35_x86_64`)<br>- GPU: `sm_80/86/89/90` trong wheel dựng sẵn (`sm_120` phải tự biên dịch từ mã nguồn trên Linux/WSL2) | - Đang chạy **Windows 10 Pro gốc** (`AICFWorkerGuard` Windows service)<br>- GPU: **RTX 5070 (`sm_120` Blackwell)** | **KHÔNG ĐẠT trên môi trường hiện tại** | **DỪNG PHA 4 THEO QUY TẮC 7:** Không tự ý cài WSL2 hay đổi hệ điều hành trên `pc-win-01`. Ghi nhận "không hỗ trợ trên Windows gốc / wheel chưa có `sm_120`". |

---

## 4. Đề xuất thực thi tiếp theo (Xin ý kiến chủ dự án tại Cổng Pha 0)

Theo đúng quy định tại **Pha 0 Check Pass** và **Quy tắc 6** trong [`KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md`](../../../KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md), báo cáo kết quả khảo sát và xin chủ dự án xác nhận lựa chọn trước khi chạm vào GPU hoặc tải gói. **Điều kiện chung cho mọi phép đo:** RAM `available` ≥ 8 GiB (hiện `~4–6 GiB`), nên cần đóng bớt ứng dụng nặng hoặc chọn khung giờ; **không** tự đóng ứng dụng của chủ dự án.

1. **Phương án khuyến nghị (Nhanh, an toàn đĩa, `0 GB` tải trọng số mới — Thực hiện Pha 1 + Pha 2A Đường 1C):**
   - **Bước 1 (Pha 1):** Đo lại đường cơ sở `llama.cpp` (`.aicf-work/bin/llama-server`) với `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` trên RTX 3060 ở `-ncmoe 20, 26, 30, 36` (`-c 8192` và `-c 32768`, ≥5 lần/điểm) để có số đo tham chiếu trong cùng phiên.
   - **Bước 2 (Pha 2A — Cổng lọc sớm):** Cài `kt-kernel==0.7.2` vào `.aicf-work/ktransformers/venv`, nạp trực tiếp các tầng MoE từ chính file `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` bằng `KTMoEWrapper(backend="LLAMAFILE")` và đo tốc độ thực thi 48 tầng MoE CPU (`num_gpu_experts = 75, 59, 48, 32`; so sánh `20` luồng `8P+12E` với `8` luồng `8P`).
   - **Bước 3 (Khôi phục cụm):** `worker-guard` trên `pc-001` **đang tắt** từ `02:04:49` (OOM-killer). Hỏi chủ dự án có khôi phục dịch vụ Coder trước khi đo không; sau khi đo xong (~15–20 phút) bật lại guard theo `deploy/demo/coding_profile.env` rồi chạy `deploy/demo/apply_coder_merged.sh`.
2. **Phương án mở rộng (Thực hiện thêm Pha 2B Đường 1B — Toàn trình `sglang-kt` + `GPTQ_INT4` AVX-VNNI):**
   - Nếu chủ dự án muốn đo luôn máy chủ `sglang-kt` toàn trình với nhân `AVXVNNI256GPTQInt4Packed_MOE`, cần tải thêm kho `Qwen/Qwen3-30B-A3B-GPTQ-Int4` (**`16,93 GB` / `15,77 GiB`**) vào `.aicf-work/` và giải phóng bớt RAM trên `pc-001` (đóng bớt tab Chrome nặng để RAM khả dụng đạt `≥ 18 GiB`).

---

## 5. Giới hạn của phép so sánh sắp thực hiện (đọc trước khi diễn giải số đo)
- **Pha 2A chỉ đo nhân CPU riêng lẻ** (`KTMoEWrapper`), không phải tốc độ sinh chữ của cả hệ thống. Lợi thế hệ thống của KTransformers (đặt một phần chuyên gia lên GPU theo từng tầng, hoãn chuyên gia, chồng lấp CPU/GPU) chỉ lộ ra ở Pha 2B. Vì thế 2A chỉ có thể **loại sớm** khi nhân KT chậm hơn rõ rệt; "ngang hoặc nhanh hơn" chưa phải kết luận.
- **Ánh xạ `-ncmoe N` ⇔ `--kt-num-gpu-experts K`** (mục 1.2) là ánh xạ theo **tỷ lệ chuyên gia trên GPU**, không đảm bảo cùng VRAM hay cùng khối lượng tính trên CPU: llama.cpp đưa **cả 128 chuyên gia của N tầng** sang CPU, KT để **một phần chuyên gia ở mọi tầng** trên CPU. Phải chỉnh K sao cho **VRAM đỉnh khớp** từng điểm llama.cpp rồi mới so tốc độ.
- **2B dùng lượng tử khác** (GPTQ-Int4, không phải GGUF Q4_K_M): so sánh tốc độ chỉ công bằng nếu kèm kiểm chất lượng; không so như "cùng model cùng lượng tử".
- Số đo cũ của llama.cpp (44,0 / 35,9 / 65,6 tok/s…) chỉ để tham khảo; đường cơ sở mới (Pha 1) phải đo cùng phiên.

## 6. Rà soát bản chốt (2026-10-30): đã kiểm lại và chưa kiểm lại
**Đã kiểm lại bằng lệnh thật `[ĐO]`:** phiên bản `kt-kernel` `0.7.2` và `sglang-kt` `0.7.2`, kích thước wheel (101,5 / 101,4 MB), phụ thuộc `torch==2.9.1`, `flashinfer_python==0.6.3`, `cuda-python<13.3,>=13.1` (PyPI JSON); dung lượng `.safetensors` của các kho Hugging Face (BF16 61,07 GB, GPTQ-Int4 16,93 GB, FP8 32,45 GB, Coder BF16 61,07 GB, Coder FP8 31,18 GB; tổng riêng file `.safetensors`, chênh ≤ 0,02 GB so với bản nháp); RAM/đĩa/GPU `pc-001` và đĩa `C:` của `pc-win-01`; trạng thái dịch vụ và nguyên nhân OOM.
**Chưa kiểm lại (tin theo bản đọc mã của agent thực hiện Pha 0, nên đối chiếu khi chạy thật):** số dòng và nội dung các file nguồn trong wheel (`kt_ep_wrapper.py`, `moe.hpp`, `amx.py`, `CMakeLists.txt`); công thức `num_gpu_experts = K × số tầng MoE`; nhánh `mmap` alias của `LLAMAFILE`; sự có mặt của `doc/en/kt-kernel/AVX2-Tutorial.md`. **Cách kiểm khi chạy:** ghi nhật ký số chuyên gia thực sự nạp lên GPU/CPU và VRAM đỉnh cho từng K; nếu lệch công thức, sửa bảng mục 1.2.
