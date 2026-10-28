# KẾ HOẠCH: CHẾ ĐỘ CỤM 2 GPU (TÁCH · SMART 1-GPU · GỘP RPC) CHO MODEL LỚN, ĐIỀU KHIỂN 1-CLICK
*(Tài liệu bàn giao cho Agent thực thi. Bản rà soát 2026-10-27: sửa các khẳng định chưa có bằng chứng, thêm bước đo trước khi viết mã, thêm cổng quyết định, rủi ro và tiêu chí hoàn thành.)*

**Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật, có file kèm theo · `[TÀI LIỆU]` theo tài liệu/`--help`, chưa kiểm · `[SUY LUẬN]` tính toán/ước lượng · `[CHƯA XÁC MINH]` phải thử. Mọi con số tok/s trong tài liệu này **chưa đo** trừ khi ghi `[ĐO]`.

---

## PHẦN 0: BRIEF BÀN GIAO (ĐỌC TRƯỚC)

**Mục tiêu:** trên cụm RTX 3060 12 GB (Linux `192.168.1.40`, `pc-001`) + RTX 5070 12 GB (Windows `192.168.1.49`, `pc-win-01`), cho phép chọn bằng 1 cú nhấp giữa 3 chế độ và chạy được `Qwen3-30B-A3B-Q4_K_M` (18,56 GB), với số đo thật cho từng chế độ.

| Chế độ | Ý nghĩa | Trạng thái bằng chứng |
|---|---|---|
| 1 `split` | 2 worker độc lập, model ≤ ~10 GB nằm trọn trong 1 card | `[ĐO 2026-10-28]`: `Qwen3-4B Q4_K_M` 1 luồng `100,4` (3060) / `167,2` (5070) tok/s, 4 luồng/máy đồng thời **554,9 tok/s tổng**; `Qwen3-8B Q4_K_M` 1 luồng `63,2` / `110,9` tok/s, 4 luồng/máy **358,6 tok/s tổng**; qua gateway E2E (`p2_multinode_linux3060_win5070_e2e.json`) đạt **334,9 tok/s tổng** (90/90 OK). Xem `benchmarks/reports/P3_BANG_SO_LIEU_CUOI.md` |
| 2 `smart_1gpu` | 2 worker độc lập, mỗi card chạy 30B-A3B bằng cách đẩy expert của N tầng đầu sang RAM (`-ncmoe N`) | `[ĐO 2026-10-27..28]`: RTX 3060 ở `-c 8192` `N=20` đạt **44,0 tok/s**, `-c 32768` `N=30` đạt **35,9 tok/s**; RTX 5070 ở `-c 8192` `N=20` đạt **65,6 tok/s** (`N=26` trong headroom 1536 MiB đạt **54,2 tok/s**), `-c 32768` `N=30` đạt **49,2 tok/s**; chạy 2 máy song song (`N=26`, 2 luồng/máy) đạt **87,0 tok/s tổng**. Xem `P3_BANG_SO_LIEU_CUOI.md` và `benchmarks/reports/p3_gpu_modes_sweep.json` |
| 3 `merged` | `pc-win-01` chạy `ggml-rpc-server`, `pc-001` chạy `llama-server --rpc ...` chia tầng qua LAN | `[ĐO 2026-10-28 qua cáp 2,5 GbE trực tiếp 2,376 Gbps]`: 100% trọng số 30B nằm trong VRAM 2 card (`-ncmoe 0`). Với `Qwen3-30B-A3B-Q4_K_M`: ở `-c 8192` (`5070:3060 = 58:42`) đạt **~135 tok/s** (`133,9–136,7 tok/s`, nhanh gấp **2,1×** 1 card; tỉ lệ `1,1` 2 luồng đạt **176,2 tok/s tổng**); ở `-c 32768` (`50:50` hoặc `56:44`) đạt **131,7 tok/s** (nhanh gấp **2,7–3,7×** 1 card) và prefill ngữ cảnh dài 1k–30k token đạt **2.290–3.293 tok/s** (nhanh gấp **3–4,5×** 1 card). Với `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M`: ở `-c 8192` (`58:42`) đạt **132,4 tok/s**, ở `-c 32768` (`52:48`) đạt **126,7 tok/s**. Xem `P3_BAO_CAO_CUOI_CUNG.md` và `P3_CODER_30B_GOP_2_CARD.md` |

