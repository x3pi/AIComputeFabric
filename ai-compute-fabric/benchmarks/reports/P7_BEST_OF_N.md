# Báo cáo P7 — Best-of-$N$ cho Sinh Code trên AI Compute Fabric (`pc-001` + `pc-win-01`)

- **Ngày thực hiện:** 2026-10-31
- **Kế hoạch tham chiếu:** `KE_HOACH_BEST_OF_N_CODE.md` (Pha 0, Pha A, Pha 1, Pha 2, Pha 3, Pha 4, Pha 5; đối chiếu Pha E tại `benchmarks/reports/P7E_DAN_AGENT_DAN_HOI.md`)
- **Cấu hình cụm đo thực nghiệm:**
  - `pc-001` (Linux, Intel Core Ultra 7 265K, 8 nhân P `CPU 0..7`, RAM 64 GiB, NVIDIA RTX 3060 12 GB)
  - `pc-win-01` (Windows 11, NVIDIA RTX 5070 12 GB, kết nối LAN Gigabit / RPC `:50052`)
  - Mô hình chính: `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` chạy ở chế độ C (gộp 2 card qua `llama-server` RPC, tensor-split `48,52`, `-c 32768 -np 1 -t 8 -Cr 0-7 --cpu-strict 1`) `[ĐO]`

## Quy ước nhãn bằng chứng

- `[ĐO]`: Số liệu đo trực tiếp trong phiên thực thi này, có tệp kết quả JSON và kịch bản tái lập đi kèm.
- `[ĐỌC MÃ]`: Khẳng định dựa trên mã nguồn thực tế trong kho mã `ai-compute-fabric/`.
- `[TÀI LIỆU]`: Số liệu dẫn chiếu từ các báo cáo đo phần cứng trước đó trong cùng kho (`P3_BANG_SO_LIEU_CUOI.md`, `P3_CODER_30B_GOP_2_CARD.md`, `P9_CPU_AFFINITY_KV.md`, `P7E_DAN_AGENT_DAN_HOI.md`).
- `[SUY LUẬN]`: Ước tính ngoại suy từ các phép đo thành phần khi không tự ý chuyển đổi mô hình/chế độ trên nút Windows theo Quy tắc an toàn cụm (Mục 3 Quy tắc 6).
- `[CHƯA XÁC MINH]`: Nội dung chưa đo trực tiếp trên phần cứng thật.

---

## 1. Pha 0 — Đo nền (`pass@1`) và Kiểm chứng Cách ly Sandbox

### 1.1. Bộ bài chuẩn và Tính toàn vẹn Dữ liệu (`[ĐO]`)

Hai bộ bài chuẩn được tải về tại `ai-compute-fabric/benchmarks/data/` kèm tệp giấy phép và thông tin nguồn gốc (`benchmarks/data/README.md`):

| Bộ bài | Số lượng bài | Dung lượng (bytes) | SHA-256 | Giấy phép |
|---|---:|---:|---|---|
| `HumanEval.jsonl` | `164` `[ĐO]` | `214.438` `[ĐO]` | `1d49078ba3e2b196b9344535bef34a43021f038fad9561d6ee7c53450609a6a2` `[ĐO]` | MIT (`LICENSE.HumanEval`) |
| `sanitized-mbpp.json` | `427` `[ĐO]` | `255.053` `[ĐO]` | `ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9` `[ĐO]` | Apache-2.0 / CC-BY-4.0 (`LICENSE.MBPP`) |

### 1.2. Kiểm tra Đột biến Cách ly Sandbox (`[ĐO]`)

Mọi đoạn mã do mô hình sinh ra trong `code_eval.py` và `bestofn/runner.py` đều bắt buộc chạy qua `pick_sandbox(require_network_isolation=True)` (`DockerSandbox` với `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --pids-limit 64 --memory 128m`) `[ĐỌC MÃ]`.

Kết quả kiểm tra đột biến bảo mật (`benchmarks/reports/p7_pha0_sandbox_mutation.json`):

| Bài kiểm đột biến (Mutation Test) | Hành vi độc hại giả lập | Kết quả thực tế trong `DockerSandbox` | Đạt cổng an toàn |
|---|---|---|---|
| `network_external_blocked` | Kết nối ra Internet (`socket.create_connection(("1.1.1.1", 80))`) | Bị chặn ngay bởi `--network none` (`OSError: Network is unreachable`) `[ĐO]` | **PASS** `[ĐO]` |
| `network_loopback_gateway_blocked` | Kết nối tới `127.0.0.1:18080` (cổng Gateway nội bộ) | Bị chặn (`ConnectionRefusedError` trong netns cô lập) `[ĐO]` | **PASS** `[ĐO]` |
| `readonly_fs_blocked` | Ghi tệp `/etc/pwned` hoặc `/tool/pwned` | Bị chặn bởi `--read-only` + UID `65534` (`OSError: Read-only file system`) `[ĐO]` | **PASS** `[ĐO]` |
| `infinite_loop_killed` | Vòng lặp vô hạn `while True: pass` | Bị hủy đúng hạn bởi `SandboxLimits(timeout_s=2.0)` (`2.607,4 ms`) `[ĐO]` | **PASS** `[ĐO]` |
| `output_flood_bounded` | In tràn `print("A" * 1_000_000)` | Bị cắt gọn an toàn ở ngưỡng `max_output_bytes` (`output_truncated=True`) `[ĐO]` | **PASS** `[ĐO]` |
| `valid_code_passes` | Hàm hợp lệ + `assert` đúng | Chạy thành công, trả JSON chuẩn (`compile_ok=True, passed=True`) `[ĐO]` | **PASS** `[ĐO]` |
| `rejects_unisolated_backend` | Ép `NullSandbox(isolates_network=False)` vào `SandboxPool` | Ném ngoại lệ `SandboxUnavailable`, từ chối thực thi (`test_bestofn.py`) `[ĐO]` | **PASS** `[ĐO]` |

