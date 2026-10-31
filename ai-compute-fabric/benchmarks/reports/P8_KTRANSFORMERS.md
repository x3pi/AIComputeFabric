# BÁO CÁO P8: KHẢO SÁT KHẢ THI & THỬ NGHIỆM KTRANSFORMERS (`kvcache-ai/ktransformers`)

- **Ngày thực hiện:** `2026-10-30`
- **Trạng thái hiện tại:** **Hoàn thành Pha 0, Pha 1 và Pha 2A (Đã kiểm chứng & sửa lỗi thiết kế micro-benchmark)** — Đã đo thực tế đường cơ sở `llama.cpp` (Pha 1 + `llama-bench` đa luồng) và vi mô nhân `kt-kernel==0.7.2` `LLAMAFILE` (AVX2) trên máy `pc-001`. Toàn bộ 5 điểm phản biện của báo cáo kiểm chứng độc lập đã được khắc phục và đo lại. Theo tiêu chí cổng Pha 2A tại Phần 3 của kế hoạch, chưa đủ điều kiện dừng sớm tự động ("KHÔNG ĐÁNG") nếu chưa qua Pha 2B hoặc quyết định của chủ dự án. Pha 4 (Windows/RTX 5070) tạm dừng do không hỗ trợ `sm_120`.

### Tóm tắt điều hành
- **Đã đo thực tế đầy đủ Pha 1 và Pha 2A (bản sửa chuẩn) `[ĐO]`**: Mọi điểm đo đều lặp lại $\ge 5$ lần, báo trung vị/min/max, kiểm soát chặt chẽ ranh giới VRAM đỉnh và RAM khả dụng (`18,6 – 23,3 GiB`), không phát sinh OOM.
- **Pha 1 (`llama.cpp` đo lại cùng phiên `[ĐO]`):**
  - Ngữ cảnh 8k (`llama-server`, mặc định `-t 4`): `-ncmoe 20` đạt **`42,16 tok/s`** (`23,72 ms/tok`, VRAM `11.733 MiB`), `-ncmoe 26` đạt **`37,43 tok/s`** (`26,72 ms/tok`, VRAM `9.683 MiB`), `-ncmoe 30` đạt **`33,50 tok/s`** (`29,85 ms/tok`, VRAM `8.291 MiB`), `-ncmoe 36` đạt **`31,29 tok/s`** (`31,96 ms/tok`, VRAM `6.251 MiB`), `-ncmoe 48` (toàn CPU) đạt **`25,74 tok/s`** (`38,85 ms/tok`, VRAM `1.969 MiB`).
  - Ngữ cảnh 32k (`llama-server`): `-ncmoe 30` đạt **`34,99 tok/s`** (VRAM `10.629 MiB`), prefill 24k đạt **`654,8 tok/s`** (TTFT `36,8 s`).
  - Mốc `llama-bench -ngl 99 -ncmoe 48` đa luồng `[ĐO]`: Khi ghim đúng 8 nhân P (`taskset -c 0-7 -t 8`), tốc độ giải mã toàn trình của `llama.cpp` tăng từ `26,54 tok/s` (`37,67 ms/tok` ở `-t 4`) lên **`34,53 tok/s` (`28,96 ms/tok`)** (`+30,1%`), và prefill `p=2048` đạt **`438,4 tok/s`** (`453,8 tok/s` ở `-t 20`). Phân rã hồi quy tuyến tính ($R^2 = 0,9966$) cho thấy riêng phần tính 48 tầng MoE trên CPU của `llama.cpp` tốn **`~29,4 ms/tok`** ở `-t 4` và **`~21,4 ms/tok`** khi ghim 8 nhân P (`taskset -c 0-7 -t 8`) `[SUY LUẬN]`.
- **Pha 2A (`kt-kernel==0.7.2` `LLAMAFILE` AVX2 vi mô 48 tầng — đã sửa lỗi bốc `topk_ids` và singleton luồng `[ĐO]`):**
  - Đã khắc phục triệt để 2 lỗi của lần đo đầu: (1) bốc `topk_ids` không hoàn lại (`torch.topk`) độc lập cho từng tầng và từng bước giải mã (`16 bước × 48 tầng = 768` lượt/lần lặp), ghi nhận chính xác `actual_cpu_experts_per_tok` khớp kỳ vọng lý thuyết $8 \times (128-K)/128$; (2) chạy mỗi cấu hình `threads` trong tiến trình con riêng và cô lập luồng điều phối (`python3`, `cuda-EvtHandlr`) khỏi các nhân `WorkerPool` spin-wait 100% CPU.
  - Ở `threads=8` (`worker_cores=0-7`, `def=0`), thời gian chạy **riêng phần 48 tầng MoE trên CPU** khi decode (`qlen=1`) tăng tuyến tính theo số chuyên gia CPU `[ĐO]`: **`16,29 ms`** ($K=75$, hit `3,23/8`), **`19,08 ms`** ($K=59$, hit `4,25/8`), **`18,82 ms`** ($K=48$, hit `4,90/8`), **`21,67 ms`** ($K=32$, hit `6,00/8`), và **`28,43 ms`** ($K=0$, hit `8,00/8`).
  - Ở `threads=18` (`8P + 10E`, chừa 2 nhân E cho host/CUDA), prefill `qlen=2048` ($K=0$) tăng lên **`409,5 tok/s`** (`2,442 ms/tok`, so với `301,8 tok/s` ở `th=8`), nhưng decode ở tải hỗn hợp ($K=75, 48$) chậm hơn `th=8` (`19,25 ms` và `25,74 ms`) do độ lệch rào chắn đồng bộ giữa nhân P và E `[ĐO]`. Đặc biệt, ở `threads=20` (chiếm toàn bộ 20 nhân), các luồng `WorkerPool` spin-wait làm đói CPU của luồng callback CUDA (`cuda-EvtHandlr`), khiến decode vọt lên **`551–575 ms/tok`** `[ĐO]`.
  - Ở prefill thuần MoE CPU ($K=0$), `kt-kernel` đạt **`297,5–301,8 tok/s`** (`th=8`) và **`397,5–409,5 tok/s`** (`th=18`), **chậm hơn `10%–31%`** so với prefill toàn trình của `llama.cpp` (`422,6–453,8 tok/s`) `[ĐO]`.
