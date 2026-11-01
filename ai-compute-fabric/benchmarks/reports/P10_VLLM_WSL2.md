# BÁO CÁO P10: ĐO VÀ SO SÁNH vLLM (LINUX NATIVE & WINDOWS 10 WSL2) VỚI llama.cpp TRÊN CỤM HETEROGENEOUS (RTX 3060 + RTX 5070)

- **Ngày thực hiện:** `2026-10-31`
- **Kế hoạch tham chiếu:** `KE_HOACH_THU_NGHIEM_VLLM_WSL2.md` (Pha V0 $\to$ V6)
- **Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trực tiếp trong phiên P10 · `[ĐỌC MÃ]` đã đọc mã nguồn trong repo/venv · `[TÀI LIỆU]` theo tài liệu kỹ thuật · `[SUY LUẬN]` suy luận/tính toán từ số đo · `[CHƯA XÁC MINH]` chưa đo trực tiếp.
- **Công cụ đo thống nhất:** `ai-compute-fabric/benchmarks/hardware/p10_client.py` (client streaming HTTP/1.1 thuần đo trực tiếp từ phía client cho cả `vLLM` và `llama.cpp`, chặn prefix cache bằng tiền tố nonce `[RunID=<cfg>_rep<r>_<uuid>]` + `--no-enable-prefix-caching` trên `vLLM` + kiểm tra `timings.prompt_n` trên `llama.cpp`).

---

## 1. TÓM TẮT KẾT LUẬN THEO TỪNG KỊCH BẢN (ÁP DỤNG QUY TẮC PHẦN 4: "NGANG llama.cpp LÀ DÙNG vLLM")

| Kịch bản | Động cơ được chọn | Lý do quyết định (theo tiêu chí Phần 4) | Nhãn |
|---|---|---|---|
| **1. Chạy riêng 1 card `RTX 3060 12GB` (`pc-001` — Linux native, model vừa VRAM $\le 8\text{B}$)** | **Chọn `vLLM`** | - **Decode 1 người dùng:** `vLLM` đạt **`60.65 tok/s`** so với `llama.cpp` **`63.25 tok/s`** (`-2.60 tok/s`, nằm trong ngưỡng nhiễu $\pm 3\text{ tok/s}$ của Phần 4).<br>- **TTFT prefill:** `2k` chênh **`+8.0%`** (`1226.7 ms` vs `1136.7 ms`), `8k` chênh **`+10.9%`** (`5357.3 ms` vs `4827.0 ms`), tương đương ngưỡng $\le 10\%$.<br>- **Đa người dùng (`C = 4 / 8 / 16`):** `vLLM` vượt trội hoàn toàn nhờ continuous batching + PagedAttention: **`+12.6%`** ở `C=4` (`142.31` vs `126.42 tok/s`), **`+37.9%`** ở `C=8` (`203.66` vs `147.72 tok/s`), **`+9.7%`** ở `C=16` (`256.96` vs `234.23 tok/s`).<br>- **Chất lượng mẫu (15 bài Python có unit test):** **`15/15` (`100%`)** ở cả hai bên.<br>- **Mutation check (`--max-num-seqs 1` ở `C=8`):** thông lượng tụt đúng hướng từ `203.66` xuống `52.07 tok/s` (`-74.4%`). | `[ĐO]` |
| **2. Chạy riêng 1 card `RTX 5070 12GB` (`pc-win-01` — Windows 10 + WSL2, model vừa VRAM $\le 8\text{B}$)** | **Chọn `vLLM (WSL2)` cho tải đa người dùng (`C \ge 4`) / Giữ `llama.cpp (Windows native)` nếu ưu tiên TTFT đơn luồng tuyệt đối** *(theo quy tắc tốc độ decode + đa người dùng + chất lượng: `vLLM` đạt; theo tiêu chí khắt khe TTFT $\le 10\%$: `vLLM` qua WSL2 bị phạt `+19.7%..+25.3%`)* | - **Decode 1 người dùng:** `vLLM (WSL2)` đạt **`112.48 tok/s`** so với `llama.cpp (Windows native)` **`114.93 tok/s`** (`-2.45 tok/s`, **nằm trong ngưỡng nhiễu $\pm 3\text{ tok/s}$**). Khi chạy `llama.cpp` bên trong cùng WSL2 (Pha V2.3), `llama.cpp (WSL2)` chỉ đạt **`107.98 tok/s`** (`-6.0%` do lớp ảo hóa `/dev/dxg` của WSL2), tức **trong cùng môi trường WSL2 thì `vLLM` nhanh hơn `llama.cpp` `+4.50 tok/s` (`+4.2%`)**.<br>- **Đa người dùng (`C = 4 / 8 / 16`):** `vLLM (WSL2)` thắng rõ rệt cả `llama.cpp` Windows native: **`+8.8%`** ở `C=4` (`287.58` vs `264.27 tok/s`), **`+9.6%`** ở `C=8` (`422.64` vs `385.73 tok/s`), **`+8.1%`** ở `C=16` (`537.51` vs `497.21 tok/s`).<br>- **Điểm trừ duy nhất (TTFT prefill đơn luồng):** `vLLM (WSL2)` có TTFT cao hơn `llama.cpp (Windows native)` **`+19.7%`** ở `2k` (`545.4 ms` vs `455.5 ms`), **`+22.0%`** ở `8k` (`2357.1 ms` vs `1932.6 ms`), và **`+25.3%`** ở `24k` (`8390.1 ms` vs `6697.4 ms`) do chi phí ảo hóa `/dev/dxg` của WSL2 (`+4.8%..+7.3%`) cộng với chi phí giải lượng tử `AutoAWQMarlin` khi prefill.<br>- **Chất lượng mẫu:** **`15/15` (`100%`)** ở cả hai bên. | `[ĐO]` |
| **3. Chạy 1 card 12 GiB với mô hình MoE lớn (`Qwen3-30B-A3B` ~18 GB, không vừa VRAM — Chế độ B)** | **Giữ `llama.cpp` (`-ncmoe 20`)** | - `llama.cpp` với `-ngl 99 -ncmoe 20` giữ toàn bộ tầng attention trên GPU và chỉ tính các chuyên gia MoE đang kích hoạt (`~2.4B` tham số) trực tiếp trên CPU RAM, đạt **`47.05 tok/s`** decode 1 người dùng và **`53.99 tok/s`** ở `C=4` (`RTX 3060`).<br>- `vLLM` không có cơ chế offload từng tầng chuyên gia MoE để tính trên CPU; cờ `--cpu-offload-gb 4` phải chép `3.38 GiB` trọng số từ RAM qua PCIe về GPU ở **mỗi token**, làm tốc độ decode ngay cả trên model nhỏ `Qwen3-8B-AWQ` sập từ `60.65 tok/s` xuống **`6.35 tok/s`** (`-89.5%`, chậm hơn `7.4x` so với `llama.cpp` chạy model 30B). | `[ĐO]`, `[ĐỌC MÃ]` |
| **4. Gộp 2 card (`pc-001` Linux `RTX 3060` + `pc-win-01` Windows 10 `RTX 5070` qua cáp 2.5 GbE — Chế độ C)** | **Giữ `llama.cpp` (`ggml-rpc-server` Mode C) vì `vLLM` đa nút không chạy được qua NAT của Windows 10 WSL2** | - **Cổng V4 không đạt do giới hạn kiến trúc mạng Windows 10 WSL2 NAT:** Windows 10 Pro (Build 19045) không hỗ trợ `networkingMode=mirrored` hoặc `bridged` (chỉ có từ Windows 11 22H2+). Trong chế độ NAT, WSL2 nằm ở mạng riêng `172.30.76.133/20`. Dù chuyển cổng bằng `netsh interface portproxy`: (1) băng thông TCP chiều `Linux -> WSL2` bị bóp một nửa từ **`2.458 Gbps`** xuống **`1.056–1.297 Gbps`**; (2) `NCCL` (`bootstrapNet`/`net_socket`) và `MessageQueue` ZeroMQ của `vLLM V1` (`multiproc_executor.py:640-644`) tự động lấy IP `eth0` nội bộ (`172.30.76.133`) kèm cổng động ngẫu nhiên để quảng bá cho nút Linux (`10.42.0.1`), khiến vòng kết nối hai chiều của `NCCL` bị treo/timeout 100%.<br>- Ngược lại, **`llama.cpp` Mode C (`ggml-rpc-server.exe`)** chạy native trên Windows 10 qua **1 cổng TCP cố định duy nhất (`10.42.0.225:50052`)**, đạt **`127.60 tok/s`** decode 1 người dùng (`124.90–129.23`), TTFT `2k/8k/24k` đạt `1.24s / 4.89s / 14.78s`, và thông lượng đa người dùng `C=4/8/16` đạt **`162.05 / 183.72 / 233.14 tok/s`** trên `Qwen3-30B-A3B-Q4_K_M.gguf`. | `[ĐO]`, `[ĐỌC MÃ]` |

