# BÁO CÁO PHA E (P7E): NỀN "ĐÀN AGENT ĐÀN HỒI" TRÊN NATS JETSTREAM

- **Ngày thực hiện:** 2026-10-29
- **Máy đo:** `pc-001` (Linux `192.168.1.40`, Intel Core Ultra 7 265K 20 luồng, RAM 30 GB)
- **Tập lệnh đo:** `benchmarks/hardware/bus_elastic_scale_bench.py`
- **Bộ kiểm thử tích hợp & bảo mật:** `tests/integration/test_task_bus.py`, `tests/integration/test_elastic_agent_bus.py`, `tests/security/test_task_bus_security.py`
- **Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` theo tài liệu bên ngoài · `[SUY LUẬN]` tính toán/ước lượng · `[CHƯA XÁC MINH]` chưa đo thực nghiệm.

> **CẢNH BÁO QUAN TRỌNG VỀ PHẠM VI GIẢ LẬP:**
> Mọi số liệu thông lượng ở mục 3 và mục 4 với `K = 1, 10, 50, 200` được đo bằng **`BusWorker` giả lập chạy bằng coroutine (`asyncio`) với độ trễ mô phỏng `100–350 ms`/tác vụ** trên tiến trình `nats-server v2.10.29` cô lập (cổng riêng trên `127.0.0.1`, không chạm tới container `aicf-nats` cổng `54222` hay cụm 2 GPU đang phục vụ `Qwen3-Coder-30B-A3B`).
> Kết quả giả lập này **CHỈ chứng minh mặt phẳng điều phối/bus, sổ KV, bảo mật mTLS và cơ chế giao lại co giãn đúng đến K = 200**, hoàn toàn **KHÔNG thay thế cho số đo thông lượng sinh token trên GPU thật** (vốn bị giới hạn bởi 2 card RTX 3060 12 GB + RTX 5070 12 GB ở mức `126,7–132,4 tok/s` khi gộp hoặc `358,6 tok/s` với Qwen3-8B chế độ tách `[ĐO]`).

---

## 1. Xác minh hiện trạng mã nguồn trước khi thực thi (Phần 2C)

| Khẳng định trong kế hoạch | Kết quả xác minh ngày 2026-10-29 | Bằng chứng |
|---|---|---|
| `BusWorker`/`BusExecutor` trong `agents/task_bus.py` chưa được dùng ngoài test | **ĐÚNG** `[ĐO]` | `grep -rn "BusWorker\|BusExecutor\|task_bus" ai-compute-fabric/` chỉ khớp `agents/task_bus.py`, `tests/integration/test_task_bus.py`, `tests/integration/_bus_worker.py` và `README.md`. |
| NATS chưa có xác thực/TLS | **ĐÚNG** `[ĐO]` + `[ĐỌC MÃ]` | Container `aicf-nats` chạy `nats:2.10-alpine -js` trên `127.0.0.1:54222` không có cờ TLS hay xác thực (`docker inspect aicf-nats`). |
| `agents/tasks.py` có 15 `TaskType` và `Task` có `data_class` | **ĐÚNG** `[ĐỌC MÃ]` | `len(TaskType) == 15`, `Task.data_class: Literal["PUBLIC", "INTERNAL", "RESTRICTED"] = "INTERNAL"`. |
| Gateway quên nhóm gộp khi khởi động lại (Phần 0 mục 7 & Phần 3 mục 4) | **LỖI THỜI MỘT PHẦN → ĐÃ CẬP NHẬT TRÊN CỤM THẬT** `[ĐỌC MÃ]` + `[ĐO]` | Commit `5ed77e2` (`2026-10-28 16:06`) đã thêm `-topology-file` cho `site-gateway` và `--state-file` cho `worker-guard` để tự lưu/khôi phục `groups[]`. Đầu phiên binary trong `.demo/bin/` được dựng trước `5ed77e2`; sau khi chủ dự án cho phép cập nhật cụm dev (`2026-10-29 15:25`), đã biên dịch lại `.demo/bin/site-gateway`, `.demo/bin/worker-guard`, `.demo/bin/pki`, `.demo/bin/gcp-server` và khởi động lại `site-gateway` kèm `-topology-file .demo/topology.json` cùng `agents.dag_api` kèm `--nats-url nats://127.0.0.1:54222`. Xác minh `site-gateway` tự khôi phục nhóm gộp 2 GPU (`pc-001` + `pc-win-01`) về `status: "ready"` và mở trực tiếp `/api/agents/metrics` + `/api/agents/supervisor/recommendations` `[ĐO]`. |
| Danh tính mTLS worker và allowlist `/v1` trên gateway | **ĐÃ CÓ Ở GATEWAY** `[ĐỌC MÃ]` | Commit `f603ecf` (`2026-10-29 08:26`) đã thêm `verifyWorkerPeer` (`CN == WorkerId`) trong `apps/site-gateway/server.go` và allowlist `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` trong `proxy.go`. |