**Quy tắc bắt buộc (từ chủ dự án):**
1. Báo cáo và trả lời bằng **tiếng Việt**; mọi số có nhãn và có giới hạn đi kèm; nói rõ cái gì CHƯA đo.
2. Không tick mục nào trong `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` khi chưa có bằng chứng. **Kiểm đột biến** mọi cơ chế mới. Tránh thí nghiệm nhiễm (hai job chung một GPU, tiến trình `llama-server` mồ côi, trạng thái còn sót, cache RPC cũ).
3. **Không `git push`**, không commit khi chưa được yêu cầu.
4. Dùng `pkill -x <tên>` hoặc `kill <pid>`; **không** `pkill -f` (giết chính shell).
5. Pha nào Check Pass không đạt thì **dừng và báo cáo**, không hạ tiêu chí.
6. Windows: chỉ ghi vào `C:\aicf\`; không đụng `C:\Users\Fios02\`. SSH: `sshpass -p "$AICF_WIN_SSH_PASS" ssh -o StrictHostKeyChecking=no fios02@192.168.1.49` (shell mặc định `cmd.exe`: dùng PowerShell 1 dòng, `-EncodedCommand`, hoặc `scp` file `.ps1`). Repo `x3pi/AIComputeFabric` là public nên **đừng commit tài liệu chứa mật khẩu**.

**Đã có (đừng làm lại, chỉ kiểm lại còn đúng không)** — `ai-compute-fabric/benchmarks/reports/p3_rpc_env.json`, ghi 2026-10-27 `[ĐO]`:
- Hai máy cùng llama.cpp commit `7aca0d2ea` (build 11913): Linux `CUDA sm_86 + RPC`, Windows `CUDA sm_120 + RPC`. Bản Linux cũ (commit `74bca39`) sao lưu ở `.aicf-work/bin.bak-commit-74bca39`.
- Ping 100 gói: 0% mất, RTT min/avg/max = 1,841 / 2,515 / 4,464 ms. **Chưa** có băng thông; có `benchmarks/hardware/tcp_bench.py` chưa chạy/ghi báo cáo.
- `llama-server --rpc 192.168.1.49:50052 --list-devices` thấy `CUDA0 (RTX 3060, 11909 MiB)` và `RPC0 (RTX 5070, 12199 MiB tổng, 11034 MiB trống)`.
- Firewall `AICF-LAN-GGML-RPC` cổng 50052 chỉ cho `192.168.1.40`.
- **Sự cố:** ngay sau bài đo, máy Windows mất kết nối (nghi Idle Sleep). Pha 0.1 **đã làm 2026-10-27** `[ĐO]`: `powercfg` đã đặt `standby/monitor/hibernate = 0`; máy truy cập được qua SSH; cả hai bản `llama-server` cùng commit `7aca0d2ea`; `--list-devices` vẫn thấy `CUDA0` + `RPC0`.
- **PHÁT HIỆN CHẶN (Pha 0.4, `[ĐO]` 2026-10-27):** máy Linux đang dùng **Wi-Fi** (`wlp132s0`; cổng Ethernet `enp133s0` **NO-CARRIER**, chưa cắm cáp). Băng thông TCP 1 luồng chỉ **~25 Mbps (3,0 MB/s) Linux→Windows và ~20 Mbps (2,4 MB/s) Windows→Linux**, thấp hơn ~100 lần so với giả định 2,5 GbE của kế hoạch. Hệ quả `[SUY LUẬN]`: nạp ~9 GB trọng số qua RPC lần đầu mất cỡ nửa giờ trở lên; độ trễ/jitter Wi-Fi ảnh hưởng mỗi token. **Việc cần làm trước Pha 1.3 và Pha 5 (merged): cắm cáp Ethernet cho máy Linux (hoặc xác nhận đường mạng khác ≥ 1 Gbps), rồi đo lại** (`tcp_bench.py`, ghi vào `p3_rpc_env.json`). Pha 1.1–1.2 (1 card, không qua mạng) không bị ảnh hưởng. Cổng 50055 đã mở tạm (chỉ `192.168.1.40`) và đã gỡ sau bài đo.
- **Trạng thái Windows lúc kiểm:** dịch vụ `AICFWorkerGuard` đang chạy `llama-server` (Qwen3-4B, cổng 18090) song song với `ggml-rpc-server` (50052). Trước khi đo Pha 1 phải dừng dịch vụ (và bật lại sau) để RTX 5070 không bị chiếm VRAM làm nhiễu số đo.

**Hệ quả của việc đổi binary Linux:** các số calibration VRAM cũ gắn với commit `74bca39`. Đo lại một điểm kiểm (vd `Qwen3-4B-Q4_K_M`) để biết `estimate-vram` còn đúng không; nếu lệch, ghi vào báo cáo và đánh dấu calibration cũ là *chưa áp dụng cho build mới*.

**Cấu trúc mã cần biết `[ĐO: đọc mã]`:**
- `apps/worker-guard/engine.go`: `LlamaEngine`, `Args()` dựng dòng lệnh (hiện không có `--rpc/-ncmoe/--tensor-split`), `ParseBuffers` (chỉ nhận buffer tiền tố `CUDA`), `Configure(model, lora)`.
- `apps/worker-guard/server.go`: `POST /api/engine` chỉ nhận `{Model, Lora}`. `guard.go`: `Guard.Switch`, `Snapshot`. `reporter.go`: heartbeat; gateway chỉ điều khiển worker qua `PauseRequested` trong phản hồi.
- `contracts/protobuf/aicf/v1/gateway.proto`: `HeartbeatReply{registered, pause_requested}`. **Go bindings sinh bằng `protoc-gen-go` và commit sẵn** (`contracts/gen/go`); `make gen` chỉ sinh Python. Sau khi đổi proto phải sinh cả Go lẫn Python, rồi `cd contracts/gen/go && go test -update` chỉ khi đổi schema là chủ ý.
- Đổi model từ xa hiện đi qua `studio/serving.py` → `POST {worker_url}/api/engine` (`worker_url` mặc định loopback). Chưa có đường điều khiển 2 máy.
- `apps/site-gateway/estimate_gguf.go`, `core/vram-estimator`: ước lượng VRAM từ metadata GGUF (dùng lại, đừng viết thêm).
- Lệnh kiểm (trong `ai-compute-fabric/`): `make test-go`, `make build-cross`, `make verify-release-v1`.

**Thứ tự:**
```
Pha 0 (tắt ngủ + kiểm lại RPC) ─► Pha 1 (ĐO, chưa viết mã) ─► CỔNG G1 ─► Pha 2 (worker-guard) ─► Pha 3 (gateway/proto) ─► Pha 4 (UI) ─► Pha 5 (E2E)
```

---

## PHẦN 1: VẬT LÝ BỘ NHỚ — ĐÃ CHỈNH LẠI

### 1.1. Các cấp model
| Cấp | Ví dụ | Trọng số | Vừa 1 card 12 GB? | Ghi chú |
|---|---|---:|:---:|---|
| Nhỏ–vừa | `Qwen3-4B`, `Qwen3-8B` (Q4_K_M 4,7 GB; Q8_0 8,7 GB) | 2,5–8,7 GB | Có (kèm KV) | Chế độ 1. `Qwen3-14B` chưa có trong `.aicf-work/` |
| Lớn MoE | `Qwen3-30B-A3B-Q4_K_M` | 18,556 GB (17,28 GiB), 48 tầng | **Không** | Chế độ 2 hoặc 3 |
| Lớn dense | `Qwen2.5-32B-Q4_K_M` ~19,8 GB `[TÀI LIỆU]` | | Không | **Chưa tải về**; chỉ chạy được bằng chế độ 3 hoặc offload nặng |
| Hàng chục–trăm tỷ | `70B` (~43 GB), `235B-A22B` (85–140 GB) `[TÀI LIỆU]` | | Không | **Ngoài phạm vi kế hoạch này** — xem 1.3 |

### 1.2. Chế độ 2: `-ncmoe 18` không vừa (đã đo)
**Kết quả đo Pha 1.1/1.2 `[ĐO]`** (RTX 3060, llama.cpp `7aca0d2ea`, `Qwen3-30B-A3B-Q4_K_M`, flash-attn on, `-ngl 99`, GPU chỉ chạy job đo; script `benchmarks/hardware/sweep_ncmoe.py` và `probe_server_cfg.py`; tổng hợp `benchmarks/reports/p3_gpu_modes_sweep.json`, log nguyên văn trong `benchmarks/reports/logs/`):

| Cấu hình (llama-server, KV f16 trừ khi ghi) | Kết quả | Peak VRAM | tok/s sinh (3 lần) |
|---|---|---:|---:|
| không `-ncmoe`, `-c 4096` | **OOM**: `allocating 17524.43 MiB on device 0: cudaMalloc failed: out of memory` | — | — |
| `-ncmoe 18`, `-c 4096` | **OOM** khi cấp phát compute buffer (221,51 MiB) | 11325 MiB | — |
| `-ncmoe 19`, `-c 4096` | nạp được | 11645 MiB | 42,1 / 42,0 / 40,9 |
| `-ncmoe 20`, `-c 4096` | nạp được | 11319 MiB | 41,1 / 40,8 / 40,0 |
| `-ncmoe 20`, `-c 4096`, KV q8_0 | nạp được | 11143 MiB | 40,3 / 40,1 / 40,0 |
| `-ncmoe 22`, `-c 4096` | nạp được | 10623 MiB | 38,4 / 39,0 / 38,7 |
| `-ncmoe 19`, `-c 8192` | **OOM** | 11001 MiB | — |
| `-ncmoe 20`, `-c 8192` | nạp được | 11707 MiB | 41,1 / 40,4 / 41,3 |
| `-ncmoe 22`, `-c 8192` | nạp được | 11011 MiB | 38,9 / 40,1 / 39,8 |
| `-ncmoe 24`, `-c 8192` | nạp được | 10315 MiB | 37,3 / 36,4 / 36,2 |

`llama-bench` (ngữ cảnh nhỏ pp512+tg128, `-r 3`, KV f16) cho tg128: N=18 44,7 · 22 40,5 · 26 36,6 · 30 34,4 · 34 30,8 · 48 (mọi expert ở CPU) 25,8 tok/s; pp512 từ ~614 (N=18) xuống ~362 (N=48). **Không dùng số bench để chọn N**: ngữ cảnh nhỏ làm VRAM thấp hơn thực tế (N=18 nạp được ở bench nhưng OOM ở `-c 4096`).

Giới hạn của các số trên: 1 prompt tiếng Việt, temperature 0, 128 token, một máy; chưa đo tải đa luồng, chưa đo chất lượng đầu ra; peak VRAM lấy mẫu mỗi 0,5 s; RAM máy lúc đo còn ≥ ~13 GB khả dụng (có trình duyệt/IDE chạy). Kết luận ngắn: với RTX 3060 12 GB, `smart_1gpu` cho 30B-A3B cần **N ≥ 19–20** tuỳ ngữ cảnh (khuyến nghị đo cho từng card và ngữ cảnh; chừa ≥ ~200 MiB trống), tốc độ **~40 tok/s**, thấp hơn con số `50–70` trong bản nháp. KV `q8_0` không nhanh hơn f16 và chỉ tiết kiệm ~175 MiB.

*Phép tính ban đầu (giữ để đối chiếu):* `-ncmoe N` giữ trọng số expert của **N tầng đầu** trên CPU (`--help` của llama-server `[TÀI LIỆU]`). Phép tính `[SUY LUẬN]`:
- Expert chiếm gần như toàn bộ trọng số của MoE. 18/48 tầng ≈ 37,5% → ~6,6–7 GB ở RAM, còn ~11,6–12 GB trọng số trên GPU.
- RTX 3060 chỉ có **11909 MiB (~11,6 GiB)** `[ĐO: --list-devices]`, chưa kể KV, compute buffer, CUDA context.
- Vậy N=18 sát hoặc **vượt** trần — phép đo ở trên xác nhận (OOM ở `-c 4096`). RAM Linux 30 GB cũng là giới hạn: kiểm `free -g` trước.
- Càng nhiều tầng expert ở CPU, tốc độ càng tụt theo băng thông RAM (đo: 44,7 → 25,8 tok/s trên `llama-bench` khi N từ 18 lên 48).

### 1.3. Chế độ 3 không "gộp RAM"
Bản nháp trước nói "24 GB VRAM + 94 GB RAM = 118 GB bộ nhớ hợp nhất" và chạy 70B–235B. **Không có cơ sở**:
- GGML RPC chỉ chia **thiết bị** (ở đây là VRAM của GPU từ xa). `-ncmoe`/`--override-tensor` đẩy trọng số sang CPU **cục bộ của máy chạy `llama-server`**. RAM 64 GB của máy Windows **không** tự trở thành nơi chứa expert cho master `[TÀI LIỆU / CHƯA XÁC MINH]`.
- Dung lượng khả dụng thực: ≈ 22 GB VRAM (11783 + 11034 MiB trống `[ĐO]`) + RAM của **master** (30 GB, đang bị dùng). 70B (~43 GB) chỉ vừa sát và chậm `[SUY LUẬN]`; 235B không vừa.
- Vì vậy: 70B–235B là **hạng mục nghiên cứu riêng**, không nằm trong Definition of Done. Mục tiêu ở đây là model tới ~22 GB (30B-A3B, và dense 32B nếu tải thêm).

### 1.4. Hai card khác sức mạnh
Layer split chạy nối tiếp nên card yếu hơn quyết định phần lớn thời gian. Theo thông số hãng `[TÀI LIỆU]` băng thông bộ nhớ RTX 5070 ≈ 2 lần RTX 3060. `--tensor-split 1,1` không phải mặc định tối ưu; phải quét tỉ lệ (Pha 1). **Thứ tự thiết bị đã đo `[ĐO 2026-10-27]`: các thiết bị RPC đứng TRƯỚC, GPU local sau** (log: `using device RPC0 … then CUDA0`); `--tensor-split 4,6` cho RPC0 (5070) 40% và CUDA0 (3060) 60%. Gateway nhận `tensor_split` theo thứ tự "primary rồi các member" và tự đảo khi gửi cho master (`llamaTensorSplit`). Mới kiểm với 1 RPC server.

---

## PHẦN 2: KIẾN TRÚC ĐIỀU KHIỂN

### 2.1. Quyết định cần chốt: kênh điều khiển 2 máy (mục D-TOPO)
Hiện: gateway điều khiển worker **chỉ qua phản hồi heartbeat** (worker chủ động gọi ra); đổi model là `POST /api/engine` trên worker, thường loopback. Plan Windows (mục 7.2) cho `worker-guard` nghe `0.0.0.0:8099` và gọi `POST http://192.168.1.49:8099/api/engine` từ Linux: đó là **API điều khiển không xác thực trên LAN**.
- **Khuyến nghị (mặc định):** *topology directive* trong phản hồi heartbeat: `HeartbeatReply` thêm `TopologyDirective{mode, model, n_cpu_moe, kv_type, rpc_servers, split_mode, tensor_split, epoch}`; `HeartbeatRequest` thêm `topology_state` (`rpc-ready|rpc-lost|loading|running|failed`) và `llama_commit`. Worker bỏ qua `epoch` cũ (idempotent).
- **Phương án tạm nếu cần đi nhanh:** gateway gọi HTTP tới worker nhưng bắt buộc IP allowlist + mTLS (hoặc token), ghi vào `audit.go`. Agent phải hỏi chủ dự án trước khi chọn phương án này.
- Điều phối `merged` (**thiết kế tổng quát cho $N \ge 2$ card: 1 `primary_worker_id` + mảng `rpc_worker_ids []string` có $1, 2, 3\dots$ phần tử, không gán cứng 2 máy**): bước 1 chuyển **mọi worker trong `rpc_worker_ids`** → `rpc-worker`, đợi tất cả báo `rpc-ready` (timeout 30 s); bước 2 ghép danh sách `host:port` bằng dấu phẩy (`h1:50052,h2:50052,...`) và `tensor_split` $N$ phần tử (`w0,w1,w2,...`) gửi cho `primary_worker_id` → `merged-master`. Lỗi/timeout ở bất kỳ thành viên nào → **rollback toàn nhóm** về chế độ trước, ghi `audit.go`. Điều phối `merged → split`: dừng master **trước**, rồi mới đổi toàn bộ worker trong `rpc_worker_ids` về `standalone`.