- **Đánh giá Cổng Pha 2A (Phần 3 Kế hoạch):** So với `llama.cpp` mặc định `-t 4` (`~29,4 ms` CPU MoE ở $N=48$), nhân `kt-kernel` ($K=0$: `28,43 ms` ở `th=8`, `26,14 ms` ở `th=18`) ngang ngửa hoặc nhanh hơn nhẹ (`+3%` đến `+11%`), và khi bật `max_deferred=2..4` ở $K=75$ thời gian chờ nhân CPU giảm xuống `10,99–10,13 ms` `[ĐO]`. Do đó điều kiện dừng sớm tại Pha 2A (*"nhân KT chậm hơn rõ rệt $\ge 15\%$ ở decode VÀ không nhanh hơn ở prefill"*) **không thỏa mãn**, không được tự ý kết luận "KHÔNG ĐÁNG" cho toàn hệ thống nếu chưa hỏi chủ dự án về Pha 2B.
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

---

## 7. Kết quả đo thực tế Pha 1 — Đường cơ sở `llama.cpp` trên RTX 3060 (Đo lại cùng phiên 2026-10-30) `[ĐO]`

- **Điều kiện đo thực tế `[ĐO]`:**
  - Máy chủ: Linux `pc-001` (`Intel Core Ultra 7 265K`, RTX 3060 12GB, Driver `580.178.04`, CUDA 13.0).
  - RAM khả dụng trước khi chạy: **`20.255 MiB` (~20,2 GiB)** `[ĐO]` (sau khi tắt Chrome, vượt xa ngưỡng tối thiểu 8 GiB).
  - Mô hình: `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` (`18.550.716.416 bytes`, SHA256 `0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5` `[ĐO]`).
  - Lệnh máy chủ: `.aicf-work/bin/llama-server -m <model> -c <ctx> -np 1 -ngl 99 -fa on --no-warmup --fit off --port 18199 -lv 3 [-ncmoe N]`.
  - Phương pháp đo: Mỗi điểm đo lặp lại **$\ge 5$ lần** (prompt ngắn 128 token sinh, `cache_prompt: false`, `temperature: 0`, `seed: 42+i`). Đo kiểm prompt dài 1k, 8k, 24k token bằng sinh văn bản cảm biến ngẫu nhiên không trùng lặp. VRAM đỉnh được lấy mẫu liên tục mỗi 0,2s bằng luồng nền `nvidia-smi`.
  - Dữ liệu thô: `ai-compute-fabric/benchmarks/reports/p8_ktransformers_p1_baseline.jsonl` và các file log chi tiết tại `ai-compute-fabric/benchmarks/reports/logs/p8_p1_*.log`.

### 7.1 Bảng số liệu đo quét `-ncmoe N` (Prompt ngắn 128 token) `[ĐO]`

| Cấu hình (`tag`) | Ngữ cảnh (`ctx`) | `-ncmoe N` (Số tầng MoE trên CPU) | VRAM đỉnh (`MiB`) `[ĐO]` | RAM khả dụng tối thiểu (`MiB`) `[ĐO]` | Giải mã Trung vị (`tok/s`) `[ĐO]` | Khoảng Giải mã Min – Max (`tok/s`) `[ĐO]` | Prefill ngắn (`tok/s`) `[ĐO]` | TTFT ngắn (`ms`) `[ĐO]` | Trạng thái / OOM `[ĐO]` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| `c8k_N20` | 8.192 | 20 | **`11.733`** | 20.438 | **`42,16`** | 32,23 – 43,56 | 70,12 | 599,0 | Healthy (Khớp P3 cũ `44,0`) |
| `c8k_N26` | 8.192 | 26 | **`9.683`** | 20.370 | **`37,43`** | 31,93 – 38,62 | 73,20 | 573,8 | Healthy |
| `c8k_N30` | 8.192 | 30 | **`8.291`** | 20.354 | **`33,50`** | 32,86 – 34,12 | 66,66 | 630,1 | Healthy |
| `c8k_N36` | 8.192 | 36 | **`6.251`** | 20.347 | **`31,29`** | 30,57 – 31,92 | 69,10 | 607,8 | Healthy |
| `c8k_N48_all_cpu` | 8.192 | 48 | **`1.969`** | 20.341 | **`25,74`** | 25,37 – 27,82 | 49,33 | 851,5 | Healthy (Toàn bộ MoE CPU) |
| `c8k_N0_pure_gpu` | 8.192 | 0 | 127 | 20.340 | — | — | — | — | **OOM** (Vượt 12 GB VRAM, rc=1) |
| `c32k_N30` | 32.768 | 30 | **`10.629`** | 20.334 | **`34,99`** | 33,29 – 36,15 | 76,87 | 546,4 | Healthy (Khớp P3 cũ `35,9`) |
| `c32k_N36` | 32.768 | 36 | **`8.579`** | 20.332 | **`32,58`** | 31,23 – 35,02 | 67,91 | 618,4 | Healthy |
| `c32k_N48_all_cpu` | 32.768 | 48 | **`4.297`** | 20.329 | **`27,58`** | 24,05 – 28,65 | 48,70 | 862,5 | Healthy |
| `c32k_N24_oom_probe`| 32.768 | 24 | 9.283 | 20.325 | — | — | — | — | **OOM** (Đúng như P3 ghi nhận) |

### 7.2 Bảng số liệu đo Prompt dài (1k, 8k, 24k token, 64 token sinh, 5 lần lặp/điểm) `[ĐO]`

| Cấu hình | Độ dài prompt mục tiêu | Số token thực tế (`prompt_n`) | Prefill Trung vị (`tok/s`) `[ĐO]` | Khoảng Prefill Min – Max (`tok/s`) `[ĐO]` | Giải mã Trung vị (`tok/s`) `[ĐO]` | TTFT Trung vị (`ms`) `[ĐO]` | Thời gian chờ TTFT thực tế |
|---|---:|---:|---:|---:|---:|---:|---:|
| `c8k_N20` (`-c 8192`) | ~1.000 | 1.033 | **`718,7`** | 523,4 – 747,1 | 42,77 | 1.437,2 | ~1,4 s |
| `c8k_N20` (`-c 8192`) | ~8.000 | 8.055 | **`886,7`** | 881,3 – 900,7 | 40,10 | 9.084,0 | ~9,1 s |
| `c32k_N30` (`-c 32768`) | ~1.000 | 1.055 | **`551,4`** | 535,0 – 576,7 | 36,63 | 1.913,3 | ~1,9 s |
| `c32k_N30` (`-c 32768`) | ~8.000 | 8.042 | **`709,2`** | 696,6 – 710,9 | 32,77 | 11.339,5 | ~11,3 s |
| `c32k_N30` (`-c 32768`) | ~24.000 | 24.068 | **`654,8`** | 652,4 – 655,9 | 27,41 | 36.759,1 | **~36,8 s** |