### 1.3. Điểm nền `pass@1` trên 164 bài HumanEval (`[ĐO]`)

Kịch bản `benchmarks/hardware/code_eval.py` chạy hai lần độc lập trên toàn bộ 164 bài HumanEval với `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (`temperature=0.0`, `seed=42`, `max_tokens=512`, qua cổng `/v1/chat/completions` với `X-Priority: batch`):

| Lần đo | Tệp báo cáo | Số bài giải đúng / Tổng | `pass@1` (%) | Tổng thời gian (`s`) | Trung bình / bài (`s`) | Trung vị tốc độ (`tok/s`) |
|---|---|---:|---:|---:|---:|---:|
| **Lần 1** | `p7_pha0_humaneval_run1.json` | **`149 / 164`** `[ĐO]` | **`90,85%`** `[ĐO]` | `388,00 s` `[ĐO]` | `2,366 s` `[ĐO]` | `123,67 tok/s` `[ĐO]` |
| **Lần 2** | `p7_pha0_humaneval_run2.json` | **`147 / 164`** `[ĐO]` | **`89,63%`** `[ĐO]` | `387,21 s` `[ĐO]` | `2,361 s` `[ĐO]` | `124,89 tok/s` `[ĐO]` |
| **Độ lệch** | — | **`2 bài`** ($\le 2$ bài yêu cầu) `[ĐO]` | `1,22 điểm %` `[ĐO]` | `0,79 s` `[ĐO]` | `0,005 s` `[ĐO]` | `1,22 tok/s` `[ĐO]` |

**Phân tích nguyên nhân 17 bài thất bại hợp nhất giữa Lần 1 và Lần 2 (`[ĐO]`):**
1. **Nhóm lỗi `SyntaxError` do cắt cụt ở `max_tokens=512` (8 bài):** `HumanEval/32, 75, 76, 81, 113, 124, 130, 132`. Ở cấu hình nền, mô hình chép lại nguyên văn docstring dài của đề bài vào bên trong khối `def ...:` trước khi viết thân hàm, dẫn đến chạm trần 512 token.
2. **Nhóm lỗi `AssertionError` do sai thuật toán / trường hợp biên (9 bài):** `HumanEval/115, 116, 118, 122, 127, 134, 145, 160, 163` (ví dụ: sắp xếp theo số bit 1 trong `HumanEval/116`, quy ước dấu số âm trong `HumanEval/122`, thứ tự toán tử lũy thừa trong `HumanEval/160`).

> **Check Pass Pha 0:** **ĐẠT** (lặp lại 2 lần chênh đúng `2` bài $\le 2$, toàn bộ kiểm tra đột biến sandbox đều PASS).

---

## 2. Pha A — Giao diện Trừu tượng hóa Compute Agent (Che giấu Phần cứng)

### 2.1. Thực thi trên `site-gateway` và `bestofn` (`[ĐỌC MÃ]` + `[ĐO]`)

1. **`apps/site-gateway/compute_agents.go` & `router.go` & `proxy.go`:**
   - Bổ sung `GET /api/compute-agents` (hỗ trợ lọc `?label=`, `?model=`, `?state=`, phân trang `?offset=&limit=`, trả header `X-Total-Count`) và `GET /api/compute-agents/stream` (SSE đẩy mỗi khi danh sách compute agent thay đổi) `[ĐỌC MÃ]`.
   - Quy tắc hợp nhất phần cứng (`BuildComputeAgents`):
     - Nhóm gộp (`mode = "merged"` trong `TopologyCoordinator`) xuất hiện thành **1** `ComputeAgent` duy nhất với `kind = "group"`, `agent_id = primary_worker_id` (`pc-001`), `tok_s_ref = 126.7`.
     - Thành viên RPC (`pc-win-01` hoặc bất kỳ worker nào có `model` dạng `rpc://...` / `rpc-server` hoặc nằm trong `rpc_worker_ids`) **tuyệt đối không xuất hiện** trong `/api/compute-agents` `[ĐỌC MÃ]` `[ĐO]`.
     - Trường `members` bị ẩn hoàn toàn đối với mọi caller thông thường (kể cả khi truyền `?include_members=1`); chỉ khi caller có quyền `admin` hoặc `operator` VÀ truyền `?include_members=1` thì `members` mới được trả về để chẩn đoán vận hành `[ĐỌC MÃ]` `[ĐO]`.
   - Bổ sung định tuyến có ghim đích `X-AICF-Agent: <agent_id>` và `X-AICF-Label: <label>` trên `/v1/chat/completions`: khi có `X-AICF-Agent`, `Router.Acquire` chỉ chọn đúng agent đó; nếu agent đang `busy` (`inflight >= slots`), `switching`, hoặc `offline`, Gateway trả ngay `503 Service Unavailable` kèm `Retry-After` để `bestofn/sampler.py` chủ động tái phân bổ, **không bao giờ âm thầm chuyển sang worker khác** `[ĐỌC MÃ]` `[ĐO]`.
2. **`bestofn/agents_client.py` & Kiểm tra đột biến tĩnh (`tests/unit/test_bestofn.py`):**
   - Quét AST và biểu thức chính quy trên toàn bộ các tệp trong `bestofn/` xác nhận **không có bất kỳ hằng số hay chuỗi phần cứng nào** (`pc-001`, `pc-win-01`, `3060`, `5070`, `vram`, `tensor_split`, `ncmoe`, `rpc_servers`, `/api/workers`, `/api/cluster/topology`) `[ĐO]`.
   - `go test ./...` trong `apps/site-gateway` (`PASS`, `5,08 s`), `pytest tests/integration/test_openapi_conformance.py` (`6 passed`), và `pytest tests/unit/test_bestofn.py` (`5 passed`) đều vượt qua 100% `[ĐO]`.

### 2.2. Kết quả Mở rộng Quy mô Mô phỏng $K \in \{10, 25, 50\}$ Compute Agent (`[ĐO]`)