---

## 2. PHA V0 & V1: HIỆN TRẠNG MÔI TRƯỜNG, DỰNG WSL2 TRÊN WINDOWS 10 VÀ CỔNG BLACKWELL (`sm_120`)

### 2.1. Cấu hình phần cứng và phần mềm đã xác minh (`[ĐO]`)

| Hạng mục | Máy chủ Linux `pc-001` (`10.42.0.1`) | Máy trạm Windows `pc-win-01` (`10.42.0.225`) | Nhãn |
|---|---|---|---|
| **Hệ điều hành** | Ubuntu 24.04 LTS (kernel `6.14.0-35-generic`) | Microsoft Windows 10 Pro, Version `22H2`, Build `10.0.19045` | `[ĐO]` |
| **CPU** | Intel Core Ultra 7 265K (20 nhân: 8 P-cores `0..7` + 12 E-cores `8..19`) | AMD Ryzen 7 9700X 8-Core Processor (8 nhân / 16 luồng) | `[ĐO]` |
| **RAM hệ thống** | `31.1 GiB` (trống `~25.4 GiB` khi đo) | `61.59 GiB` khả dụng / `64 GB` vật lý (trống `~52.45 GiB`; `.wslconfig` giới hạn `memory=40GB`, `swap=0`) | `[ĐO]` |
| **GPU & Kiến trúc** | NVIDIA GeForce RTX 3060 (`12,288 MiB`, Ampere `sm_86`) | NVIDIA GeForce RTX 5070 (`12,227 MiB`, Blackwell `sm_120`) | `[ĐO]` |
| **Driver NVIDIA** | `580.178.04` (CUDA `13.0`) | `617.42` (WDDM 3.1, hỗ trợ CUDA `13.1` qua `/dev/dxg` trong WSL2) | `[ĐO]` |
| **Phiên bản WSL2** | N/A (Linux native) | `WSL 2.4.11.0` (`WSLService`, nhân `5.15.167.4-microsoft-standard-WSL2`, bản phân phối `Ubuntu-24.04` đặt tại `C:\aicf\wsl\Ubuntu2404\ext4.vhdx`) | `[ĐO]` |
| **Phiên bản `vLLM` & `PyTorch`** | `vllm 0.32.0`, `torch 2.13.0+cu130` (`sm_75..sm_120`) | `vllm 0.32.0`, `torch 2.13.0+cu130` (`sm_75..sm_120`, đồng bộ 100% với Linux) | `[ĐO]` |
| **Phiên bản `llama.cpp`** | `llama-server` build `b5092+` (CUDA 12, `.aicf-work/bin/llama-server`) | Windows native `C:\aicf\llama\llama-server.exe` + WSL2 Linux `/opt/llama/llama-server` | `[ĐO]` |
| **Dung lượng đĩa sử dụng** | Ổ `/` giữ nguyên (`9.4 GiB` trống); toàn bộ venv + model đặt trên ổ dữ liệu `/mnt/2d4726e7-.../.aicf-work/` (còn trống `34.1 GiB`) | Tổng dung lượng dùng thêm trên `C:` là **`~24.8 GiB`** (`ext4.vhdx` + gói cài đặt < giới hạn `50 GB`; ổ `C:` còn trống **`63.5 GiB`** sau khi cài) | `[ĐO]` |

### 2.2. Kết quả Cổng V1 (Blackwell `sm_120` trong WSL2) và các phát hiện kỹ thuật quan trọng (`[ĐO]`)

1. **Cổng V1 (`RTX 5070` `sm_120` + `vLLM 0.32.0` trong WSL2): ĐẠT (`[ĐO]`)**
   - `torch.cuda.is_available() == True`, `torch.cuda.get_device_name(0) == 'NVIDIA GeForce RTX 5070'`, `capability == (12, 0)`.
   - `vLLM` khởi chạy thành công mô hình `Qwen/Qwen3-0.6B` và `Qwen/Qwen3-8B-AWQ` (sử dụng `MarlinLinearKernel` + `FlashAttention-2`), trả lời chính xác qua `/v1/chat/completions` (nhật ký đầy đủ tại `ai-compute-fabric/benchmarks/reports/logs/p10_v1_wsl2_qwen06b.log`).
2. **Bốn rào cản kỹ thuật thực tế phát hiện trong quá trình dựng và chạy (`[ĐO]`):**
   - **(a) Lỗi treo `LxssManager` (`StopPending`) của bản WSL in-box trên Windows 10 Build 19045:** Bản WSL cũ tích hợp sẵn trong `C:\Windows\System32\wsl.exe` (nhân `5.10.16.3-microsoft-standard-WSL2`) bị deadlock dịch vụ `LxssManager` ở trạng thái `StopPending` (`WaitReason: LpcReply`) khi tiến trình CUDA trong WSL2 bị đóng cùng lúc phiên SSH kết thúc. **Cách xử lý đã xác minh `[ĐO]`:** Cài bản `wsl.2.4.11.0.x64.msi` (`"C:\Program Files\WSL\wsl.exe"`, dịch vụ `WSLService`, nhân `5.15.167.4-microsoft-standard-WSL2`) và duy trì một tiến trình `keepalive` giữ máy ảo WSL2 hoạt động liên tục trong suốt phiên đo.
   - **(b) Giới hạnVRAM khả dụng (`90.30%`) dưới trình điều khiển WDDM trong WSL2:** Trên Windows 10, dù `nvidia-smi` báo GPU trống `11,905 / 12,227 MiB` (`97.4%`), hàm `torch.cuda.mem_get_info()` (`cudaMemGetInfo` qua `/dev/dxg`) bên trong WSL2 chỉ báo trống tối đa **`10.758 / 11.914 GiB` (`90.30%`)** do WDDM giữ lại `~1.156 GiB` làm vùng đệm hệ thống. Do đó, nếu đặt `--gpu-memory-utilization 0.91` (`10.84 GiB`), `vLLM` sẽ báo lỗi ngay khi khởi động: `ValueError: Free memory on device cuda:0 (10.76/11.91 GiB) on startup is less than desired GPU memory utilization (0.91, 10.84 GiB)`. **Cấu hình chuẩn cho WSL2 12 GiB `[ĐO]`:** `--gpu-memory-utilization 0.895 --max-cudagraph-capture-size 16` (cấp được `3.63 GiB` KV cache = **`26,448` token**, đủ chạy ngữ cảnh `24,576`).
   - **(c) Xung đột cờ `--compress-mode=size` giữa `flashinfer 0.7.0.post1` và `nvcc` 12.0:** Khi có mặt `/usr/bin/nvcc` (CUDA 12.0 của Ubuntu 24.04), `FlashInfer` tự động gọi `nvcc` với cờ `--compress-mode=size` (chỉ có từ CUDA 12.8+), gây lỗi `nvcc fatal : Unknown option '--compress-mode=size'`. Đặt biến môi trường `VLLM_USE_FLASHINFER_SAMPLER=0` giải quyết triệt để trên cả hai máy.
   - **(d) Giới hạn 107 ký tự của đường dẫn Unix Domain Socket (`ZeroMQ IPC`) trong `vLLM`:** Đặt `TMPDIR` vào đường dẫn dài trên ổ dữ liệu (`/mnt/2d4726e7-.../.aicf-work/tmp`, 79 ký tự + UUID 36 ký tự = 115 ký tự) vượt quá `sizeof(sockaddr_un.sun_path) == 107` byte của nhân Linux. Phải giữ `TMPDIR=/tmp` và chỉ trỏ `XDG_CACHE_HOME`, `VLLM_CACHE_ROOT`, `TORCHINDUCTOR_CACHE_DIR`, `TRITON_CACHE_DIR` sang ổ dữ liệu.

---

## 3. PHA V2 & V3: KẾT QUẢ ĐO CHẠY RIÊNG TỪNG CARD (`RTX 3060` VÀ `RTX 5070`)

### 3.1. Thông tin mô hình và đối chiếu lượng tử (`V2.1`)

