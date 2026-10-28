# KẾ HOẠCH: CHẾ ĐỘ CỤM 2 GPU (TÁCH · SMART 1-GPU · GỘP RPC) CHO MODEL LỚN, ĐIỀU KHIỂN 1-CLICK
*(Tài liệu bàn giao cho Agent thực thi. Bản rà soát 2026-10-27: sửa các khẳng định chưa có bằng chứng, thêm bước đo trước khi viết mã, thêm cổng quyết định, rủi ro và tiêu chí hoàn thành.)*

**Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật, có file kèm theo · `[TÀI LIỆU]` theo tài liệu/`--help`, chưa kiểm · `[SUY LUẬN]` tính toán/ước lượng · `[CHƯA XÁC MINH]` phải thử. Mọi con số tok/s trong tài liệu này **chưa đo** trừ khi ghi `[ĐO]`.

---

## PHẦN 0: BRIEF BÀN GIAO (ĐỌC TRƯỚC)

**Mục tiêu:** trên cụm RTX 3060 12 GB (Linux `192.168.1.40`, `pc-001`) + RTX 5070 12 GB (Windows `192.168.1.49`, `pc-win-01`), cho phép chọn bằng 1 cú nhấp giữa 3 chế độ và chạy được `Qwen3-30B-A3B-Q4_K_M` (18,56 GB), với số đo thật cho từng chế độ.

| Chế độ | Ý nghĩa | Trạng thái bằng chứng |
|---|---|---|
| 1 `split` | 2 worker độc lập, model ≤ ~10 GB nằm trọn trong 1 card | Cơ chế đã có sẵn. Thông lượng `~335 tok/s` là `[CHƯA XÁC MINH]` (không tìm thấy báo cáo nguồn) |
| 2 `smart_1gpu` | 2 worker độc lập, mỗi card chạy 30B-A3B bằng cách đẩy expert của N tầng đầu sang RAM (`-ncmoe N`) | `[ĐO 2026-10-27]` trên RTX 3060 (Pha 1.1): **N=18 hết bộ nhớ ở `-c 4096`**; N nhỏ nhất nạp được là **19 (`-c 4096`) / 20 (`-c 8192`)**, tốc độ sinh **~40–42 tok/s**, không phải `50–70`. Xem 1.2 và `benchmarks/reports/p3_gpu_modes_sweep.json` |
| 3 `merged` | `pc-win-01` chạy `ggml-rpc-server`, `pc-001` chạy `llama-server --rpc ...` chia tầng qua LAN | Kết nối đã chứng minh `[ĐO]`; **chưa từng nạp/chạy model qua RPC** |

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
- [ ] 0.1 Tắt Sleep Windows:
  ```bash
  sshpass -p "$AICF_WIN_SSH_PASS" ssh -o StrictHostKeyChecking=no fios02@192.168.1.49 \
    'powershell -NoProfile -ExecutionPolicy Bypass -Command "powercfg /change standby-timeout-ac 0; powercfg /change standby-timeout-dc 0; powercfg /change monitor-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; Write-Host POWERCFG_NEVER_SLEEP_OK"'
  ```
  Nếu máy không trả lời: báo chủ dự án đánh thức máy (không thể tự làm từ xa).
- [ ] 0.2 Kiểm lại: `LD_LIBRARY_PATH=.aicf-work/bin .aicf-work/bin/llama-server --version` và bản Windows cùng commit `7aca0d2ea`; `--list-devices` vẫn thấy `CUDA0` + `RPC0` (cần `ggml-rpc-server.exe` đang chạy bên Windows).
- [ ] 0.3 Dọn trước khi đo: không còn `llama-server` nào trên cả hai GPU (`nvidia-smi` Linux; `Get-Process llama-server` Windows); `free -g` ghi lại RAM trống.
- [x] 0.1 Tắt Sleep, 0.2 kiểm commit + `--list-devices` — xong 2026-10-27 `[ĐO]`.
- [~] 0.4 Băng thông: đã đo qua Wi-Fi (~20–25 Mbps, xem trên), đã ghi vào `p3_rpc_env.json`; **phải đo lại sau khi cắm Ethernet** trước khi quét chế độ 3.