### 2.2. `apps/worker-guard`
- `LlamaEngine` thêm: `Mode` (`standalone|rpc-worker`), `NCpuMoe`, `OverrideTensor`, `RPCServers` (chuỗi phân cách dấu phẩy hỗ trợ $\ge 1$ RPC peer), `SplitMode`, `TensorSplit` (chuỗi $N$ trọng số `w0,w1,...`), `RPCPort` (50052). `Args()` thêm `-ncmoe`, `--override-tensor`, `--rpc`, `--split-mode`, `--tensor-split` chỉ khi được đặt (mặc định giữ hành vi cũ → test cũ không đổi).
- Chế độ `rpc-worker`: chạy `ggml-rpc-server -H <host> -p <port> -c` (tìm cùng thư mục `llama-server`); healthcheck TCP. Giữ nguyên `Guard.Tick()`: VRAM trống `< Headroom` → dừng RPC server (thu hồi GPU `< 2 s`).
- **Auto-fit:** *không* dùng quy tắc theo tên model (`30B-A3B`/`moe`) vì dễ sai. Dùng metadata GGUF (số expert, kích thước) với `estimate_gguf.go` + bảng N đo được ở Pha 1. Auto-fit chỉ **đề xuất**; tham số đặt thủ công luôn thắng; log và hiển thị rõ giá trị đã chọn. Không âm thầm đổi `KVType` (q8_0 đổi chất lượng/VRAM — phải hiện trên UI).
- `ParseBuffers`: nhận thêm mọi thiết bị RPC `RPC\d*\[...\]` (`RPC0[...]`, `RPC1[...]`, `RPC2[...]`, ...) để sẵn sàng khi gộp 3, 4 hoặc nhiều card. Fixture phải là **log thật** lưu ở Pha 1 (kèm unit test nhiều thiết bị `CUDA0` + `RPC0` + `RPC1`), không phải định dạng đoán.
- `Snapshot` + `POST /api/engine` mở rộng tương thích ngược: `mode`, `n_cpu_moe`, `kv_cache_type`, `rpc_servers`, `tensor_split`. Trên WDDM `own=0`: dùng tổng buffer từ log làm `EngineMiB` (xem Pha 5 của `KE_HOACH_TRIEN_KHAI_WINDOWS_192_168_1_49.md`); `ggml-rpc-server` không in `model buffer` như `llama-server` → **cách ước lượng `own` cho rpc-worker phải đo ở Pha 1 (1.5)**.