### 7.3 Mốc đối chiếu `llama-bench -ngl 99 -ncmoe 48` đa luồng & Phân rã thời gian MoE CPU `[ĐO]` + `[SUY LUẬN]`
Để đối chiếu công bằng ở cùng số luồng CPU với `kt-kernel` (Bước 2A.4), chúng tôi đo `llama-bench -m .aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf -ngl 99 -ncmoe 48 -p 512,2048 -n 128 -r 5` ở `-t 4` (mặc định), `-t 8` (tự do), `taskset -c 0-7 -t 8` (ghim đúng 8 nhân P `CPU 0..7` tương đương `WorkerPool(8)` của `kt-kernel`), và `-t 20` (toàn bộ 20 nhân `8P + 12E`). Dữ liệu thô lưu tại [`p8_ktransformers_p2a4_llamabench.json`](./p8_ktransformers_p2a4_llamabench.json) và [`p8_ktransformers_p2a4_llamabench_t8_pcores.json`](./p8_ktransformers_p2a4_llamabench_t8_pcores.json):

| Cấu hình luồng `llama-bench` (`-ngl 99 -ncmoe 48`) | Decode `n=128` Trung vị (`tok/s`) `[ĐO]` | Decode Khoảng Min – Max (`tok/s`) `[ĐO]` | Tổng thời gian 1 bước Decode toàn trình (`ms/tok`) `[ĐO]` | Ước tính riêng 48 tầng MoE CPU (`ms/tok`) `[SUY LUẬN]` | Prefill `p=512` Trung vị (`tok/s` / `ms/tok`) `[ĐO]` | Prefill `p=2048` Trung vị (`tok/s` / `ms/tok`) `[ĐO]` |
|---|---:|---:|---:|---:|---:|---:|
| `-t 4` (mặc định `llama.cpp`) | **`26,48`** (TB `26,54`) | 26,23 – 26,70 | **`37,76 ms`** | **`~29,4 – 30,1 ms`** | **`416,5`** (`2,40 ms`) | **`424,7`** (`2,35 ms`) |
| `-t 8` (không ghim nhân) | **`29,39`** (TB `29,72`) | 29,21 – 30,90 | **`34,02 ms`** | **`~25,7 – 26,4 ms`** | **`397,3`** (`2,52 ms`) | **`441,5`** (`2,27 ms`) |
| **`taskset -c 0-7 -t 8` (ghim 8 nhân P `0..7`)** | **`34,53`** (TB `34,54`) | **33,83 – 35,00** | **`28,96 ms`** | **`~20,7 – 21,4 ms`** | **`422,6`** (`2,37 ms`) | **`438,4`** (`2,28 ms`) |
| `-t 20` (toàn bộ 20 nhân `8P + 12E`) | **`27,13`** (TB `27,18`) | 26,75 – 27,82 | **`36,86 ms`** | **`~28,6 – 29,3 ms`** | **`446,0`** (`2,24 ms`) | **`458,2`** (`2,18 ms`) |

- **Cơ sở phân rã thời gian GPU vs CPU MoE trong `llama.cpp` `[SUY LUẬN]` từ số liệu `[ĐO]`:**
  - Hồi quy tuyến tính tổng thời gian giải mã 1 token (`ms/tok`) theo số tầng MoE trên CPU ($N \in \{20, 26, 30, 36, 48\}$) từ Bảng 7.1 (`-c 8192`, mặc định `-t 4`):
    $$\text{Thời gian 1 bước giải mã (ms/tok)} = 13,04 + 0,5369 \times N \quad (R^2 = 0,9966)$$
  - Trong đó, mỗi tầng MoE chuyển từ GPU sang CPU làm tăng thêm `0,5369 ms/tok`. Do 1 tầng MoE chạy trên RTX 3060 (băng thông `360 GB/s`, 8 chuyên gia kích hoạt $\approx 22,5\text{ MB}$/tầng) tốn `~0,075 ms/tầng` (`~3,6 ms` cho 48 tầng), mỗi tầng MoE trên CPU ở `-t 4` tốn $0,5369 + 0,075 \approx \mathbf{0,612\text{ ms/tầng}}$ (tức **`~29,4 ms`** cho 48 tầng ở $N=48$, và **`~12,2 ms`** cho 20 tầng ở $N=20$) `[SUY LUẬN]`.
  - Phần không phải chuyên gia trên GPU (48 tầng Attention + Router + RMSNorm + LM Head + đồng bộ PCIe) tốn $13,04 - 3,6 \approx \mathbf{9,4\text{ ms/tok}}$ trên `llama-server` (`-c 8192`) và khoảng **`~7,6 – 8,3 ms/tok`** trên `llama-bench` (`n_depth=0`) `[SUY LUẬN]`.
  - **Phát hiện quan trọng cho chính `llama.cpp` `[ĐO]`:** Mặc định `llama.cpp` trên Core Ultra 7 265K chỉ chạy `-t 4` hoặc để Linux xếp lịch tự do qua cả nhân E khiến decode ở $N=48$ chỉ đạt `26,48–29,39 tok/s`. Chỉ cần ghim 8 luồng vào đúng 8 nhân P (`taskset -c 0-7 -t 8`), tốc độ decode toàn trình của `llama.cpp` ở $N=48$ tăng vọt lên **`34,53 tok/s` (`28,96 ms/tok`)** — tức **toàn bộ bước giải mã (gồm cả GPU Attention + 48 tầng CPU MoE) của `llama.cpp` chỉ mất `28,96 ms/tok`**, trong đó riêng 48 tầng CPU MoE chỉ tốn **`~20,7 – 21,4 ms/tok`** `[SUY LUẬN]`.

---

## 8. Kết quả đo thực tế Pha 2A (Bản đã sửa & kiểm chứng) — Đo vi mô nhân `kt-kernel==0.7.2` `LLAMAFILE` (AVX2) `[ĐO]`