### PHA 1 — ĐO THỰC NGHIỆM (chưa viết mã sản phẩm; tắt mọi tải khác trên GPU)
Lưu **toàn bộ log** nạp model của mỗi cấu hình; mỗi cấu hình ≥ 3 lần lặp; ghi commit llama.cpp, `ctx`, `-np`, loại KV, nhiệt độ GPU.
- [x] 1.1 *(xong 2026-10-27, xem 1.2)* **Chế độ 2, quét N:** `Qwen3-30B-A3B`, 1 card, `-ngl 99 -fa on -ncmoe N`, N ∈ {18, 22, 26, 30, 34} (và `-cmoe` làm mốc), `-c 4096`, KV `f16` và `q8_0`. Ghi: nạp được/OOM, VRAM, RAM, TTFT, tok/s (`llama-bench` + một prompt tiếng Việt qua `/v1/chat/completions`).
- [x] 1.2 *(xong, xem 1.2)* **Chứng minh OOM 1 card:** `-ngl 99 --fit off` không `-ncmoe` → ghi nguyên văn lỗi.
- [~] 1.3 *(mới làm với Qwen3-4B, xem `benchmarks/reports/P3_BAO_CAO_3_CHE_DO.md` mục 4; chưa làm với 30B vì Wi-Fi)* **Chế độ 3, quét `tensor_split`:** `llama-server --model ... --rpc 192.168.1.49:50052 --split-mode layer -ngl 99 --fit off -c 4096` với `--tensor-split` ∈ {`1,1`, `4,6`, `3,7`}; thêm quét `-c` ∈ {4096, 8192} và KV `f16`/`q8_0`. Ghi VRAM từng card, tok/s, TTFT, **thời gian nạp lần 1 và lần 2** (cờ `-c` của rpc-server).
- [ ] 1.4 **Đúng/sai đầu ra:** cùng ≥ 10 prompt tiếng Việt, temperature 0, seed cố định: so sánh đầu ra chế độ 2 và 3. Báo tỉ lệ token khớp; không tuyên bố "giống hệt".
- [ ] 1.5 **Ước lượng `own` của rpc-worker:** ghi `memory.used` Windows trước/sau khi nạp model qua RPC, đối chiếu log; xác định công thức cho `EngineMiB`.
- [ ] 1.6 **Lỗi sơ bộ:** kill `ggml-rpc-server` giữa lúc master đang sinh → master xử lý ra sao (lỗi, treo?). Ghi thời gian phát hiện.
- [ ] 1.7 (Tuỳ chọn) kiểm giả thuyết 1.3: có cách để expert nằm trên RAM Windows qua RPC không. Nếu không thử, ghi `[CHƯA XÁC MINH]`.
- [ ] 1.8 Lưu `benchmarks/reports/p3_gpu_modes_sweep.json` (mọi cấu hình, kể cả thất bại) và log vào `benchmarks/reports/logs/p3_*`.

**CỔNG G1 (dừng và báo chủ dự án):** tóm tắt 1 bảng "cấu hình → nạp được? → tok/s → VRAM/RAM". Chủ dự án quyết định: chế độ nào đáng làm UI; giá trị mặc định `N`, `tensor_split`; có làm chế độ 3 hay không. Không tự tiếp tục khi chưa có quyết định.

### PHA 2 — `apps/worker-guard`
- [ ] 2.1 Cài đặt mục 2.2 (cờ dòng lệnh, `Mode`, `rpc-worker`, `ParseBuffers` RPC với fixture thật, `Snapshot`/`POST /api/engine` mở rộng, công thức `own` cho rpc-worker từ 1.5, cờ `main.go`: `--rpc-server-bin`, `--rpc-port`, `--rpc-host`).
- [ ] 2.2 Test: dựng dòng lệnh từng chế độ; chuyển mode hai chiều; thu hồi GPU khi `rpc-worker`; tương thích ngược. **Kiểm đột biến**: bỏ `--rpc`, bỏ tiền tố `RPC` ở regex, bỏ nhánh thu hồi → test phải đỏ.
- [ ] 2.3 `make test-go`, `make build-cross` xanh; build `worker-guard.exe`, cập nhật dịch vụ `AICFWorkerGuard` trên Windows (theo plan Windows Pha 6); kiểm vòng đời service sau nâng cấp.