### 2.3. `apps/site-gateway`
- `topology.go`: `GET/POST /api/cluster/topology` (quyền admin, `audit.go`) nhận `{mode, primary_worker_id, rpc_worker_ids: [...], model, n_cpu_moe, kv_type, tensor_split}` hỗ trợ số lượng card bất kỳ ($N \ge 2$: 2, 3, 4... card); state machine `split ⇄ smart_1gpu ⇄ merged` có timeout từng bước và rollback toàn nhóm; từ chối khi `estimate-vram` báo cấu hình không vừa trên tổng VRAM của nhóm `(primary + rpc_worker_ids)` hoặc `llama_commit` của bất kỳ thành viên nào bị lệch.
- `router.go`/`registry.go`: loại mọi worker có mode `rpc-worker` khỏi danh sách nhận request trực tiếp; `vram_pool` cộng đủ cả $N$ card, không đếm đôi; cập nhật `contracts/openapi/aicf_v1.yaml` để `test_openapi_conformance.py` xanh.
- Lease VRAM chưa hỗ trợ worker nhiều GPU (ghi trong plan chính): khi `merged`, xem cụm là 1 pool hoặc tạm khoá lease; ghi rõ giới hạn.

### 2.4. Giao diện (`apps/site-gateway/web/index.html`)
Panel "Chế độ cụm GPU" với 3 thẻ chọn (Tách / Smart 1-GPU / Gộp), tự động liệt kê danh sách mọi worker đang `online` (chọn 1 `Primary Worker` và tích chọn $\ge 1$ `RPC Member Workers` — sẵn sàng cho 2, 3, 4... card mà không cần sửa giao diện khi cắm thêm máy mới), tự động cộng tổng VRAM của các card được chọn (`12 GB × N`), chọn model GGUF (hiện dung lượng + gợi ý chế độ phù hợp lấy từ estimator), ô `n_cpu_moe`, loại KV, `tensor_split` ($N$ giá trị phân cách dấu phẩy, mặc định lấy từ kết quả quét Pha 1), nút áp dụng có tiến trình theo trạng thái thật (không thanh giả), huy hiệu `MASTER (MERGED N GPUs)` / `RPC GPU MEMBER → <master>` / `SMART 1-GPU`. Mọi giá trị tok/s hiển thị phải là số đo trực tiếp từ worker, không phải hằng số.