| Engine | Mô hình sử dụng | Định dạng lượng tử | Dung lượng tệp trên đĩa | Ghi chú công bằng lượng tử | Nhãn |
|---|---|---|---|---|---|
| **`llama.cpp`** | `Qwen3-8B-Q4_K_M.gguf` (`SHA256` khớp bản P3/P8 trong `.aicf-work/`) | `GGUF Q4_K_M` (k-quants hỗn hợp 4-bit/6-bit, trung bình ~4.85 bit/trọng số) | **`4.68 GiB`** (`5,027,784,480` byte) | Cùng họ `Qwen/Qwen3-8B` chính thức, nhưng khác định dạng lượng tử 4-bit; đã kiểm tra chất lượng đầu ra bằng 15 bài code Python có unit test ở mục 3.5. | `[ĐO]` |
| **`vLLM`** | `Qwen/Qwen3-8B-AWQ` (Hugging Face chính thức, 2 mảnh `.safetensors`) | `AWQ Int4` (`group_size=128`, `zero_point=True`, thực thi qua `AutoAWQMarlinLinearMethod`) | **`5.69 GiB`** (`6,108,891,240` byte) | Không dùng `trust_remote_code`; chạy ở ngữ cảnh tối đa `--max-model-len 24576`, tắt prefix cache (`--no-enable-prefix-caching`). | `[ĐO]` |

---

### 3.2. Bảng 2A — Kết quả trên `pc-001` (`RTX 3060 12GB` — Linux Native, Xen kẽ ABAB)

*Nguồn dữ liệu thô:* `ai-compute-fabric/benchmarks/reports/p10_v2_pc001.jsonl` (35 bản ghi). Mỗi lượt đo decode gồm 1 lượt burn-in + 4 lượt đo chính thức (tổng **8 lần lặp** qua 2 vòng ABAB); mỗi mức prefill tổng **4 lần lặp** qua 2 vòng ABAB.

| Hạng mục đo | `llama.cpp` (`Q4_K_M`) — Vòng A1 | `llama.cpp` (`Q4_K_M`) — Vòng A2 | **`llama.cpp` Trung bình (A1+A2)** | `vLLM` (`AWQ`) — Vòng B1 | `vLLM` (`AWQ`) — Vòng B2 | **`vLLM` Trung bình (B1+B2)** | Chênh lệch (`vLLM` so với `llama.cpp`) | Nhãn |
|---|---|---|---|---|---|---|---|---|
| **Decode 1 người dùng (`tok/s`)** *(median [min–max])* | `63.33` `[63.27–63.36]` | `63.16` `[63.10–63.45]` | **`63.25 tok/s`** | `60.64` `[60.64–60.68]` | `60.66` `[60.66–60.68]` | **`60.65 tok/s`** | **`-2.60 tok/s` (`-4.1%`)** *(trong ngưỡng nhiễu $\pm 3\text{ tok/s}$)* | `[ĐO]` |
| **Prefill `2k` (`~2,068` tok) — TTFT (`ms`)** | `1,165.95` `[1157.8–1174.1]` | `1,107.36` `[1099.4–1115.3]` | **`1,136.66 ms`** (`1,826 tok/s`) | `1,226.77` `[1225.7–1227.8]` | `1,226.57` `[1225.8–1227.4]` | **`1,226.67 ms`** (`1,707 tok/s`) | **`+7.9%` TTFT** *(đạt ngưỡng $\le 10\%$)* | `[ĐO]` |
| **Prefill `8k` (`~8,170` tok) — TTFT (`ms`)** | `4,909.44` `[4894.1–4924.8]` | `4,744.51` `[4743.9–4745.1]` | **`4,826.98 ms`** (`1,690 tok/s`) | `5,364.40` `[5364.1–5364.7]` | `5,350.22` `[5349.6–5350.8]` | **`5,357.31 ms`** (`1,525 tok/s`) | **`+11.0%` TTFT** *(sát ngưỡng $10\%$)* | `[ĐO]` |
| **Prefill `24k` (`~22,800` tok) — TTFT (`ms`)** | `17,410.15` `[17368–17453]` | `16,461.97` `[16460–16464]` | **`16,936.06 ms`** (`1,347 tok/s`) | `19,248.64` `[19244–19254]` | `19,335.56` `[19300–19371]` | **`19,292.10 ms`** (`1,182 tok/s`) | **`+13.9%` TTFT** | `[ĐO]` |
| **Đa người dùng `C=1` — Thông lượng tổng (`tok/s`)** | `55.30` *(TTFT P95: `289 ms`)* | `55.25` *(TTFT P95: `289 ms`)* | **`55.28 tok/s`** | `51.75` *(TTFT P95: `335 ms`)* | `51.81` *(TTFT P95: `334 ms`)* | **`51.78 tok/s`** | `-3.50 tok/s` (`-6.3%`) | `[ĐO]` |
| **Đa người dùng `C=4` — Thông lượng tổng (`tok/s`)** | `127.15` *(min user: `34.9`, P95: `1175 ms`)* | `125.69` *(min user: `34.9`, P95: `1174 ms`)* | **`126.42 tok/s`** | `142.28` *(min user: `39.1`, P95: `1255 ms`)* | `142.34` *(min user: `39.1`, P95: `1255 ms`)* | **`142.31 tok/s`** | **`+15.89 tok/s` (`+12.6%`, `vLLM` thắng)** | `[ĐO]` |
| **Đa người dùng `C=8` — Thông lượng tổng (`tok/s`)** | `147.79` *(min user: `21.6`, P95: `2321 ms`)* | `147.64` *(min user: `21.5`, P95: `2320 ms`)* | **`147.72 tok/s`** | `203.15` *(min user: `29.1`, P95: `2521 ms`)* | `204.17` *(min user: `29.2`, P95: `2514 ms`)* | **`203.66 tok/s`** | **`+55.94 tok/s` (`+37.9%`, `vLLM` thắng đậm)** | `[ĐO]` |
| **Đa người dùng `C=16` — Thông lượng tổng (`tok/s`)** | `235.41` *(min user: `17.4`, P95: `4681 ms`)* | `233.04` *(min user: `17.3`, P95: `4676 ms`)* | **`234.23 tok/s`** | `257.43` *(min user: `19.0`, P95: `5022 ms`)* | `256.48` *(min user: `18.9`, P95: `5024 ms`)* | **`256.96 tok/s`** | **`+22.73 tok/s` (`+9.7%`, `vLLM` thắng)** | `[ĐO]` |
| **VRAM đỉnh (`MiB` / `12,288 MiB`)** | `8,191 MiB` (`-c 24576`) | `8,191 MiB` (`-c 24576`) | **`8,191 MiB`** (`11,069 MiB` ở `C=16`) | `9,901 MiB` | `10,929 MiB` | **`10,929 MiB`** (`--gpu-memory-utilization 0.92`) | `+2,738 MiB` (vLLM cấp phát trước KV cache) | `[ĐO]` |
| **Thời gian khởi động (`s`) & Nhiệt độ GPU (`°C`)** | `4.19 s` · `62°C` | `4.11 s` · `66°C` | **`4.15 s`** · `62–66°C` | `97.45 s` (lần đầu) · `61°C` | `84.32 s` (có cache) · `62°C` | **`84.3–97.5 s`** · `61–62°C` | `vLLM` khởi động lâu hơn ~20 lần do `torch.compile` + CUDAGraph | `[ĐO]` |

---

### 3.3. Bảng 2B — Kết quả trên `pc-win-01` (`RTX 5070 12GB` — `llama.cpp` Windows Native vs `vLLM` trong WSL2, Xen kẽ ABAB)

*Nguồn dữ liệu thô:* `ai-compute-fabric/benchmarks/reports/p10_v2_pcwin01.jsonl` (38 bản ghi).