---

## 2. Số đo nền cho các quyết định kiến trúc E.0 (`D-FAB-1..5`)

Chi tiết quyết định được ghi trong `docs/adr/ADR-002-elastic-agent-fabric-bus.md`. Bảng dưới đây là số đo thực nghiệm trên `pc-001` (`50` mẫu lặp/phép đo):

| Phép đo (`127.0.0.1`, 50 lần lặp) | Trung vị (ms) | p95 (ms) | Ý nghĩa đối với `D-FAB-1..5` |
|---|---:|---:|---|
| HTTP POST trực tiếp vòng lặp nội bộ (`/v1/chat/completions` giả lập) | **0,44** `[ĐO]` | **1,15** `[ĐO]` | Làm đường cơ sở cho `D-FAB-1` và `D-FAB-2` (gọi thẳng gateway cho bước sinh mẫu độ trễ thấp). |
| Vòng tác vụ NATS JetStream không mã hóa (`BusExecutor` $\rightarrow$ `BusWorker` $\rightarrow$ `ack` & `reply`) | **1,15** `[ĐO]` | **2,61** `[ĐO]` | Phụ phí bus chỉ **+0,71 ms** so với HTTP trực tiếp; đổi lại có lưu bền, `ack_wait` và tự giao lại khi mất worker (`D-FAB-1`). |
| Vòng tác vụ NATS JetStream bật **mTLS (`verify_and_map: true`) + ACL** | **1,17** `[ĐO]` | **2,17** `[ĐO]` | Phụ phí mTLS sau khi bắt tay chỉ **+0,02 ms** (`D-FAB-5`). |
| Ghi nhịp tim vào JetStream KV `AICF_AGENTS` (`kv.put`) | **0,11** `[ĐO]` | — | Nhanh hơn **5,9 lần** so với PostgreSQL upsert, có sẵn TTL tự hết hạn (`D-FAB-3`). |
| Đọc danh sách agent sống từ JetStream KV (`list_live_agents`) | **0,80** `[ĐO]` | — | Phục vụ `/metrics` và `ElasticSupervisor` dưới 1 ms (`D-FAB-3`). |
| Ghi nhịp tim vào PostgreSQL `127.0.0.1:55432` (`INSERT ... ON CONFLICT DO UPDATE`) | **0,65** `[ĐO]` | — | Chậm hơn KV 5,9 lần và chiếm kết nối trong giới hạn `max_connections = 100` của `aicf-pg` (`D-FAB-3`). |
| Truy vấn danh sách agent sống trên PostgreSQL (`SELECT ... WHERE updated_at > ...`) | **0,15** `[ĐO]` | — | Nhanh khi ít dòng nhưng cần tiến trình dọn rác định kỳ (không có TTL tự nhiên như KV). |

---

## 3. Bảng đo quy mô E.4: `K = 1, 10, 50, 200` `BusWorker` giả lập

- **Thiết lập:** Mỗi tác vụ mô phỏng thời gian thực thi `sim_task_delay_s = 0,10 s` (`100 ms`). Các `BusWorker` chạy dưới dạng coroutine độc lập (`run_async`) kết nối thật qua TCP tới tiến trình `nats-server` riêng; mỗi worker đăng ký hồ sơ vào `AICF_AGENTS` KV và kéo từ consumer bền `workers-TEXT_GENERATE`.
- **Tải PostgreSQL (`pg_batch_log_ms`):** Thời gian thực tế ghi nhật ký `N` dòng thực thi vào bảng `aicf_exec` trên `aicf-pg` (`127.0.0.1:55432`) và dọn sạch sau lượt đo.