Kịch bản `benchmarks/hardware/p7_pha_a_scale_bench.py` dựng một tiến trình `site-gateway` thật cùng bộ phát heartbeat gRPC cho $K \in \{10, 25, 50\}$ compute agent (xen kẽ `kind="single"` và `kind="group"` kèm $5, 12, 25$ thành viên RPC ẩn, tốc độ khác nhau từ `25` đến `150 tok/s`, `slots` từ `1` đến `4`) và chạy `bestofn` ở $N \in \{8, 16, 32\}$ mẫu (`benchmarks/reports/p7_pha_a_scale.json`):

| Số Compute Agent hiển thị ($K$) | Số RPC Member bị ẩn | Số mẫu ($N$) | Overhead lập lịch `planner + gateway` (`ms`) | Thời gian lấy mẫu song song (`ms`) | Thời gian `SandboxPool(M=4)` (`ms`) | Số mẫu hoàn tất / vượt test |
|---:|---:|---:|---:|---:|---:|---:|
| `10` `[ĐO]` | `5` `[ĐO]` | `8` | `0,65 ms` `[ĐO]` | `2.069,33 ms` `[ĐO]` | — | `8 / 8` `[ĐO]` |
| `10` `[ĐO]` | `5` `[ĐO]` | `16` | `1,25 ms` `[ĐO]` | `1.066,75 ms` `[ĐO]` | — | `16 / 16` `[ĐO]` |
| `10` `[ĐO]` | `5` `[ĐO]` | `32` | `0,95 ms` `[ĐO]` | `1.098,28 ms` `[ĐO]` | — | `32 / 32` `[ĐO]` |
| `25` `[ĐO]` | `12` `[ĐO]` | `8` | `1,01 ms` `[ĐO]` | `2.064,87 ms` `[ĐO]` | — | `8 / 8` `[ĐO]` |
| `25` `[ĐO]` | `12` `[ĐO]` | `16` | `1,76 ms` `[ĐO]` | `79,62 ms` `[ĐO]` | — | `16 / 16` `[ĐO]` |
| `25` `[ĐO]` | `12` `[ĐO]` | `32` | `0,69 ms` `[ĐO]` | `1.099,04 ms` `[ĐO]` | — | `32 / 32` `[ĐO]` |
| `50` `[ĐO]` | `25` `[ĐO]` | `8` | `0,55 ms` `[ĐO]` | `2.053,78 ms` `[ĐO]` | — | `8 / 8` `[ĐO]` |
| `50` `[ĐO]` | `25` `[ĐO]` | `16` | `1,27 ms` `[ĐO]` | `46,88 ms` `[ĐO]` | — | `16 / 16` `[ĐO]` |
| **`50`** `[ĐO]` | **`25`** `[ĐO]` | **`32`** | **`2,73 ms`** `[ĐO]` | **`1.110,36 ms`** `[ĐO]` | **`4.705,12 ms`** (`6,80 mẫu/s`) `[ĐO]` | **`32 / 32`** `[ĐO]` |

**Kiểm tra ngắt 20% agent (`10/50` agent) giữa chừng ở $K=50, N=32$ (`[ĐO]`):**
- Khi 10 agent (trong đó có 6 agent đang giữ mẫu được phân bổ ban đầu: `agent-00, 01, 02, 03, 07, 10, 11, 14, 15, 18`) bị ngắt đột ngột và trả `503 Service Unavailable`, `bestofn/sampler.py` tự động phát hiện và tái phân bổ cả **6** mẫu sang các agent còn sống (`reassignments_count = 6`) `[ĐO]`.
- Toàn bộ **`32 / 32`** mẫu đều hoàn tất và vượt qua unit test trong `SandboxPool(M=4)` (`sampling_wall_ms = 2.097,25 ms`, `sandbox_wall_ms = 4.779,18 ms`) `[ĐO]`.

> **Check Pass Pha A:** **ĐẠT** toàn bộ 4 tiêu chí (a), (b), (c), (d): cùng một mã `BestOfNOrchestrator` chạy qua 3 chế độ topo mà không đổi dòng code nào; tự tái phân bổ khi rớt agent giữa chừng; không lộ phần cứng; ở $K=50, N=32$ overhead lập lịch chỉ `2,73 ms < 50 ms` và chịu lỗi ngắt 10/50 agent hoàn tất đủ 32/32 mẫu.

---

## 3. Pha 1 — Chạy Song song hay Tuần tự trên Cụm Hiện tại?

Theo Quy tắc an toàn cụm (Mục 3 Quy tắc 6 trong `KE_HOACH_BEST_OF_N_CODE.md` và chỉ thị không can thiệp tiến trình trên `pc-win-01` / giữ nguyên chế độ C đang phục vụ), bảng dưới đây tổng hợp số đo trực tiếp `[ĐO]` trên chế độ C hiện hành kết hợp với số đo phần cứng `[TÀI LIỆU]` từ `P3_BANG_SO_LIEU_CUOI.md`, `P3_CODER_30B_GOP_2_CARD.md`, `P9_CPU_AFFINITY_KV.md` và ước tính thời gian sinh $N=8$ mẫu (`~150 token/mẫu`):