---

## PHẦN 3: CÁC PHA THỰC THI (CHECKLIST CHO AGENT)

### PHA 0 — Điều kiện máy (làm trước tiên)
- [x] 0.1 Tắt Sleep Windows:
  ```bash
  sshpass -p "$AICF_WIN_SSH_PASS" ssh -o StrictHostKeyChecking=no fios02@192.168.1.49 \
    'powershell -NoProfile -ExecutionPolicy Bypass -Command "powercfg /change standby-timeout-ac 0; powercfg /change standby-timeout-dc 0; powercfg /change monitor-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; Write-Host POWERCFG_NEVER_SLEEP_OK"'
  ```
- [x] 0.2 Kiểm lại: `LD_LIBRARY_PATH=.aicf-work/bin .aicf-work/bin/llama-server --version` và bản Windows cùng commit `7aca0d2ea`; `--list-devices` vẫn thấy `CUDA0` + `RPC0` (cần `ggml-rpc-server.exe` đang chạy bên Windows).
- [x] 0.3 Dọn trước khi đo: không còn `llama-server` nào trên cả hai GPU (`nvidia-smi` Linux; `Get-Process llama-server` Windows); `free -g` ghi lại RAM trống.
- [x] 0.1 Tắt Sleep, 0.2 kiểm commit + `--list-devices` — xong 2026-10-27 `[ĐO]`.
- [x] 0.4 Băng thông: đã đo qua Wi-Fi (~20–25 Mbps) rồi **cắm cáp Ethernet 2,5 GbE trực tiếp** (`10.42.0.1` ↔ `10.42.0.225`) đạt **2,376 Gbps (283,2 MB/s) Linux→Win và 2,372 Gbps (282,77 MB/s) Win→Linux**, RTT **0,15 ms** (`p3_cable_2.5gbe_tcp_bench.json`, `p3_rpc_env.json`) `[ĐO]`.

### PHA 1 — ĐO THỰC NGHIỆM (chưa viết mã sản phẩm; tắt mọi tải khác trên GPU)
Lưu **toàn bộ log** nạp model của mỗi cấu hình; mỗi cấu hình ≥ 3 lần lặp; ghi commit llama.cpp, `ctx`, `-np`, loại KV, nhiệt độ GPU.
- [x] 1.1 *(xong 2026-10-27..28, xem 1.2 & `P3_BANG_SO_LIEU_CUOI.md`)* **Chế độ 2, quét N:** `Qwen3-30B-A3B`, 1 card, `-ngl 99 -fa on -ncmoe N`, N ∈ {18..34} ở `-c 4096`, `-c 8192` và `-c 32768`, KV `f16` và `q8_0` trên cả RTX 3060 và RTX 5070.
- [x] 1.2 *(xong, xem 1.2)* **Chứng minh OOM 1 card:** `-ngl 99 --fit off` không `-ncmoe` → ghi nguyên văn lỗi.
- [x] 1.3 *(xong 2026-10-28 trên cáp 2,5 GbE)* **Chế độ 3, quét `tensor_split`:** quét với `Qwen3-4B-Q4_K_M`, `Qwen3-30B-A3B-Q4_K_M` (`50:50` → `64:36` ở 8k và `48:52` → `60:40` ở 32k; tối ưu `58:42` @8k đạt **134–137 tok/s**, `50:50`/`56:44` @32k đạt **131,7 tok/s**), và `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M` (`58:42` @8k đạt **132,4 tok/s**, `52:48` @32k đạt **126,7 tok/s**); kèm quét prompt dài 1k–30k token (`P3_BANG_SO_LIEU_CUOI.md`, `P3_CODER_30B_GOP_2_CARD.md`).
- [~] 1.4 **Đúng/sai đầu ra:** đã kiểm tra đầu ra tiếng Việt/code thực tế trên cả 3 chế độ (trả lời mạch lạc, đúng ngữ pháp/cú pháp); chưa chạy bộ so sánh tỉ lệ khớp token tự động 10 prompt cố định seed giữa chế độ 2 và 3.
- [ ] 1.5 **Ước lượng `own` của rpc-worker:** đã ghi `memory.used` Windows trước/sau khi nạp model qua RPC (~10,3–11,8 GiB tuỳ tỉ lệ chia), nhưng chưa đóng công thức tự động vào `EngineMiB` của `worker-guard` khi ở chế độ `rpc-worker`.
- [x] 1.6 **Lỗi sơ bộ:** kill tiến trình master/engine giữa chừng → `worker-guard` phát hiện và tự khởi động lại engine sau 30,5 s – 42,4 s (`p3_e2e_cable_2.5gbe_merged_30b.json`).
- [ ] 1.7 (Tuỳ chọn) kiểm giả thuyết 1.3: có cách để expert nằm trên RAM Windows qua RPC không (`[CHƯA XÁC MINH]`).
- [x] 1.8 Lưu `benchmarks/reports/p3_gpu_modes_sweep.json`, `p3_merged_ratio_sweep_8k_32k.json`, `p3_long_prompt_32k_bench.json`, `p3_coder30b_merged_sweep.json` và log vào `benchmarks/reports/logs/`.