| Số agent (`K`) | Tổng số việc (`N`) | Thời gian hoàn tất (s) | Thông lượng (việc/s) | Tăng tốc so với `K=1` | Chờ hàng đợi trung vị (ms) | Chờ hàng đợi p95 (ms) | Số việc chạy trùng | RAM `nats-server` (MiB) | CPU `nats-server` (%) | RAM tiến trình Python (MiB) | Ghi nhật ký PostgreSQL (ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** | 20 | 2,046 `[ĐO]` | **9,78** `[ĐO]` | 1,00× `[ĐO]` | 973,56 `[ĐO]` | 1.937,39 `[ĐO]` | **0** `[ĐO]` | 16,41 `[ĐO]` | 1,5% `[ĐO]` | 407,1 `[ĐO]` | 13,11 `[ĐO]` |
| **10** | 40 | 0,422 `[ĐO]` | **94,89** `[ĐO]` | 9,70× `[ĐO]` | 158,63 `[ĐO]` | 318,06 `[ĐO]` | **0** `[ĐO]` | 18,52 `[ĐO]` | ~0,0% `[ĐO]` | 407,1 `[ĐO]` | 21,01 `[ĐO]` |
| **50** | 200 | 0,494 `[ĐO]` | **404,53** `[ĐO]` | 41,36× `[ĐO]` | 193,10 `[ĐO]` | 366,23 `[ĐO]` | **0** `[ĐO]` | 23,31 `[ĐO]` | 12,1% `[ĐO]` | 407,1 `[ĐO]` | 26,52 `[ĐO]` |
| **200** | 400 | 0,330 `[ĐO]` | **1.211,44** `[ĐO]` | 123,87× `[ĐO]` | 73,87 `[ĐO]` | 176,54 `[ĐO]` | **0** `[ĐO]` | 35,58 `[ĐO]` | 36,3% `[ĐO]` | 407,1 `[ĐO]` | 39,07 `[ĐO]` |

**Nhận xét số đo `K = 1..200`:**
- Với `K = 200` agent giả lập chạy đồng thời, thông lượng bus đạt **1.211,44 việc/s** (tăng tốc **123,87 lần** so với `K = 1`), không có việc nào chạy trùng (**0**), bộ nhớ RSS của `nats-server` chỉ tăng từ **16,41 MiB** lên **35,58 MiB** (**+19,17 MiB** cho 200 kết nối + JetStream KV), và tiến trình Python giữ nguyên ở mức **407,1 MiB** (an toàn tuyệt đối cho giới hạn RAM 30 GB của `pc-001`).

---

## 4. Co giãn động giữa lúc chạy (`5 -> 50 -> 8`) và Kill đột ngột 20% Agent

Thực hiện liên tục trên cùng một `BusExecutor` và cùng một tiến trình `nats-server`, **không sửa cấu hình trung tâm và không khởi động lại tiến trình điều phối**:

| Kịch bản / Giai đoạn | Cấu hình biến đổi | Số việc gửi | Thông lượng (việc/s) | Số agent sống trong KV `AICF_AGENTS` | Số việc hoàn tất | Số việc chạy trùng | Số việc bị mất | Thời gian hồi phục / giao lại (s) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **Giai đoạn 1:** Khởi đầu | `K = 5` (`sim_delay = 120 ms`) | 25 | **39,90** `[ĐO]` | 5 `[ĐO]` | 25/25 `[ĐO]` | **0** `[ĐO]` | **0** `[ĐO]` | — |
| **Giai đoạn 2:** Thêm nóng 45 agent giữa lúc đang chạy | `5 -> 50` (bật thêm 45 `BusWorker` khi 100 việc đang bay) | 100 | **279,62** `[ĐO]` (**+7,01×**) | 50 `[ĐO]` | 100/100 `[ĐO]` | **0** `[ĐO]` | **0** `[ĐO]` | 0,000 `[ĐO]` |
| **Giai đoạn 3:** Rút lui êm (`drain`) 42 agent giữa lúc đang chạy | `50 -> 8` (gửi lệnh `request_drain()` cho 42 agent khi 60 việc đang bay) | 60 | **146,36** `[ĐO]` | 8 `[ĐO]` | 60/60 `[ĐO]` | **0** `[ĐO]` | **0** `[ĐO]` | 0,000 `[ĐO]` |
| **Giai đoạn 4:** Kill đột ngột **20% agent** (`10/50`) giữa lúc đang giữ việc | `K = 50`, `ack_wait = 1,2 s`, `sim_delay = 350 ms`; hủy đột ngột 10 agent khi đang chạy dở việc | 50 | **26,34** `[ĐO]` | 40 (sau TTL) `[ĐO]` | **50/50** `[ĐO]` | **10 lần chạy lại** (`0` kết quả trùng) `[ĐO]` | **0** `[ĐO]` | **1,817 s** `[ĐO]` (sau thời điểm kill) |

**Phân tích Check Pass (a) và (b):**
- **Check Pass (a) ĐẠT:** Khi thêm nóng từ `5` lên `50` agent giữa lúc chạy, thông lượng tăng ngay lập tức từ **39,90 việc/s** lên **279,62 việc/s** (**7,01 lần**) mà không sửa cấu hình hay khởi động lại `BusExecutor` hoặc các agent đang chạy.
- **Check Pass (b) ĐẠT:**
  - Khi rút lui êm (`drain` `50 -> 8`), cả 42 agent nhận lệnh dừng kéo việc mới, hoàn tất trọn vẹn việc đang giữ, `ack`, xóa khóa khỏi `AICF_AGENTS` KV (số agent sống giảm chính xác từ `50` về `8`): tổng **185/185** việc qua 3 giai đoạn hoàn tất với **0 việc mất, 0 việc chạy trùng**.
  - Khi kill đột ngột 20% (`10/50`) agent đang chạy dở việc (không `ack`), đúng **10 việc** đang nằm trên 10 agent bị chết được NATS JetStream giao lại tự động sau `ack_wait = 1,2 s` cho 40 agent còn sống; tổng thời gian từ lúc kill đến lúc hoàn tất toàn bộ 50 việc là **1,817 s** (`~1,2 s ack_wait + 0,35 s chạy lại + phụ phí bus`), **0 việc bị mất**, và người gọi chỉ nhận đúng 1 kết quả hợp lệ cho mỗi `task_id` (`duplicate_completions = 0`).

---

## 5. Bằng chứng Kiểm Đột Biến (Mutation Testing) cho Check Pass (c), (d), (e) và E.3

Mọi cơ chế an toàn mới đều đã được kiểm đột biến thực tế (phá thử cơ chế trong `agents/task_bus.py` $\rightarrow$ chạy `pytest` thấy **ĐỎ** $\rightarrow$ khôi phục mã $\rightarrow$ chạy `pytest` thấy **XANH**):

| Cơ chế kiểm đột biến | Đột biến đã tiêm vào `agents/task_bus.py` | Lệnh kiểm thử | Kết quả khi đột biến | Kết quả sau khôi phục |
|---|---|---|---|---|
| **Check Pass (c):** Từ chối agent không có chứng chỉ hợp lệ / sai CA / hết hạn / giả mạo CN | Cho `BusTLSConfig.verify_client_certificate()` trả về `return` sớm (bỏ kiểm tra chứng chỉ & CN) | `.venv/bin/pytest -v tests/security/test_task_bus_security.py -k test_mtls_rejects` | **ĐỎ (`FAILED`, exit code 1)** `[ĐO]` | **XANH (`1 passed in 0.29s`)** `[ĐO]` |
| **Check Pass (d):** Chặn agent kéo `TaskType` hoặc `data_class` (`RESTRICTED`) không được cấp quyền | Cho `BusWorker._validate_worker_capability()` trả về `return` sớm (bỏ kiểm tra `allowed_types` & `allowed_data_classes`) | `.venv/bin/pytest -v tests/security/test_task_bus_security.py -k test_authorization_blocks` | **ĐỎ (`FAILED: DID NOT RAISE BusPermissionError`, exit code 1)** `[ĐO]` | **XANH (`1 passed in 1.04s`)** `[ĐO]` |
| **Check Pass (e):** Chịu tải ngược khi hàng đợi quá sâu (`max_queue_depth`), hạn mức tenant, kích thước gói tin | Cho `BusExecutor._check_and_acquire_admission()` trả về `return` sớm (bỏ kiểm tra `max_queue_depth`, `tenant_quotas`, `max_payload_bytes`) | `.venv/bin/pytest -v tests/security/test_task_bus_security.py -k test_payload_limit_ttl_tenant_quota_and_queue_depth_backpressure` | **ĐỎ (`FAILED`, exit code 1)** `[ĐO]` | **XANH (`1 passed`)** `[ĐO]` |
| **E.3 / D-FAB-4:** Khóa cứng chế độ `propose_only=True` của `ElasticSupervisor` | Đổi `if not propose_only:` thành `if False and not propose_only:` trong `ElasticSupervisor.__init__` | `.venv/bin/pytest -v tests/integration/test_elastic_agent_bus.py -k test_prometheus_metrics_and_propose_only_supervisor` | **ĐỎ (`FAILED: DID NOT RAISE PermissionError`, exit code 1)** `[ĐO]` | **XANH (`1 passed`)** `[ĐO]` |

---

## 6. Điểm bắt đầu hỏng khi `K` lớn (Check Pass f) và Giới hạn thực tế

Khi đẩy quy mô `K` vượt qua các ngưỡng thiết kế mặc định, hệ thống bắt đầu chạm các điểm nghẽn/hỏng theo đúng thứ tự sau:

1. **Điểm gãy 1 — Hồ luồng mặc định của Python (`K = 24` trên `pc-001`) nếu dùng Executor đồng bộ:**
   - `[ĐỌC MÃ]` + `[SUY LUẬN]` `asyncio.to_thread` dùng `ThreadPoolExecutor` mặc định của Python với tối đa `min(32, os.cpu_count() + 4) = 24` luồng trên CPU 20 luồng của `pc-001`. Nếu `K > 24` worker chạy chung một vòng lặp sự kiện mà gọi hàm đồng bộ `executor.execute()`, thông lượng bị trần cứng ở 24 việc song song.
   - **Cách đã xử lý trong Pha E:** `BusWorker._handle()` ưu tiên gọi `await executor.execute_async()` nếu có, giúp `K = 200` chạy hoàn toàn bằng coroutine không nghẽn luồng.
2. **Điểm gãy 2 — Giới hạn kết nối PostgreSQL (`max_concurrency = 84` trên `Orchestrator`):**
   - `[ĐO]` Container `aicf-pg` có `max_connections = 100`, `superuser_reserved_connections = 3`, và luôn có `9` kết nối đang mở bởi `studio` + `dag_api`, chỉ còn **88 kết nối trống**.
   - `[ĐỌC MÃ]` `Orchestrator` (`agents/orchestrator.py` dòng 72) mở `ConnectionPool(..., max_size=max(8, max_concurrency + 4))`. Do đó khi `max_concurrency > 84` (`max_size > 88`), PostgreSQL từ chối kết nối mới với lỗi `FATAL: sorry, too many clients already`.
   - Ngoài ra, ở `K = 200`, thời gian ghi nhật ký đồng bộ vào PostgreSQL tăng lên **39,07 ms/lượt** `[ĐO]` (so với **13,11 ms** ở `K = 1`).
3. **Điểm gãy 3 — Giới hạn `ConsumerConfig.max_waiting = 512` của NATS JetStream (`K >= 513`):**
   - `[ĐO]` Trên `nats-server v2.10.29`, mỗi pull consumer bền (`workers-<type>`) có cấu hình mặc định `max_waiting = 512` (số yêu cầu `sub.fetch()` chờ đồng thời tối đa) và `max_ack_pending = 1000`.
   - `[ĐO]` Khi thử nghiệm đẩy `520` yêu cầu `fetch` đồng thời vào cùng một consumer `w-break`, bắt đầu từ **`K = 513`** (`max_waiting + 1`), các yêu cầu vượt ngưỡng 512 bị NATS từ chối/rơi vào timeout. Để vượt `K > 500` trên một loại tác vụ, phải tăng `max_waiting` trong `ConsumerConfig` hoặc chia nhỏ theo nhãn `aicf.task.<type>.<label>`.
4. **Điểm gãy 4 — Độ trễ `nc.drain()` của `nats-py` trên kết nối TLS (`30 giây`/kết nối):**
   - `[ĐO]` Trên Python 3.12 + `nats-py v2.16.0`, gọi `await nc.drain()` trên kết nối `tls://` gặp cảnh báo `returning true from eof_received() has no effect when using ssl` của `asyncio.sslproto`, làm mỗi lệnh đóng kết nối bị treo đúng `DEFAULT_DRAIN_TIMEOUT = 30s`.
   - **Cách đã xử lý trong Pha E:** Sau khi dừng vòng kéo và hoàn tất các tác vụ đang chạy, `BusWorker` và `BusExecutor` gọi `await nc.flush(timeout=2.0)` rồi `await nc.close()`, giảm thời gian đóng kết nối TLS từ **30.000 ms** xuống **< 5 ms** `[ĐO]`.
5. **Điểm gãy 5 — Giới hạn phần cứng thật trên cụm 2 máy hiện tại (Những gì giả lập KHÔNG đạt/không thay thế được):**
   - **Hộp cát `DockerSandbox` trên `pc-001`:** RAM Linux 30 GB đang gánh `llama-server` (`Qwen3-Coder-30B-A3B` gộp), `Qwen3-Embedding-0.6B`, `bge-reranker-v2-m3`, `studio`, `aicf-pg` và `aicf-nats`. Mỗi container `DockerSandbox` giới hạn `128 MB` (`SandboxLimits`), nên chỉ chạy an toàn **4–8 container song song** `[SUY LUẬN]` — không thể chạy 50–200 container Docker cùng lúc trên 1 máy 30 GB RAM.
   - **Thông lượng GPU thật:** Hệ thống chỉ có **2 GPU** (`RTX 3060 12 GB` + `RTX 5070 12 GB`), đang chạy `Qwen3-Coder-30B-A3B` gộp với `PARALLEL=1` (`126,7–132,4 tok/s` `[ĐO]`). Dù có bật 200 `BusWorker` điều phối thì nút cổ chai sinh code thực tế vẫn nằm ở số slot GPU của cụm.