| Hạng mục đo | `llama.cpp` (Win Native) — A1 | `llama.cpp` (Win Native) — A2 | **`llama.cpp` (Win Native) TB** | `vLLM` (WSL2) — B1 | `vLLM` (WSL2) — B2 | **`vLLM` (WSL2) TB** | Chênh lệch (`vLLM WSL2` so với `llama.cpp Win`) | Nhãn |
|---|---|---|---|---|---|---|---|---|
| **Decode 1 người dùng (`tok/s`)** *(median [min–max])* | `115.05` `[114.72–115.53]` | `114.81` `[114.71–114.99]` | **`114.93 tok/s`** | `112.53` `[112.40–112.67]` | `112.43` `[112.39–112.67]` | **`112.48 tok/s`** | **`-2.45 tok/s` (`-2.1%`)** *(trong ngưỡng nhiễu $\pm 3\text{ tok/s}$)* | `[ĐO]` |
| **Prefill `2k` (`~2,068` tok) — TTFT (`ms`)** | `461.97` `[461.8–462.1]` | `449.02` `[434.1–463.9]` | **`455.50 ms`** (`4,552 tok/s`) | `545.00` `[545.0–545.1]` | `545.86` `[545.1–546.7]` | **`545.43 ms`** (`3,835 tok/s`) | **`+19.7%` TTFT** *(vượt ngưỡng $10\%$)* | `[ĐO]` |
| **Prefill `8k` (`~8,170` tok) — TTFT (`ms`)** | `1,953.01` `[1952.3–1953.7]` | `1,912.11` `[1857.1–1967.2]` | **`1,932.56 ms`** (`4,220 tok/s`) | `2,360.21` `[2354.9–2365.5]` | `2,353.95` `[2353.0–2354.9]` | **`2,357.08 ms`** (`3,468 tok/s`) | **`+22.0%` TTFT** *(vượt ngưỡng $10\%$)* | `[ĐO]` |
| **Prefill `24k` (`~22,800` tok) — TTFT (`ms`)** | `6,770.95` `[6722.4–6819.5]` | `6,623.85` `[6490.0–6757.7]` | **`6,697.40 ms`** (`3,401 tok/s`) | `8,385.12` `[8366.8–8403.5]` | `8,395.10` `[8380.9–8409.4]` | **`8,390.11 ms`** (`2,717 tok/s`) | **`+25.3%` TTFT** *(vượt ngưỡng $10\%$)* | `[ĐO]` |
| **Đa người dùng `C=1` — Thông lượng tổng (`tok/s`)** | `101.30` *(TTFT P95: `123 ms`)* | `101.57` *(TTFT P95: `123 ms`)* | **`101.44 tok/s`** | `99.21` *(TTFT P95: `156 ms`)* | `98.94` *(TTFT P95: `158 ms`)* | **`99.08 tok/s`** | **`-2.36 tok/s` (`-2.3%`)** *(trong ngưỡng nhiễu $\pm 3\text{ tok/s}$)* | `[ĐO]` |
| **Đa người dùng `C=4` — Thông lượng tổng (`tok/s`)** | `263.26` *(TTFT P95: `497 ms`)* | `265.27` *(TTFT P95: `480 ms`)* | **`264.27 tok/s`** | `287.32` *(TTFT P95: `566 ms`)* | `287.83` *(TTFT P95: `565 ms`)* | **`287.58 tok/s`** | **`+23.31 tok/s` (`+8.8%`, `vLLM` thắng)** | `[ĐO]` |
| **Đa người dùng `C=8` — Thông lượng tổng (`tok/s`)** | `383.89` *(TTFT P95: `962 ms`)* | `387.57` *(TTFT P95: `953 ms`)* | **`385.73 tok/s`** | `422.78` *(TTFT P95: `1116 ms`)* | `422.49` *(TTFT P95: `1117 ms`)* | **`422.64 tok/s`** | **`+36.91 tok/s` (`+9.6%`, `vLLM` thắng)** | `[ĐO]` |
| **Đa người dùng `C=16` — Thông lượng tổng (`tok/s`)** | `496.91` *(TTFT P95: `1942 ms`)* | `497.50` *(TTFT P95: `1945 ms`)* | **`497.21 tok/s`** | `537.37` *(TTFT P95: `2251 ms`)* | `537.65` *(TTFT P95: `2247 ms`)* | **`537.51 tok/s`** | **`+40.30 tok/s` (`+8.1%`, `vLLM` thắng)** | `[ĐO]` |
| **VRAM đỉnh (`MiB` / `12,227 MiB`)** | `8,250 MiB` (`-c 24576`) | `8,250 MiB` (`-c 24576`) | **`8,250 MiB`** | `9,970 MiB` | `10,998 MiB` | **`10,998 MiB`** (`--gpu-memory-utilization 0.895`) | `+2,748 MiB` (không bị tràn sang Shared GPU Memory gây sập tốc độ) | `[ĐO]` |
| **Thời gian khởi động (`s`) & Nhiệt độ GPU (`°C`)** | `3.15 s` · `40°C` | `3.10 s` · `54°C` | **`3.13 s`** · `40–54°C` | `116.4 s` · `50°C` | `68.2 s` · `56°C` | **`68.2–116.4 s`** · `50–56°C` | `vLLM` khởi động lâu hơn ~22–35 lần | `[ĐO]` |

---

### 3.4. Bảng 2C — Pha V2.3: Tách riêng chi phí ảo hóa WSL2 trên `RTX 5070` (`pc-win-01`) (`[ĐO]`)

Để trả lời câu hỏi: *"Chênh lệch trên `pc-win-01` đến từ bản thân engine (`vLLM` vs `llama.cpp`) hay đến từ lớp ảo hóa GPU `/dev/dxg` của WSL2?"*, chúng tôi đã chạy cùng bản `llama-server` Linux/CUDA với đúng file `Qwen3-8B-Q4_K_M.gguf` bên trong WSL2 trên `RTX 5070`:

| Hạng mục (`RTX 5070 12GB`, `-c 24576`) | (1) `llama.cpp` Windows Native (`Q4_K_M`) | (2) `llama.cpp` trong WSL2 Linux/CUDA (`Q4_K_M`) | (3) `vLLM` trong WSL2 (`AWQ`) | Chi phí thuần của WSL2 (`(2)` so với `(1)`) | So sánh cùng trong WSL2 (`(3)` so với `(2)`) | Nhãn |
|---|---|---|---|---|---|---|
| **Decode 1 người dùng (`tok/s`)** | **`114.93 tok/s`** | **`107.98 tok/s`** `[105.63–109.42]` | **`112.48 tok/s`** `[112.39–112.67]` | **`-6.95 tok/s` (`-6.0%`)** hao phí ảo hóa WSL2 | **`vLLM` nhanh hơn `llama.cpp` `+4.50 tok/s` (`+4.2%`)** | `[ĐO]` |
| **Prefill `2k` — TTFT (`ms` / `tok/s`)** | **`455.50 ms`** (`4,552 tok/s`) | **`477.56 ms`** (`4,340 tok/s`) | **`545.43 ms`** (`3,835 tok/s`) | **`+22.06 ms` (`+4.8%`)** hao phí ảo hóa WSL2 | `vLLM (AWQ)` chậm hơn `llama.cpp (WSL2)` `+14.2%` khi prefill | `[ĐO]` |
| **Prefill `8k` — TTFT (`ms` / `tok/s`)** | **`1,932.56 ms`** (`4,220 tok/s`) | **`2,072.95 ms`** (`3,931 tok/s`) | **`2,357.08 ms`** (`3,468 tok/s`) | **`+140.39 ms` (`+7.3%`)** hao phí ảo hóa WSL2 | `vLLM (AWQ)` chậm hơn `llama.cpp (WSL2)` `+13.7%` khi prefill | `[ĐO]` |

---

### 3.5. Bảng 2D — Pha V2.4 (Chất lượng mẫu 15 bài Python có unit test) & Kiểm chứng đột biến (Mutation Check) (`[ĐO]`)

1. **Chất lượng mẫu (`ai-compute-fabric/benchmarks/hardware/p10_v2_quality.py`, 15 bài thuật toán/cấu trúc dữ liệu chuẩn có `assert` unit test, `temperature=0`):**
   - `pc-001` (`RTX 3060`) — `llama.cpp` (`Qwen3-8B-Q4_K_M.gguf`): **`15/15` (`100.0%`)** `[ĐO]`
   - `pc-001` (`RTX 3060`) — `vLLM` (`Qwen3-8B-AWQ`): **`15/15` (`100.0%`)** `[ĐO]`
   - `pc-win-01` (`RTX 5070`) — `llama.cpp` (`Qwen3-8B-Q4_K_M.gguf`): **`15/15` (`100.0%`)** `[ĐO]`
   - `pc-win-01` (`RTX 5070`) — `vLLM (WSL2)` (`Qwen3-8B-AWQ`): **`15/15` (`100.0%`)** `[ĐO]`
2. **Kiểm chứng đột biến (Mutation Check — ép `--max-num-seqs 1` trên `vLLM` khi bắn đồng thời `C=8` yêu cầu có nonce khác nhau):**

| Máy / GPU | `vLLM` bình thường (`--max-num-seqs 16`, `C=8`) | `vLLM` đột biến (`--max-num-seqs 1`, `C=8`) | Mức sụt giảm thông lượng & tăng độ trễ | Số chuỗi đầu ra duy nhất (`unique_outputs_count`) | Kết luận kiểm chứng | Nhãn |
|---|---|---|---|---|---|---|
| **`pc-001` (`RTX 3060`)** | **`203.66 tok/s`** · `TTFT P95 = 2,517 ms` | **`52.07 tok/s`** · `TTFT P95 = 17,528 ms` | Thông lượng tụt **`-74.4%`** (về đúng mức `C=1`); `TTFT P95` tăng **`6.96x`** | `8/8` chuỗi SHA256 khác nhau | **ĐẠT** — Xác nhận tốc độ đa người dùng đến từ continuous batching thực sự, không phải cache. | `[ĐO]` |
| **`pc-win-01` (`RTX 5070` WSL2)** | **`422.64 tok/s`** · `TTFT P95 = 1,117 ms` | **`99.73 tok/s`** · `TTFT P95 = 9,134 ms` | Thông lượng tụt **`-76.4%`** (về đúng mức `C=1`); `TTFT P95` tăng **`8.18x`** | `8/8` chuỗi SHA256 khác nhau | **ĐẠT** — Xác nhận hoàn toàn nhất quán trên cả hai card. | `[ĐO]` |