| Chế độ vận hành | Mô hình | Cấu hình `slots` (`PARALLEL`) & Ngữ cảnh (`CTX`) | Tốc độ giải mã 1 luồng (`tok/s`) | Hành vi VRAM / KV Cache | Thời gian sinh $N=8$ mẫu (`s`) | Chất lượng nền `pass@1` HumanEval |
|---|---|---|---|---|---:|---:|
| **Chế độ C (1 Compute Agent `kind="group"`, gộp 2 GPU)** | `Qwen3-Coder-30B-A3B-Q4_K_M` | `slots=1`, `CTX=32768` | **`124,89 tok/s`** `[ĐO]` (`126,7 tok/s` `[TÀI LIỆU]`) | Ổn định, đủ 32k context trên 24 GB VRAM gộp (`48,52`) `[ĐO]` | **`8,54 s – 12,23 s`** `[ĐO]` | **`90,85%` / `89,63%`** `[ĐO]` |
| **Chế độ C (gộp 2 GPU, tăng `PARALLEL`)** | `Qwen3-Coder-30B-A3B-Q4_K_M` | `slots=2`, `CTX=16384` (`8192/slot`) | `~118 tok/s` tổng (`~59 tok/s/slot`) `[SUY LUẬN]` | Giữ nguyên tổng KV 16k–32k thì không tràn VRAM, nhưng mỗi slot chỉ còn 8k–16k context `[TÀI LIỆU]` | `~9,5 s` `[SUY LUẬN]` | `~90,8%` `[SUY LUẬN]` |
| **Chế độ C (gộp 2 GPU, `PARALLEL=4` ở 32k)** | `Qwen3-Coder-30B-A3B-Q4_K_M` | `slots=4`, `CTX=32768` | Không khả thi hoặc tụt tốc độ do chạm vách đá VRAM `[TÀI LIỆU]` | Tràn VRAM 12 GB trên từng card nếu nhân 4 bộ đệm KV 32k `[TÀI LIỆU]` | — | — |
| **Chế độ B (2 Compute Agent `kind="single"` độc lập, `-ncmoe`)** | `Qwen3-Coder-30B-A3B-Q4_K_M` (`-ncmoe 20` / `16`) | `pc-001` (`slots=1, CTX=8192`, ghim P-core `0-7`) + `pc-win-01` (`slots=1, CTX=8192`) | `pc-001`: **`46,15 tok/s`** `[TÀI LIỆU]` (`P9`)<br>`pc-win-01`: **`52,20 tok/s`** `[TÀI LIỆU]` (`P3`)<br>Tổng cụm: **`98,35 tok/s`** | Chạy độc lập không qua RPC LAN; mỗi máy tự giữ 4+4 mẫu nhưng tổng băng thông (`98,35 tok/s`) vẫn thấp hơn chế độ C gộp 2 card (`124,89 tok/s`) | `~12,2 s` `[SUY LUẬN]` | `~90,8%` (cùng trọng số Q4_K_M) `[SUY LUẬN]` |
| **Chế độ A (2 Compute Agent `kind="single"`, mô hình đặc 14B)** | `DeepSeek-R1-Distill-Qwen-14B-Q4_K_M` | `pc-001` (`slots=1`) + `pc-win-01` (`slots=1`) | `pc-001`: **`41,1 tok/s`** `[TÀI LIỆU]`<br>`pc-win-01`: **`76,8 tok/s`** `[TÀI LIỆU]`<br>Tổng cụm: **`117,9 tok/s`** | Bắt buộc suy luận `<think>` dài nên số token sinh mỗi mẫu lớn gấp 3–4 lần `[TÀI LIỆU]` | `~28 – 35 s` `[SUY LUẬN]` | `[CHƯA XÁC MINH]` |
| **Chế độ A (2 Compute Agent `kind="single"`, mô hình đặc 8B)** | `Qwen3-8B-Q4_K_M` | `pc-001` (`slots=2`) + `pc-win-01` (`slots=2`) | `pc-001`: **`63,2 tok/s`** `[TÀI LIỆU]`<br>`pc-win-01`: **`110,9 tok/s`** `[TÀI LIỆU]`<br>Tổng cụm: **`174,1 tok/s`** | Vừa hoàn toàn VRAM 12 GB trên từng máy, 4 slot song song toàn cụm `[TÀI LIỆU]` | `~6,9 s` `[SUY LUẬN]` | `[CHƯA XÁC MINH]` |

**Nhận xét Pha 1:**
- Với mô hình MoE chuyên code **`Qwen3-Coder-30B-A3B-Instruct-Q4_K_M`** (chỉ kích hoạt `3B` tham số mỗi token), chế độ **C (gộp 2 card `PARALLEL=1, CTX=32768`)** đạt tốc độ đơn luồng rất cao (**`124,89 tok/s`** `[ĐO]`), vượt cả tổng thông lượng của 2 máy chạy độc lập ở chế độ B (`46,15 + 52,20 = 98,35 tok/s` `[TÀI LIỆU]`), đồng thời bảo toàn trọn vẹn cửa sổ ngữ cảnh **32k token** cho các bài toán kèm ngữ cảnh tệp lớn.
- Khi có từ 2 nhóm máy trở lên (như đã chứng minh ở Pha E với 2 worker NATS JetStream đạt speedup **`1,83×`** `[TÀI LIỆU]` và Pha A.4 với 10–50 compute agent `[ĐO]`), bộ lập lịch `bestofn/sampler.py` tự động chia mẫu tỷ lệ thuận với `slots * tok_s_ref` mà không cần sửa mã.

---

## 4. Pha 2 & Pha 3 — Hiệu quả Best-of-$N$ ($N = 1, 2, 4, 8$) khi Có Test Sẵn vs. Tự Sinh Test ($M = 3$)

### 4.1. Thiết kế Thực nghiệm (`benchmarks/hardware/p7_pha2_3_bestofn_eval.py`)

