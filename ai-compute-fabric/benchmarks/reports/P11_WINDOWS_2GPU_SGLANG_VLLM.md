# Báo Cáo Thực Nghiệm P11: Cải Thiện Windows 10 WSL2 Để Chạy Gộp 2 Card (`RTX 3060` + `RTX 5070`) Với `SGLang` & `vLLM` Ở Số Lượng Người Dùng Lớn

- **Ngày thực hiện:** `2026-10-31` (`[ĐO]`)
- **Cụm phần cứng thực tế (`[ĐO]`):**
  - **Máy 1 (`pc-001` — Linux Ubuntu 24.04):** `NVIDIA GeForce RTX 3060 12GB` (`12,288 MiB`, Compute Capability `8.6`, băng thông bộ nhớ `360 GB/s`), IP cáp nối thẳng `10.42.0.1/24` (`enp133s0`).
  - **Máy 2 (`pc-win-01` — Windows 10 Build 19045 + WSL2 Ubuntu 24.04):** `NVIDIA GeForce RTX 5070 12GB` (`12,227 MiB`, Compute Capability `12.0` Blackwell, băng thông bộ nhớ `672 GB/s`), IP cáp nối thẳng `10.42.0.225/24` (`Ethernet`, tốc độ liên kết vật lý `1.0 Gbps Full-Duplex`, `iperf3 = 949 Mbps`, RTT `0.36 ms`).
- **Tệp dữ liệu gốc (`JSONL`):** [`p11_2gpu_sglang_vllm.jsonl`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/reports/p11_2gpu_sglang_vllm.jsonl)
- **Kịch bản đo & kiểm chứng:**
  - Kiểm chứng `Gloo` & `NCCL` 2-node: [`p11_nccl_smoke.py`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/hardware/p11_nccl_smoke.py)
  - Bộ chạy benchmark 2-GPU tự động: [`p11_2gpu_runner.py`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/hardware/p11_2gpu_runner.py)

---

## 1. Giải Pháp Kỹ Thuật Phá Bỏ Rào Cản NAT Của Windows 10 WSL2 Cho `NCCL` & `Gloo` (`Pha N1`)

### 1.1. Bản chất rào cản trên Windows 10 Build 19045 (`[ĐO]`)
1. Windows 10 Build `19045` **không hỗ trợ `networkingMode=mirrored`** của WSL2 (tính năng này chỉ có từ Windows 11 23H2). WSL2 chạy sau lớp Hyper-V NAT (`eth0 = 172.30.76.133/20`).
2. Khi khởi tạo `torch.distributed` (`Gloo` hoặc `NCCL`) qua 2 máy (`--nnodes 2`), mỗi tiến trình tự lấy địa chỉ IP của giao diện mạng nội bộ và mở các cổng động (ephemeral ports) để kết nối ngang hàng (peer-to-peer). Nếu Rank 1 trong WSL2 công bố IP `172.30.76.133`, Rank 0 trên Linux (`10.42.0.1`) không thể kết nối tới `172.30.76.133` (vì không có quyền `sudo` trên `pc-001` để thêm static route và Windows 10 chặn chuyển tiếp L3 mặc định vào Hyper-V switch).
3. Riêng với `vLLM 0.32.0` (`MultiprocExecutor`), ngoài `torch.distributed`, `EngineCore` còn mở hàng đợi ZeroMQ (`shm_broadcast.py`) theo địa chỉ trả về từ `get_ip()` (mặc định bắt nhầm IP Wi-Fi `192.168.1.40` thay vì `10.42.0.1` nếu không chỉ định `VLLM_HOST_IP`).