### 8.1 Ghi nhận & khắc phục 2 lỗi thiết kế micro-benchmark từ phản biện độc lập `[ĐỌC MÃ]` + `[ĐO]`
Qua kiểm chứng chéo kết quả chạy lần đầu, chúng tôi xác nhận và đã sửa triệt để 2 lỗi trong [`ai-compute-fabric/benchmarks/hardware/p8_phase2a_runner.py`](../hardware/p8_phase2a_runner.py):
1. **Lỗi 1 — Bốc `topk_ids` một lần có hoàn lại trong lần chạy đầu:**
   - *Nguyên nhân:* Lần chạy đầu tạo `topk_ids = torch.randint(0, 128, (1, 8))` một lần duy nhất (với `seed=42` ra `[[39, 57, 92, 60, 18, 57, 66, 64]]`) rồi dùng lại cho cả 48 tầng và 5 lần lặp. Với $K=75$, chỉ có **1/8** chuyên gia (`92`) rơi vào vùng CPU ($\ge 75$), thấp hơn nhiều so với kỳ vọng lý thuyết $8 \times (128-75)/128 = 3,312$ chuyên gia/token, làm thời gian $K=75$ bị tụt giả tạo xuống `5,70 ms`.
   - *Khắc phục `[ĐO]`:* Sinh trước bể định tuyến 256 lát cắt độc lập bằng `torch.topk` **không hoàn lại** (8 chuyên gia phân biệt trên mỗi token). Ở chế độ decode (`qlen=1`), mỗi lần lặp đo `decode_steps=16` token liên tiếp qua toàn bộ 48 tầng (`768` lượt gọi tầng = `6.144` lượt chuyên gia mỗi lần lặp, mỗi tầng/bước lấy một lát cắt định tuyến khác nhau) rồi chuẩn hóa về `ms / token` (48 tầng). Nhật ký JSONL ghi rõ cả `expected_cpu_experts_per_tok` và `actual_cpu_experts_per_tok` (chênh lệch thực tế $< 0,09$ chuyên gia/token).
2. **Lỗi 2 — Singleton `_MoEBase._cpu_infer_instance` và hiện tượng đói CPU của luồng callback CUDA (`cuda-EvtHandlr`):**
   - *Nguyên nhân `[ĐỌC MÃ]` + `[ĐO]`:* Trong `kt_kernel/experts_base.py` (dòng 297), `_cpu_infer_instance` là biến singleton toàn tiến trình nên lần đo đầu chạy `th=20` chung tiến trình sau `th=8` thực chất vẫn dùng `WorkerPool(8)`. Khi tách tiến trình con riêng và kiểm tra `/proc/self/task/*/status` `[ĐO]`, `CPUInfer` tự động ghim từng luồng `numa_0_m_0`, `numa_0_t_1..th-1` vào đúng từng nhân `CPU 0 .. th-1` và quay vòng bận (busy-spin 100% CPU). Nếu đặt `threads=20` trên CPU 20 nhân (`0..19`) hoặc dùng `taskset -c 0-7` ép cả tiến trình vào 8 nhân `0..7`, các luồng điều phối `python3` và `cuda-EvtHandlr` (dùng bởi `submit_with_cuda_stream` / `sync_with_cuda_stream`) không còn nhân trống, bị bộ lập lịch Linux làm trễ `~12 ms` mỗi tầng ($\approx 550\text{–}630\text{ ms}$ cho 48 tầng!). Ngoài ra, việc khởi tạo lại 48 đối tượng `LlamafileMoEWrapper` 11 lần liên tiếp trong cùng tiến trình gây rò rỉ bộ nhớ khóa trang (`NV_ERR_NO_MEMORY`).
   - *Khắc phục `[ĐO]`:*
     - Chạy mỗi cấu hình `threads` (`8`, `18`, `20`) trong một tiến trình con độc lập.
     - Trong mỗi tiến trình con, chỉ khởi tạo và `load_weights()` 48 tầng **đúng 1 lần** (`3,24 – 3,46 giây`), sau đó cập nhật trực tiếp mặt nạ bộ nhớ chia sẻ `l.gpu_experts_mask` (`uint8_t*` dùng chung giữa Python và C++ `MOE`, dòng 428 `experts_base.py`) và `l.max_deferred_experts_per_token`. Không phát sinh bất kỳ lỗi `NV_ERR_NO_MEMORY` hay OOM nào.
     - Khi `threads < 20` (`th=8` và `th=18`), cô lập các luồng điều phối (`python3`, `cuda-EvtHandlr`) sang các nhân còn lại (`CPU 8..19` cho `th=8`, `CPU 18..19` cho `th=18`) để không tranh chấp nhân với các luồng `WorkerPool` spin-wait (`CPU 0..th-1`).

### 8.2 Bảng số liệu vi mô 48 tầng MoE trên CPU (`kt-kernel==0.7.2` `LLAMAFILE`, đã sửa chuẩn, 5 lần lặp/điểm) `[ĐO]`
*(Lưu ý quan trọng: Các con số dưới đây chỉ đo **riêng thời gian thực thi 48 tầng MoE trên CPU** qua `LlamafileMoEWrapper.forward`, **chưa** bao gồm thời gian chạy $K$ chuyên gia trên GPU, Attention, Router, KV cache hay LM Head. Cột "Trần tok/s chỉ tính CPU MoE" = $qlen \times 1000 / \text{ms}$ chỉ là chặn trên lý thuyết của riêng phần CPU MoE, **không phải** tốc độ toàn hệ thống.)*

Dữ liệu thô: [`p8_ktransformers_p2a_microbench.jsonl`](./p8_ktransformers_p2a_microbench.jsonl) và [`logs/p8_p2a_microbench.log`](./logs/p8_p2a_microbench.log).