### PHA 3 — `site-gateway` + proto
- [ ] 3.1 Sửa `gateway.proto` (mục 2.1), sinh Go + Python, chạy `golden_test` có chủ ý; trường mới optional để không làm vỡ worker bản cũ.
- [ ] 3.2 `topology.go`, router, registry, OpenAPI (mục 2.3). Test: từng bước state machine (cho cả cụm 2 node và cụm $\ge 3$ node: 1 master + 2 RPC workers; và kịch bản cụm 3 node trong đó gộp 2 node + giữ 1 node chạy `standalone` độc lập), timeout, rollback khi 1 trong $N$ RPC worker lỗi, epoch cũ bị bỏ, quyền admin, audit. **Đột biến**: bỏ rollback, bỏ kiểm epoch, bỏ loại `rpc-worker` khỏi router → test đỏ.
- [ ] 3.3 Cập nhật `tests/integration/test_openapi_conformance.py` nếu cần.

### PHA 4 — UI
- [ ] 4.1 Panel theo 2.4; kiểm bằng Playwright hoặc kịch bản thủ công có ảnh chụp lưu cạnh báo cáo.

### PHA 5 — E2E trên 2 máy thật
- [ ] 5.1 `split`: 4 request đồng thời → cả hai worker nhận tải; ghi tok/s tổng.
- [ ] 5.2 `smart_1gpu` với N đã chốt ở G1: nạp 30B trên từng card, hai worker song song; ghi tok/s mỗi worker.
- [ ] 5.3 `merged` với `tensor_split` đã chốt: `pc-win-01` → `rpc-worker`, `pc-001` nạp 30B; câu hỏi tiếng Việt qua gateway; ghi VRAM hai card.
- [ ] 5.4 Chuyển đổi hai chiều giữa 3 chế độ, đo thời gian mỗi lần chuyển.
- [ ] 5.5 **Kịch bản lỗi:** rút LAN / kill `ggml-rpc-server` khi `merged` → gateway phát hiện, rollback về `split`, request đang chạy nhận lỗi rõ ràng (không treo); người dùng Windows mở tác vụ nặng → `rpc-worker` bị thu hồi `< 2 s`, cụm tự về `split`.
- [ ] 5.6 Lưu `benchmarks/reports/p3_adaptive_topology_and_large_model_e2e.json`; `make verify-release-v1` exit 0 (dán đuôi log).
- [ ] 5.7 Cập nhật `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` mục P3.4b và dòng bảng 4/4b: chỉ `[~]`/`[x]` **kèm giới hạn** (2 máy, model nào, chưa soak dài, chưa đo ở 3–4 PC).

---

## PHẦN 4: RỦI RO