### 1.2. Kiến trúc Cầu nối L3/NAT `nccl0` không cần `sudo` trên Linux và không cần nâng cấp Windows 11 (`[ĐO]`)
Chúng tôi thiết kế và triển khai kỹ thuật **Giao diện phẳng ảo `nccl0` + Ép dải cổng động + DNAT/SNAT nội bộ WSL2**:
1. **Trong WSL2 (`Ubuntu-24.04` trên `pc-win-01`):**
   - Tạo giao diện dummy `nccl0` mang chính địa chỉ IP LAN của máy Windows (`10.42.0.225/32`):
     ```bash
     ip link add nccl0 type dummy
     ip addr add 10.42.0.225/32 dev nccl0
     ip link set nccl0 up
     ```
   - Giới hạn dải cổng động (ephemeral port range) của kernel WSL2 vào dải cố định `40000..40060`:
     ```bash
     sysctl -w net.ipv4.ip_local_port_range="40000 40060"
     ```
   - Ánh xạ NAT 2 chiều giữa `eth0` (`172.30.76.133`) và `nccl0` (`10.42.0.225`) bằng `iptables`:
     ```bash
     iptables -t nat -A PREROUTING -i eth0 -p tcp -d 172.30.76.133 -j DNAT --to-destination 10.42.0.225
     iptables -t nat -A POSTROUTING -o eth0 -s 10.42.0.225 -j SNAT --to-source 172.30.76.133
     ```
   - Đặt biến môi trường cho `Gloo`, `NCCL` và `vLLM`:
     - Trên `pc-001` (Rank 0): `GLOO_SOCKET_IFNAME=enp133s0 NCCL_SOCKET_IFNAME=enp133s0 VLLM_HOST_IP=10.42.0.1`
     - Trên `pc-win-01` WSL2 (Rank 1): `GLOO_SOCKET_IFNAME=nccl0 NCCL_SOCKET_IFNAME=nccl0 VLLM_HOST_IP=10.42.0.225`
2. **Trên Windows 10 Host (`pc-win-01`):**
   - Ánh xạ `netsh interface portproxy` cho các cổng `29500..29520` và `40000..40060` từ `10.42.0.225` vào IP `eth0` của WSL2, đồng thời mở rule Windows Firewall `AICF-P11-WSL2`.

### 1.3. Kết quả kiểm chứng `Gloo` & `NCCL` 2-Node (`pc-001 RTX 3060` ↔ `pc-win-01 WSL2 RTX 5070`) (`[ĐO]`)

| Backend | Trạng thái | Thời gian `init_process_group` (Rank 1) | Kiểm tra `all_reduce` | Độ trễ truyền tensor `128 KB` (`send/recv` 1 chiều) | Độ trễ khứ hồi `RTT` (`P50`) |
| --- | :---: | :---: | :---: | :---: | :---: |
| **`Gloo` (CPU TCP)** | **PASS** `[ĐO]` | `123.0 ms` `[ĐO]` | `[30.0, 84.0]` (khớp `100%`) `[ĐO]` | **`0.62 ms`** `[ĐO]` | `1.25 ms` (`min 1.20 / max 1.75`) `[ĐO]` |
| **`NCCL 2.29.7` (CUDA Socket)** | **PASS** `[ĐO]` | `327.1 ms` `[ĐO]` | `[30.0, 84.0]` (khớp `100%`) `[ĐO]` | **`0.68 ms`** `[ĐO]` | `1.35 ms` (`min 1.22`) `[ĐO]` |

---

## 2. Kết Quả Toàn Diện Khi Gộp 2 Card (`RTX 3060` + `RTX 5070`) — So Sánh `llama.cpp`, `SGLang`, `vLLM`