Trên mô hình `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (chế độ C gộp 2 GPU), chúng tôi thực hiện đo trực tiếp (`[ĐO]`):
1. **Tập chuyên sâu HumanEval 30 bài (`cohort_total = 30`):** bao gồm **toàn bộ 17 bài thất bại** ở Pha 0 (`union_fail_ids`) cộng với **13 bài đậu** được lấy mẫu cách đều trên toàn bộ 147 bài đậu của Pha 0. Với mỗi bài, hệ thống sinh đủ $N=8$ ứng viên theo 8 chiến lược của `bestofn/planner.py` (mẫu `0` là greedy anchor `T=0.0` kèm chỉ dẫn không lặp lại docstring dài; các mẫu `1..7` trải nhiệt độ từ `0.40` đến `0.95` với các gợi ý kiểm tra biên, thư viện chuẩn, bất biến vòng lặp...).
2. **Đánh giá Pha 2 (Có bộ test chuẩn của đề bài):** chấm cả 8 ứng viên trong `SandboxPool(pool_size=4)` và chọn ứng viên tốt nhất cho các tập con $N \in \{1, 2, 4, 8\}$. Kết hợp với 134 bài đã vượt qua ổn định ở cả 2 lần đo nền Pha 0 (và được kiểm chứng lại trên 13 bài đại diện đạt `13/13 = 100%` ở mọi $N$), ta thu được điểm **`pass@best-of-N` trên toàn bộ 164 bài HumanEval** `[ĐO]`.
3. **Đánh giá Pha 3 (Không có test sẵn — tự sinh $M=3$ bộ test + đồng thuận CodeT):** không dùng bộ test chuẩn của HumanEval trong bước chọn; mô hình tự sinh $M=3$ bộ `assert` độc lập (`bestofn/test_gen.py`), lọc bỏ các `assert` không chạy được trên bất kỳ ứng viên nào (`filter_executable_generated_tests`), chấm điểm đồng thuận chéo CodeT (`test_source="generated"`) để chọn ứng viên thắng cuộc cho $N \in \{1, 2, 4, 8\}$, rồi mới đối chiếu ứng viên được chọn với bộ test chuẩn của HumanEval để đo:
   - Tỷ lệ giải đúng thực tế khi tự sinh test;
   - **Tỷ lệ chọn nhầm so với Oracle (`false_selection_vs_oracle`)**: trong $N$ mẫu có ít nhất 1 mẫu đúng nhưng bộ test tự sinh lại chọn mẫu sai;
   - **Tỷ lệ làm hỏng mẫu gốc (`false_selection_broke_anchor`)**: mẫu `0` (`N=1`) đã đúng nhưng bộ test tự sinh lại chọn nhầm sang một mẫu `1..N-1` bị sai.
4. **Đánh giá bổ sung trên MBPP (`15` bài đầu từ `sanitized-mbpp.json`):** chạy đủ $N \in \{1, 2, 4, 8\}$ mẫu qua `SandboxPool(pool_size=4)` (`[ĐO]`).

### 4.2. Bảng Kết quả Tổng hợp trên HumanEval và MBPP (`[ĐO]`)

Dữ liệu chi tiết lưu tại `benchmarks/reports/p7_pha2_3_bestofn_eval.json`:

| Cấu hình | Nhóm 30 bài chuyên sâu (Có test sẵn) | Số bài lỗi Pha 0 được cứu / 17 bài (Có test sẵn) | Toàn bộ **164 bài HumanEval** (Có test sẵn) | Nhóm 30 bài chuyên sâu (**Tự sinh test $M=3$**) | Số bài lỗi Pha 0 được cứu / 17 bài (**Tự sinh test $M=3$**) | Tỷ lệ chọn nhầm vs. Oracle (Pha 3) | Tỷ lệ làm hỏng mẫu gốc `N=1` (Pha 3) | MBPP 15 bài (Có test sẵn) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Pha 0 `pass@1` (Lần 1 / Lần 2)** | `15/30` (`50,00%`) / `13/30` (`43,33%`) `[ĐO]` | `0 / 17` `[ĐO]` | **`149/164` (`90,85%`)** / **`147/164` (`89,63%`)** `[ĐO]` | — | — | — | — | — |
| **Best-of-1 ($N=1$, Planner Anchor)** | `18 / 30` (`60,00%`) `[ĐO]` | `5 / 17` (`+29,4%`) `[ĐO]` | **`152 / 164` (`92,68%`)** `[ĐO]` | `18 / 30` (`60,00%`) `[ĐO]` | `5 / 17` `[ĐO]` | `0 / 30` (`0,00%`) `[ĐO]` | `0 / 30` (`0,00%`) `[ĐO]` | `15 / 15` (`100,0%`) `[ĐO]` |
| **Best-of-2 ($N=2$)** | `19 / 30` (`63,33%`) `[ĐO]` | `6 / 17` (`+35,3%`) `[ĐO]` | **`153 / 164` (`93,29%`)** `[ĐO]` | `18 / 30` (`60,00%`) `[ĐO]` | `5 / 17` `[ĐO]` | `1 / 30` (`3,33%`) `[ĐO]` | `0 / 30` (`0,00%`) `[ĐO]` | `15 / 15` (`100,0%`) `[ĐO]` |
| **Best-of-4 ($N=4$)** | **`23 / 30` (`76,67%`)** `[ĐO]` | **`10 / 17` (`+58,8%`)** `[ĐO]` | **`157 / 164` (`95,73%`)** `[ĐO]` | **`20 / 30` (`66,67%`)** `[ĐO]` | **`7 / 17`** `[ĐO]` | `3 / 30` (`10,00%`) `[ĐO]` | `1 / 30` (`3,33%`) `[ĐO]` | `15 / 15` (`100,0%`) `[ĐO]` |
| **Best-of-8 ($N=8$)** | **`25 / 30` (`83,33%`)** `[ĐO]` | **`12 / 17` (`+70,6%`)** `[ĐO]` | **`159 / 164` (`96,95%`)** `[ĐO]` | **`23 / 30` (`76,67%`)** `[ĐO]` | **`10 / 17`** `[ĐO]` | **`2 / 30` (`6,67%`)** `[ĐO]` | **`0 / 30` (`0,00%`)** `[ĐO]` | `15 / 15` (`100,0%`) `[ĐO]` |

### 4.3. Chi tiết Từng Bài trong 17 Bài Thất bại ở Pha 0 (`[ĐO]`)

Bảng dưới đây cho thấy chính xác cách `Best-of-N` cứu từng bài trong số 17 bài thất bại của Pha 0 (`benchmarks/reports/p7_pha2_3_bestofn_eval.json`):

| Mã bài (`task_id`) | Lỗi ở Pha 0 (`pass@1`) | $N=1$ (Planner) | $N=4$ (Có test sẵn) | $N=8$ (Có test sẵn) | $N=8$ (Tự sinh test $M=3$) | Phân tích kỹ thuật (`[ĐO]`) |
|---|---|---|---|---|---|---|
| `HumanEval/32` (`find_zero`) | `SyntaxError` (cắt cụt) | Trượt | Trượt | **ĐẬU** (mẫu `#5`) | **ĐẬU** (mẫu `#5`) | Bài đa thức bậc lẻ: chỉ có mẫu `#5` (`alternative_paradigm`, chia đôi khoảng tìm nghiệm) vượt qua toàn bộ test; cả Pha 2 và Pha 3 đều chọn đúng mẫu `#5`. |
| `HumanEval/75` (`is_multiply_prime`) | `SyntaxError` (Lần 2) | Trượt | **ĐẬU** (mẫu `#2`) | **ĐẬU** (mẫu `#2`) | Trượt (chọn mẫu `#0`) | Có 3/8 mẫu đúng (`#2, #5, #6`), nhưng test tự sinh quá dễ nên mẫu `#0` cũng vượt qua test tự sinh (1 trong 2 ca chọn nhầm ở $N=8$). |
| `HumanEval/76` (`is_simple_power`) | `SyntaxError` (cắt cụt) | **ĐẬU** | **ĐẬU** | **ĐẬU** | **ĐẬU** | Nhờ nhắc bỏ docstring dài trong `planner.py`, cả 8/8 mẫu đều ngắn gọn và vượt qua 100% test. |
| `HumanEval/81` (`numerical_letter_grade`) | `SyntaxError` (cắt cụt) | **ĐẬU** | **ĐẬU** | **ĐẬU** | **ĐẬU** | Chuỗi `if/elif` dài 13 nhánh không còn bị cắt cụt ở 512 token khi bỏ docstring lặp lại. |
| `HumanEval/113` (`odd_count`) | `SyntaxError` (Lần 2) | **ĐẬU** | **ĐẬU** | **ĐẬU** | **ĐẬU** | 8/8 mẫu đều giải đúng hoàn toàn. |
| `HumanEval/115` (`max_fill`) | `AssertionError` | Trượt | Trượt | Trượt | Trượt | Cả 8 mẫu đều nhầm công thức làm tròn lên `math.ceil(sum(row)/capacity)` ở một ca biên. |
| `HumanEval/116` (`sort_array`) | `AssertionError` | **ĐẬU** | **ĐẬU** | **ĐẬU** | **ĐẬU** | Gợi ý `direct_clean` giúp khóa sắp xếp `(bin(x).count('1'), x)` chính xác cho số không âm. |
| `HumanEval/118` (`get_closest_vowel`) | `AssertionError` | Trượt | **ĐẬU** (mẫu `#1`) | **ĐẬU** (mẫu `#1`) | **ĐẬU** (mẫu `#1`) | Mẫu `#0` sai chỉ số biên; các mẫu `#1..#7` (`edge_cases`) đều đúng và đồng thuận CodeT chọn đúng mẫu `#1`. |
| `HumanEval/122` (`add_elements`) | `AssertionError` | Trượt | **ĐẬU** (mẫu `#2`) | **ĐẬU** (mẫu `#2`) | Trượt (chọn mẫu `#0`) | Mẫu `#2, #3, #4, #7` xử lý đúng số âm 2 chữ số (`len(str(abs(x))) <= 2`), nhưng test tự sinh thiếu số âm nên chọn mẫu `#0`. |
| `HumanEval/124` (`valid_date`) | `SyntaxError` (cắt cụt) | **ĐẬU** | **ĐẬU** | **ĐẬU** | **ĐẬU** | Bỏ docstring dài giúp hàm kiểm tra ngày tháng hoàn tất trọn vẹn trong 512 token. |
| `HumanEval/127` (`intersection`) | `AssertionError` | Trượt | **ĐẬU** (mẫu `#1`) | **ĐẬU** (mẫu `#1`) | **ĐẬU** (mẫu `#1`) | Mẫu `#0` sai khi độ dài đoạn giao $\le 1$; mẫu `#1` (`edge_cases`) kiểm tra đúng số nguyên tố và được cả Pha 2 lẫn Pha 3 chọn đúng. |
| `HumanEval/130` (`tri`) | `SyntaxError` (cắt cụt) | Trượt | Trượt | Trượt | Trượt | Quy nạp dãy Tribonacci chẵn/lẻ khó; cả 8 mẫu đều sai công thức truy hồi. |
| `HumanEval/132` (`is_nested`) | `SyntaxError` (cắt cụt) | Trượt | Trượt | Trượt | Trượt | Bài ngoặc vuông lồng nhau đánh lừa cả 8 mẫu ở ca biên `[[]]`. |
| `HumanEval/134` (`check_if_last_char_is_a_letter`) | `AssertionError` | Trượt | Trượt | **ĐẬU** (mẫu `#7`) | **ĐẬU** (mẫu `#7`) | Chỉ duy nhất mẫu `#7` (`minimal_defensive`, `T=0.95`) xử lý đúng chuỗi kết thúc bằng khoảng trắng đơn; cả Pha 2 lẫn Pha 3 đều chọn đúng mẫu `#7`! |
| `HumanEval/145` (`order_by_points`) | `AssertionError` | Trượt | Trượt | Trượt | Trượt | Quy ước cộng chữ số đầu mang dấu âm trong đề HumanEval gây nhập nhằng cho cả 8 mẫu. |
| `HumanEval/160` (`do_algebra`) | `AssertionError` | Trượt | **ĐẬU** (mẫu `#1`) | **ĐẬU** (mẫu `#1`) | **ĐẬU** (mẫu `#3`) | Mẫu `#0` nối chuỗi sai thứ tự ưu tiên toán tử; các mẫu `#1, #2, #3, #5, #6, #7` dùng biểu thức chuẩn và đều vượt qua test. |
| `HumanEval/163` (`generate_integers`) | `AssertionError` | Trượt | Trượt | Trượt | Trượt | Cả 8 mẫu đều hiểu chữ số chẵn của toàn bộ các số trong `[a, b]` thay vì giới hạn trong `[2, 8]`. |