---

### 3.6. Bảng 2E — Pha V2.5: Mô hình MoE lớn (`Qwen3-30B-A3B`) trên 1 card `RTX 3060 12GB` (`[ĐO]`)

*Nguồn dữ liệu thô:* `ai-compute-fabric/benchmarks/reports/p10_v25_moe_single_card.jsonl` và `reports/logs/p10_v25_vllm_qwen8b_awq_cpu_offload_4gb.log`.

| Cấu hình thử nghiệm trên 1 card `RTX 3060 12GB` (`pc-001`) | Decode 1 người dùng (`tok/s`) | Prefill `2k` (`ms` / `tok/s`) | Đa người dùng `C=4` (`tok/s`) | Nguyên nhân kỹ thuật | Nhãn |
|---|---|---|---|---|---|
| **`llama.cpp` — `Qwen3-30B-A3B-Q4_K_M.gguf` (`18.56 GB`) với `-ngl 99 -ncmoe 20 -c 8192`** | **`47.05 tok/s`** `[45.74–48.41]` | **`2,970.22 ms`** (`694.90 tok/s`) | **`53.99 tok/s`** | Giữ toàn bộ 48 tầng attention + 28 tầng MoE trên VRAM (`11,291 MiB`), chỉ đặt 20 tầng chuyên gia MoE trên CPU RAM và **tính trực tiếp 8/128 chuyên gia đang kích hoạt ngay trên CPU** (không chép trọng số qua PCIe). | `[ĐO]` |
| **`vLLM` — `--cpu-offload-gb 4` (ngay cả với model nhỏ `Qwen3-8B-AWQ` `5.69 GiB`)** | **`6.35 tok/s`** `[6.32–6.37]` *(tụt `-89.5%` từ `60.65 tok/s`)* | N/A (`~7.0 tok/s` prompt throughput trong log engine) | N/A | `vLLM` không hỗ trợ thực thi lai (hybrid CPU expert compute) cho các tầng offload: cờ `--cpu-offload-gb` chỉ cất trọng số trên RAM (giảm VRAM model xuống `2.33 GiB`) rồi **DMA toàn bộ `3.38 GiB` trọng số qua bus PCIe về GPU ở mỗi bước sinh token** (`3.38 GiB × 6.35 tok/s ≈ 21.5 GiB/s`, nghẽn cứng băng thông PCIe). Với model 30B-A3B (`~17 GB`), cần `--cpu-offload-gb >= 8`, tốc độ còn `< 3 tok/s`. | `[ĐO]`, `[ĐỌC MÃ]` |

---

## 4. PHA V4 & V5: SPIKE GỘP 2 CARD GIỮA LINUX (`10.42.0.1`) VÀ WINDOWS 10 WSL2 (`10.42.0.225` NAT) SO VỚI `llama.cpp` MODE C

*Nguồn dữ liệu thô:* `ai-compute-fabric/benchmarks/reports/p10_v4_spike.jsonl`, `reports/logs/p10_v4_spike.log`, và `ai-compute-fabric/benchmarks/reports/p10_v3_llama_merged.jsonl`.

### 4.1. Đo băng thông và độ trễ mạng thực tế giữa Linux (`10.42.0.1`) và WSL2 (`172.30.76.133` sau NAT `10.42.0.225`) (`V4.1` — `[ĐO]`)

| Chiều kết nối | Giao thức / Cơ chế | Độ trễ (`ping` RTT min / avg / max) | Băng thông TCP truyền `512 MiB` | Nhận xét kỹ thuật | Nhãn |
|---|---|---|---|---|---|
| **`Linux (10.42.0.1) -> Windows Host (10.42.0.225)`** | Cáp trực tiếp 2.5 GbE (`enp133s0`) | **`0.164 / 0.450 / 0.756 ms`** | **`~2.39 Gbps`** | Đường truyền vật lý 2.5 GbE ổn định, dùng trực tiếp bởi `ggml-rpc-server.exe` (`10.42.0.225:50052`). | `[ĐO]` |
| **`WSL2 (172.30.76.133) -> Linux (10.42.0.1:15201)`** | Chiều đi trực tiếp qua Hyper-V Virtual Switch NAT | **`0.360 / 0.609 / 1.036 ms`** | **`2.369–2.458 Gbps`** *(nhận `2.352–2.354 Gbps`)* | Chiều từ trong WSL2 đi ra ngoài qua NAT của nhân Windows đạt tối đa băng thông cáp 2.5 GbE. | `[ĐO]` |
| **`Linux (10.42.0.1) -> WSL2 (172.30.76.133:15202)`** | Chiều vào qua `netsh interface portproxy` (`0.0.0.0:15202 -> 172.30.76.133:15202`) | N/A (`portproxy` chỉ chuyển tiếp TCP) | **`1.056–1.297 Gbps`** *(nhận `1.045–1.282 Gbps`)* | Dịch vụ `iphlpsvc` (`netsh interface portproxy`) của Windows 10 chạy ở user-space, **làm mất `-45.8%` đến `-55.8%` băng thông TCP chiều vào WSL2**. | `[ĐO]` |

### 4.2. Kết quả thử nghiệm khởi tạo 2 nút (`torch.distributed` và `vLLM --nnodes 2 --pipeline-parallel-size 2`) (`V4.2` & `V4.3`)

| Bước thử nghiệm | Cấu hình & Cổng đã mở | Kết quả thực tế | Nguyên nhân gốc rễ (`[ĐO]` + `[ĐỌC MÃ]`) | Nhãn |
|---|---|---|---|---|
| **Thử nghiệm 1 (`V4.2a`): `torch.distributed` với backend `gloo` (CPU)** | `MASTER_ADDR=10.42.0.1`, `MASTER_PORT=29500`, `GLOO_SOCKET_IFNAME=enp133s0` (Linux) / `eth0` (WSL2), `world_size=2` | **THÀNH CÔNG (`[ĐO]`)** — `dist.all_reduce` trả về `[3.0, 3.0, 3.0, 3.0]` trên cả Rank 0 và Rank 1. | Trong `Gloo` với `world_size=2`, Rank 1 (`172.30.76.133`) chủ động mở kết nối TCP chiều đi (`connect()`) tới địa chỉ lắng nghe của Rank 0 (`10.42.0.1`), nên đi xuyên qua NAT chiều ra thành công. | `[ĐO]` |
| **Thử nghiệm 2 (`V4.2b`): `torch.distributed` với backend `nccl` (GPU)** | `MASTER_ADDR=10.42.0.1`, `MASTER_PORT=29500`, `NCCL_SOCKET_IFNAME=enp133s0` / `eth0`, `NCCL_IB_DISABLE=1`, `world_size=2` | **THẤT BẠI / TIMEOUT (`[ĐO]`)** — Treo ở bước `Initializing nccl...` cho tới khi hết timeout. | Trong `NCCL` (`bootstrapNet` / `net_socket`), mỗi rank mở một cổng TCP động ngẫu nhiên trên card mạng cục bộ (`eth0` trong WSL2 có IP `172.30.76.133`) và ghi cặp `172.30.76.133:<ephemeral_port>` vào `TCPStore`. Khi Rank 0 (`10.42.0.1`) đọc `TCPStore` và gọi `connect(172.30.76.133:<ephemeral_port>)`, gói tin không thể đi vào mạng NAT nội bộ của WSL2 và cổng là cổng động nên `netsh interface portproxy` không thể ánh xạ trước. | `[ĐO]` |
| **Thử nghiệm 3 (`V4.3`): `vLLM 0.32.0` (`--nnodes 2 --pipeline-parallel-size 2 --distributed-executor-backend mp`)** | Node 0 (`10.42.0.1`) + Node 1 (`WSL2` `--headless`), `--master-addr 10.42.0.1 --master-port 29505`, model `Qwen3-0.6B` | **THẤT BẠI (`[ĐO]`)** — `WorkerProc initialization failed` / timeout. | Ngoài điểm nghẽn `NCCL` ở trên, mã nguồn `vllm/v1/executor/multiproc_executor.py` (dòng `152–171` và `627–644`) khởi tạo thêm các hàng đợi `ZeroMQ MessageQueue` (`create_mq_broadcaster` và `create_single_reader_mq_broadcasters`) gắn vào `get_ip()` cục bộ (`172.30.76.133`) trên các cổng TCP ngẫu nhiên để truyền `SchedulerOutput` và `ModelRunnerOutput` giữa các nút. Trên Windows 10 WSL2 NAT, IP `172.30.76.133` không định tuyến được từ `10.42.0.1`. | `[ĐO]`, `[ĐỌC MÃ]` |