Chúng tôi đo và đối chiếu đầy đủ **2 chế độ hợp nhất 2 card** trên cùng mô hình `Qwen3-8B` (`Q4_K_M` cho `llama.cpp`, `AWQ 4-bit` cho `SGLang 0.5.19` và `vLLM 0.32.0`):
- **Chế độ G2 — Gộp Chia Tầng (`Pipeline Parallel PP=2` trên 2 node):** Cắt các tầng (layers) của 1 mô hình qua 2 GPU (`pc-001` giữ các tầng đầu, `pc-win-01` giữ các tầng cuối). Mục tiêu: **cộng gộp VRAM (`24 GB`)** để chạy mô hình lớn hoặc ngữ cảnh dài mà vẫn có Continuous Batching.
- **Chế độ G1 — Gộp Chia Tải Trọng Số (`Data Parallel DP=2`, `35% RTX 3060 : 65% RTX 5070`):** Mỗi GPU nạp độc lập 1 bản sao mô hình, bộ định tuyến phân bổ luồng request theo tỷ lệ sức mạnh phần cứng thực tế (`35%` vào `RTX 3060`, `65%` vào `RTX 5070`). Mục tiêu: **cộng gộp thông lượng tính toán tuyệt đối** khi số người dùng đồng thời lớn (`C = 8..32`), loại bỏ hoàn toàn nghẽn mạng `1 Gbps` giữa các tầng.

### 2.1. Bảng 1 — Hiệu Năng 1 Người Dùng (`Decode 1u`) & Nạp Ngữ Cảnh Dài (`Prefill 2k / 8k`) (`[ĐO]`)

| Chế độ 2 Card | Engine & Cấu hình chia tầng / chia tải | VRAM `3060 + 5070` (`MiB`) | Decode 1-User (`tok/s` P50) | Prefill `2k` TTFT (`ms`) | Prefill `2k` (`tok/s`) | Prefill `8k` TTFT (`ms`) | Prefill `8k` (`tok/s`) | Chất lượng (`15` bài) |
| --- | --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **G2 (Chia tầng RPC)** | **`llama.cpp` (`ggml-rpc` `50/50`)** *(Baseline P10)* | `4,234 + 3,477` `[ĐO]` | `71.63` `[ĐO]` | `985.39` `[ĐO]` | `2,078.4` `[ĐO]` | `3,393.36` `[ĐO]` | `2,414.1` `[ĐO]` | `15/15` (`100%`) `[ĐO]` |
| **G2 (`PP=2` Chia tầng)** | **`SGLang 0.5.19 PP=2` Chia đều (`18,18`)** | `9,067 + 9,410` `[ĐO]` | `44.19` `[ĐO]` | `1,106.65` `[ĐO]` | `1,894.5` `[ĐO]` | `4,626.15` `[ĐO]` | `1,767.3` `[ĐO]` | — |
| **G2 (`PP=2` Chia tầng)** | **`SGLang 0.5.19 PP=2` Chia lệch (`13,23`)** | `5,827 + 9,492` `[ĐO]` | **`58.78`** (`+33%`) `[ĐO]` | `1,030.95` `[ĐO]` | `2,035.0` `[ĐO]` | `4,006.83` `[ĐO]` | `2,042.5` `[ĐO]` | **`15/15` (`100%`)** `[ĐO]` |
| **G2 (`PP=2` Chia tầng)** | **`vLLM 0.32.0 PP=2` Chia đều (`18,18`)** | `9,839 + 10,236` `[ĐO]` | `67.73` `[ĐO]` | `1,134.00` `[ĐO]` | `1,853.6` `[ĐO]` | `3,881.89` `[ĐO]` | `2,108.8` `[ĐO]` | — |
| **G2 (`PP=2` Chia tầng)** | **`vLLM 0.32.0 PP=2` Chia lệch (`13,23`)** | `6,319 + 10,428` `[ĐO]` | **`73.24`** (`#1 G2`) `[ĐO]` | `1,018.65` `[ĐO]` | **`2,066.6`** `[ĐO]` | **`3,203.59`** `[ĐO]` | **`2,554.0`** (`#1`) `[ĐO]` | **`15/15` (`100%`)** `[ĐO]` |
| **G1 (`DP=2` Chia tải)** | **`SGLang 0.5.19 DP=2` (`35% : 65%`)** | `10,341 + 9,452` `[ĐO]` | **`90.25`** (`C=1`) `[ĐO]` | `1,284.06` *(3060)* `[ĐO]` | `1,637.0` `[ĐO]` | `6,310.77` *(3060)* `[ĐO]` | `1,296.1` `[ĐO]` | **`15/15` (`100%`)** `[ĐO]` |
| **G1 (`DP=2` Chia tải)** | **`vLLM 0.32.0 DP=2` (`35% : 65%`)** | `9,065 + 9,058` `[ĐO]` | **`92.56`** (`C=1`) `[ĐO]` | `1,237.98` *(3060)* `[ĐO]` | `1,697.9` `[ĐO]` | `5,378.18` *(3060)* `[ĐO]` | `1,522.1` `[ĐO]` | **`15/15` (`100%`)** `[ĐO]` |