### 4.4. Phân rã Thời gian Thực thi ở $N = 8$ (`[ĐO]`)

Trung bình trên 30 bài HumanEval chạy đầy đủ cả Pha 2 + Pha 3 (`p7_pha2_3_bestofn_eval.json`):
- **Thời gian sinh 8 mẫu ứng viên (`sampling_8_candidates_ms`):** **`12.225,82 ms`** (`12,23 s`, tương ứng `~1,53 s/mẫu`) `[ĐO]`.
- **Thời gian chấm 8 mẫu trong `SandboxPool(M=4)` (`sandbox_pool_m4_8_candidates_ms`):** **`1.214,61 ms`** (`1,21 s` cho 8 lần chạy container Docker cô lập mạng song song 4 luồng) `[ĐO]`.
- **Thời gian tự sinh $M=3$ bộ test + chấm chéo CodeT trên 8 mẫu (`self_test_generation_plus_eval_m3_ms`):** **`4.137,23 ms`** (`4,14 s`) `[ĐO]`.
- **Tổng thời gian trung bình 1 bài khi chạy gộp cả Pha 2 + Pha 3 ($N=8$):** **`17.577,67 ms`** (`17,58 s`) `[ĐO]`.

> **Check Pass Pha 2 & Pha 3:** **ĐẠT** (bảng kết quả đầy đủ cho $N \in \{1, 2, 4, 8\}$, phân biệt rõ có test sẵn vs. tự sinh test $M=3$, ghi nhận chính xác tỷ lệ chọn nhầm `6,67%` ở $N=8$ và tỷ lệ làm hỏng anchor `0,00%` ở $N=8$).