| Cấu hình (`tag`) | Luồng `WorkerPool` (`worker_cores` / `host_cores`) | $K$ (GPU) / CPU exp mỗi tầng | Chuyên gia CPU / tok: Kỳ vọng vs Thực tế `[ĐO]` | Hoãn (`def`) | Decode `qlen=1`: Trung vị 48 tầng CPU MoE (`ms/tok`) `[ĐO]` | Decode `qlen=1`: Min – Max (`ms/tok`) `[ĐO]` | Trần `tok/s` riêng CPU MoE (`qlen=1`) `[ĐO]` | Prefill `qlen=512`: Trung vị (`ms/tok` / `tok/s`) `[ĐO]` | Prefill `qlen=2048`: Trung vị (`ms/tok` / `tok/s`) `[ĐO]` |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| `th8_K75_def0` | `8` (`0-7` / `8-19`) | `75` / `53` | `3,31` / **`3,23`** | 0 | **`16,29 ms`** | 14,49 – 22,74 | 61,4 | `1,457 ms` (`686,2`) | `1,392 ms` (`718,6`) |
| `th8_K59_def0` | `8` (`0-7` / `8-19`) | `59` / `69` | `4,31` / **`4,25`** | 0 | **`19,08 ms`** | 16,07 – 19,40 | 52,4 | `1,851 ms` (`540,2`) | `1,835 ms` (`544,9`) |
| `th8_K48_def0` | `8` (`0-7` / `8-19`) | `48` / `80` | `5,00` / **`4,90`** | 0 | **`18,82 ms`** | 15,46 – 19,33 | 53,1 | `2,070 ms` (`483,1`) | `2,094 ms` (`477,6`) |
| `th8_K32_def0` | `8` (`0-7` / `8-19`) | `32` / `96` | `6,00` / **`6,00`** | 0 | **`21,67 ms`** | 18,55 – 21,95 | 46,2 | `2,503 ms` (`399,6`) | `2,475 ms` (`404,1`) |
| `th8_K0_def0` | `8` (`0-7` / `8-19`) | `0` / `128` | `8,00` / **`8,00`** | 0 | **`28,43 ms`** | 27,63 – 30,06 | 35,2 | `3,361 ms` (`297,5`) | `3,313 ms` (`301,8`) |
| `th18_K75_def0` | `18` (`0-17` / `18-19`) | `75` / `53` | `3,31` / **`3,43`** | 0 | **`19,25 ms`** | 16,89 – 31,45 | 52,0 | `1,268 ms` (`788,6`) | `1,132 ms` (`883,5`) |
| `th18_K48_def0` | `18` (`0-17` / `18-19`) | `48` / `80` | `5,00` / **`4,99`** | 0 | **`25,74 ms`** | 21,58 – 33,30 | 38,8 | `2,104 ms` (`475,3`) | `1,553 ms` (`644,1`) |
| `th18_K0_def0` | `18` (`0-17` / `18-19`) | `0` / `128` | `8,00` / **`8,00`** | 0 | **`26,14 ms`** | 23,79 – 51,39 | 38,3 | `2,516 ms` (`397,5`) | `2,442 ms` (`409,5`) |
| `th20_K75_def0` | `20` (`0-19` / quá tải) | `75` / `53` | `3,31` / **`3,36`** | 0 | **`569,06 ms`** | 473,62 – 674,18 | 1,8 | `2,613 ms` (`382,7`) | `1,480 ms` (`675,6`) |
| `th20_K48_def0` | `20` (`0-19` / quá tải) | `48` / `80` | `5,00` / **`5,05`** | 0 | **`551,00 ms`** | 492,06 – 609,06 | 1,8 | `2,848 ms` (`351,2`) | `1,819 ms` (`549,8`) |
| `th20_K0_def0` | `20` (`0-19` / quá tải) | `0` / `128` | `8,00` / **`8,00`** | 0 | **`574,75 ms`** | 547,00 – 626,69 | 1,7 | `3,909 ms` (`255,8`) | `2,911 ms` (`343,5`) |

### 8.3 Tác động của cơ chế hoãn chuyên gia (`max_deferred_experts_per_token`, 8 luồng P-core) `[ĐO]` + `[ĐỌC MÃ]`

| Cấu hình (`tag`) | $K$ (GPU) | Hoãn (`def`) | Chuyên gia xử lý tức thời / hoãn sang tầng $l+1$ | Decode `qlen=1`: Trung vị 48 tầng (`ms/tok`) `[ĐO]` | Decode `qlen=1`: Min – Max (`ms/tok`) `[ĐO]` | Prefill `qlen=2048` (`ms/tok` / `tok/s`) `[ĐO]` | Hiệu ứng so với `def=0` & Lưu ý kỹ thuật |
|---|---:|---:|---|---:|---:|---:|---|
| `th8_K75_def0` | 75 | 0 | 8 tức thời / 0 hoãn | **`16,29 ms`** | 14,49 – 22,74 | `1,392 ms` (`718,6`) | Chuẩn đồng bộ (toàn bộ chuyên gia CPU tính xong trong tầng $l$) |
| `th8_K75_def1` | 75 | 1 | 7 cao nhất / 1 thấp nhất | **`12,51 ms`** | 11,64 – 24,99 | `1,576 ms` (`634,4`) | Giảm `23,2%` thời gian chờ đồng bộ ở `qlen=1` |
| `th8_K75_def2` | 75 | 2 | 6 cao nhất / 2 thấp nhất | **`10,99 ms`** | 10,98 – 11,66 | `1,452 ms` (`688,6`) | Giảm `32,5%` thời gian chờ đồng bộ ở `qlen=1` |
| `th8_K75_def4` | 75 | 4 | 4 cao nhất / 4 thấp nhất | **`10,13 ms`** | 9,89 – 10,33 | `1,451 ms` (`689,4`) | Giảm `37,8%` thời gian chờ ở `qlen=1` |
| `th8_K48_def0` | 48 | 0 | 8 tức thời / 0 hoãn | **`18,82 ms`** | 15,46 – 19,33 | `2,094 ms` (`477,6`) | Chuẩn đồng bộ |
| `th8_K48_def1` | 48 | 1 | 7 cao nhất / 1 thấp nhất | **`16,54 ms`** | 15,75 – 28,83 | `2,155 ms` (`464,0`) | Giảm `12,1%` thời gian chờ ở `qlen=1` |
| `th8_K48_def2` | 48 | 2 | 6 cao nhất / 2 thấp nhất | **`18,34 ms`** | 17,76 – 18,69 | `2,175 ms` (`459,7`) | Giảm `2,6%` thời gian chờ ở `qlen=1` |
| `th8_K48_def4` | 48 | 4 | 4 cao nhất / 4 thấp nhất | **`13,61 ms`** | 13,23 – 15,83 | `2,136 ms` (`468,1`) | Giảm `27,7%` thời gian chờ ở `qlen=1` |