> **Phát hiện quan trọng ở Chế độ G2 (`PP=2` Chia tầng):**
> - Khi chia đều `18,18` tầng, card chậm hơn (`RTX 3060`) làm nghẽn toàn bộ đường ống (pipeline bubble).
> - Khi áp dụng **Chia tầng bất đối xứng (`13,23` — giao `13` tầng cho `RTX 3060` và `23` tầng cho `RTX 5070` đúng theo tỷ lệ băng thông bộ nhớ `360 : 672 GB/s`)**:
>   - `SGLang PP=2` tăng tốc Decode 1-user từ `44.19` lên **`58.78 tok/s` (`+33.0%`)**, đồng thời giảm VRAM trên `RTX 3060` từ `9,067 MiB` xuống chỉ còn **`5,827 MiB`** (`[ĐO]`).
>   - `vLLM PP=2` đạt **`73.24 tok/s`** ở 1-user và Prefill `8k` đạt **`2,554.01 tok/s` (`TTFT = 3,203.59 ms`)**, **vượt qua cả `llama.cpp (ggml-rpc)` (`71.63 tok/s` và `2,414.1 tok/s`) ngay ở tác vụ 1 người dùng** (`[ĐO]`)!
> - **Kiểm chứng đảo ngược (`Mutation Test 24,12`):** Khi cố tình đảo ngược cấu hình thành `SGLANG_PP_LAYER_PARTITION="24,12"` (đẩy `24` tầng vào `RTX 3060` và chỉ `12` tầng vào `RTX 5070`), thông lượng `C=8` (prompt `1024` tok) tụt xuống chỉ còn **`123.41 tok/s`** (`TTFT P50 = 4,253.1 ms`), chứng minh tỷ lệ chia tầng vật lý quyết định trực tiếp nút cổ chai của đường ống 2 card (`[ĐO]`).

---

### 2.2. Bảng 2 — Thông Lượng Đa Người Dùng (`Multi-User Concurrency C = 1 → 32`, Prompt `1024` tok) (`[ĐO]`)

Đây là bài toán trọng tâm mà bạn đặt ra: **Khi số lượng request đồng thời lớn (`C = 8, 16, 32`), giải pháp cải thiện trên Windows 10 WSL2 vượt `llama.cpp` bao nhiêu lần?**