---

## 5. Pha 4 & Pha 5 — Cổng HTTP `/api/bestofn/v1/chat/completions` và Giả lập Client IDE

### 5.1. Kiểm chứng Bảo mật, Xác thực và Giới hạn Đầu vào (Pha 4, `[ĐO]`)

Dịch vụ `bestofn.service` lắng nghe tại `127.0.0.1:18099` và chỉ được mở ra ngoài qua Reverse Proxy xác thực của `site-gateway` tại `/api/bestofn/*` (`apps/site-gateway/compute_agents.go`, `admin.go`, `deploy/demo/run_demo.sh`). Hợp đồng OpenAPI được cập nhật tại `contracts/openapi/aicf_v1.yaml` và kiểm chứng tự động qua `tests/integration/test_openapi_conformance.py` (`6 passed`) `[ĐO]`.

Kết quả kiểm tra thực tế và kiểm tra đột biến xác thực qua `benchmarks/hardware/p7_pha4_5_api_ide_bench.py` (`benchmarks/reports/p7_pha4_5_api_ide.json`):

| Kịch bản kiểm tra | Cổng gọi | Mã HTTP kỳ vọng | Mã HTTP thực tế | Độ trễ (`ms`) | Kết quả |
|---|---|---:|---:|---:|---|
| Không gửi `Authorization: Bearer` (Mutation Auth) | `POST /api/bestofn/v1/chat/completions` | `401` | **`401 Unauthorized`** `[ĐO]` | `17,86 ms` `[ĐO]` | **PASS** `[ĐO]` |
| Gửi API key sai (`Bearer aicf_invalid_key...`) | `POST /api/bestofn/v1/chat/completions` | `401` | **`401 Unauthorized`** `[ĐO]` | `5,96 ms` `[ĐO]` | **PASS** `[ĐO]` |
| Vượt trần số mẫu `bon.n = 20` (`> 16`) | `POST /api/bestofn/v1/chat/completions` | `400` | **`400 Bad Request`** `[ĐO]` | `7,01 ms` `[ĐO]` | **PASS** `[ĐO]` |
| Ngôn ngữ chưa bật `bon.language = "rust"` | `POST /api/bestofn/v1/chat/completions` | `400` | **`400 Bad Request`** `[ĐO]` | `48,15 ms` `[ĐO]` | **PASS** `[ĐO]` |
| Vượt trần thời gian `bon.timeout_s = 60` (`> 30s`) | `POST /api/bestofn/v1/chat/completions` | `400` | **`400 Bad Request`** `[ĐO]` | `50,19 ms` `[ĐO]` | **PASS** `[ĐO]` |
| Key hợp lệ (`bestofn_api_key.txt`), `bon.n = 4 / 8` | `POST /api/bestofn/v1/chat/completions` | `200` | **`200 OK`** `[ĐO]` | Xem bảng 5.2 | **PASS** `[ĐO]` |

### 5.2. Giả lập Client IDE (Continue / Cline) ở Chế độ `stream: false` và `stream: true` SSE (Pha 5, `[ĐO]`)

Client IDE gọi chuẩn OpenAI `/api/bestofn/v1/chat/completions` với `model: "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"` (`p7_pha4_5_api_ide.json`):

| Số mẫu ($N$) | Chế độ `stream` | HTTP Status | Tổng thời gian đầu-cuối phía Client (`ms`) | Thời gian sinh $N$ mẫu (`sampling_wall_ms`) | Thời gian chạy `SandboxPool(M=4)` (`sandbox_wall_ms`) | Số mẫu biên dịch được / vượt hết test | Độ tự tin (`confidence`) & Tỷ lệ đồng thuận |
|---:|---|---:|---:|---:|---:|---:|---|
| **`N = 4`** | `false` (JSON) | `200` `[ĐO]` | **`4.857,02 ms`** (`4,86 s`) `[ĐO]` | `4.166,02 ms` `[ĐO]` | `642,12 ms` `[ĐO]` | `4 / 4` `[ĐO]` | `high` (`1,00`) `[ĐO]` |
| **`N = 4`** | `true` (SSE) | `200` `[ĐO]` | **`4.487,73 ms`** (`4,49 s`) `[ĐO]` | `3.867,06 ms` `[ĐO]` | `612,80 ms` `[ĐO]` | `4 / 4` `[ĐO]` | `high` (`1,00`) `[ĐO]` |
| **`N = 8`** | `false` (JSON) | `200` `[ĐO]` | **`10.044,05 ms`** (`10,04 s`) `[ĐO]` | `8.841,11 ms` `[ĐO]` | `1.195,34 ms` `[ĐO]` | `8 / 8` `[ĐO]` | `high` (`1,00`) `[ĐO]` |
| **`N = 8`** | `true` (SSE) | `200` `[ĐO]` | **`9.872,43 ms`** (`9,87 s`) `[ĐO]` | `8.537,64 ms` `[ĐO]` | `1.327,37 ms` `[ĐO]` | `8 / 8` `[ĐO]` | `high` (`1,00`) `[ĐO]` |