- **Giải thích cơ chế `max_deferred_experts_per_token` `[ĐỌC MÃ]` (`experts_base.py:492-520, 767-878`):**
  - Khi `def > 0`, hàm `select_deferred_experts` giữ lại `8 - def` chuyên gia có trọng số định tuyến (`topk_weights`) lớn nhất làm `immediate_task` và đẩy `def` chuyên gia trọng số nhỏ nhất sang `deferred_task`.
  - Hàm `sync_forward` gọi `sync_with_cuda_stream(cuda_stream, allow_pending=1)` — tức chỉ đợi `immediate_task` hoàn tất rồi trả kết quả ngay cho tầng hiện tại $l$, trong khi `deferred_task` tiếp tục chạy ngầm trên `WorkerPool` và được cộng dồn (`incremental = True`) vào đầu ra MoE của tầng kế tiếp $l+1$.
  - **Hệ quả đối với đánh giá công bằng:** Vì đầu ra của `def` chuyên gia ở tầng $l$ bị cộng trễ vào tầng $l+1$, đây là phép **xấp xỉ thay đổi đồ thị tính toán của mô hình** (không bảo toàn toán học như `def=0`). Do đó ở Pha 2A chưa thể dùng số đo `def > 0` để kết luận tương đương chất lượng nếu chưa kiểm tra chất lượng sinh chữ thực tế ở Pha 2B.

---

## 9. Pha 3 — Bảng so sánh có kiểm soát & Kiểm đột biến phép đo `[ĐO]`

### 9.1 Kiểm đột biến phép đo (Mutation Check) — **ĐẠT** `[ĐO]`
1. **Đột biến tham số `-ncmoe N` phía `llama.cpp`:** Khi tăng $N$ từ $20 \rightarrow 26 \rightarrow 30 \rightarrow 36 \rightarrow 48$ (`-c 8192`), VRAM đỉnh giảm đơn điệu ($11.733 \rightarrow 9.683 \rightarrow 8.291 \rightarrow 6.251 \rightarrow 1.969\text{ MiB}$) và tốc độ giải mã giảm đơn điệu ($42,16 \rightarrow 37,43 \rightarrow 33,50 \rightarrow 31,29 \rightarrow 25,74\text{ tok/s}$). Khi đặt $N=0$ (`-c 8192`) và $N=24$ (`-c 32768`), máy chủ báo lỗi OOM đúng như kỳ vọng.
2. **Đột biến tham số $K$ (`gpu_experts_mask`) phía `kt-kernel`:**
   - Ở prefill `qlen=2048` (`th=8`, `def=0`), khi giảm $K$ từ $75 \rightarrow 59 \rightarrow 48 \rightarrow 32 \rightarrow 0$ (tăng tỷ lệ chuyên gia CPU từ $41,4\% \rightarrow 53,9\% \rightarrow 62,5\% \rightarrow 75,0\% \rightarrow 100\%$), thời gian 48 tầng CPU MoE tăng tuyến tính nghiêm ngặt: **`2.849,9 ms` $\rightarrow$ `3.758,8 ms` $\rightarrow$ `4.288,1 ms` $\rightarrow$ `5.068,5 ms` $\rightarrow$ `6.785,5 ms`** ($R^2 = 0,9998$ với số chuyên gia CPU thực tế `actual_cpu_experts_per_tok`) `[ĐO]`.
   - Ở decode `qlen=1` (`th=8`, `def=0`), thời gian 48 tầng CPU MoE tăng theo đúng xu hướng tải CPU: **`16,29 ms`** ($K=75$, hit `3,23/8`) $\rightarrow$ **`19,08 ms`** ($K=59$, hit `4,25/8`) $\rightarrow$ **`18,82 ms`** ($K=48$, hit `4,90/8`, dao động nhẹ trong dải min–max `15,46–19,33 ms`) $\rightarrow$ **`21,67 ms`** ($K=32$, hit `6,00/8`) $\rightarrow$ **`28,43 ms`** ($K=0$, hit `8,00/8`) `[ĐO]`.
   - Khi kiểm thử $K=128$ (toàn bộ chuyên gia trên GPU), chuẩn đầu ra CPU MoE bằng `0.0000` và thời gian vòng lặp chỉ còn `2,43 ms` (chi phí khung gọi hàm) `[ĐO]`.
3. **Đột biến số luồng & phân bổ nhân (`th=8` vs `th=18` vs `th=20`):**
   - Tăng luồng từ `8` (8 nhân P) lên `18` (`8P + 10E`, chừa 2 nhân E cho host/CUDA) làm tăng thông lượng prefill `qlen=2048` ($K=0$) từ `301,8 tok/s` lên **`409,5 tok/s` (`+35,7%`)**, nhưng khi tăng lên `20` luồng (chiếm hết 20 nhân, `0` nhân cho host/CUDA), decode `qlen=1` bị nghẽn lập lịch callback lên **`551–575 ms/tok`** `[ĐO]`.

### 9.2 Bảng đối chiếu tách bạch: (A) Toàn trình `llama.cpp` vs (B) Riêng nhân CPU MoE (`llama.cpp` vs `kt-kernel LLAMAFILE`) `[ĐO]` + `[SUY LUẬN]`

> **Nguyên tắc đọc bảng:** Không ghép trực tiếp thời gian chạy riêng nhân CPU MoE của `kt-kernel` (`ms/tok`) với tốc độ toàn hệ thống (`tok/s`) của `llama.cpp`. Bảng dưới tách riêng **toàn trình `llama.cpp` (`[ĐO]`)**, **phần MoE CPU ước tính của `llama.cpp` (`[SUY LUẬN]` từ hồi quy Bảng 7.1 & 7.3)** và **phần MoE CPU đo trực tiếp của `kt-kernel` (`[ĐO]`)**.