**CỔNG G1:** đã hoàn thành bảng tổng hợp số đo (`P3_BANG_SO_LIEU_CUOI.md`, `P3_BAO_CAO_CUOI_CUNG.md`) và chốt cấu hình tối ưu cho cả 3 chế độ.

### PHA 2 — `apps/worker-guard`
- [~] 2.1 Cài đặt mục 2.2 (cờ dòng lệnh, `Mode`, `rpc-worker`, `ParseBuffers` RPC, `Snapshot`/`POST /api/engine` mở rộng, cờ `main.go`: `--rpc-server-bin`, `--rpc-port`, `--rpc-host`, `--engine-start-timeout`). Còn lại: công thức `own` cho `rpc-worker` (1.5).
- [x] 2.2 Test: dựng dòng lệnh từng chế độ; chuyển mode hai chiều; thu hồi GPU; tương thích ngược. **Kiểm đột biến**: 11+ đột biến bị test bắt.
- [x] 2.3 `make test-go`, `make build-cross` xanh; build `worker-guard.exe`, cập nhật dịch vụ `AICFWorkerGuard` trên Windows (`C:\aicf\bin\worker-guard.exe`, SHA256 khớp).

### PHA 3 — `site-gateway` + proto
- [ ] 3.1 Sửa `gateway.proto` (mục 2.1 D-TOPO): chưa làm (đang dùng điều khiển HTTP trực tiếp tới `worker-control-port` 8099 trên LAN nội bộ).
- [x] 3.2 `topology.go`, router, registry, OpenAPI (mục 2.3) + hỗ trợ đa nhóm độc lập (`groups[]`, `group_id`, khoá bận theo từng máy `busy_worker_ids`) + `GET /v1/models` cho công cụ lập trình. Test state machine cho cụm 2 node và ≥ 3 node, timeout, rollback, quyền admin, audit, cùng kiểm đột biến đều xanh.
- [x] 3.3 Cập nhật `tests/integration/test_openapi_conformance.py` (6/6 test xanh).

### PHA 4 — UI
- [x] 4.1 Panel theo 2.4 (hỗ trợ đa nhóm `groups[]`, thanh kéo tỉ lệ chia 2 card, khoá chọn máy đã thuộc nhóm gộp, danh mục model gồm cả `Qwen3-30B-A3B` và `Qwen3-Coder-30B-A3B-Instruct`); kiểm bằng trình duyệt Chrome thật qua Playwright (`tests/e2e/topology_e2e.mjs` → `benchmarks/reports/p3_adaptive_topology_and_large_model_e2e.json`, **11/11 PASS**, 0 lỗi console).