> **Kết luận Cổng V4 (`[ĐO]`):** **Không đạt** trên Windows 10 Build 19045 do WSL2 chỉ có chế độ mạng `NAT` (thiếu `networkingMode=mirrored` của Windows 11 22H2+). Theo đúng kế hoạch Phần 3 (Pha V4), dừng chuyển sang V5 cho `vLLM` và giữ `llama.cpp` Mode C (`ggml-rpc-server.exe`) cho kịch bản gộp 2 card.

---

### 4.3. Bảng 4C — Số liệu đối chứng gộp 2 card `llama.cpp` Mode C (`RTX 3060` + `RTX 5070` qua `ggml-rpc-server.exe`) đo bằng `p10_client.py` (`[ĐO]`)

Để bảng so sánh kịch bản gộp 2 card có số liệu `[ĐO]` mới nhất bằng cùng công cụ `p10_client.py` (không dùng số cũ làm mẫu số), chúng tôi đã đo trực tiếp `Qwen3-30B-A3B-Q4_K_M.gguf` (`18.56 GB`, `--rpc 10.42.0.225:50052 -ngl 99 --split-mode layer --tensor-split 52,48 -fa on -ctk f16 -ctv f16 -t 8 -Cr 0-7 --cpu-strict 1`):

| Hạng mục đo (`Qwen3-30B-A3B-Q4_K_M.gguf` `18.56 GB`, gộp 2 card 24 GiB VRAM) | `llama.cpp` Mode C (`pc-001` Linux + `pc-win-01` Windows RPC `:50052`) | `vLLM` (`--nnodes 2 --pipeline-parallel-size 2` qua Windows 10 WSL2 NAT) | Nhãn |
|---|---|---|---|
| **Trạng thái vận hành** | **Chạy ổn định (`startup = 18.4 s`)** | **Không chạy được qua Windows 10 WSL2 NAT** (kẹt ở `NCCL` ring & `ZeroMQ MessageQueue` IP nội bộ `172.30.76.133`) | `[ĐO]` |
| **Decode 1 người dùng (`tok/s`, 7 lần lặp)** | **`127.60 tok/s`** *(min `124.90`, max `129.23`, mean `127.39`, stdev `1.33`)* | N/A | `[ĐO]` |
| **Prefill `2k` (`~2,068` tok, 3 lần lặp)** | **TTFT = `1,243.70 ms`** `[1240.2–1267.2]` (**`1,659.45 tok/s`**) | N/A | `[ĐO]` |
| **Prefill `8k` (`~8,170` tok, 3 lần lặp)** | **TTFT = `4,885.58 ms`** `[4858.4–5187.6]` (**`1,665.72 tok/s`**) | N/A | `[ĐO]` |
| **Prefill `24k` (`~22,800` tok, 3 lần lặp)** | **TTFT = `14,775.32 ms`** `[14750.5–14858.9]` (**`1,540.14 tok/s`**) | N/A | `[ĐO]` |
| **Đa người dùng `C=1` (`prompt=512`, `max_tokens=128`)** | **Tổng `75.19 tok/s`** *(decode đơn: `121.52 tok/s`, TTFT P95: `630.64 ms`, lỗi: `0/1`)* | N/A | `[ĐO]` |
| **Đa người dùng `C=4` (`-c 8192 -np 4`)** | **Tổng `162.05 tok/s`** *(min user: `57.44 tok/s`, TTFT P95: `1,094.04 ms`, lỗi: `0/4`)* | N/A | `[ĐO]` |
| **Đa người dùng `C=8` (`-c 16384 -np 8`)** | **Tổng `183.72 tok/s`** *(min user: `28.05 tok/s`, TTFT P95: `1,955.23 ms`, lỗi: `0/8`)* | N/A | `[ĐO]` |
| **Đa người dùng `C=16` (`-c 32768 -np 16`)** | **Tổng `233.14 tok/s`** *(min user: `16.12 tok/s`, TTFT P95: `3,693.02 ms`, lỗi: `0/16`)* | N/A | `[ĐO]` |
| **VRAM sử dụng (`pc-001` RTX 3060 / `pc-win-01` RTX 5070)** | `11,645 MiB` (RTX 3060) + `9,912 MiB` (RTX 5070) | N/A | `[ĐO]` |

---

## 5. CẢNH BÁO CHI PHÍ VẬN HÀNH, SAI LỆCH SO VỚI KẾ HOẠCH VÀ ĐIỀU CHƯA ĐO

### 5.1. Chi phí vận hành thêm nếu chọn `vLLM` cho kịch bản đơn card (`[ĐO]` & `[ĐỌC MÃ]`)

1. **Thời gian khởi động (Cold/Warm Start) lâu hơn 20–35 lần (`[ĐO]`):**
   - `llama.cpp` khởi động `Qwen3-8B-Q4_K_M.gguf` trong **`3.1–4.2 giây`**.
   - `vLLM` mất **`68–116 giây`** để nạp safetensors, chạy `torch.compile` (Inductor) và chụp CUDAGraph. Khi tích hợp qua `worker-guard`, `--engine-start-timeout` bắt buộc phải đặt $\ge 180\text{s}$ (thay vì $60\text{s}$).
2. **Chiếm dụng VRAM cố định và giới hạn WDDM trên Windows WSL2 (`[ĐO]`):**
   - `llama.cpp` chỉ cấp phát đúng lượng VRAM theo `-c` (`8,191–8,250 MiB` ở `-c 24576`, chừa trống `~4 GiB` VRAM).
   - `vLLM` cấp phát trước toàn bộ vùng đệm PagedAttention (`10,929–10,998 MiB`). Đặc biệt trên Windows WSL2 (WDDM), `cudaMemGetInfo()` chỉ thấy tối đa `90.30%` VRAM (`10.76 GiB`), nên `--gpu-memory-utilization` không được đặt quá `0.895–0.90`.
3. **Hai bản trọng số trên đĩa cho cùng một model (`[ĐO]`):**
   - `vLLM` cần thư mục Hugging Face `.safetensors` (`AWQ` / `GPTQ-Int4`, `5.69 GiB` cho `Qwen3-8B-AWQ`), trong khi `llama.cpp` dùng `.gguf` (`4.68 GiB`). Nếu chạy hỗn hợp (`vLLM` cho model 8B đơn card, `llama.cpp` cho model 30B MoE / gộp 2 card), không cần nhân đôi cùng một model nếu mỗi model được gắn cố định với một engine.
4. **Khác biệt ánh xạ kiểu KV cache trong adapter (`[ĐỌC MÃ]`):**
   - Trong `ai-compute-fabric/inference/adapters/vllm/adapter.py`, `kv_type="q8_0"` được ánh xạ sang `--kv-cache-dtype fp8` của `vLLM` (khác định dạng lượng tử khối `q8_0` của `ggml`). Trong toàn bộ phiên P10, cả hai bên đều đo ở KV cache 16-bit (`f16`/`auto`) để đảm bảo công bằng.

### 5.2. Sai lệch nhỏ so với kế hoạch gốc (ghi nhận theo Quy tắc 8)

1. **Độ dài ngữ cảnh tối đa ở Pha V2 (`24,576` token):** Khi khởi động `vLLM` với `Qwen3-8B-AWQ` (`5.71 GiB` trọng số) trên card 12 GiB, `vLLM` cấp được tối đa **`26,528` token KV cache** trên Linux (`--gpu-memory-utilization 0.92`) và **`26,448` token KV cache** trên WSL2 (`--gpu-memory-utilization 0.895 --max-cudagraph-capture-size 16`). Vì vậy cả hai engine đều được đặt cố định ở `-c 24576` / `--max-model-len 24576` và bậc prefill cao nhất là `~22,800` token (đúng bậc `24k` của kế hoạch).
2. **Kiểm tra `Ray` ở Pha V4.3:** Môi trường `.aicf-work/venv-vllm` cài `vllm 0.32.0` sử dụng executor đa nút nội bộ (`--distributed-executor-backend mp` với `torch.distributed` + `ZeroMQ MessageQueue`, không cài gói `ray`). Chúng tôi đã kiểm tra trực tiếp cả `torch.distributed` (`gloo` và `nccl`) lẫn `vLLM --nnodes 2 --pipeline-parallel-size 2 --distributed-executor-backend mp` và xác minh điểm nghẽn nằm ở tầng `NCCL` socket ring + `ZeroMQ MessageQueue` quảng bá IP NAT nội bộ `172.30.76.133` (cùng bản chất với lỗi quảng bá IP nội bộ của `Ray`).