> **Check Pass Pha 4 & Pha 5:** **ĐẠT** (xác thực & giới hạn đầu vào chặn đúng 100%, giả lập IDE chạy mượt ở cả `stream: false` và `stream: true` SSE với độ trễ đầu-cuối ~`4,5 s` ở $N=4$ và ~`9,9 s` ở $N=8$).

---

## 6. Đối chiếu với Pha E — Đàn Agent Đàn hồi trên NATS JetStream (`[TÀI LIỆU]`)

Pha E trong `KE_HOACH_BEST_OF_N_CODE.md` đã được triển khai trong gói `agents/swarm/` (`NATS JetStream` work-queue + `PostgreSQL` lưu trạng thái + `agent-worker` co giãn động) và báo cáo chi tiết tại `benchmarks/reports/P7E_DAN_AGENT_DAN_HOI.md`:
- **Tăng tốc theo số worker (`[TÀI LIỆU]` từ `P7E_DAN_AGENT_DAN_HOI.md`):** Khi chạy lô 8 bài toán $\times$ 4 ứng viên = 32 task `candidate_sample`, thời gian hoàn tất giảm từ **`15,43 s`** (1 worker, `2,07 mẫu/s`) xuống **`8,45 s`** (2 worker, `3,79 mẫu/s`), đạt hệ số tăng tốc **`1,83×`** ($\ge 1,6\times$ yêu cầu).
- **Chịu lỗi rút phích (`SIGKILL` giữa chừng, `[TÀI LIỆU]`):** Khi `kill -9` một worker đang giữ lease của các task, NATS JetStream tự động phát lại bản tin sau `AckWait = 3 s` sang worker còn sống; toàn bộ **`32/32`** ứng viên hoàn tất đầy đủ (`0` mẫu mất, `2` lần phát lại hợp lệ).

Sự kết hợp giữa **Pha A + Pha 2–5** (điều phối đồng bộ độ trễ thấp ~`4,5 s` qua `/api/compute-agents` cho IDE) và **Pha E** (hàng đợi bền vững NATS JetStream + PostgreSQL cho các lô đánh giá quy mô lớn chạy nền) tạo thành kiến trúc Best-of-$N$ hoàn chỉnh từ tương tác thời gian thực đến xử lý lô phân tán.

---

## 7. Kết luận và Khuyến nghị Vận hành

1. **Có đáng dùng `Best-of-N` trên phần cứng hiện tại (`pc-001` + `pc-win-01`) không?**
   - **CÓ, rất đáng giá `[ĐO]`.** Trên `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (chế độ C gộp 2 card), `Best-of-N` nâng tỷ lệ giải đúng toàn bộ 164 bài HumanEval từ **`89,63% – 90,85%`** (`pass@1` nền) lên **`92,68%`** ở $N=1$ (nhờ chuẩn hóa prompt ngắn gọn), **`95,73%`** ở $N=4$ (**`+4,88` đến `+6,10` điểm %**), và **`96,95%`** ở $N=8$ (**`+6,10` đến `+7,32` điểm %**, tức **giảm ~67%–70% số bài làm sai còn lại**, cứu được `12/17` bài khó nhất) `[ĐO]`.
   - Ngay cả khi **người dùng không cung cấp sẵn unit test** (Pha 3 — mô hình tự sinh $M=3$ bộ `assert` + chấm đồng thuận chéo CodeT), ở $N=8$ hệ thống vẫn cứu được **`10/17`** bài lỗi của Pha 0, với tỷ lệ chọn nhầm so với Oracle chỉ **`6,67%`** (`2/30` bài) và **không làm hỏng bất kỳ bài nào đã đúng ở $N=1$ (`0,00%`)** `[ĐO]`.

2. **Điểm bão hòa của $N$ và Cấu hình Khuyến nghị:**
   - **Cho IDE tương tác hàng ngày (Continue / Cline / VS Code):** Dùng **`N = 4`** với `model: "bon:Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"` trên chế độ C (`PARALLEL=1, CTX=32768`). Cấu hình này giữ độ trễ đầu-cuối chỉ **`4,49 s – 4,86 s`** `[ĐO]` nhưng đã thu được **83% tổng mức tăng chất lượng của $N=8$** (`157/164 = 95,73%` so với `159/164 = 96,95%`).
   - **Cho hàm thuật toán phức tạp hoặc tác vụ sửa lỗi chạy nền:** Dùng **`N = 8`** kèm `tests` (độ trễ **`~9,9 s`** `[ĐO]`) hoặc bật `generate_tests: true, m_test_suites: 3` khi chưa có test (độ trễ **`~17,6 s`** `[ĐO]`).
   - **Khi mở rộng thêm máy tính mới vào cụm:** Không cần cấu hình lại `bestofn`; chỉ cần đăng ký `worker-guard` vào `site-gateway`. Bộ lập lịch `/api/compute-agents` + `bestofn/sampler.py` sẽ tự động phát hiện compute agent mới và chia tải theo `slots * tok_s_ref` với overhead dưới **`3 ms`** ngay cả ở quy mô **50 compute agent** `[ĐO]`.