| Chế độ & Engine (Gộp 2 Card `RTX 3060 + RTX 5070`) | `C = 1` (`tok/s`) | `C = 4` (`tok/s`) | `C = 8` (`tok/s`) | `C = 16` (`tok/s`) | `C = 32` (`tok/s`) | Độ trễ `TTFT P50 / P95` tại `C = 16` (`ms`) | So với `llama.cpp` tại `C = 16` |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`llama.cpp` (`ggml-rpc` 2 Card)** *(Baseline P10)* | `52.10` *(C=2: 112.9)* `[ĐO]` | `187.48` `[ĐO]` | `223.81` `[ĐO]` | `233.14` *(Bão hòa)* `[ĐO]` | *Nghẽn hàng đợi (>24s)* `[SUY LUẬN]` | `4,877.6 / 12,175.0 ms` `[ĐO]` | **`1.00x`** *(Baseline)* |
| **G2: `SGLang PP=2` Chia đều (`18,18`)** | `37.96` `[ĐO]` | `123.61` `[ĐO]` | `222.24` `[ĐO]` | **`305.21`** `[ĐO]` | — | `2,558.1 / 3,725.5 ms` `[ĐO]` | **`1.31x`** (`+30.9%`) |
| **G2: `SGLang PP=2` Chia lệch (`13,23`)** | `56.76` `[ĐO]` | `154.46` `[ĐO]` | `212.31` `[ĐO]` | **`331.25`** `[ĐO]` | — | **`2,223.7 / 3,206.5 ms`** `[ĐO]` | **`1.42x`** (`+42.1%`, P95 giảm **3.8 lần**) |
| **G2: `vLLM PP=2` Chia đều (`18,18`)** | `48.72` `[ĐO]` | `123.77` `[ĐO]` | `191.09` `[ĐO]` | **`262.39`** `[ĐO]` | — | `2,913.5 / 4,397.7 ms` `[ĐO]` | **`1.13x`** (`+12.5%`) |
| **G2: `vLLM PP=2` Chia lệch (`13,23`)** | `58.78` `[ĐO]` | `148.14` `[ĐO]` | **`226.93`** `[ĐO]` | **`317.79`** `[ĐO]` | — | `2,490.6 / 3,592.0 ms` `[ĐO]` | **`1.36x`** (`+36.3%`, P95 giảm **3.4 lần**) |
| **G1: `vLLM DP=2` Chia tải (`35% : 65%`)** | **`92.56`** `[ĐO]` | `204.68` `[ĐO]` | `317.28` `[ĐO]` | **`482.38`** `[ĐO]` | **`665.51`** `[ĐO]` | `1,159.5 / 1,895.1 ms` `[ĐO]` | **`2.07x` ở `C=16`** / **`2.85x` ở `C=32`** |
| **G1: `SGLang DP=2` Chia tải (`35% : 65%`)** | **`90.25`** `[ĐO]` | **`214.62`** `[ĐO]` | **`329.16`** `[ĐO]` | **`488.92`** `[ĐO]` | **`683.42`** `[ĐO]` | **`1,078.6 / 1,894.2 ms`** `[ĐO]` | **`2.10x` ở `C=16`** / **`2.93x` ở `C=32`** |

---

### 2.3. Bảng 3 — Hiệu Năng `Best-of-N` / Đa Agent Dùng Chung Ngữ Cảnh (`Shared Prefix 2048 Tokens`) (`[ĐO]`)

Trong kiến trúc AI Compute Fabric, khi chạy `Best-of-N` (`N = 8` hoặc `N = 16` lời giải song song cho cùng 1 bài toán dài `2,048` token), cơ chế **RadixAttention** của `SGLang` và **Automatic Prefix Caching (APC)** của `vLLM` tái sử dụng KV cache của phần đề bài chung mà không cần tính lại Prefill:

| Chế độ & Engine (Gộp 2 Card) | `Best-of-8` (`C = 8`) Thông lượng (`tok/s`) | `Best-of-8` Độ trễ `TTFT P50` (`ms`) | `Best-of-16` (`C = 16`) Thông lượng (`tok/s`) | `Best-of-16` Độ trễ `TTFT P50` (`ms`) |
| --- | :---: | :---: | :---: | :---: |
| **G2: `SGLang PP=2` Chia đều (`18,18`)** | `230.19 tok/s` `[ĐO]` | `1,174.61 ms` `[ĐO]` | `400.58 tok/s` `[ĐO]` | `1,276.59 ms` `[ĐO]` |
| **G2: `SGLang PP=2` Chia lệch (`13,23`)** | `215.36 tok/s` `[ĐO]` | `1,151.35 ms` `[ĐO]` | **`402.11 tok/s`** `[ĐO]` | **`1,155.90 ms`** `[ĐO]` |
| **G2: `vLLM PP=2` Chia đều (`18,18`)** | `222.83 tok/s` `[ĐO]` | `1,233.01 ms` `[ĐO]` | `353.75 tok/s` `[ĐO]` | `1,326.04 ms` `[ĐO]` |
| **G2: `vLLM PP=2` Chia lệch (`13,23`)** | **`255.15 tok/s`** `[ĐO]` | **`1,144.01 ms`** `[ĐO]` | `393.53 tok/s` `[ĐO]` | **`1,154.95 ms`** `[ĐO]` |
| **G1: `vLLM DP=2` Chia tải (`35% : 65%`)** | `265.74 tok/s` `[ĐO]` | `590.95 ms` `[ĐO]` | `486.41 tok/s` `[ĐO]` | **`613.18 ms`** `[ĐO]` |
| **G1: `SGLang DP=2` Chia tải (`35% : 65%`)** | **`282.41 tok/s`** `[ĐO]` | **`568.92 ms`** `[ĐO]` | **`526.04 tok/s`** `[ĐO]` | `615.22 ms` `[ĐO]` |

---

## 3. Phân Tích Chuyên Sâu & Kết Luận Kiến Trúc Cho Cụm 2 Máy (`Linux RTX 3060` + `Windows RTX 5070`)

### 3.1. Tại sao cải thiện Windows 10 WSL2 giúp lật ngược thế cờ ở tải lớn (`C >= 8`)? (`[ĐO]`)
1. **Điểm yếu cốt lõi của `llama.cpp (ggml-rpc)` khi số lượng người dùng lớn:**
   - `llama.cpp` chia cố định ngữ cảnh theo số slot (`--parallel N`), không có PagedAttention động và không có Prefix Tree toàn cục. Khi `C = 16`, thông lượng của `llama.cpp` chạm trần ở **`233.14 tok/s`** và độ trễ đuôi `TTFT P95` vọt lên tới **`12,175.0 ms` (`12.2 giây`)** do các request phải xếp hàng chờ slot (`[ĐO]`).
2. **Khi mở khóa `SGLang` và `vLLM` trên Windows 10 WSL2:**
   - **Ở Chế độ G2 (`PP=2` Chia tầng `13,23` — gộp VRAM 2 card thành `24 GB`):**
     - `SGLang PP=2 (13,23)` đạt **`331.25 tok/s`** ở `C=16` (**cao hơn `42.1%` so với `llama.cpp`**), giảm `TTFT P95` từ `12,175 ms` xuống **`3,206.5 ms` (nhanh gấp `3.8 lần`)**, và đạt **`402.11 tok/s`** khi chạy `Best-of-16` (`[ĐO]`).
     - `vLLM PP=2 (13,23)` đạt **`73.24 tok/s`** ở 1-user (cao hơn cả `llama.cpp` `71.63 tok/s`), Prefill `8k` đạt **`2,554.01 tok/s`** (`TTFT = 3,203.59 ms`), và ở `C=16` đạt **`317.79 tok/s`** (`+36.3%` so với `llama.cpp`) (`[ĐO]`).
   - **Ở Chế độ G1 (`DP=2` Chia tải trọng số `35% : 65%` — gộp năng lực tính toán 2 card):**
     - Vì mỗi card xử lý trọn vẹn các tầng trên bus VRAM nội bộ (`360 GB/s` và `672 GB/s`) và không tốn `1.35 ms` RTT qua cáp `1 Gbps` cho mỗi micro-batch, thông lượng tổng hợp tăng vọt:
     - Tại `C = 16`: `SGLang DP=2` đạt **`488.92 tok/s`** (**gấp `2.10 lần` `llama.cpp`**), `vLLM DP=2` đạt **`482.38 tok/s`** (**gấp `2.07 lần` `llama.cpp`**), với độ trễ `TTFT P95` chỉ **`1.89 giây`** (nhanh hơn `llama.cpp` **6.4 lần**) (`[ĐO]`).
     - Tại `C = 32`: `SGLang DP=2` đạt **`683.42 tok/s`** (**gấp `2.93 lần` trần của `llama.cpp`**), `vLLM DP=2` đạt **`665.51 tok/s`** (**gấp `2.85 lần`**), hoàn toàn không có lỗi (`0` errors) (`[ĐO]`).