### 5.3. Danh sách những điều CHƯA đo (`[CHƯA XÁC MINH]`)

1. **Gộp 2 card `vLLM` (`Pha V5` với `Qwen3-30B-A3B-GPTQ-Int4`):** Chưa đo vì Cổng V4 không đạt trên Windows 10 Build 19045 (WSL2 NAT chặn kết nối ngược `NCCL` / `ZeroMQ`). Muốn chạy `vLLM` gộp 2 card cần một trong hai thay đổi hạ tầng: (a) nâng `pc-win-01` lên **Windows 11 22H2+** để bật `networkingMode=mirrored` trong `.wslconfig`, hoặc (b) chạy **Linux native** (dual-boot) trên `pc-win-01` `[SUY LUẬN]`.
2. **Chất lượng và tốc độ của `--kv-cache-dtype fp8` trên `vLLM`:** Chưa đo trong P10 (P10 giữ cố định KV cache 16-bit trên cả hai engine).
3. **Tải tương tác thực tế từ VS Code / Cline trong 30 phút liên tục:** P10 mới đo bằng client tổng hợp `p10_client.py` (tổng thời gian chạy thực tế trên mỗi card ~25–30 phút không có bất kỳ lỗi OOM hay crash nào `[ĐO]`).

---

## 6. TRẠNG THÁI DỊCH VỤ TRƯỚC VÀ SAU KHI ĐO (`[ĐO]`)

Toàn bộ dịch vụ trên cả hai máy đã được khôi phục về đúng trạng thái ban đầu trước P10:

| Máy / Dịch vụ | Trạng thái TRƯỚC P10 (`p10_v0_initial_state.json`) | Trạng thái SAU P10 (đã khôi phục & kiểm tra) | Nhãn |
|---|---|---|---|
| **`pc-win-01` — `netsh interface portproxy`** | Trống (không có rule chuyển cổng) | Trống (`netsh interface portproxy reset` đã xóa sạch mọi rule thử nghiệm) | `[ĐO]` |
| **`pc-win-01` — WSL2 (`Ubuntu-24.04`)** | Chưa cài | Đã dừng hoàn toàn (`wsl --shutdown`), không chiếm RAM hay VRAM | `[ĐO]` |
| **`pc-win-01` — `AICFWorkerGuard` & `ggml-rpc-server.exe`** | `RUNNING` (`Automatic`), chế độ `rpc-worker` (`:50052`) | `RUNNING` (`Automatic`), `worker-guard.exe` + `ggml-rpc-server.exe` đang phục vụ cổng `50052` | `[ĐO]` |
| **`pc-001` — `site-gateway`, `bestofn.service`, `worker-guard`** | Đang chạy (`:18080`, `:18099`, `:8099`) | Đang chạy (`:18080`, `:18099`, `:8099`) | `[ĐO]` |
| **`pc-001` — `llama-server` (Chế độ C gộp 2 card)** | `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (`-c 32768 -np 1 --rpc 10.42.0.225:50052 --tensor-split 52,48`) | `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (`status: ready`, `mode: merged`, `pc-001` + `pc-win-01`) | `[ĐO]` |

---

## 7. DANH MỤC HIỆN VẬT BÀN GIAO

- **Mã nguồn client & runner:**
  - `ai-compute-fabric/benchmarks/hardware/p10_client.py`
  - `ai-compute-fabric/benchmarks/hardware/p10_v2_quality.py`
  - `ai-compute-fabric/benchmarks/hardware/p10_v2_runner.py`
  - `ai-compute-fabric/benchmarks/hardware/p10_v2_win_runner.py`
  - `ai-compute-fabric/benchmarks/hardware/p10_v3_v4_runner.py`
  - `ai-compute-fabric/benchmarks/hardware/p10_sglang_lmdeploy_runner.py`
- **Dữ liệu thô (`.jsonl`):**
  - `ai-compute-fabric/benchmarks/reports/p10_v0_initial_state.json`
  - `ai-compute-fabric/benchmarks/reports/p10_v2_pc001.jsonl` (35 bản ghi ABAB + Mutation + Quality trên `RTX 3060`)
  - `ai-compute-fabric/benchmarks/reports/p10_v2_pcwin01.jsonl` (38 bản ghi ABAB + Mutation + Quality + V2.3 WSL2 `llama.cpp` trên `RTX 5070`)
  - `ai-compute-fabric/benchmarks/reports/p10_v25_moe_single_card.jsonl` (MoE 30B `-ncmoe 20` vs `vLLM --cpu-offload-gb`)
  - `ai-compute-fabric/benchmarks/reports/p10_v4_spike.jsonl` (Spike mạng NAT, `gloo`, `nccl`, `vLLM --nnodes 2`)
  - `ai-compute-fabric/benchmarks/reports/p10_v3_llama_merged.jsonl` (Đối chứng gộp 2 card `llama.cpp` Mode C bằng `p10_client.py`)
  - `ai-compute-fabric/benchmarks/reports/p10_sglang_lmdeploy.jsonl` (Dữ liệu thực nghiệm `SGLang 0.5.19` & `LMDeploy 0.18.0` trên 1 card, CPU offload, Best-of-N RadixAttention, và gộp 2 card)
- **Nhật ký chi tiết (`reports/logs/p10_*.log`):**
  - `p10_v1_wsl2_qwen06b.log`, `p10_v2_pc001_*.log`, `p10_v2_pcwin01_*.log`, `p10_v23_pcwin01_wsl2_llama.log`, `p10_v25_*.log`, `p10_v3_llama_merged_qwen30b.log`, `p10_v4_spike.log`, `p10_pc001_lmdeploy_turbomind.log`, `p10_pc001_sglang*.log`, `p10_pcwin01_lmdeploy_*.log`, `p10_pcwin01_sglang.log`, `p10_2card_*.log`.

---

## 10. Phần Mở Rộng: Thực Nghiệm Đối Đầu 4 Giải Pháp (`llama.cpp` vs `vLLM` vs `SGLang` vs `LMDeploy`) `[ĐO]`

Tất cả phép đo dưới đây sử dụng cùng bộ khung `p10_client.py` và `p10_v2_quality.py`, dữ liệu thô lưu tại `reports/p10_sglang_lmdeploy.jsonl`.

### 10.1. Trường hợp 1A — Chạy riêng 1 card `RTX 3060 12GB` (`pc-001`, Linux Native) (`Qwen3-8B`) `[ĐO]`

| Hạng mục đo | `llama.cpp` (`Q4_K_M`) | `vLLM 0.32.0` (`AWQ`) | `SGLang 0.5.19` (`AWQ Marlin`) | `LMDeploy 0.18.0` (`TurboMind AWQ`) |
|---|---:|---:|---:|---:|
| **Decode 1-user (`median`, `min–max`)** | **`63.25 tok/s`** (`62.88–63.48`) | `60.65 tok/s` (`60.53–60.75`) | **`62.98 tok/s`** (`62.97–63.26`) | **`61.56 tok/s`** (`61.56–61.57`) |
| **Prefill `2k` TTFT (`ms` / `tok/s`)** | **`1,136.7 ms`** (`1,843 tok/s`) | `1,226.7 ms` (`1,704 tok/s`) | `1,277.3 ms` (`1,639 tok/s`) | `1,194.9 ms` (`1,763 tok/s`) |
| **Prefill `8k` TTFT (`ms` / `tok/s`)** | **`4,827.0 ms`** (`1,696 tok/s`) | `5,357.3 ms` (`1,528 tok/s`) | `6,271.2 ms` (`1,303 tok/s`) | `5,056.0 ms` (`1,619 tok/s`) |
| **Đa người dùng `C = 1` (`agg tok/s`)** | **`55.28 tok/s`** | `51.78 tok/s` | `53.69 tok/s` | `52.46 tok/s` |
| **Đa người dùng `C = 4` (`agg tok/s`)** | `126.42 tok/s` | **`142.31 tok/s`** | `130.64 tok/s` | `141.78 tok/s` |
| **Đa người dùng `C = 8` (`agg tok/s`)** | `147.72 tok/s` | `203.66 tok/s` | **`207.41 tok/s` (`+40.4%`)** | `202.10 tok/s` |
| **Đa người dùng `C = 16` (`agg tok/s`)** | `234.23 tok/s` | `256.96 tok/s` | `206.96 tok/s` | **`260.03 tok/s` (`+11.0%`)** |
| **Best-of-N (`C = 8` chung prefix `2048` tok)** | — | — | `77.42 tok/s` (tắt Radix) $\to$ **`250.81 tok/s` (bật RadixAttention, `3.24x`)** | **`247.56 tok/s`** (`--enable-prefix-caching`) |
| **Chất lượng 15 bài code Python + unit test** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`15/15` (`100%`)** |