| # | Rủi ro | Mức | Biện pháp / điều kiện |
|---|---|---|---|
| R1 | GGML RPC **không xác thực, không mã hoá**; mâu thuẫn Cổng W5 và P7.3 của plan chính | Cao | Firewall chỉ cho `192.168.1.40` (đã làm `[ĐO]`); ngoài lab dùng đường hầm (WireGuard/stunnel mTLS); không ra WAN; thêm vào quét cổng P7.3; ghi rõ là ngoại lệ có kiểm soát, mặc định tắt |
| R2 | API `worker-guard` nghe `0.0.0.0:8099` không xác thực | Cao | D-TOPO (2.1): directive qua heartbeat, hoặc allowlist + mTLS |
| R3 | Lệch commit llama.cpp giữa 2 máy sau này | Cao | Heartbeat báo commit; gateway từ chối `merged` nếu lệch (hiện cùng `7aca0d2ea` `[ĐO]`) |
| R4 | `-ncmoe 18` OOM hoặc tụt tốc độ mạnh | Cao | Quét N ở Pha 1; auto-fit chỉ đề xuất |
| R5 | Windows Sleep / người dùng chiếm VRAM làm RPC chết giữa chừng | Cao | Pha 0.1; Pha 5.5; gateway phát hiện `rpc-lost` và rollback |
| R6 | Chuyển mode dở dang (nửa cụm đã đổi) | Cao | `epoch`, idempotent, timeout, rollback, audit; test đột biến |
| R7 | Gộp chậm/không thực tế do LAN: RTT trung bình 2,5 ms `[ĐO]` nhưng băng thông hiện chỉ ~20–25 Mbps vì máy Linux dùng Wi-Fi `[ĐO]` | **Cao** | Cắm Ethernet và đo lại trước Pha 1.3; G1; chỉ bật mặc định chế độ thắng |
| R8 | Calibration VRAM cũ không áp dụng cho build mới `7aca0d2ea` | Trung bình | Đo lại điểm kiểm; ghi giới hạn |
| R9 | RAM Linux thấp (30 GB, đã dùng nhiều) khi `-ncmoe` | Trung bình | `free -g` trước mỗi lần đo; giảm `-np`/`-c` |
| R10 | Lease VRAM chưa hỗ trợ worker nhiều GPU | Trung bình | Xem 2.3; ghi giới hạn |
| R11 | Mật khẩu SSH trong tài liệu, repo public | Thấp (đã ẩn bằng `$AICF_WIN_SSH_PASS`) | Dùng biến môi trường `$AICF_WIN_SSH_PASS` hoặc khoá SSH khi commit lên repo công khai |

---

## PHẦN 5: ĐỊNH NGHĨA HOÀN THÀNH (DOD)

- [ ] Pha 0–1 xong, `p3_gpu_modes_sweep.json` có đủ cấu hình (kể cả thất bại), log thật được lưu, G1 đã được chủ dự án quyết.
- [ ] `worker-guard` + `site-gateway`: test xanh, đột biến từng cơ chế bị bắt, `make verify-release-v1` exit 0.
- [ ] E2E 3 chế độ trên 2 máy thật kèm kịch bản lỗi; báo cáo JSON có giới hạn thí nghiệm.
- [ ] Mọi con số trong báo cáo và UI có nhãn nguồn; mục chưa đo ghi `[CHƯA XÁC MINH]`; plan chính chỉ tick kèm giới hạn.
- Ngoài phạm vi: 70B–235B, quá 2 card, gộp qua WAN, huấn luyện phân tán.

**Báo cáo cuối cho chủ dự án (tiếng Việt, ngắn):** đã làm gì; bảng số đo chính kèm nhãn; cái gì chưa đo/giới hạn; phát hiện bất ngờ; mục cần quyết định.

