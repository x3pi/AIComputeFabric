# KẾ HOẠCH v2: NỀN TẢNG SERVERLESS GPU / PYTHON (MÔ HÌNH MODAL.COM) TRÊN AI COMPUTE FABRIC
*(Tài liệu bàn giao cho Agent thực thi. v1 soạn 2026-11-05; **v2 rà soát và hoàn thiện cùng ngày** sau khi đối chiếu từng khẳng định với mã nguồn thật trong `ai-compute-fabric/` — danh sách lỗi v1 đã sửa ở **Phụ lục A**. Dựa trên `ADR-001`, `ADR-002`, `DANH_GIA_TINH_KHA_THI_PHAN_CUNG_VA_MANG.md`, `KE_HOACH_CHIA_SE_MAY_LAN_INTERNET.md`. Trạng thái: **KẾ HOẠCH — chưa sửa dịch vụ đang chạy**.)*

**Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` theo tài liệu ngoài, chưa kiểm trên máy này · `[SUY LUẬN]` tính toán/ước lượng từ kiến trúc · `[CHƯA XÁC MINH]` bắt buộc đo/kiểm ở Pha 0 trước khi chốt. Mọi con số không có nhãn `[ĐO]` đều là **chưa đo**.

**Tiêu chí "chuẩn production" của kế hoạch này** (mọi pha phải phục vụ 5 tiêu chí; mỗi tiêu chí có cổng nghiệm thu đo được):
1. **An toàn:** mã người dùng không bao giờ chạy ngoài sandbox; không rò dữ liệu giữa tenant; không làm hại LLM đang phục vụ và chủ máy.
2. **Hiệu suất:** có SLO số cụ thể (mục 2.6), đường nóng (hot path) không có RPC thừa.
3. **Dễ dùng:** SDK kiểu Modal, lỗi dễ hiểu, `aicf run/ps/stop/logs` đủ dùng, không cần biết NATS/Docker.
4. **Vận hành được:** metrics, log theo `call_id`, kill switch, runbook, rollout/rollback, soak test.
5. **Tăng dần từng lớp:** có lát cắt chạy được end-to-end sớm (Pha 2, CPU), GPU chỉ bật sau khi cổng an toàn đạt (Pha 3).

---

## TRẠNG THÁI TRIỂN KHAI (cập nhật liên tục)

| Pha | Trạng thái | Bằng chứng |
|---|---|---|
| 0 | **XONG (Linux)**; Windows hoãn (D-WIN) | `benchmarks/reports/p_serverless_0_probe.json`, `benchmarks/serverless/p0_probe.py`, `docs/adr/ADR-003-serverless-gpu.md` |
| 1 | **XONG** — 79 test xanh (50 unit lõi + 13 runner + 16 Docker thật), 20/20 đột biến bị giết | `agents/env_cache.py`, `agents/_warm_runner.py`, `agents/sandbox.py` (`GpuDockerSandbox`, `WarmDockerPool`), `tests/serverless/`, `tests/mutation/`, `benchmarks/reports/p_serverless_1_{core,mutations}.json` |
| 2 | **XONG (lát cắt CPU)** — 215 test serverless xanh (có e2e thật: gateway Go → service → NATS → worker → container), 31/31 đột biến bị giết; SLO warm p50 3,9 ms / p95 6,4 ms (mục tiêu 20/60) `[ĐO]` | `serverless/`, `agents/serverless_worker.py`, `agents/sl_security.py`, `agents/sl_store.py`, `apps/site-gateway/serverless.go`, `sdk/python/aicf/`, `tests/mutation/serverless_p2.py`, `benchmarks/reports/p_serverless_2_{slo,mutations}.json` |
| 3–8 | Chưa bắt đầu (GPU vẫn tắt sau cờ `--gpu-enabled` + `gpu_allowed` của tenant) | — |

**Kết quả Pha 0 làm thay đổi kế hoạch:** (1) `--gpus` hỏng trên host, dùng `--runtime=nvidia`; (2) Object Store chỉ ≤ 8 MiB (get 512 MiB mất 389 s); (3) env phải dưới cùng mount cha + hardlink để tiết kiệm đĩa (host còn ~7 GB); (4) Lease truy cập qua cầu nối HTTP trong gateway, không dùng grpcio; (5) `OSUser` đã chứa VRAM Serverless ⇒ cần bucket riêng ở worker-guard; (6) một wheel torch cu130 phủ sm_86 và sm_120.

---

## PHẦN 0: BRIEF BÀN GIAO & QUY TẮC BẮT BUỘC (ĐỌC TRƯỚC KHI CHẠM VÀO CODE)

### 0.1. Mục tiêu và ngoài phạm vi
**Mục tiêu:** lớp **Serverless GPU / Python Compute** trên `ai-compute-fabric`: người dùng viết Python với `@app.function`, `@app.cls`, `@app.web_endpoint`, khai báo phần cứng (`gpu`, `vram_mb`, `memory_mb`, `keep_warm_s`) và môi trường (`aicf.Image`), rồi gọi `.remote()` / `.map()` / `.spawn()`:
1. **Đặt chỗ VRAM theo Lease ngắn hạn + Scale-to-Zero** khi hết `keep_warm_s`.
2. **Warm Sandbox Pool**: lượt gọi 2.. không tốn nạp lại thư viện/model.
3. **Cô lập fail-closed**: mã và dữ liệu `cloudpickle` là **không tin cậy**, chỉ giải nén trong container cô lập.
4. **Thứ tự ưu tiên tài nguyên cố định: chủ máy > LLM (`/v1/chat/completions`) > Serverless.** Serverless luôn là bên bị evict đầu tiên, `G-RECLAIM < 2 s`.

**Ngoài phạm vi v2 (ghi rõ để không phình scope):** cron/schedule, GPU đa-node cho một hàm (`gpu="2xA100"`), build image từ Dockerfile tùy ý, mạng ra Internet từ sandbox, thanh toán tiền thật, snapshot bộ nhớ GPU. Mọi mục này chỉ làm khi có yêu cầu mới.

### 0.2. Hệ thống hiện có — sự thật đã kiểm (tái sử dụng, KHÔNG viết lại)
- **Phần cứng `[ĐO]`:** `pc-001` Linux (`192.168.1.40`, cáp trực tiếp `10.42.0.1/24`), Core Ultra 7 265K, RAM 30 GB, RTX 3060 12 GB (Ampere, sm_86). `pc-win-01` Windows 10 Pro (`10.42.0.225`, RTT `0,15 ms`, `2,376 Gbps`), Ryzen 7 9700X, RAM 64 GB, RTX 5070 12 GB (Blackwell, sm_120 `[TÀI LIỆU]`), có WSL2.
- **Docker trên `pc-001` `[ĐO]` (2026-11-05):** runtime `nvidia` đã đăng ký cùng `runc`; Cgroup v2, driver cgroup `systemd`; `uv` có tại `~/.local/bin/uv`; Python hệ thống 3.12.3.
- **`apps/site-gateway` (Go) `[ĐỌC MÃ]`:**
  - **Không có client NATS** (`grep nats` trong `go.mod` của `site-gateway` và `worker-guard` = rỗng). Mô hình đã dùng cho dịch vụ Python: gateway xác thực rồi **reverse-proxy** `/api/agents/`, `/api/studio/`, `/api/bestofn/` sang dịch vụ Python (`BestOfNProxy` trong `compute_agents.go`), xóa `Authorization`, chuyển tiếp `X-Aicf-Tenant`/`X-Aicf-User`/`X-Aicf-Role`.
  - `Auth`/`Principal` chỉ có 3 vai trò `viewer|operator|admin` (`rbac.go`); `Allowed()` chỉ cho `operator` POST/PUT vào `/api/studio/*` và pause/resume worker. **Không có khái niệm "scope"**. `Tenant` (`tenants.go`) chỉ có `ID, Name, KeyHash, Weight, MaxInflight, Disabled`.
  - `/v1/` đã gắn cho cổng suy luận (`mux.Handle("/v1/", inf)`, `main.go`) ⇒ **không được đặt route Serverless dưới `/v1/`**.
  - `LeaseManager` (`leases.go`) chỉ được phục vụ qua **gRPC** (`pb.RegisterLeaseServiceServer`, `main.go`), **không có route HTTP**. `RequestLease` chọn worker trong `Registry` có `State=="running" && Online`, so `Buckets.Allocatable` trừ tổng reservation; `RequestLeaseRequest` **không có `tenant`, nhãn phần cứng, loại workload**; `MaxTTL` mặc định 10 phút; `sweepLocked()` chỉ chạy lười khi có `Request/Extend/Active`.
- **Mặt phẳng tác vụ (`agents/task_bus.py`) `[ĐỌC MÃ]`:** NATS JetStream `WORK_QUEUE`; chủ đề `task_subject(type, label, data_class)`; client dùng **inbox reply** (`reply: inbox`, ngoại lệ `WorkerLost`, ghi `wait_s`); worker có `ack_wait=10s`, `max_deliver=5`, **`max_payload_bytes=256 KiB`**, và tự `m.in_progress()` mỗi `ack_wait/3`. Latency 1 vòng `1,17 ms` p50 / `2,17 ms` p95 `[ĐO: ADR-002]`. `TaskType` hiện 15 giá trị, chưa có Serverless.
- **`agents/sandbox.py` `[ĐỌC MÃ]`:** `DockerSandbox` mặc định **`python:3.12-alpine` (musl)**, `--tmpfs /tmp:size=8m,noexec`, `--cpus 1`, chạy **one-shot** (`docker run --rm -i` mỗi lần), `limits.timeout_s=10`, `memory_mb=128`; `_nnp_works()` bỏ `no-new-privileges` nếu AppArmor làm hỏng. Không hỗ trợ GPU, không giữ ấm.
- **`apps/worker-guard` (Go) `[ĐỌC MÃ]`:** chỉ quản **một engine** (`llama-server`) với `Grace` SIGTERM→SIGKILL; chính sách `HeadroomMiB` (dưới ngưỡng ⇒ dừng engine); `ProcSampler` đọc `nvidia-smi --query-compute-apps=pid,used_memory` (~30 ms/lần). **Chưa có** đường nào để evict một tiến trình/container ngoài engine, cũng không phân biệt VRAM của Serverless.
- **Metering (`apps/global-control-plane/metering.go`) `[ĐỌC MÃ]`:** `Usage{Site,Tenant,Job,Seq,PromptTokens,CompletionTokens,GPUMs,VRAMMiBSec,AudioMs,MicroKWh}` với khóa idempotent `(Site,Seq)`, sổ cái băm chuỗi, `VerifyLedgerFile`. **Đã có `GPUMs` và `VRAMMiBSec` — không cần sổ cái mới.**
- **Kiểm thử/gate sẵn có `[ĐỌC MÃ]`:** `make verify-release-v1`; `tests/integration/test_openapi_conformance.py`, `tests/unit/test_release_readiness.py` (đối chiếu OpenAPI); `tests/security`, `tests/resilience`, `tests/performance`, `tests/e2e/*.mjs` (Playwright). Cổng hiện dùng: 18080 (gateway), 18090/18091, 18099 (bestofn), 18199.

### 0.3. Quy tắc bắt buộc của chủ dự án (Agent thực thi phải tuân thủ 100%)
1. **Ngôn ngữ & nhãn:** báo cáo tiếng Việt; mọi con số có nhãn bằng chứng.
2. **Không làm gián đoạn dịch vụ đang chạy:** không tự tắt/khởi động lại `site-gateway`, `worker-guard`, `llama-server`, `aicf-nats`, `aicf-pg` khi chưa hỏi. Mọi thử nghiệm tích hợp chạy trên **tiến trình/cổng/stream NATS/tên container có tiền tố `aicf-sl-test-` cô lập**.
3. **Kiểm đột biến bắt buộc** cho mọi cơ chế an toàn/phân quyền/giới hạn VRAM/sandbox/thu hồi lease/chữ ký: phá điều kiện → test ĐỎ → khôi phục → test XANH. Ghi log đột biến (file, dòng, kết quả) vào báo cáo pha.
4. **Quản lý tiến trình:** chỉ `pkill -x <tên>` hoặc `kill <pid>`; cấm `pkill -f`.
5. **Windows (`pc-win-01`):** chỉ đọc/ghi `C:\aicf\`; không đụng `C:\Users\Fios02\`; không chạy mã không tin cậy trực tiếp trên host Windows; giữ `NullSandbox` fail-closed cho tới khi Pha 0 chứng minh WSL2 đạt chuẩn (Pha 8, tùy chọn).
6. **Không `git push`;** chỉ commit khi chủ dự án yêu cầu.
7. **Cổng nghiệm thu cứng:** pha chưa đạt đủ `Check Pass` thì dừng và báo cáo trung thực; không hạ tiêu chí.
8. **(MỚI) Công tắc an toàn mặc định TẮT:** `--serverless-enabled=false`, `--serverless-gpu-enabled=false`. Cờ GPU chỉ được bật sau khi **Pha 3 xanh hoàn toàn**. Bật/tắt không cần build lại.
9. **(MỚI) Không thay đổi hành vi LLM hiện tại khi cờ tắt:** mọi sửa `worker-guard`/`leases.go`/proto phải giữ nguyên golden test cũ (`contracts/gen/go` `go test`), chỉ `-update` sau khi thay đổi schema có chủ đích và đã review.
10. **(MỚI) Một bằng chứng cho mỗi phát biểu hiệu suất:** không ghi "nhanh hơn" nếu chưa có số đo cùng điều kiện (cùng máy, cùng image, ≥ 30 lần, báo p50/p95); nêu rõ yếu tố nhiễu (cache đĩa, NVMe, GPU clocks).

---

## PHẦN 1: QUYẾT ĐỊNH KIẾN TRÚC & LỰA CHỌN STACK

### 1.1. Giải pháp bên ngoài
| Giải pháp | Đánh giá | Quyết định |
|---|---|---|
| **Knative / OpenFaaS / K8s** | Cần cụm K8s thuần Linux; không hợp dịch vụ Windows SCM `AICFWorkerGuard` (WDDM) và cơ chế trả GPU `< 2 s` (`ADR-001`). Image AI 10–15 GB cold-start nhiều phút nếu thiếu registry. | **Không dùng.** |
| **Beam `beta9`** | SDK Python (`@function`, `@endpoint`, `Image`) sát Modal `[TÀI LIỆU]`, nhưng control plane gắn chặt K8s + etcd + JuiceFS, trùng `site-gateway` + NATS. | **Học giao diện SDK**, không chạy control plane của nó. |
| **`cloudpickle`** (BSD-3) | Chuẩn công nghiệp serialize hàm/closure. Yêu cầu **cùng phiên bản Python minor** và phiên bản `cloudpickle` tương thích ở hai đầu `[TÀI LIỆU]`. | **Dùng**, `loads()` chỉ trong sandbox; SDK kiểm tra khớp phiên bản (mục 2.7). |
| **`uv`** | Có sẵn `[ĐO]`. Cache wheel + hardlink nhanh `[TÀI LIỆU]`. | **Dùng** để dựng venv — **trong container builder, không trên host** (D4). |
| **Docker + NVIDIA runtime** | Runtime `nvidia` đã có `[ĐO]`. | **Dùng** làm runtime mặc định trên Linux. |
| **gVisor / Kata / Firecracker** | Giảm bề mặt tấn công kernel/driver nhưng GPU passthrough khó, tốn công. | **Chưa dùng**; ghi rủi ro R3 và hạn chế `data_class` (D6). |
| **NATS JetStream Object Store** | Có sẵn `[ĐO: ADR-002]`. Chia chunk nhỏ, **hiệu năng với tệp nhiều GB chưa đo**. | Dùng cho **bundle hàm, artifact nhỏ**; **không** dùng cho trọng số mô hình (D7). |

### 1.2. Các quyết định thiết kế đã chốt (thay thế các giả định ngầm của v1)
- **D1 — Thêm dịch vụ Python `serverless/` (cổng đề xuất `18110`, Pha 0 xác minh rảnh), KHÔNG để Go publish NATS.** Gateway Go chỉ làm xác thực, tenant, RBAC, rate-limit, audit và reverse-proxy `/api/serverless/` — đúng mẫu `bestofn`. Dịch vụ Python dùng `BusClient` của `task_bus.py` (inbox reply, `WorkerLost`) nên không thêm nats.go vào Go. *Lý do: tránh trùng cơ chế bus và thêm dependency lớn vào binary đang chạy.*
- **D2 — Dịch vụ `serverless/` chỉ tin header `X-Aicf-Tenant` khi tới từ gateway:** lắng nghe `127.0.0.1` (hoặc mTLS) **và** kiểm tra bí mật chia sẻ `X-Aicf-Proxy-Secret` do gateway chèn. Gọi thẳng với header giả ⇒ 401 (có mutation test).
- **D3 — Lease theo *container*, không theo *lượt gọi*.** Cold path: xin 1 lease cho container; warm path: **không gọi LeaseService** (không RPC trên đường nóng). Lease TTL **ngắn (30 s)** + gia hạn nhịp 10 s do dịch vụ `serverless/` thực hiện **chỉ khi** worker còn nhịp tim trong `AICF_AGENTS`. `keep_warm_s` là chính sách cục bộ của worker, **không** bằng TTL lease (sửa lỗi v1: crash worker sẽ giữ VRAM tới 10 phút).
- **D4 — Dựng môi trường trong container builder cùng base image với runtime, không bao giờ trên host.** `uv pip install` các sdist chạy `setup.py` = thực thi mã tùy ý trên host nếu không cô lập. Quy tắc: `--only-binary=:all:` (cấm sdist), lock có băm (`uv pip compile --generate-hashes`), chỉ index nội bộ/mirror allowlist, builder không mount thư mục nhạy cảm, quota CPU/RAM/thời gian/dung lượng, chạy bất đồng bộ (không chặn worker). Venv được tạo bằng **đúng interpreter của image runtime** (tránh lệch ABI glibc/musl).
- **D5 — Base image runtime là glibc (không phải alpine):** `python:3.12-slim` + wheel PyTorch đã gói CUDA, hoặc `nvidia/cuda:<tag>-runtime`. Chọn ở Pha 0 theo kích thước và thời gian pull. `env_hash = SHA-256(base_image_digest, python_version, cuda_tag, lock_with_hashes, local_source_hash)`.
- **D6 — Chính sách `data_class` trên GPU dùng chung:** `PUBLIC` và `INTERNAL` được phép; **`RESTRICTED` bị từ chối** cho tới khi có đánh giá riêng (GPU passthrough dùng chung driver kernel NVIDIA — R3).
- **D7 — `aicf.Volume` có hai loại:** (a) `Volume` nhỏ/trung bình trên Object Store (≤ giới hạn đo ở Pha 0, mặc định 256 MiB/tệp), vật chất hóa ra thư mục cục bộ của worker, **commit khi hàm kết thúc**, last-writer-wins có phiên bản; (b) **`ModelCache` chỉ đọc** do quản trị viên nạp sẵn trên NVMe từng worker (tải HF/GGUF ngoài sandbox), mount `:ro` vào container. Trọng số nhiều GB không đi qua Object Store.
- **D8 — Ngữ nghĩa thực thi mặc định *at-most-once sau khi bắt đầu chạy mã người dùng*;** retry tự động chỉ khi hàm khai báo `retries=n` (hàm phải idempotent). Mất worker giữa chừng ⇒ trả `WorkerLost` rõ ràng (xem 2.4).
- **D9 — Hai chế độ gọi:** `invoke` đồng bộ (tối đa 60 s, vì qua proxy/trình duyệt) và `spawn` bất đồng bộ (`call_id`, `GET /calls/{id}`, `DELETE` hủy, luồng log SSE) cho `timeout_s` tới 600 s và batch lớn.
- **D10 — Ưu tiên tài nguyên:** `worker-guard` là nơi duy nhất quyết định evict. Khi vi phạm headroom, thứ tự: **evict Serverless trước → lấy mẫu lại → chỉ khi vẫn thiếu mới dừng engine LLM.** Đây là thay đổi hành vi có kiểm soát trong `apps/worker-guard` (v1 bỏ sót hoàn toàn thành phần này).

---

## PHẦN 2: KIẾN TRÚC ĐÍCH & HỢP ĐỒNG

### 2.1. Luồng end-to-end (đã sửa theo D1–D3)

```mermaid
sequenceDiagram
    autonumber
    participant Dev as aicf SDK
    participant GW as site-gateway (Go) :18080
    participant SVC as serverless service (Python) :18110
    participant Lease as LeaseService (gRPC, leases.go)
    participant NATS as NATS JetStream
    participant W as ServerlessWorker (Python, cùng host với worker-guard)
    participant Box as GpuDockerSandbox (warm container)
    participant WG as worker-guard (Go)

    Dev->>GW: POST /api/serverless/v1/invoke|spawn (Bearer tenant key)
    GW->>GW: Auth, tenant enabled?, rate-limit, MaxInflight, Audit, xóa Authorization
    GW->>SVC: proxy + X-Aicf-Tenant + X-Aicf-Proxy-Secret
    SVC->>SVC: Kiểm quota tenant, data_class, ký (HMAC) bundle_ref+args
    alt WARM HIT (tenant, env_hash, fn_id đang ấm trên worker X)
        SVC->>NATS: submit pinned label=w-X (KHÔNG gọi Lease)
    else COLD
        SVC->>Lease: RequestLease(tenant, kind=serverless, labels, vram, ttl=30s)
        Lease-->>SVC: LeaseToken(worker_id, lease_id) | no_capacity → xếp hàng WFQ ≤ queue_timeout_s, quá hạn → 429
        SVC->>NATS: submit pinned label=w-<worker_id>, kèm lease_id
    end
    NATS->>W: pull (mTLS) → xác minh chữ ký, nonce, hạn
    W->>Box: warm: ghi frame vào stdin | cold: docker run (--gpus, --network none, venv :ro)
    Box-->>W: result frame (JSON/msgpack) + stdout/stderr (giới hạn) + vram_peak
    W-->>SVC: reply qua inbox (cold_start, exec_ms, queue_ms, worker_id)
    SVC->>SVC: ghi Usage (GPUMs, VRAMMiBSec) + metrics + gia hạn lease khi worker còn nhịp tim
    SVC-->>GW: 200 / 202 → Dev
    Note over W,WG: Nhịp tim cục bộ ≤ 250 ms. Khi headroom bị vi phạm, worker-guard gọi POST localhost /v1/evict của W:<br/>W docker kill toàn bộ container < 2 s, báo lease released. Không phụ thuộc đường lease/mạng.
```

### 2.2. Đường nóng và đường lạnh (nguyên tắc hiệu suất)
- **Đường nóng (warm hit):** HTTP → proxy → `SVC` tra bảng ấm trong RAM → NATS pinned → frame vào pipe `stdin` của container đã mở → kết quả. **Không** có: gọi LeaseService, hash lại bundle, `docker exec`, `docker run`, truy cập đĩa. Bundle cache theo `sha256` trên worker; lượt gọi chỉ gửi `bundle_sha256 + args`.
- **Đường lạnh:** admission (lease) → kiểm cache môi trường (`env_hash`) → `docker run` → nạp bundle → chạy. Dựng môi trường chưa có cache là **tác vụ riêng bất đồng bộ** (`EnvBuild`), báo trạng thái `building` cho `call_id`, không giữ chân luồng worker.
- **Pre-warm tùy chọn:** `min_containers` (mặc định 0). Mặc định scale-to-zero để không giam VRAM của LLM.
- **Giới hạn khởi động đồng thời:** tối đa 2 cold-start/worker và 1 `EnvBuild`/worker (tránh bão cold-start làm trễ LLM).
- **Payload:** inline ≤ 64 KiB (an toàn dưới `max_payload_bytes=256 KiB` kể cả base64 +33 % `[ĐỌC MÃ]`); lớn hơn ⇒ tải lên Object Store theo `sha256` và gửi `object_ref`. Kết quả > 64 KiB trả qua `object_ref` tương tự.

### 2.3. Mô hình đe dọa & bảo mật (bắt buộc thiết kế từ đầu)
| # | Mối đe dọa | Biện pháp bắt buộc | Mutation test |
|---|---|---|---|
| T1 | Mã tenant thoát sandbox/đọc host | `--network none`, `--read-only`, `--cap-drop ALL`, `--user 65534:65534`, `--pids-limit`, `--memory[-swap]`, `no-new-privileges` (nếu chạy được), seccomp mặc định Docker, **không** `--privileged`, **không** mount docker.sock, không mount từ đường dẫn do tenant cung cấp | Gửi bundle mở socket/ghi `/`/đọc `/proc/1/environ` host ⇒ bị chặn |
| T2 | `cloudpickle.loads` ngoài sandbox | Gateway/Service/Worker host coi bundle là bytes mờ; chỉ `_warm_runner.py` trong container gọi `loads` | Test tĩnh (AST/grep) cấm `cloudpickle`/`pickle.loads` trong `serverless/` và `serverless_worker.py`, trừ runner |
| T3 | Bundle bị sửa/replay trên đường truyền | Service ký HMAC-SHA256 trên `(tenant_id, call_id, bundle_sha256, args_sha256, lease_id, nonce, exp)` bằng khóa triển khai (file 0600, xoay vòng được); worker xác minh hằng-thời-gian, từ chối `exp` quá hạn và `nonce` đã dùng (cache LRU). *HMAC chứng minh tính xác thực/toàn vẹn, **không** chứng minh an toàn — an toàn do sandbox.* | Sửa 1 byte / đổi tenant / replay ⇒ từ chối trước khi chạy |
| T4 | Rò dữ liệu giữa tenant (RAM/VRAM/đĩa) | Khóa Warm Pool = `(tenant_id, env_hash, fn_id)`; không bao giờ dùng chung container giữa tenant; chuyển tenant trên cùng GPU ⇒ **kill container cũ trước**, `/tmp` là tmpfs hủy theo container | Phá `tenant_id` trong khóa ⇒ test ĐỎ |
| T5 | Giả mạo định danh tenant ở dịch vụ Python | D2 (loopback + shared secret) | Gọi thẳng `:18110` với header giả ⇒ 401 |
| T6 | Lạm dụng tài nguyên (VRAM, CPU, đĩa, log, fork bomb) | Quota theo tenant (2.5); `--pids-limit`, `--cpus`, `--memory`; tmpfs có kích thước; giới hạn stdout/stderr (mặc định 256 KiB/lượt); dung lượng Volume | Vượt từng hạn mức ⇒ bị chặn/kill, không để lại tiến trình mồ côi |
| T7 | Vượt `vram_mb` (tự gọi `set_per_process_memory_fraction` lại hoặc không dùng) | **Cưỡng chế ngoài tiến trình:** `ServerlessWorker` lấy mẫu VRAM theo container (ánh xạ pid→container qua `/proc/<pid>/cgroup` `[CHƯA XÁC MINH]`), vượt `vram_mb` + dung sai ⇒ `docker kill` + ghi `OOM_VRAM`. Giới hạn `set_per_process_memory_fraction` trong runner chỉ là lớp mềm. | Hàm cố cấp phát 2× `vram_mb` ⇒ container bị kill, LLM không bị ảnh hưởng |
| T8 | Lệnh Docker bị chèn tham số từ dữ liệu tenant | Worker dựng `argv` cố định; tên container sinh nội bộ (`aicf-sl-<uuid>`); mọi giá trị tenant chỉ đi vào biến JSON qua stdin, không vào CLI; regex chặt cho tên gói/`env` | Fuzz tên app/fn/gói có `;`, `$()`, `--privileged`, `../` ⇒ bị từ chối |
| T9 | Chuỗi cung ứng gói Python | D4 (chỉ wheel, băm khóa, index allowlist), SBOM của image runtime vào quy trình `cyclonedx-bom` sẵn có | Gói không có băm/là sdist ⇒ từ chối |
| T10 | Mã trả pickle độc về SDK | Kết quả mặc định **JSON/msgpack**; chỉ trả cloudpickle khi người gọi đồng thời là chủ hàm (`.remote()` từ SDK của chính tenant) và SDK cảnh báo; **web endpoint luôn JSON** | Web endpoint không bao giờ trả pickle |
| T11 | Tài khoản Docker của worker = root tương đương | Worker chạy bằng user riêng, chỉ thao tác `argv` cố định (T8); ghi vào rủi ro R2; đánh giá Docker rootless ở Pha 0 | — |

### 2.4. Ngữ nghĩa thực thi (hợp đồng với người dùng)
- **Giao hàng:** JetStream là at-least-once `[ĐỌC MÃ]`. Mỗi lượt có `call_id` (UUID do Service sinh) và `Nats-Msg-Id = call_id` để khử trùng khi publish lại.
- **Trước khi chạy mã người dùng:** có thể giao lại (tối đa `max_deliver=5`) mà không lộ hiệu ứng phụ.
- **Sau khi bắt đầu chạy:** worker đánh dấu `started` (ack sớm); mất worker ⇒ `WorkerLost` trả cho người gọi, **không** chạy lại im lặng, trừ khi hàm có `retries=n`. `.map` mặc định `retries=0`; `.map(..., retries=2)` yêu cầu hàm idempotent (ghi rõ trong tài liệu SDK).
- **Message độc (poison):** quá `max_deliver` ⇒ chuyển sang trạng thái `failed` + lý do, không quay vòng vô hạn; đếm vào metric `sl_poison_total`.
- **Heartbeat dài:** worker gọi `in_progress()` mỗi `ack_wait/3` `[ĐỌC MÃ]` trong lúc hàm chạy để không bị giao lại.
- **Hủy:** `DELETE /calls/{id}` hoặc `Ctrl-C` SDK ⇒ worker `kill` hàm đang chạy (container được tái dùng nếu runner còn sống, nếu không thì dừng container).
- **Hết hạn:** quá `timeout_s` ⇒ kill container, `Timeout`; quá `memory_mb` ⇒ `OOMKilled`; quá `vram_mb` ⇒ `OOM_VRAM`. Mỗi trường hợp có mã lỗi riêng và gợi ý khắc phục trong thông báo.
- **Ngoại lệ Python:** runner bắt, trả `exc_type`, `exc_message`, `traceback` (đã cắt); SDK ném lại `aicf.RemoteError` kèm traceback từ xa.

### 2.5. Lập lịch, admission và quota
- **Quota theo tenant** (thêm vào `Tenant`/tệp tenants, mặc định *tắt Serverless*): `serverless_enabled`, `max_concurrency`, `max_warm_containers`, `max_vram_mb_total`, `gpu_seconds_per_day`, `max_volume_bytes`, `max_bundle_bytes`, `allowed_gpus`, `allowed_data_classes`. Hết hạn mức ⇒ `429` + `Retry-After` + mã lỗi nói rõ hạn mức nào.
- **Hàng đợi công bằng:** tận dụng `Tenant.Weight` và cơ chế weighted fair queuing đã có trong `router.go` của gateway (cờ `--fair`, `fair_test.go`) `[ĐỌC MÃ]`; Serverless có hàng đợi riêng trong `serverless/scheduler.py` dùng cùng trọng số, không chen vào hàng đợi suy luận; hết `queue_timeout_s` (mặc định 30 s) ⇒ `429`.
- **Evict nhường chỗ:** khi tenant B cần VRAM mà tenant A giữ container *idle*, Service yêu cầu worker evict container idle lâu nhất (LRU) trước khi trả `no_capacity`; container đang chạy lượt gọi **không** bị evict vì cạnh tranh giữa tenant (chỉ bị evict bởi G-RECLAIM/quá hạn).
- **Chọn GPU:** `gpu="any"|"rtx3060"|"rtx5070"|None` ánh xạ sang nhãn `gpu-rtx3060`... (nhãn sẵn có của `task_subject`). Tương thích ISA: `env_hash` chứa `cuda_tag`; cặp 3060 (sm_86) + 5070 (sm_120) cần wheel PyTorch build đủ kiến trúc `[CHƯA XÁC MINH — Pha 0]`.
- **Kế toán VRAM chung với LLM (rủi ro đếm hai lần/hụt):** `Buckets.Allocatable` của worker phản ánh VRAM thực còn lại *sau* khi LLM chiếm; Lease cộng dồn reservation chưa vật chất hóa. Pha 0 phải đọc `core/vram-estimator/estimator.go` và `apps/worker-guard/reporter.go` (nơi tính/báo `Buckets`) để xác định chính xác ngữ nghĩa `Allocatable`, rồi Pha 3 khóa bất biến: *Σ(reservation chưa vật chất hóa) + VRAM đã dùng thực ≤ Allocatable − headroom*, có test.

### 2.6. SLO (giả thuyết cần chốt bằng số đo ở Pha 0, sau đó thành cổng cứng)
| Chỉ số | Mục tiêu đề xuất | Cơ sở |
|---|---|---|
| Overhead đường nóng (HTTP→kết quả trừ thời gian hàm), payload ≤ 4 KiB | p50 ≤ 20 ms, p95 ≤ 60 ms | `[SUY LUẬN]` từ NATS `1,17 ms` `[ĐO]` + proxy + pipe; **chưa đo** |
| Cold-start có GPU, env đã cache (container + `import torch` + tensor GPU, chưa gồm dispatch) | p95 ≤ 3,0 s | `[ĐO]` Pha 0: p50 1,96 s / p95 2,04 s (n=30); mục tiêu = 1,5 × p95 đo được |
| Dựng env mới (`EnvBuild`) | Không SLO cứng; phải bất đồng bộ, có tiến độ, không chặn LLM | — |
| Evict Serverless khi G-RECLAIM (từ lúc phát hiện đến VRAM giải phóng) | **< 2,0 s p99 (cứng)** | Yêu cầu dự án |
| Ảnh hưởng tới LLM khi Serverless CPU hoạt động | p95 TTFT và tok/s của `/v1/chat/completions` không xấu hơn 5 % so với baseline | `[CHƯA XÁC MINH]` — đo A/B |
| `.map(200)` trên K worker | ≥ 85 % hiệu suất tuyến tính với item ≥ 1 s | `[CHƯA XÁC MINH]` |
| Rò rỉ sau soak 24 h | 0 container mồ côi, 0 lease kẹt, RSS Service/Worker không tăng > 10 % | Yêu cầu production |

### 2.7. Giao diện `aicf` Python SDK
```python
import aicf

app = aicf.App("ocr-pipeline")

image = (
    aicf.Image.debian_slim(python_version="3.12")        # phải khớp Python cục bộ nếu dùng closure; SDK kiểm tra
    .uv_pip_install("torch==2.7.*", "numpy", "pillow")   # chỉ wheel; băm lock tự sinh
    .add_local_python_source("my_pkg")                    # BẮT BUỘC có: đưa module cục bộ vào image (v1 thiếu)
    .env({"OMP_NUM_THREADS": "4"})
)
vol = aicf.Volume.from_name("shared-artifacts", create_if_missing=True)
models = aicf.ModelCache.from_name("whisper-large-v3")   # chỉ đọc, do admin nạp sẵn

@app.function(
    image=image, gpu="any", vram_mb=4096, memory_mb=2048, cpu=2,
    timeout_s=60.0, keep_warm_s=120, min_containers=0, retries=0,
    data_class="INTERNAL", volumes={"/data": vol}, model_caches={"/models": models},
)
def process_item(x: int) -> dict:
    import torch
    return {"x_sq": x * x, "cuda": torch.cuda.is_available()}

@app.cls(image=image, gpu="any", vram_mb=6144, keep_warm_s=300)
class Embedder:
    @aicf.enter()                 # chạy đúng 1 lần mỗi container
    def load(self): ...
    @aicf.method()
    def encode(self, texts: list[str]): ...

@app.web_endpoint(method="POST")   # JSON in/out; xác thực bằng khóa tenant mặc định
def classify(payload: dict) -> dict: ...

if __name__ == "__main__":
    with app.run():
        single = process_item.remote(10)
        batch  = list(process_item.map(range(200), max_concurrency=16, return_exceptions=True))
        call   = process_item.spawn(5);  print(call.object_id, call.get(timeout=300))
```
**Trải nghiệm lỗi (yêu cầu "dễ dùng"):** lỗi phải nêu *nguyên nhân + việc cần làm*; ví dụ: `QuotaExceeded(max_vram_mb_total=8192): đang dùng 6144 + yêu cầu 4096 → chạy "aicf ps" để xem container, hoặc "aicf stop <id>"`; `PythonVersionMismatch(local 3.11, image 3.12)`; `NoCapacity(queue 30s)`; `GpuReclaimed: chủ máy cần GPU — lượt gọi bị hủy, thử lại sau`; `EnvBuildFailed: gói X không có wheel`.

---

## PHẦN 3: KẾ HOẠCH THỰC HIỆN THEO PHA & CỔNG NGHIỆM THU (`CHECK PASS`)

> Thứ tự đã đổi so với v1 để có lát cắt chạy được sớm (Pha 2 – CPU) và để **GPU chỉ bật sau khi cổng an toàn (Pha 3) đạt**. Mỗi pha kết thúc bằng báo cáo `ai-compute-fabric/benchmarks/reports/p_serverless_<N>_*.json|md` có nhãn bằng chứng, nhật ký đột biến, và danh sách lệnh đã chạy.

### PHA 0: Khảo sát thực nghiệm, mô hình đe dọa & ADR-003 (2–3 ngày)
**Mục tiêu:** đóng mọi `[CHƯA XÁC MINH]` trước khi viết code hệ thống.

**Công việc:**
1. **GPU trong Docker trên `pc-001`:** xác nhận `docker run --rm --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=0 --network none --read-only --cap-drop ALL --user 65534:65534 <image glibc> python -c "import torch;print(torch.cuda.is_available())"` chạy được (kể cả `no-new-privileges`, tmpfs `/tmp` đủ lớn và cho phép thực thi nếu Triton/compile cần). Đo thời gian khởi động (≥ 30 lần, p50/p95).
2. **Chọn base image (D5):** so `python:3.12-slim`+wheel gói CUDA với `nvidia/cuda-runtime`: kích thước, pull, cold-start. Xác nhận cùng wheel PyTorch chạy cả sm_86 và sm_120 `[CHƯA XÁC MINH]`.
3. **Dựng env bằng `uv` trong builder cô lập (D4):** đo thời gian cold/warm cache, kiểm `--only-binary`, băm khóa; chứng minh builder không đọc được thư mục host ngoài cache.
4. **Cold vs warm:** đo lượt 1 (container + `import torch` + cấp phát tensor GPU) và lượt 2..100 qua pipe length-prefixed; đo overhead đường nóng đầy đủ để chốt SLO 2.6.
5. **Cưỡng chế VRAM (T7):** kiểm chứng ánh xạ pid→container từ `nvidia-smi --query-compute-apps` trên host Linux; đo độ trễ phát hiện; đo độ chính xác `vram_peak` (lấy mẫu ≠ đỉnh thật — ghi rõ).
6. **Đọc `core/vram-estimator/estimator.go` + `apps/worker-guard/reporter.go` để chốt ngữ nghĩa `Buckets.Allocatable`** (mục 2.5); quyết định mở rộng `RequestLeaseRequest` (thêm `tenant_id`, `workload_kind`, `labels`, `preemptible`) và vị trí `worker_id` của Serverless = `worker_id` của `worker-guard` cùng máy.
7. **LeaseService từ Python:** kiểm tra có sẵn binding Python trong `contracts/gen` và chọn `grpcio` (pin) hay cầu nối HTTP nội bộ; xác minh gateway đang bật gRPC ở đâu/cổng nào.
8. **Object Store:** đo thông lượng put/get ở 1 MiB, 64 MiB, 1 GiB để chốt ngưỡng D7.
9. **Khảo sát `pc-win-01` (WSL2):** Docker `--gpus all` trong WSL2 có chạy? `AICFWorkerGuard` (PDH `\GPU Process Memory(*)\Dedicated Usage`) có thấy VRAM của container WSL2 và thu hồi `< 2 s`? Docker rootless?
10. **Xác minh gate sẵn có:** `make verify-release-v1` thật sự đối chiếu route↔OpenAPI (đọc `test_openapi_conformance.py`); cổng `18110` còn trống; `/api/serverless/` chưa bị route khác chiếm.
11. **Viết ADR-003** (quyết định D1–D10, runtime Windows, base image, mô hình đe dọa 2.3, rủi ro chấp nhận).

**Cổng nghiệm thu Pha 0:**
- [ ] `p_serverless_0_probe.json` + `.md` có số `[ĐO]` cho: cold-start GPU, warm overhead (p50/p95), dựng env, Object Store, độ trễ phát hiện VRAM theo container, kết quả G-RECLAIM với tiến trình GPU trong container (Linux), kết quả WSL2.
- [ ] Bảng SLO 2.6 được cập nhật bằng số đo (không còn "đề xuất" cho mục nào mà đo được).
- [ ] ADR-003 hoàn chỉnh với quyết định rõ ràng cho Windows: WSL2 Docker Sandbox *hoặc* chỉ chạy LLM (khuyến nghị mặc định nếu chưa đạt).
- [ ] Không có thay đổi nào tới dịch vụ đang chạy (kiểm bằng `git status` + `docker ps` + `pgrep -x` trước/sau).

---

### PHA 1: Lõi chạy nền — Env cache, `GpuDockerSandbox`, `WarmDockerPool`, runner (`agents/`)
**Mục tiêu:** thư viện chạy được, test được độc lập, chưa gắn bus/HTTP.

**Công việc:**
1. **`agents/env_cache.py`** (D4/D5): nhận `ServerlessEnvSpec`, validate chặt (regex tên gói/phiên bản, cấm URL/đường dẫn/`--index-url`/VCS), sinh `env_hash`, dựng venv trong container builder, lưu `<cache_dir>/<env_hash>` bất biến (chỉ ghi lúc dựng, sau đó `:ro`), khóa tệp chống dựng trùng, dọn LRU theo dung lượng.
2. **`agents/sandbox.py`:** thêm `GpuDockerSandbox` (tái dùng `command()`, thêm `--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=<idx> -e NVIDIA_DRIVER_CAPABILITIES=compute,utility` (KHÔNG `--gpus`: lỗi CDI trên host này `[ĐO]`), tmpfs cấu hình được, `--cpus`, `HOME`/cache dirs trỏ vào tmpfs) và `WarmDockerPool`: mỗi container là tiến trình `docker run -i` bền, đồng bộ theo **khung length-prefixed** (4 byte độ dài + payload, giới hạn tối đa), khóa `(tenant_id, env_hash, fn_id)`, một lượt/lần (concurrency=1 mặc định), reaper nền (`keep_warm_s`, đổi tenant, chết bất thường), `kill_all()` có hạn thời gian cho G-RECLAIM, đếm container mồ côi theo nhãn Docker `aicf.sl=1`.
3. **`agents/_warm_runner.py`** (chạy trong container): vòng lặp đọc khung → `cloudpickle.loads` → chạy → ghi khung kết quả (JSON/msgpack) + stdout/stderr cắt giới hạn; `@aicf.enter()` gọi 1 lần; bắt ngoại lệ và trả traceback; `set_per_process_memory_fraction` (lớp mềm); không `eval` thứ gì ngoài bundle đã xác minh.
4. **Nhãn & dọn dẹp:** mọi container gắn `--label aicf.sl=1 --label aicf.tenant=… --label aicf.worker=…`; `docker ps -f label=aicf.sl=1` là nguồn sự thật cho test "không còn mồ côi".

**Cổng nghiệm thu Pha 1:**
- [ ] Unit + integration xanh 100%: lượt 1 `cold_start=True`, lượt 2 `False` với `exec_duration_ms` giảm rõ rệt `[ĐO]`; so với `DockerSandbox` one-shot.
- [ ] Sau `keep_warm_s`, `docker ps -f label=aicf.sl=1` rỗng (scale-to-zero).
- [ ] **Đột biến:** (1) bundle mở mạng/ghi `/`/đọc host ⇒ chặn; (2) phá `tenant_id` trong khóa pool ⇒ ĐỎ; (3) vượt `timeout_s`/`memory_mb`/`pids` ⇒ container bị kill sạch, 0 mồ côi; (4) fuzz T8 ⇒ từ chối; (5) test tĩnh T2 ⇒ ĐỎ nếu thêm `pickle.loads` ngoài runner.
- [ ] Khung quá lớn / bị cắt giữa chừng / runner chết giữa lượt ⇒ lỗi rõ ràng, pool tự phục hồi, không treo.

---

### PHA 2: Lát cắt dọc CPU — Hợp đồng tác vụ, `ServerlessWorker`, dịch vụ `serverless/`, proxy gateway, SDK tối thiểu
**Mục tiêu:** `process_item.remote(10)` chạy end-to-end bằng **CPU** qua gateway thật (instance cô lập). GPU vẫn bị cờ chặn.

**Công việc:**
1. **`agents/tasks.py`:** `TaskType.SERVERLESS_EXEC`; model Pydantic `Strict`/`extra="forbid"`: `ServerlessEnvSpec`, `ServerlessExecIn` (`tenant_id, call_id, app_name, fn_id, env, bundle_sha256, bundle_ref|bundle_b64 ≤ 64 KiB, args_b64|args_ref, vram_mb≥0≤24576, memory_mb, cpu, timeout_s≤600, keep_warm_s≤3600, retries, lease_id, sig{nonce, exp, hmac}`), `ServerlessExecOut` (`result_b64|result_ref, stdout, stderr, status{ok|error|timeout|oom|oom_vram|reclaimed|worker_lost}, error{type,message,traceback}, cold_start, queue_ms, exec_ms, vram_peak_mb, worker_id`). Phải cập nhật mọi nơi liệt kê `TaskType` (grep) và test hợp đồng.
2. **`agents/serverless_worker.py`:** `BusWorker` chuyên `SERVERLESS_EXEC`; đăng ký `AICF_AGENTS` (`task_types`, nhãn `gpu-*`/`cpu-only`, `w-<worker_id>`, `max_serverless_vram`, danh sách container ấm trong metadata nhịp tim); xác minh chữ ký (T3); điều phối `WarmDockerPool`; `in_progress()` khi chạy; endpoint cục bộ `127.0.0.1:<port>/v1/evict` (dùng ở Pha 3).
3. **`serverless/` (Python, `:18110`):** `app.py` (HTTP, mẫu `bestofn/service.py`), `scheduler.py` (bảng ấm, admission, WFQ), `quota.py`, `signer.py`, `store.py` (call state, artifact), `metrics.py`. API (xem OpenAPI, Pha 5): `POST /v1/invoke`, `POST /v1/spawn`, `GET/DELETE /v1/calls/{id}`, `GET /v1/calls/{id}/logs` (SSE), `GET /v1/containers`, `DELETE /v1/containers/{id}`, `GET /v1/healthz`, `GET /v1/metrics`. Xác thực D2.
4. **`apps/site-gateway/serverless.go`:** `ServerlessProxy` theo mẫu `BestOfNProxy` (tenant key hoặc token admin), kiểm `Tenant.ServerlessEnabled` + quota thô, rate-limit, ghi Audit (call_id, tenant, fn, kết quả), chèn `X-Aicf-Proxy-Secret`, giới hạn body, cờ `--serverless-url` (rỗng ⇒ route trả 503 `serverless disabled`). Mở rộng `Allowed()` để rõ ràng (tenant key đi đường riêng; operator/viewer chỉ đọc `GET`).
5. **SDK tối thiểu `sdk/python/aicf/`:** `App`, `Image` (băm, kiểm phiên bản), `function`, `.local()`, `.remote()`; cấu hình `AICF_GATEWAY_URL`, `AICF_API_KEY`; truyền lỗi từ xa; CLI `aicf run`.

**Cổng nghiệm thu Pha 2:**
- [ ] `tests/integration/test_serverless_e2e.py` (CPU) xanh: cold→warm→scale-to-zero qua **gateway cô lập → Service → NATS → Worker → sandbox**.
- [ ] Không key / sai key ⇒ `401`; tenant `ServerlessEnabled=false` ⇒ `403`; gọi thẳng `:18110` với header giả ⇒ `401` (T5); cờ `--serverless-url` rỗng ⇒ `503`.
- [ ] **Đột biến:** sửa 1 byte `bundle`/`args`/`tenant_id`/`exp`, replay `nonce` ⇒ worker từ chối **trước khi** chạy; phá kiểm chữ ký ⇒ test ĐỎ.
- [ ] Đo SLO đường nóng thực (p50/p95) và ghi vào báo cáo; nếu vượt mục tiêu 2.6 phải có phân tích điểm nghẽn trước khi sang pha sau.
- [ ] Ngoại lệ Python từ xa hiện đúng traceback tại SDK; `Timeout`/`OOMKilled` có mã lỗi riêng.

---

### PHA 3: GPU an toàn — Lease, co-tenancy với LLM, cưỡng chế VRAM, G-RECLAIM (CỔNG AN TOÀN TRỌNG YẾU)
**Mục tiêu:** bật được GPU mà **không bao giờ** làm hại LLM/chủ máy. Pha này chỉ thực nghiệm trên cổng/instance cô lập; thay đổi `worker-guard`/`leases.go` được build và kiểm thử riêng, **chỉ triển khai vào dịch vụ đang chạy khi chủ dự án chấp thuận bằng văn bản**.

**Công việc:**
1. **Mở rộng proto + `leases.go`:** `RequestLeaseRequest` thêm `tenant_id`, `workload_kind` (`llm|serverless`), `labels`, `preemptible`; chọn worker theo nhãn GPU; khi chưa có container, reservation tính vào bất biến 2.5; thêm `Active()` theo tenant cho UI; chủ động quét hết hạn bằng ticker (không chỉ lười). Cập nhật `contracts/gen` và golden test **có chủ đích**.
2. **Dịch vụ `serverless/` ↔ Lease:** cold path xin lease ngắn (30 s), gia hạn nhịp 10 s *chỉ khi* worker còn nhịp tim; `ReleaseLease` ngay khi container tắt; warm path không gọi Lease (D3).
3. **`apps/worker-guard` (Go):** (a) đăng ký "đồng thuê" với `ServerlessWorker` cục bộ (khai báo URL `/v1/evict`, shared secret); (b) tách VRAM Serverless khỏi VRAM "người dùng khác" trong 6-bucket; (c) **thứ tự evict D10**: khi `free < Headroom` ⇒ gọi evict Serverless (hạn 1,5 s) ⇒ lấy mẫu lại ⇒ chỉ nếu vẫn thiếu mới dừng engine; (d) khi `Pause`/chuyển `merged` ⇒ evict Serverless trước; (e) nếu `/v1/evict` không phản hồi trong hạn ⇒ guard tự `docker kill` theo nhãn `aicf.sl=1` (chỉ `argv` cố định, chạy trên Linux; Windows để Pha 8). Mọi hành vi mới dưới cờ, mặc định tắt ⇒ guard cũ không đổi.
4. **`ServerlessWorker` cưỡng chế (T7):** lấy mẫu VRAM theo container, vượt `vram_mb` + dung sai ⇒ kill + `OOM_VRAM`; `vram_peak_mb` ghi nhận là *đỉnh lấy mẫu* (ghi chú trong API).
5. **Admission:** từ chối/ xếp hàng đúng thiết kế khi thiếu VRAM; không bao giờ cấp lease khiến `free − Σreservation < Headroom`.

**Cổng nghiệm thu Pha 3:**
- [ ] Yêu cầu `vram_mb = 8192` khi chỉ còn `4096 MiB` ⇒ `no_capacity`/xếp hàng, `llama-server` (instance thử cô lập) **không OOM**.
- [ ] Container giữ 4 GB, kích hoạt G-RECLAIM (hạ headroom/`Pause`) ⇒ toàn bộ container Serverless tắt và VRAM trả về ở **< 2,0 s p99 trên ≥ 30 lần `[ĐO]`**; engine LLM **không bị dừng** khi evict Serverless đã đủ.
- [ ] **A/B LLM:** tải `/v1/chat/completions` đồng thời với tải Serverless GPU/CPU ⇒ p95 TTFT và tok/s xấu hơn baseline ≤ 5 % (hoặc ngưỡng đã chốt ở Pha 0) — báo cáo kèm yếu tố nhiễu.
- [ ] **Đột biến:** (1) `SIGKILL` `ServerlessWorker` khi đang giữ lease ⇒ sau ≤ TTL (30 s) lease tự giải phóng, 0 VRAM kẹt; (2) phá thứ tự D10 (dừng engine trước) ⇒ test ĐỎ; (3) phá kiểm `vram_mb` (T7) ⇒ test ĐỎ; (4) phá bất đẳng thức admission ⇒ test ĐỎ; (5) `/v1/evict` treo ⇒ guard vẫn trả GPU < 2 s qua `docker kill`.
- [ ] Golden test `contracts/gen/go` và test cũ của `worker-guard`/`leases` **không đổi kết quả** khi cờ tắt.
- [ ] **Chỉ sau khi toàn bộ mục trên xanh** mới cho phép bật `--serverless-gpu-enabled=true` (ghi vào báo cáo).

---

### PHA 4: SDK đầy đủ & CLI (`.map`, `@app.cls`, `.spawn`, Image local source)
**Công việc:**
1. `.map(iterable, order_outputs=True, max_concurrency=16, return_exceptions=False, retries=0)`: chia việc thành nhiều `SERVERLESS_EXEC`, chạy song song trên các worker rảnh, giữ thứ tự, hỗ trợ backpressure (không đẩy toàn bộ 10⁶ phần tử vào NATS cùng lúc), `starmap`, `for_each`.
2. `.spawn()` → `FunctionCall` (`.get(timeout)`, `.cancel()`, `.object_id`), `FunctionCall.from_id`.
3. `@app.cls` + `@aicf.enter()/@aicf.exit()/@aicf.method()`: `enter` đúng 1 lần mỗi container; `exit` khi dừng.
4. `Image`: `debian_slim`, `uv_pip_install`, `pip_install_from_requirements` (bắt buộc băm), `add_local_python_source`, `add_local_file/dir` (đóng gói vào bundle có băm, giới hạn dung lượng), `env`, kiểm tra khớp phiên bản Python/cloudpickle với thông báo hướng dẫn.
5. `Volume`, `ModelCache` (client side), `Secret.from_name` (secret do admin tạo, tiêm qua biến môi trường trong container, **không** ghi log/audit).
6. CLI: `aicf run`, `aicf deploy`, `aicf ps`, `aicf stop`, `aicf logs <call|fn>`, `aicf volume put/get/ls`, `aicf doctor` (kiểm cấu hình, kết nối, phiên bản, quota). Thông báo lỗi hành động được (2.7).

**Cổng nghiệm thu Pha 4:**
- [ ] `@app.cls`: nạp model ở `enter`, gọi `.remote()` 10 lần ⇒ `enter` chạy **đúng 1 lần**; 9 lần sau dùng lại trạng thái (đếm bằng biến đếm trong container).
- [ ] `.map(200)` trên ≥ 2 worker (Linux + Windows nếu Pha 8 đạt; **nếu chưa, 2 `ServerlessWorker` riêng cô lập trên `pc-001`**): chia đều, kết quả đúng thứ tự; ngắt 1 worker giữa chừng ⇒ phần tử *chưa bắt đầu* được giao lại, phần tử *đang chạy* theo D8 (`WorkerLost` hoặc retry nếu `retries>0`); 0 kết quả sai, 0 mất im lặng.
- [ ] `.map` 10 000 phần tử nhỏ: bộ nhớ SDK/Service phẳng (backpressure hoạt động).
- [ ] **Đột biến:** sửa `fn_bundle` trên đường truyền ⇒ từ chối (T3); `Secret` không xuất hiện trong log/audit (quét chuỗi).
- [ ] `aicf doctor` phát hiện đúng 5 lỗi cấu hình mẫu (sai URL, sai khóa, lệch Python, tenant tắt, hết quota).

---

### PHA 5: Cổng HTTP đầy đủ, Web endpoint, Deploy/Version, Volume, Metering, OpenAPI
**Công việc:**
1. **Route** (đều dưới `/api/serverless/v1/…`, **không** dưới `/v1/`): `invoke`, `spawn`, `calls/{id}`(+`/logs`), `containers`, `deploy`, `apps`, `volumes/{vol}/{path}`, `web/{tenant}/{app}/{fn}` (thay `/v1/webhooks/...` của v1 vì đụng cổng suy luận). Đặc tả vào `contracts/openapi/aicf_v1.yaml`.
2. **Deploy bất biến theo phiên bản:** `deploy` tạo version = `sha256(bundle+config)`; giữ N phiên bản; `aicf rollback`; web endpoint trỏ vào version đang hoạt động; thay version không làm gián đoạn lượt đang chạy (drain container cũ).
3. **Web endpoint:** xác thực mặc định bằng khóa tenant (`requires_auth=True`), JSON-only (T10), giới hạn body/timeout/rate, CORS cấu hình rõ, không lộ nội dung lỗi nội bộ.
4. **`aicf.Volume` (D7):** đường dẫn chuẩn hóa, chặn `..`/tuyệt đối/symlink thoát, phân vùng chặt `tenant_id`, `max_volume_bytes`, đồng nhất commit khi hàm kết thúc, kiểm tra toàn vẹn băm. `ModelCache` chỉ đọc, do admin nạp.
5. **Metering (`metering.go`, dùng `Usage` sẵn có):** mỗi lượt ghi `Tenant`, `Job=call_id`, `GPUMs`, `VRAMMiBSec`; ghi **thêm bản ghi cho thời gian container giữ lease khi idle** (VRAM bị giam cũng tốn tài nguyên) — khác v1 chỉ tính thời gian chạy. Nếu cần cờ `cold_start`, thêm trường có kiểm soát vào `Usage` (cập nhật golden/ledger test) hoặc đặt ở audit.
6. **Audit (`audit.go`):** deploy, invoke (tóm tắt, không ghi dữ liệu), evict (lý do: `idle|quota|reclaim|admin|oom_vram`), truy cập Volume bị từ chối.

**Cổng nghiệm thu Pha 5:**
- [ ] `make verify-release-v1` xanh, `test_openapi_conformance.py` đối chiếu đủ route mới (nếu Pha 0 phát hiện gate chưa đối chiếu route ⇒ bổ sung test trước).
- [ ] Không khóa / khóa tenant sai / tenant `ServerlessEnabled=false` ⇒ `401/403` tại gateway; Tenant A đọc/ghi `Volume` của B, `../../etc/passwd`, symlink thoát ⇒ từ chối + ghi Audit.
- [ ] Deploy v1→v2→rollback: lượt gọi đang chạy không bị gián đoạn; web endpoint chuyển đúng version.
- [ ] Đối soát: tổng `GPUMs`/`VRAMMiBSec` trong ledger khớp ±1 % với số đo độc lập của worker trong 1.000 lượt; `VerifyLedgerFile` xanh.
- [ ] **Đột biến:** phá kiểm tenant ở Volume / quota / `Allowed()` ⇒ test ĐỎ.

---

### PHA 6: Giao diện quản trị & E2E trình duyệt
**Công việc:** trong `apps/site-gateway/web`: bảng Serverless (container ấm, tenant, fn, VRAM giữ, giây còn lại tới scale-to-zero, nút **Force Evict** có xác nhận + ghi Audit), thanh 6-bucket phân biệt VRAM Serverless/LLM/chủ máy, thống kê `GPU-seconds` theo tenant trong `tenants.html`, chỉnh quota theo tenant, công tắc **Disable Serverless** và **Drain** toàn cục.
**Cổng nghiệm thu Pha 6:**
- [ ] `tests/e2e/serverless_e2e.mjs` 100 % PASS trên Chromium thật, ở `2560×1440` và `375×812` (không tràn bố cục, nút bấm được).
- [ ] Force Evict trên UI ⇒ container biến mất, lease giải phóng, có dòng Audit; viewer không thấy nút.

---

### PHA 7: Quy mô, chịu lỗi, soak & sẵn sàng production
**Công việc:**
1. **Quy mô:** K = 10, 25, 50 worker giả lập (bus thật, sandbox giả) kéo `.map(200)`; kịch bản 20 % worker rớt mạng; bão cold-start; NATS chậm/ngắt 5 s; Service khởi động lại giữa chừng (call state phục hồi từ `store.py`).
2. **Chaos có kiểm soát:** `SIGKILL` worker/Service/runner, đầy đĩa cache, Docker daemon restart, ngắt cáp 10.42.0.x; quan sát: 0 lease kẹt, 0 container mồ côi, lỗi trả đúng mã.
3. **Soak 24 h** trên `pc-001` (cổng cô lập): tải hỗn hợp ổn định + A/B LLM; theo dõi RSS, FD, container, lease, độ trễ.
4. **Bảo mật:** chạy bộ `tests/security` + test mới T1–T11; `pip-audit`, SBOM `cyclonedx-bom` cho image runtime; kiểm tra không có bí mật trong log; rà soát quyền tệp khóa HMAC.
5. **Vận hành:** bổ sung `docs/SRE_RUNBOOK.md` (triệu chứng→chẩn đoán→xử lý: lease kẹt, container mồ côi, cold-start chậm, G-RECLAIM chậm, đầy cache, vượt quota), `docs/TESTING.md`, hướng dẫn SDK (`sdk/python/README.md`), bảng mã lỗi.
6. **Rollout:** canary theo tenant (1 tenant nội bộ, CPU-only → thêm GPU → mở rộng), thời gian quan sát ≥ 48 h/bước, tiêu chí dừng tự động (LLM p95 xấu > 5 %, G-RECLAIM > 2 s, lease kẹt > 0, container mồ côi > 0), **rollback = tắt cờ `--serverless-enabled`/`--serverless-gpu-enabled` + `Drain`**, đã diễn tập.

**Cổng nghiệm thu Pha 7:**
- [ ] K = 50: 100 % `.map` hoàn tất, 0 rò lease; báo cáo `benchmarks/reports/p_serverless_7_scale_e2e.json`.
- [ ] Mọi kịch bản chaos: 0 lease kẹt sau ≤ TTL, 0 container mồ côi, không mất kết quả đã xác nhận.
- [ ] Soak 24 h: đạt dòng "Rò rỉ" của bảng SLO 2.6; A/B LLM ≤ 5 %.
- [ ] Toàn bộ `make verify-release-v1` xanh; `pip-audit` không lỗ hổng mức cao chưa xử lý; runbook được một người *khác* diễn tập thành công (ít nhất 3 kịch bản).
- [ ] Diễn tập rollback thành công trong < 5 phút `[ĐO]`.

---

### PHA 8 (TÙY CHỌN, chỉ khi ADR-003 chấp nhận): Windows `pc-win-01`
- Chỉ bật nếu Pha 0 chứng minh: Docker trong WSL2 + `--gpus` chạy; sandbox đạt cô lập mạng/tệp tương đương Linux (T1); `AICFWorkerGuard` nhìn thấy VRAM container và thu hồi < 2 s (hoặc `ServerlessWorker` trong WSL2 tự evict < 2 s theo tín hiệu từ guard).
- Nếu không đạt: Windows chỉ chạy LLM; Serverless chạy trên Linux (hoặc tiến trình trong Job Object + `uv` venv **chỉ** cho tác vụ `INTERNAL` do chính tenant tin cậy — chỉ làm nếu chủ dự án chấp thuận bằng văn bản, mặc định KHÔNG).
- Cổng nghiệm thu: lặp lại cổng G-RECLAIM, A/B LLM, đột biến T1/T4/T7 trên Windows; `.map(200)` trên 2 máy thật; ghi `p_serverless_8_windows.json`.

---

## PHẦN 4: VẬN HÀNH PRODUCTION (yêu cầu xuyên suốt)

**Metrics Prometheus (`/api/serverless/v1/metrics`, nhãn `tenant`, `fn`, `worker`, `gpu`):** `sl_calls_total{status}`, `sl_call_duration_seconds`, `sl_queue_wait_seconds`, `sl_cold_starts_total`, `sl_warm_containers`, `sl_lease_vram_bytes`, `sl_evictions_total{reason}`, `sl_reclaim_latency_seconds`, `sl_env_build_seconds`, `sl_poison_total`, `sl_orphan_containers`, `sl_quota_rejections_total{quota}`, `sl_signature_failures_total`. Cảnh báo tối thiểu: `sl_orphan_containers>0` 2 phút, `sl_reclaim_latency_seconds p99>2`, lease kẹt, tỉ lệ `worker_lost` tăng, signature failures>0.
**Log:** JSON có `call_id, tenant, worker, lease_id, status`; stdout/stderr tenant tách riêng, giới hạn kích thước + thời gian lưu (mặc định 7 ngày), không chứa Secret; `aicf logs` truy vấn qua `call_id`.
**Cấu hình:** một tệp cấu hình có schema, mặc định an toàn (tắt), kiểm tra khi khởi động (fail-fast), không có thiết lập ẩn.
**Dung lượng & dọn dẹp:** hạn mức đĩa cache env, LRU, hạn mức artifact/Volume, dọn container/mồ côi khi khởi động Service/Worker (đọc nhãn `aicf.sl=1`).
**Nâng cấp:** hợp đồng `SERVERLESS_EXEC` có `schema_version`; worker/Service từ chối phiên bản không hỗ trợ với lỗi rõ ràng; không cần tương thích ngược với v1 (chưa có bản triển khai).
**Sao lưu:** sổ cái metering và call state (`store.py`) nằm trong chu trình sao lưu hiện có; ghi rõ trong runbook.

---

## PHẦN 5: CẤU TRÚC THƯ MỤC SẼ THÊM / SỬA

```text
ai-compute-fabric/
├── agents/
│   ├── tasks.py                 # [SỬA] TaskType.SERVERLESS_EXEC + ServerlessEnvSpec/ExecIn/ExecOut
│   ├── sandbox.py               # [SỬA] GpuDockerSandbox, WarmDockerPool
│   ├── _warm_runner.py          # [MỚI] vòng lặp chạy TRONG container (nơi duy nhất gọi cloudpickle.loads)
│   ├── env_cache.py             # [MỚI] dựng env trong builder cô lập, cache theo env_hash
│   └── serverless_worker.py     # [MỚI] BusWorker SERVERLESS_EXEC + pool + cưỡng chế VRAM + /v1/evict
├── serverless/                  # [MỚI] dịch vụ Python :18110 (app, scheduler, quota, signer, store, metrics)
├── apps/
│   ├── site-gateway/
│   │   ├── serverless.go        # [MỚI] ServerlessProxy (auth, tenant, audit, rate-limit) – KHÔNG chứa NATS
│   │   ├── serverless_test.go   # [MỚI] auth/quota/proxy-secret + đột biến
│   │   ├── leases.go            # [SỬA] tenant/labels/kind/preemptible, quét chủ động
│   │   ├── tenants.go           # [SỬA] quota Serverless (mặc định tắt)
│   │   └── web/                 # [SỬA] bảng Serverless, Force Evict, Drain, thống kê tenant
│   ├── worker-guard/            # [SỬA] đồng thuê Serverless, thứ tự evict D10, fallback docker kill (cờ, mặc định tắt)
│   └── global-control-plane/metering.go   # [SỬA nhẹ] dùng Usage sẵn có; thêm trường chỉ khi cần
├── sdk/python/aicf/             # [MỚI] __init__, app, image, volume, secret, cls, cli (+ README, pyproject)
├── contracts/
│   ├── protobuf/aicf/v1/gateway.proto   # [SỬA] RequestLeaseRequest mở rộng → regenerate gen/
│   └── openapi/aicf_v1.yaml     # [SỬA] /api/serverless/v1/*
├── docs/
│   ├── adr/ADR-003-serverless-gpu.md    # [MỚI]
│   ├── SRE_RUNBOOK.md           # [SỬA] mục Serverless
│   └── TESTING.md               # [SỬA]
├── benchmarks/reports/p_serverless_{0..8}_*.json|md
└── tests/
    ├── unit/                    # [MỚI] env_cache, signer, frame, quota, pool-key
    ├── security/test_serverless_threats.py     # [MỚI] T1–T11
    ├── resilience/test_serverless_chaos.py     # [MỚI]
    ├── performance/test_serverless_slo.py      # [MỚI]
    ├── integration/test_serverless_e2e.py      # [MỚI]
    └── e2e/serverless_e2e.mjs                  # [MỚI]
```

---

## PHẦN 6: SỔ RỦI RO

| # | Rủi ro | Mức | Giảm thiểu | Chủ sở hữu quyết định |
|---|---|---|---|---|
| R1 | Serverless làm chậm/hỏng LLM đang phục vụ | Cao | D10, cờ mặc định tắt, A/B ≤ 5 %, canary, rollback < 5 phút | Chủ dự án |
| R2 | Docker daemon = quyền root tương đương cho tiến trình worker | Cao | `argv` cố định (T8), user riêng, đánh giá Docker rootless Pha 0 | Chủ dự án |
| R3 | GPU passthrough dùng chung driver kernel ⇒ bề mặt tấn công rộng hơn container thường; VRAM không được xóa chắc chắn giữa các tenant | Cao | Kill container khi đổi tenant; `RESTRICTED` bị cấm (D6); chỉ tenant nội bộ đã duyệt; đánh giá gVisor/Kata sau | Chủ dự án |
| R4 | Chuỗi cung ứng gói Python | Trung bình | D4/T9, index allowlist, băm khóa, SBOM | Agent + chủ dự án |
| R5 | Lệch kiến trúc GPU (sm_86 vs sm_120) làm một wheel không chạy trên cả hai | Trung bình | Đo Pha 0, `cuda_tag` trong `env_hash`, nhãn GPU | Agent |
| R6 | Đếm VRAM hai lần/hụt giữa Lease và `Allocatable` | Trung bình | Pha 0 đọc estimator, bất biến + test Pha 3 | Agent |
| R7 | Thay đổi `worker-guard`/`leases.go`/proto làm vỡ golden/dịch vụ chạy | Trung bình | Cờ mặc định tắt, golden không đổi khi tắt, build/test cô lập, triển khai cần chấp thuận | Chủ dự án |
| R8 | Object Store chậm với tệp lớn | Thấp–TB | D7 (ModelCache cục bộ), đo Pha 0 | Agent |
| R9 | Cold-start dài làm trải nghiệm kém | Trung bình | `min_containers`, env cache, pre-pull image, SLO đo thật | Agent |
| R10 | Người dùng viết hàm không idempotent rồi bật retry | Thấp | D8 mặc định `retries=0`, tài liệu rõ | Agent |

---

## PHỤ LỤC A: NHẬT KÝ RÀ SOÁT — LỖI/THIẾU CỦA v1 ĐÃ SỬA TRONG v2

| # | Vấn đề trong v1 | Bằng chứng | Cách sửa ở v2 |
|---|---|---|---|
| 1 | Gateway Go "publish NATS" nhưng Go không có client NATS; mẫu thật là proxy sang dịch vụ Python | `grep nats go.mod` rỗng; `BestOfNProxy` `[ĐỌC MÃ]` | D1: dịch vụ `serverless/` Python, Go chỉ proxy |
| 2 | "Scope `serverless`" không tồn tại; `Tenant` không có quyền/quota Serverless | `rbac.go`, `tenants.go` | Quota tenant mới + cờ `ServerlessEnabled` mặc định tắt (2.5) |
| 3 | Route `POST /v1/webhooks/...` đụng cổng suy luận `/v1/` | `main.go: mux.Handle("/v1/", inf)` | Đặt dưới `/api/serverless/v1/web/...` |
| 4 | `LeaseManager` chỉ có gRPC, `RequestLease` không có tenant/nhãn GPU/loại workload; chọn worker theo `Model` | `leases.go`, `gateway.proto` | Mở rộng proto (Pha 3) |
| 5 | Lease theo mỗi lượt gọi (RPC trên đường nóng, rò lease) | Sơ đồ v1 | D3: lease theo container, warm không gọi Lease |
| 6 | Lease TTL = `keep_warm_s` (≤ 3600) ⇒ crash worker giam VRAM lâu; `MaxTTL` thật là 10 phút | `leases.go` | TTL 30 s + heartbeat; `keep_warm_s` cục bộ |
| 7 | Thu hồi lease phụ thuộc `sweepLocked` (lười) cho G-RECLAIM | `leases.go` | G-RECLAIM trực tiếp guard→worker cục bộ, lease chỉ là kế toán |
| 8 | `worker-guard` "kill container khi vượt VRAM" nhưng guard chỉ quản 1 engine, không có evict ngoài | `guard.go`, `engine.go` | D10 + công việc `worker-guard` Pha 3; thêm vào cấu trúc thư mục |
| 9 | Không nói thứ tự evict: guard sẽ dừng LLM thay vì Serverless | `guard.go` (headroom ⇒ dừng engine) | D10: Serverless bị evict trước |
| 10 | Base image `python:3.12-alpine` (musl, tmpfs 8 MB `noexec`, 1 CPU, 128 MB) không chạy được PyTorch/CUDA; venv dựng trên host sẽ lệch ABI | `sandbox.py` | D5: base glibc, tmpfs cấu hình, venv dựng bằng interpreter của image |
| 11 | `uv pip install` trên host chạy `setup.py` của sdist = thực thi mã tùy ý; "danh sách trắng tên gói" không đủ | — | D4: builder cô lập, chỉ wheel, băm khóa, index allowlist |
| 12 | HMAC không nêu ai ký, khóa ở đâu, replay | — | T3: chữ ký có `nonce/exp`, khóa 0600, xoay vòng |
| 13 | Payload bundle base64 vs giới hạn bus 256 KiB; `object_ref` không được định nghĩa | `task_bus.py max_payload_bytes` | 2.2: ngưỡng 64 KiB, content-addressed, cache bundle theo sha256 |
| 14 | "Giao lại khi worker rớt" ⇒ hàm có hiệu ứng phụ chạy hai lần | JetStream at-least-once | D8/2.4: at-most-once sau khi bắt đầu, `retries` opt-in |
| 15 | Không có chế độ bất đồng bộ/hủy/log; `timeout_s` 600 s qua HTTP đồng bộ | — | D9: `spawn`, `calls/{id}`, SSE logs, hủy |
| 16 | Không có SDK cho module cục bộ (`add_local_python_source`), kiểm phiên bản Python/cloudpickle | — | 2.7, Pha 4 |
| 17 | `aicf.Volume` trên Object Store cho mọi dữ liệu (trọng số nhiều GB) | — | D7: tách `Volume` nhỏ và `ModelCache` cục bộ chỉ đọc |
| 18 | Metering chỉ tính thời gian chạy, bỏ VRAM giam khi idle; đề xuất sổ cái mới trong khi `Usage` đã có `GPUMs/VRAMMiBSec` | `metering.go` | Pha 5 mục 5 |
| 19 | Thiếu SLO số, thiếu A/B tác động lên LLM, thiếu soak/chaos, thiếu rollout/rollback, thiếu observability/runbook | — | 2.6, Pha 3/7, Phần 4 |
| 20 | Thiếu mô hình đe dọa tổng thể (Docker = root-tương-đương, GPU dùng chung, pickle kết quả, giả mạo header tenant) | — | 2.3 (T1–T11), Rủi ro R2/R3 |
| 21 | Thứ tự pha trì hoãn lát cắt end-to-end tới Pha 3; GPU và an toàn chưa có cổng bật/tắt | — | Pha 2 (CPU) trước, Pha 3 khóa an toàn rồi mới bật GPU; cờ mặc định tắt |
| 22 | Cổng nghiệm thu Pha 3 v1 dùng "2 worker" nhưng Windows có thể chưa đủ chuẩn | — | Dùng 2 `ServerlessWorker` cô lập trên `pc-001` khi Windows chưa đạt |
| 23 | Lỗi trình bày: ký tự lạ ("綁") và LaTeX `$\rightarrow$` không hiển thị trong Markdown thường | — | Sửa văn bản, dùng "→" |