| Điểm cấu hình (Ánh xạ theo tỷ lệ chuyên gia) | `llama.cpp` Toàn trình: Tổng thời gian / Tốc độ (`[ĐO]`) | `llama.cpp` Riêng phần MoE trên CPU (`[SUY LUẬN]` từ `[ĐO]`) | `kt-kernel LLAMAFILE` Riêng phần MoE trên CPU (`th=8`, `def=0`) `[ĐO]` | `kt-kernel LLAMAFILE` Riêng phần MoE trên CPU (`th=18`, `def=0`) `[ĐO]` | `kt-kernel LLAMAFILE` Có hoãn chuyên gia (`th=8`, `def=2..4`) `[ĐO]` | So sánh riêng phần nhân CPU MoE (`kt-kernel def=0` so với `llama.cpp`) |
|---|---|---|---|---|---|---|
| **Toàn bộ MoE trên CPU — Decode**<br>(`llama.cpp -ncmoe 48` vs `KT K=0`, 8/8 chuyên gia CPU) | • `-t 4` (`llama-server`): `38,85 ms` (`25,74 tok/s`)<br>• `-t 4` (`llama-bench`): `37,76 ms` (`26,48 tok/s`)<br>• `-t 8` tự do: `34,02 ms` (`29,39 tok/s`)<br>• **`taskset 0-7 -t 8`: `28,96 ms` (`34,53 tok/s`)** | • Ở `-t 4`: **`~29,4 ms/tok`**<br>• Ở `-t 8` tự do: **`~26,0 ms/tok`**<br>• Ở `taskset 0-7 -t 8`: **`~21,4 ms/tok`** | **`28,43 ms/tok`**<br>(min `27,63`, max `30,06`) | **`26,14 ms/tok`**<br>(min `23,79`, max `51,39`) | Không áp dụng (tất cả chuyên gia đều trên CPU) | • So với `-t 4` (`~29,4 ms`): KT nhanh hơn nhẹ **`~3%–11%`**.<br>• So với cùng 8 nhân P `taskset 0-7 -t 8` (`~21,4 ms`): **KT chậm hơn `~25%` (`28,43 ms` vs `~21,4 ms`)**. |
| **Tải hỗn hợp ~58,5% GPU / ~41,5% CPU — Decode**<br>(`llama.cpp -ncmoe 20` vs `KT K=75`, ~3,31/8 chuyên gia CPU) | • `-t 4` (`llama-server c8k_N20`, VRAM `11.733 MiB`): **`23,72 ms/tok` (`42,16 tok/s`)** | • Ở `-t 4` (20 tầng CPU × `0,612 ms`): **`~12,2 ms/tok`** | **`16,29 ms/tok`**<br>(hit `3,23/8`, min `14,49`, max `22,74`) | **`19,25 ms/tok`**<br>(hit `3,43/8`) | • `def=2`: **`10,99 ms/tok`**<br>• `def=4`: **`10,13 ms/tok`**<br>*(chưa kiểm chất lượng)* | • Ở `def=0`: **KT chậm hơn `~33%` (`16,29 ms` vs `~12,2 ms`)** do phải đồng bộ CPU↔GPU qua cả 48 tầng thay vì chỉ 20 tầng.<br>• Ở `def=2..4`: KT giảm còn `10,99–10,13 ms`. |
| **Tải hỗn hợp ~46,0% GPU / ~54,0% CPU — Decode**<br>(`llama.cpp -ncmoe 26` vs `KT K=59`, ~4,31/8 chuyên gia CPU) | • `-t 4` (`llama-server c8k_N26`, VRAM `9.683 MiB`): **`26,72 ms/tok` (`37,43 tok/s`)** | • Ở `-t 4` (26 tầng CPU × `0,612 ms`): **`~15,9 ms/tok`** | **`19,08 ms/tok`**<br>(hit `4,25/8`, min `16,07`, max `19,40`) | *(Không quét)* | *(Không quét)* | • Ở `def=0`: **KT chậm hơn `~20%` (`19,08 ms` vs `~15,9 ms`)**. |
| **Tải hỗn hợp 37,5% GPU / 62,5% CPU — Decode**<br>(`llama.cpp -ncmoe 30` vs `KT K=48`, 5,00/8 chuyên gia CPU) | • `-t 4` (`llama-server c8k_N30`, VRAM `8.291 MiB`): **`29,85 ms/tok` (`33,50 tok/s`)** | • Ở `-t 4` (30 tầng CPU × `0,612 ms`): **`~18,4 ms/tok`** | **`18,82 ms/tok`**<br>(hit `4,90/8`, min `15,46`, max `19,33`) | **`25,74 ms/tok`**<br>(hit `4,99/8`) | • `def=1`: `16,54 ms`<br>• `def=2`: `18,34 ms`<br>• `def=4`: **`13,61 ms`** | • Ở `def=0`: **Ngang ngửa (`18,82 ms` vs `~18,4 ms`, lệch `~2%`)**. |
| **Tải hỗn hợp 25,0% GPU / 75,0% CPU — Decode**<br>(`llama.cpp -ncmoe 36` vs `KT K=32`, 6,00/8 chuyên gia CPU) | • `-t 4` (`llama-server c8k_N36`, VRAM `6.251 MiB`): **`31,96 ms/tok` (`31,29 tok/s`)** | • Ở `-t 4` (36 tầng CPU × `0,612 ms`): **`~22,0 ms/tok`** | **`21,67 ms/tok`**<br>(hit `6,00/8`, min `18,55`, max `21,95`) | *(Không quét)* | *(Không quét)* | • Ở `def=0`: **Ngang ngửa (`21,67 ms` vs `~22,0 ms`, lệch `~1,5%`)**. |
| **Toàn bộ MoE trên CPU — Prefill `512` token**<br>(`llama-bench -ncmoe 48` vs `KT K=0`) | • `-t 8` (`taskset 0-7`): **`2,366 ms/tok` (`422,6 tok/s`)**<br>• `-t 20`: **`2,242 ms/tok` (`446,0 tok/s`)** | `< 2,37 ms/tok` (`> 422,6 tok/s`, vì số toàn trình đã gồm Attention GPU) | **`3,361 ms/tok`**<br>(`297,5 tok/s`) | **`2,516 ms/tok`**<br>(`397,5 tok/s`) | `3,909 ms/tok` (`255,8 tok/s` ở `th=20`) | **KT chậm hơn từ `-6%` (`th=18`) đến `-29,6%` (`th=8`)** dù chưa tính thời gian Attention GPU. |
| **Toàn bộ MoE trên CPU — Prefill `2048` token**<br>(`llama-bench -ncmoe 48` vs `KT K=0`) | • `-t 8` (`taskset 0-7`): **`2,281 ms/tok` (`438,4 tok/s`)**<br>• `-t 20`: **`2,182 ms/tok` (`458,2 tok/s`)** | `< 2,28 ms/tok` (`> 438,4 tok/s`, vì số toàn trình đã gồm Attention GPU) | **`3,313 ms/tok`**<br>(`301,8 tok/s`) | **`2,442 ms/tok`**<br>(`409,5 tok/s`) | `2,911 ms/tok` (`343,5 tok/s` ở `th=20`) | **KT chậm hơn từ `-10,6%` (`th=18` vs `-t 20`) đến `-31,2%` (`th=8` vs `taskset -t 8`)**. |

---

## 10. Đánh giá Cổng Pha 2A (Theo đúng Phần 3 của Kế hoạch) & Xin Quyết Định Chủ Dự Án