**Dừng và hỏi chủ dự án khi:** máy Windows không thức dậy; lệch commit không build lại được; mọi cấu hình 30B đều OOM; gộp chậm hơn chế độ 2 (G1); cần thao tác phá huỷ ngoài `C:\aicf\`; cần mở firewall rộng hơn kế hoạch; chọn phương án điều khiển HTTP tạm (2.1).

---

## PHẦN 6: TRẠNG THÁI TRIỂN KHAI THỰC TẾ (CẬP NHẬT SAU KHI SỬA MÃ, 2026-10-27, CHƯA COMMIT)

Mã Pha 2–4 được viết **trước** khi Pha 1 (đo) và cổng G1 xong, trái thứ tự của kế hoạch; vì vậy các mục Pha 2–5 ở trên vẫn để `[ ]`. Phần 2.2 và danh sách "Cấu trúc mã" ở Phần 0 mô tả mã *trước* thay đổi; nguồn đúng là mã hiện tại.

### 6.1. Đã sửa và đã kiểm `[ĐO]`
- `make test-go` xanh (gofmt + vet + `-race`), `make build-cross` xanh, **`make verify-release-v1` exit 0** (436 pytest passed, 79 skipped; golangci-lint 0 issue; ruff, mypy, xác minh chữ ký SBOM đều đạt). Giới hạn: các test mới dùng worker giả (httptest), không phải 2 máy thật; coverage `worker-guard` 53,9%.
- **Kiểm đột biến:** 11 đột biến (bỏ chờ `rpc-ready`, bỏ rollback, bỏ khoá busy, bỏ "master trước khi tách", bỏ kiểm worker trùng, bỏ lọc `rpc://` ở router, bỏ reset mode, bỏ giữ marker `rpc://`, bỏ yêu cầu N của smart, đổi mặc định bind rpc về `0.0.0.0`, bỏ validate `/api/engine`) → cả 11 bị test bắt. Giới hạn: đột biến đơn, chạy bằng `go test -run` theo nhóm; một đột biến không biên dịch cũng bị tính là "bị bắt" (chưa tách riêng).
- **Lỗi thật đã sửa:** (1) `Switch()`/POST không kèm `mode` không thoát được `rpc-worker` → nay `mode` rỗng = `standalone` và xoá cấu hình đa GPU; (2) `baseName("rpc://50052")` bị cắt thành `50052` nên heartbeat mất marker và **router sẽ gửi inference vào worker RPC** — nay giữ nguyên marker (test riêng; test cũ của gateway chỉ giả lập chuỗi `rpc://` nên không bắt được lỗi này).
- **Bỏ auto-fit theo tên model** (`isLargeMoE`) và mọi giá trị `-ncmoe 18`/`q8_0` gắn cứng ở worker, gateway và UI. `smart_1gpu` bắt buộc `n_cpu_moe > 0` (worker và gateway từ chối, UI chặn); loại KV là trường riêng `kv_type` (`f16|q8_0`), không bị ép.
- **Điều phối gateway viết lại:** không giữ khoá qua lời gọi mạng (`GET` luôn đọc được, trạng thái `switching`); chuyển đang chạy thì POST thứ hai nhận **409**; yêu cầu sai nhận **400** (chế độ, model, worker lạ/offline/trùng, `tensor_split` sai số phần tử hoặc ký tự lạ, `kv_type`); chờ `rpc-ready` và chờ master `running` đúng model bằng heartbeat (timeout cấu hình được, mặc định 30 s / 300 s) thay vì `Sleep(1s)` và `202`; lỗi giữa chừng → khôi phục từng worker về model đã báo trước đó (member RPC trước, master sau nếu trước đó là merged) và báo kết quả khôi phục trong `last_error`; `merged→split` dừng master trước; huy hiệu `MASTER (MERGED n GPU, x GB)` tính từ VRAM thật; bỏ id máy gắn cứng (`pc-001`, `pc-win-01`) — bắt buộc truyền `primary_worker_id`; cụm ≥ 3 node và `tensor_split` mặc định chia đều cho n card; switch không bị huỷ khi trình duyệt ngắt kết nối.
- `/api/engine` của worker kiểm chặt `mode`, `rpc_servers` (`host:port[,…]`), `split_mode`, `tensor_split`, `kv_type`, `rpc_port` (giá trị đi vào dòng lệnh tiến trình); `rpc-worker` mặc định bind `127.0.0.1` (muốn nhận kết nối LAN phải đặt `--engine-host` có chủ ý).
- UI: chọn Primary và các member từ danh sách worker online (không còn gắn cứng 2 máy), ô `N` không có mặc định, ô KV, nhãn `tensor_split` theo số card. Cú pháp JS đã kiểm bằng `node` `[ĐO]`; **chưa chạy trong trình duyệt thật** `[CHƯA XÁC MINH]`.
- OpenAPI cập nhật (`kv_type`, mã 400/409/500, `primary_worker_id` bắt buộc ở request), `test_openapi_conformance.py` 6 test xanh. `.gitignore` bổ sung binary; hai binary rác đã xoá.