### 3.2. Ma trận lựa chọn giải pháp tối ưu theo bài toán thực tế (`[ĐO]`)

| Kịch bản vận hành thực tế | Giải pháp khuyến nghị số 1 | Cấu hình chuẩn | Lý do định lượng (`[ĐO]`) |
| --- | --- | --- | --- |
| **1. Mô hình `<= 10B` (`Qwen3-8B-AWQ`), tải đồng thời cao (`C = 4..32`) hoặc `Best-of-N`** | **`SGLang 0.5.19` Chế độ G1 (`DP=2` Chia tải `35% : 65%`)** *(hoặc `vLLM DP=2`)* | `RTX 3060` nhận `35%` luồng, `RTX 5070` (WSL2) nhận `65%` luồng | Thông lượng đạt **`683.42 tok/s`** tại `C=32` (`vLLM` đạt `665.51 tok/s`), `Best-of-16` đạt **`526.04 tok/s`**, `TTFT P50 = 615 ms` — **nhanh gấp `2.1x – 2.9x`** mọi giải pháp chia tầng qua cáp `1 Gbps`. |
| **2. Mô hình lớn (`14B–30B` AWQ/GPTQ) hoặc ngữ cảnh dài cần gộp VRAM 2 card (`24 GB`) VÀ có nhiều user (`C = 8..16`)** | **`vLLM 0.32.0 PP=2` hoặc `SGLang 0.5.19 PP=2` Chia tầng lệch (`13,23` / tỷ lệ `36:64`)** | Qua cầu nối `nccl0` + `--pp-size 2` + `LAYER_PARTITION="13,23"` | Vừa gộp được `24 GB` VRAM, vừa có Continuous Batching + Prefix Caching: **`vLLM PP=2 (13,23)`** thắng ở `C=1..8` (`73.24 tok/s` 1u, `226.93 tok/s` `C=8`), còn **`SGLang PP=2 (13,23)`** thắng ở `C=16` (**`331.25 tok/s`**, `Best-of-16 = 402.11 tok/s`). |
| **3. Mô hình MoE GGUF rất lớn (`Qwen3-Coder-30B-A3B Q4_K_M`, `18.6 GB`) chỉ phục vụ 1–2 dev (`C = 1..2`)** | **`llama.cpp` (`ggml-rpc` `48,52`)** *(Chế độ mặc định hiện tại của cụm)* | `worker-guard` + `rpc-server.exe` Native Windows | Tương thích trực tiếp định dạng GGUF MoE `18.6 GB` sẵn có mà không cần chuyển sang AWQ/FP16; đạt `~71 tok/s` cho 1–2 người dùng. |

---

## 4. Trạng Thái Cụm Sau Thực Nghiệm (`[ĐO]`)
- Toàn bộ môi trường WSL2 tối ưu (`/opt/venv-sglang`, `/opt/venv-vllm`, `/opt/hf-models/Qwen3-8B-AWQ`, `/root/p11_nccl_smoke.py`) và bộ khởi tạo mạng phẳng `nccl0` trong [`p11_2gpu_runner.py`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/hardware/p11_2gpu_runner.py) đã được lưu trữ bền vững.
- Dịch vụ `AICFWorkerGuard` trên `pc-win-01` (`10.42.0.225:50052`) và `worker-guard` trên `pc-001` (`127.0.0.1:8099`) đã được khôi phục về trạng thái vận hành `merged` (`Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`) bình thường.