### 10.2. Trường hợp 1B — Chạy riêng 1 card `RTX 5070 12GB` (`pc-win-01`, Blackwell `sm_120`, Windows 10 / WSL2) `[ĐO]`

| Hạng mục đo | `llama.cpp` (Win Native `Q4_K_M`) | `llama.cpp` (WSL2 `Q4_K_M`) | `vLLM 0.32.0` (WSL2 `AWQ`) | `SGLang 0.5.19` (WSL2 `AWQ Marlin`) | `LMDeploy 0.18.0` (WSL2 `PyTorch AWQ`) | `LMDeploy 0.18.0` (WSL2 `TurboMind`) |
|---|---:|---:|---:|---:|---:|---:|
| **Decode 1-user (`median`)** | `114.93 tok/s` | `107.98 tok/s` | `112.48 tok/s` | **`115.05 tok/s`** (`115.02–115.14`) | `90.37 tok/s` | *(Sinh rác `265.1 tok/s`)* |
| **Prefill `2k` TTFT (`ms` / `tok/s`)** | **`455.5 ms`** (`4,589 tok/s`) | `462.5 ms` (`4,534 tok/s`) | `545.4 ms` (`3,856 tok/s`) | `552.6 ms` (`3,808 tok/s`) | `691.8 ms` (`3,044 tok/s`) | *(Sinh rác)* |
| **Prefill `8k` TTFT (`ms` / `tok/s`)** | **`1,932.6 ms`** (`4,234 tok/s`) | `1,967.2 ms` (`4,162 tok/s`) | `2,357.1 ms` (`3,473 tok/s`) | `2,513.6 ms` (`3,256 tok/s`) | `2,798.7 ms` (`2,924 tok/s`) | *(Sinh rác)* |
| **Đa người dùng `C = 1`** | **`101.44 tok/s`** | — | `99.08 tok/s` | `99.80 tok/s` | `78.67 tok/s` | *(Sinh rác)* |
| **Đa người dùng `C = 4`** | `264.27 tok/s` | — | **`287.58 tok/s`** | `237.92 tok/s` | `234.80 tok/s` | *(Sinh rác)* |
| **Đa người dùng `C = 8`** | `385.73 tok/s` | — | `422.64 tok/s` | **`437.78 tok/s` (`+13.5%`)** | `304.58 tok/s` | *(Sinh rác)* |
| **Đa người dùng `C = 16`** | `497.21 tok/s` | — | **`537.51 tok/s` (`+8.1%`)** | `432.89 tok/s` | `441.01 tok/s` | *(Sinh rác)* |
| **Best-of-N (`C = 8` chung prefix `2048` tok)** | — | — | — | **`475.64 tok/s`** (`TTFT P50 = 588.6 ms`) | — | — |
| **Chất lượng 15 bài code Python + unit test** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`0/15` (`0%` — LỖI `sm_120`)** |

> **Phát hiện quan trọng từ kiểm tra chất lượng (`quality_15`) trên `RTX 5070` (`sm_120`) `[ĐO]`:**
> Backend C++ `TurboMind` của `LMDeploy 0.18.0` khởi chạy trên `RTX 5070` không văng ngoại lệ, nhưng các kernel GEMM C++ chưa hỗ trợ kiến trúc Blackwell `sm_120` nên trả về chuỗi token vô nghĩa ở tốc độ giả tạo `265.1 tok/s` và **trượt toàn bộ `0/15` bài kiểm thử code Python**. Khi chuyển sang `--backend pytorch`, `LMDeploy` đạt `15/15` bài test nhưng tốc độ chỉ đạt `90.37 tok/s` (1-user) và `441.01 tok/s` (`C=16`). Ngược lại, **`SGLang 0.5.19`** dùng trình biên dịch JIT `nvcc` CUDA 13 (`-gencode=arch=compute_120f,code=sm_120f`) biên dịch trực tiếp `sgl_kernel_jit_awq_marlin_repack.so` cho Blackwell, đạt **`115.05 tok/s`** (vượt cả `llama.cpp` Windows native `114.93 tok/s` dù chạy trong WSL2!) và đạt **`15/15`** bài code.

### 10.3. Trường hợp 1C — Offload vượt VRAM 1 card 12 GB (Chế độ B `smart_1gpu`) `[ĐO]`

| Giải pháp & Cấu hình trên `RTX 3060 12GB` | Model thử nghiệm | Tốc độ Decode 1-user (`median`) | Đánh giá thực tế |
|---|---|---:|---|
| **`llama.cpp` (`-ngl 99 -ncmoe 20`)** | `Qwen3-30B-A3B-Q4_K_M.gguf` (`30.5B` MoE) | **`47.05 tok/s`** (`C=4`: **`53.99 tok/s`**) | **CHẠY TỐT** — Chỉ đẩy các tensor chuyên gia MoE sang RAM CPU; mỗi token chỉ đọc 8/128 experts (`~3B`). |
| **`vLLM 0.32.0` (`--cpu-offload-gb 4`)** | `Qwen3-8B-AWQ` (`8B` dense) | **`6.35 tok/s`** (`-89.5%`) | **KHÔNG KHẢ THI** — `uva_offloading` đọc nguyên lớp qua PCIe ở mọi bước token. |
| **`SGLang 0.5.19` (`--cpu-offload-gb 4`)** | `Qwen3-8B-AWQ` (`8B` dense) | **`6.19 tok/s`** (`-90.2%`) | **KHÔNG KHẢ THI** — Cùng cơ chế offload toàn lớp qua PCIe như `vLLM`. |
| **`LMDeploy 0.18.0` (`TurboMind`)** | `Qwen3-8B-AWQ` | Báo lỗi `allocator.cc:49 CUDA out of memory` khi thiếu VRAM | **KHÔNG HỖ TRỢ** offload MoE xuống RAM CPU. |

### 10.4. Trường hợp 2 — Gộp 2 card (`pc-001` Linux `10.42.0.1` $\leftrightarrow$ `pc-win-01` Windows 10 `10.42.0.225` WSL2 NAT) `[ĐO]`

| Giải pháp gộp 2 card | Giao thức mạng | Trạng thái `[ĐO]` | Kết quả đo / Nguyên nhân gốc rễ từ log thực nghiệm |
|---|---|---|---|
| **`llama.cpp` (`--rpc 10.42.0.225:50052 --split-mode layer --tensor-split 52,48`)** | 1 cổng TCP tĩnh `:50052` chạy thẳng trên Windows native (`ggml-rpc-server.exe`) | **THÀNH CÔNG (`PASS`)** | Chạy `Qwen3-30B-A3B-Q4_K_M.gguf` đạt **`127.60 tok/s`** (1-user), prefill `2k/8k/24k` đạt **`1,659 / 1,666 / 1,540 tok/s`**, đa người dùng `C=4/8/16` đạt **`162.05 / 183.72 / 233.14 tok/s`**. |
| **`vLLM 0.32.0` (`--nnodes 2 --pipeline-parallel-size 2`)** | `torch.distributed` (`NCCL` + `ZMQ` cổng động) qua WSL2 NAT + `portproxy` | **THẤT BẠI (`TIMEOUT 90s`)** | Rank 1 trong WSL2 tự quảng bá IP nội bộ `172.30.76.133:<ephemeral_port>`; Rank 0 trên Linux (`10.42.0.1`) không kết nối được vào mạng NAT ảo của Windows 10 (`multiproc_executor.py:640`). |
| **`SGLang 0.5.19` (`--nnodes 2 --pp-size 2 --dist-init-addr 10.42.0.1:29500`)** | `torch.distributed` (`ProcessGroupNCCL` `nccl==2.29.7`) qua WSL2 NAT + `portproxy` | **THẤT BẠI (`TIMEOUT 45s`)** | Kết nối `TCPStore` cổng `29500` thành công, nhưng khi khởi tạo `ProcessGroupNCCL`, Rank 1 quảng bá `SocketImpl(addr=172.30.76.133:56430)` và bị treo/đóng kết nối (`p10_2card_sglang_rank1.log:11`). |
| **`LMDeploy 0.18.0` (`--nnodes 2 --tp 2 --dist-init-addr 10.42.0.1:29500`)** | `ZMQMPEngine` + `Ray` / `NCCL` | **THẤT BẠI (`12s`)** | Báo lỗi `<Dist> check failed! Multi-nodes support requires ray` và tiến trình `ZMQMPEngine` dừng (`RPCServerDeadError` tại `p10_2card_lmdeploy_rank1.log:2-45`). Ngay cả khi chạy qua Ray, tầng data plane bên dưới vẫn dùng `NCCL` nên vẫn bị chặn bởi WSL2 NAT trên Windows 10. |