### 10.1 Kiểm tra điều kiện dừng sớm tại Cổng Pha 2A
- **Quy định tại Phần 1 (Bước 2A) và Phần 3 của [`KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md`](../../../KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md):**
  - *"Pha 2A chỉ đo nhân CPU riêng lẻ (`KTMoEWrapper`), không đo tốc độ sinh chữ của cả hệ thống. Chỉ được dừng sớm (kết luận 'không đáng' ngay tại 2A) khi nhân `kt-kernel` chậm hơn rõ rệt ($\ge 15\%$) ở decode VÀ không nhanh hơn ở prefill. Nếu ngang ngửa hoặc nhanh hơn thì phải qua Pha 2B (hoặc hỏi chủ dự án) mới kết luận được cho toàn hệ thống."*
- **Đối chiếu với số đo đã kiểm chứng `[ĐO]`:**
  1. **Khi so với `llama.cpp` mặc định (`-t 4`):** Ở $K=0, 32, 48$ (`def=0`), thời gian nhân CPU MoE của `kt-kernel` (`28,43 ms`, `21,67 ms`, `18,82 ms`) **ngang ngửa** (`±1,5% – 3,3%`) với phần MoE CPU của `llama.cpp` (`~29,4 ms`, `~22,0 ms`, `~18,4 ms`).
  2. **Khi so với `llama.cpp` đã tối ưu luồng (`taskset -c 0-7 -t 8`):** Nhân `LLAMAFILE` (AVX2) của `kt-kernel` ở `def=0` chậm hơn khoảng **`~25%–33%`** (`28,43 ms` vs `~21,4 ms` ở $K=0$; `16,29 ms` vs `~12,2 ms` ở $K=75$), và chậm hơn **`10%–31%`** ở prefill.
  3. **Những yếu tố cấp hệ thống CHƯA ĐO ở Pha 2A (chỉ có ở Pha 2B `sglang-kt`):**
     - **Đặt chuyên gia theo tần suất kích hoạt (Hotkey Expert Placement) `[ĐỌC MÃ]` (`experts_base.py:162-213`):** Trong Pha 2A, chúng ta dùng định tuyến đồng đều (`uniform`), nên với $K=75$ luôn có trung bình `3,31/8` chuyên gia rơi vào CPU. Trong `sglang-kt` toàn trình, nếu 75 chuyên gia nóng nhất mỗi tầng chiếm tỷ lệ kích hoạt cao hơn trung bình ($> 58,6\%$), số chuyên gia thực tế rơi vào CPU mỗi token có thể thấp hơn `3,31/8`.
     - **Nhân `AVXVNNI256GPTQInt4Packed_MOE` (`GPTQ_INT4`) `[ĐỌC MÃ]`:** Pha 2A chỉ mới đo nhân `LLAMAFILE` (tập lệnh AVX2 trên trọng số `Q4_K_M.gguf`), **chưa đo** nhân `AVXVNNI256GPTQInt4Packed_MOE` tận dụng tập lệnh `avx_vnni` của Core Ultra 7 265K trên định dạng `GPTQ-Int4`.
     - **Chồng lấp CPU/GPU & `max_deferred_experts_per_token`:** Ở Pha 2A, `def=2..4` giúp giảm thời gian chờ nhân CPU ở $K=75$ từ `16,29 ms` xuống `10,99–10,13 ms`, nhưng chỉ có chạy toàn trình Pha 2B mới đo được tốc độ sinh chữ thực tế và kiểm tra xem `def=2` có làm suy giảm chất lượng sinh mã hay không.
     - **Ánh xạ cùng mức VRAM đỉnh:** Ở Pha 2A chưa nạp phần Attention và KV cache của `sglang-kt` lên GPU nên chưa xác định được giá trị $K$ khớp chính xác từng mức VRAM đỉnh của `llama.cpp` (`11.733 MiB`, `9.683 MiB`, `8.291 MiB`).

### 10.2 Kết luận hiện tại & Câu hỏi xin quyết định của Chủ dự án
1. **Kết luận tại thời điểm hoàn tất Pha 2A:**
   - **Bỏ kết luận "KHÔNG ĐÁNG" sớm trước đây:** Số đo Pha 2A sau khi sửa lỗi cho thấy nhân `LLAMAFILE` (AVX2) của `kt-kernel` ngang ngửa `llama.cpp` mặc định (`-t 4`) ở $K=0, 32, 48$, dù kém hơn `llama.cpp` khi `llama.cpp` được ghim 8 nhân P (`taskset -c 0-7 -t 8`) và kém hơn ở prefill. Do đó **chưa đủ căn cứ để kết luận "KHÔNG ĐÁNG" cho toàn bộ KTransformers (`sglang-kt` + `GPTQ_INT4` AVX-VNNI) nếu chỉ dựa vào Pha 2A**.
   - **Phát hiện thực tiễn có thể áp dụng ngay cho `llama.cpp` `[ĐO]`:** Cấu hình `taskset -c 0-7 -t 8` (ghim 8 luồng tính toán của `llama.cpp` vào 8 nhân P `CPU 0..7` trên Core Ultra 7 265K) giúp tăng tốc độ decode ở `-ncmoe 48` từ `26,48 tok/s` lên **`34,53 tok/s` (`+30,4%`)** hoàn toàn miễn phí mà không tốn thêm VRAM hay dung lượng đĩa.
2. **Xin quyết định của Chủ dự án (Cổng chuyển tiếp sang Pha 2B):**
   - Để có kết luận cuối cùng cấp hệ thống (đo `sglang-kt` toàn trình cùng ngân sách VRAM và kiểm chứng nhân `AVXVNNI256GPTQInt4Packed_MOE`), anh có duyệt thực hiện **Pha 2B**:
     - Tải kho trọng số `Qwen/Qwen3-30B-A3B-GPTQ-Int4` (**`16,93 GB` / `15,77 GiB`**) về `.aicf-work/models/Qwen3-30B-A3B-GPTQ-Int4` và cài đặt `sglang-kt==0.7.2` (cùng các gói phụ thuộc `torch==2.9.1`, `flashinfer_python==0.6.3` vào `.aicf-work/ktransformers/venv`, tổng dung lượng đĩa dùng thêm `~21,8 GiB / 40 GiB` trống);
   - Hay anh quyết định **dừng thử nghiệm tại cuối Pha 2A** (với đầy đủ dữ liệu đã kiểm chứng ở trên) và chỉ áp dụng tối ưu ghim nhân P (`taskset -c 0-7 -t 8`) cho `llama.cpp`?