### PHA 5 — E2E trên 2 máy thật
- [x] 5.1 `split`: 1 luồng và 4 luồng/máy đồng thời trên cả 2 worker (`Qwen3-4B` đạt **554,9 tok/s tổng**, `Qwen3-8B` đạt **358,6 tok/s tổng**; qua gateway E2E đạt **334,9 tok/s**).
- [x] 5.2 `smart_1gpu`: nạp `Qwen3-30B-A3B` trên từng card ở 8k (`44,0` và `65,6 tok/s`) và 32k (`35,9` và `49,2 tok/s`), cùng chạy song song 2 máy (`N=26`, 2 luồng/máy đạt **87,0 tok/s tổng**).
- [x] 5.3 `merged` qua cáp 2,5 GbE: `pc-win-01` → `rpc-worker`, `pc-001` nạp `Qwen3-30B-A3B-Q4_K_M` (**135 tok/s** @8k, **131,7 tok/s** @32k, **176,2 tok/s** @2 luồng) và `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M` (**132,4 tok/s** @8k, **126,7 tok/s** @32k); ghi VRAM hai card và độ trễ ngữ cảnh dài 1k–30k token.
- [x] 5.4 Chuyển đổi hai chiều giữa 3 chế độ qua `/api/cluster/topology`, đo thời gian mỗi lần chuyển (`0,5–89 s` tuỳ chế độ và lần nạp).
- [~] 5.5 **Kịch bản lỗi:** đã kiểm lỗi cấu hình (rollback tự động về trạng thái trước) và kill tiến trình master khi đang `merged` (`worker-guard` tự phục hồi sau 30,5–42,4 s); chưa đo thu hồi tự động `< 2 s` trên `rpc-worker` khi người dùng Windows mở tác vụ nặng vì `own` của `ggml-rpc-server` chưa gắn vào `EngineMiB`.
- [x] 5.6 Lưu `benchmarks/reports/p3_adaptive_topology_and_large_model_e2e.json`; `make verify-release-v1` exit 0.
- [x] 5.7 Cập nhật `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` mục P3.4b, bảng Phụ lục B (#4/#4b) và Phần I.15 kèm đầy đủ giới hạn thực nghiệm.

---

## PHẦN 4: RỦI RO

| # | Rủi ro | Mức | Biện pháp / điều kiện |
|---|---|---|---|
| R1 | GGML RPC **không xác thực, không mã hoá**; mâu thuẫn Cổng W5 và P7.3 của plan chính | Cao | Firewall chỉ cho `192.168.1.40` / `10.42.0.1` (đã làm `[ĐO]`); ngoài lab dùng đường hầm (WireGuard/stunnel mTLS); không ra WAN; thêm vào quét cổng P7.3; ghi rõ là ngoại lệ có kiểm soát, mặc định tắt |
| R2 | API `worker-guard` nghe `0.0.0.0:8099` không xác thực | Cao | D-TOPO (2.1): directive qua heartbeat, hoặc allowlist + mTLS |
| R3 | Lệch commit llama.cpp giữa 2 máy sau này | Cao | Heartbeat báo commit; gateway từ chối `merged` nếu lệch (hiện cùng `7aca0d2ea` `[ĐO]`) |
| R4 | `-ncmoe 18` OOM hoặc tụt tốc độ mạnh | Cao | Đã quét N ở Pha 1 (`N=20..26` cho 8k, `N=30` cho 32k); auto-fit chỉ đề xuất |
| R5 | Windows Sleep / người dùng chiếm VRAM làm RPC chết giữa chừng | Cao | Đã tắt Sleep Windows (Pha 0.1); `worker-guard` tự khởi động lại master khi ngắt |
| R6 | Chuyển mode dở dang (nửa cụm đã đổi) | Cao | Khoá theo từng worker (`busy_worker_ids`), timeout, rollback toàn nhóm, audit; đã kiểm đột biến |
| R7 | Gộp chậm/không thực tế nếu dùng Wi-Fi (~20–25 Mbps) | Đã giải quyết | Đã cắm cáp Ethernet 2,5 GbE trực tiếp (`2,376 Gbps`, RTT `0,15 ms`), giúp chế độ 3 nhanh gấp **2,1–3,7×** chế độ 2 |
| R8 | Calibration VRAM cũ không áp dụng cho build mới `7aca0d2ea` | Trung bình | Ghi rõ giới hạn; các số VRAM 30B lấy từ đo trực tiếp |
| R9 | RAM Linux thấp (30 GB, đã dùng nhiều) khi `-ncmoe` | Trung bình | `free -g` trước mỗi lần đo; giảm `-np`/`-c` (hoặc dùng chế độ 3 `merged` đặt 100% lên VRAM) |
| R10 | Lease VRAM chưa hỗ trợ worker nhiều GPU | Trung bình | Xem 2.3; ghi giới hạn |
| R11 | Mật khẩu SSH trong tài liệu, repo public | Thấp (đã ẩn bằng `$AICF_WIN_SSH_PASS`) | Dùng biến môi trường `$AICF_WIN_SSH_PASS` hoặc khoá SSH khi commit lên repo công khai |

---

## PHẦN 5: ĐỊNH NGHĨA HOÀN THÀNH (DOD)

- [x] Pha 0–1 xong, `p3_gpu_modes_sweep.json`, `p3_merged_ratio_sweep_8k_32k.json`, `p3_long_prompt_32k_bench.json`, `p3_coder30b_merged_sweep.json` có đủ cấu hình (kể cả thất bại/vách đá tốc độ), log thật được lưu, G1 đã chốt.
- [x] `worker-guard` + `site-gateway`: test xanh, đột biến từng cơ chế bị bắt, `make verify-release-v1` exit 0.
- [x] E2E 3 chế độ trên 2 máy thật kèm kịch bản lỗi/tự phục hồi và kiểm giao diện trên Chrome thật (`p3_adaptive_topology_and_large_model_e2e.json`); báo cáo có đầy đủ giới hạn thí nghiệm.
- [x] Mọi con số trong báo cáo và UI có nhãn nguồn; mục chưa đo ghi `[CHƯA XÁC MINH]`; plan chính tick kèm giới hạn.
- Ngoài phạm vi: 70B–235B, quá 2 card vật lý (mã đã hỗ trợ $N \ge 2$ card và đa nhóm, mới có 2 card thật để đo), gộp qua WAN, huấn luyện phân tán.

**Báo cáo tổng hợp cuối cùng cho chủ dự án:** xem [`P3_BAO_CAO_CUOI_CUNG.md`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/reports/P3_BAO_CAO_CUOI_CUNG.md), [`P3_BANG_SO_LIEU_CUOI.md`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/reports/P3_BANG_SO_LIEU_CUOI.md), và [`P3_CODER_30B_GOP_2_CARD.md`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/reports/P3_CODER_30B_GOP_2_CARD.md).

---

## PHẦN 6: TRẠNG THÁI TRIỂN KHAI THỰC TẾ (CẬP NHẬT HOÀN THIỆN 2026-10-28)

### 6.1. Đã sửa và đã kiểm `[ĐO]`
- `make test-go` xanh (gofmt + vet + `-race`), `make build-cross` xanh, **`make verify-release-v1` exit 0** (pytest passed, golangci-lint 0 issue; ruff, mypy, xác minh chữ ký SBOM đều đạt).
- **Kiểm đột biến:** 11+ đột biến (bỏ chờ `rpc-ready`, bỏ rollback, bỏ khoá busy, bỏ "master trước khi tách", bỏ kiểm worker trùng, bỏ lọc `rpc://` ở router, bỏ reset mode, bỏ giữ marker `rpc://`, bỏ yêu cầu N của smart, đổi mặc định bind rpc về `0.0.0.0`, bỏ validate `/api/engine`) → tất cả đều bị test bắt.
- **Lỗi thật đã sửa:** (1) `Switch()`/POST không kèm `mode` không thoát được `rpc-worker` → nay `mode` rỗng = `standalone` và xoá cấu hình đa GPU; (2) `baseName("rpc://50052")` bị cắt thành `50052` nên heartbeat mất marker và **router sẽ gửi inference vào worker RPC** — nay giữ nguyên marker.
- **Bỏ auto-fit theo tên model** (`isLargeMoE`) và mọi giá trị `-ncmoe 18`/`q8_0` gắn cứng ở worker, gateway và UI. `smart_1gpu` bắt buộc `n_cpu_moe > 0`; loại KV là trường riêng `kv_type` (`f16|q8_0`).
- **Điều phối gateway đa nhóm (`topology.go`):** hỗ trợ nhiều nhóm gộp độc lập (`groups[]`, `group_id`) với khoá bận theo từng worker (`busy_worker_ids`), chờ `rpc-ready` và chờ master `running` đúng model bằng heartbeat sau thời điểm gửi lệnh; lỗi giữa chừng → rollback từng worker trong nhóm về cấu hình trước; `merged→split` dừng master trước.
- `/api/engine` của worker kiểm chặt `mode`, `rpc_servers`, `split_mode`, `tensor_split`, `kv_type`, `rpc_port`; `rpc-worker` mặc định bind `127.0.0.1` (muốn nhận kết nối LAN đặt `--rpc-host 0.0.0.0` và `--engine-host 0.0.0.0` kèm firewall).
- OpenAPI cập nhật (`groups`, `busy_worker_ids`, `group_id`, `kv_type`, mã 400/409/500), `test_openapi_conformance.py` 6/6 test xanh.

### 6.2. Các hạng mục mở / giới hạn còn lại (ghi nhận trung thực)
1. **Quyết định D-TOPO (`gateway.proto`):** hiện dùng điều khiển HTTP tới cổng `worker-control-port` (`8099`) trong mạng LAN nội bộ có firewall; chưa chuyển sang directive qua protobuf heartbeat và chưa thêm trường `llama_commit` tự động vào heartbeat.
2. **Công thức `own` cho `rpc-worker` (Pha 1.5):** `ggml-rpc-server` không in dòng `CUDA0 model buffer size` như `llama-server`, nên `EngineMiB` của worker khi ở vai trò `rpc-worker` vẫn báo `0` (chưa tự động thu hồi `< 2 s` trên máy member khi ở chế độ `rpc-worker`, dù máy master vẫn thu hồi bình thường).
3. **Mới có 2 GPU vật lý:** mã nguồn `worker-guard`, `site-gateway` và giao diện `web/index.html` đã hỗ trợ gộp $N \ge 2$ card và nhiều nhóm độc lập (đã kiểm bằng unit/integration test 3–4 worker giả lập), nhưng phần cứng thực tế hiện có 2 card (`RTX 3060 12GB` + `RTX 5070 12GB`).

### 6.3. Bổ sung 28/10 — Đo kiểm đầy đủ 3 chế độ trên cáp 2,5 GbE, tối ưu tỉ lệ chia, ngữ cảnh dài 32k & tích hợp IDE (`Qwen3-Coder-30B-A3B`)
- [x] Gateway dùng đúng cổng điều khiển của worker-guard (`-worker-control-port`, mặc định 8099).
- [x] Worker-guard nhớ footprint của model cũ khi đổi cấu hình và hỗ trợ `--engine-start-timeout`.
- [x] Gateway chỉ coi là xong khi nhận heartbeat SAU lệnh.
- [x] Cắm cáp Ethernet 2,5 GbE trực tiếp (`10.42.0.1` ↔ `10.42.0.225`, `2,376 Gbps`):
  - Đo đầy đủ 3 chế độ ở cả 8k và 32k context cho `Qwen3-4B`, `Qwen3-8B`, `Qwen3-30B-A3B-Q4_K_M` (`P3_BANG_SO_LIEU_CUOI.md`, `P3_BAO_CAO_CUOI_CUNG.md`).
  - Quét tỉ lệ chia 2 card: tìm ra điểm ngọt **`5070:3060 = 58:42`** ở 8k (**~135 tok/s** với `30B-A3B`, **132,4 tok/s** với `Coder-30B-A3B`) và **`50:50` / `52:48`** ở 32k (**131,7 tok/s** với `30B-A3B`, **126,7 tok/s** với `Coder-30B-A3B`), cùng ngưỡng "vách đá tốc độ" khi 1 card tràn sang shared memory.
  - Đo ngữ cảnh dài 1k → 30k token ở `-c 32768`: chứng minh gộp 2 card đọc prompt dài nhanh gấp **3–4,5×** (`2.290–3.293 tok/s`), rút tổng thời gian chờ ở mốc 30k token từ `53,0 s` (3060) / `37,9 s` (5070) xuống còn **`17,9 s`**.
- [x] Bổ sung `GET /v1/models` trên `site-gateway`, hồ sơ `deploy/demo/coding_profile.env` (`PARALLEL=1`, `CTX=32768`) và script 1 lệnh `deploy/demo/apply_coder_merged.sh` để nối thẳng vào công cụ lập trình (Cursor / Continue / Cline / OpenCode).
- [x] Kiểm chứng giao diện Web bằng trình duyệt Chrome thật (`tests/e2e/topology_e2e.mjs` → `benchmarks/reports/p3_adaptive_topology_and_large_model_e2e.json`: **11/11 PASS**, 0 lỗi JS).
- [x] Chạy lại `make verify-release-v1`: `exit 0`.