### 6.2. Còn lại — chưa làm, không thể hoặc không nên làm khi chưa có quyết định/đo
1. **Pha 1 còn lại và cổng G1.** Đã xong 1.1, 1.2 (chế độ 2 trên 1 card, xem 1.2). Còn 1.3–1.7 (chế độ 3 qua RPC), bị chặn bởi băng thông Wi-Fi (Phần 0). `tensor_split` mặc định vẫn **không có giá trị khuyến nghị**; với `n_cpu_moe` thì RTX 3060 cần ≥ 19–20 (đã đo), nhưng giá trị cho `pc-win-01` (RTX 5070) chưa đo.
2. **Quyết định D-TOPO:** hiện dùng phương án *tạm* (gateway gọi HTTP tới `engine_url` của worker, không xác thực) — đúng loại phương án mà mục 2.1 yêu cầu hỏi chủ dự án. Phương án mặc định (directive qua heartbeat + `llama_commit` trong `gateway.proto`) **chưa làm**; khi làm sẽ sinh lại cả Go và Python.
3. **Kiểm `llama_commit` hai đầu** (R3) và kiểm VRAM tổng bằng `estimate-vram` trước khi gộp: chưa làm (cần trường mới trong heartbeat).
4. **Công thức `own` cho `rpc-worker`** (Pha 1.5): chưa có; `EngineMiB` của worker RPC vẫn là 0 nên chưa thể chứng minh thu hồi GPU `< 2 s` ở chế độ này (R5).
5. **Fixture `ParseBuffers` bằng log thật** (từ Pha 1) và test thu hồi GPU khi `rpc-worker`; cụm ≥ 3 node mới được kiểm ở tầng gateway với worker giả.
6. **Triển khai lên Windows (cập nhật 28/10):** đã xong — `worker-guard.exe` mới (SHA256 khớp bản build), model 30B có trên `pc-win-01` (SHA256 khớp), quét N cho RTX 5070 đã đo (báo cáo mục 7.1: N=20 nhanh nhất ~65 tok/s nhưng vượt headroom 1536 MiB của dịch vụ; N=26 chạy ổn). Còn: `--engine-host` cho member RPC phải đặt tường minh; dịch vụ Windows đang chạy với `--model` 4B và headroom 1536.
7. **Pha 5 E2E trên 2 máy thật** (gồm kịch bản lỗi) và cập nhật plan chính: chưa làm.
8. Thông lượng `~335 tok/s` và `50–70 tok/s` vẫn **không có nguồn đo**.


### 6.3. Bổ sung 28/10 — lỗi tìm ra khi chạy E2E thật & đo lại Chế độ 3 trên cáp 2,5 GbE (chi tiết: `ai-compute-fabric/benchmarks/reports/P3_BAO_CAO_3_CHE_DO.md` mục 7.5 & 8)
- [x] Gateway dùng đúng cổng điều khiển của worker-guard (`-worker-control-port`, mặc định 8099); trước đó mọi bước topology trên máy thật đều 500.
- [x] Worker-guard quên footprint của model cũ khi đổi cấu hình (rollback không khởi động lại engine nhỏ hơn): đã sửa + test + mutation.
- [x] Gateway chỉ coi là xong khi nhận heartbeat SAU lệnh (đổi riêng `-ncmoe` không còn báo thành công giả): đã sửa + test + mutation.
- [x] `--engine-start-timeout` cho worker-guard.
- [x] Cắm cáp Ethernet 2,5 GbE trực tiếp (`10.42.0.1` ↔ `10.42.0.225`, `2,372 Gbps / 282,77 MB/s`): chạy E2E `merged` với `Qwen3-4B-Q4_K_M` (`switch_s = 32,3 s`, 1 luồng **117,7 tok/s**, 4 luồng **200,5 tok/s tổng**) và **`Qwen3-30B-A3B-Q4_K_M` (`switch_s = 86,7 s`, 100% VRAM trên 2 card, 1 luồng 126,3 tok/s, 2 luồng 176,2 tok/s tổng — nhanh gấp 2–3× so với Smart 1-GPU)**; kiểm tự phục hồi khi tiến trình `llama-server` (merged master) bị ngắt (`worker-guard` tự khởi động lại sau 30,5 s – 42,4 s).
- [x] Chạy lại `make verify-release-v1` sau lượt sửa cuối: `exit 0`.
- [ ] Kiểm giao diện web bằng trình duyệt thật.
