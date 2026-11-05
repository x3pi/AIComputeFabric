# SRE Runbook — AI Compute Fabric (`aicf`)

Tài liệu vận hành chuẩn Production (P8.3) cho hệ thống **AI Compute Fabric**: quy trình khởi chạy, xoay vòng chứng chỉ mTLS X.509, kiểm tra chuỗi băm nhật ký kiểm toán (`AuditLog` & `Ledger`), xử lý sự cố thu hồi GPU (`G-RECLAIM`), và quy trình sao lưu / khôi phục thảm họa (Disaster Recovery).

---

## 1. Kiến trúc Dịch vụ & Cổng Mặc định

| Dịch vụ | Ngôn ngữ / Binary | Cổng mặc định | Giao thức | Vai trò |
|---|---|---|---|---|
| `site-gateway` | Go (`apps/site-gateway`) | `:8080` (HTTP), `:7443` (gRPC) | HTTP/1.1 + SSE, gRPC (mTLS) | Control Plane tại Site, OpenAI Proxy (`/v1/chat/completions`), Lease Manager, RBAC, Audit Log & Web UI |
| `worker-guard` | Go (`apps/worker-guard`) | `:8099` (Local UI), `:8090` (`llama-server`) | gRPC Client -> Gateway, HTTP Local | Daemon bảo vệ ưu tiên người dùng trên máy trạm, đo VRAM 6-Bucket thực tế, thu hồi GPU `< 2s` |
| `gcp-server` | Go (`apps/global-control-plane/cmd/gcp-server`) | `:8443` | HTTPS mTLS 1.3 | Liên kết đa Site qua WAN, thực thi chính sách `DataClass`, sổ cái đối soát `Ledger` |
| `vram_sidecar` | Python (`python -m core.vram_sidecar`) | `:8091` | HTTP JSON | Đọc trực tiếp header tệp `.gguf` trên đĩa để tính toán dự báo VRAM 6-Bucket |
| `agents.dag_api` | Python (`python -m agents.dag_api`) | `:8092` | HTTP JSON + PostgreSQL | Cung cấp trạng thái đồ thị tác vụ DAG (Zero-Payload Privacy) từ PostgreSQL & NATS JetStream |
| `studio` | Python (`python -m studio`) | `:8093` | HTTP JSON + PostgreSQL | Dịch vụ AI Studio: RAG (`pgvector`), Quản lý Prompt/Few-shot bất biến, Huấn luyện QLoRA & xuất GGUF |
| `serverless` | Python (`python -m serverless`) | `:18110` (`127.0.0.1`) | HTTP JSON + SSE (sau `ServerlessProxy`) | Dịch vụ điều phối Serverless GPU/Python: lập lịch WFQ, ký HMAC-SHA256 (`SERVERLESS_EXEC`), quản lý container ấm, cầu nối Lease HTTP, Deploy/Volume/Secret |
| `serverless_worker` | Python (`python -m agents.serverless_worker`) | `:18111` (`127.0.0.1`) | NATS JetStream + Core NATS + HTTP `/v1/evict` | Thực thi hàm trong `WarmDockerPool` (`--runtime=nvidia`), giám sát VRAM theo cgroup, nhận lệnh thu hồi `G-RECLAIM` từ `worker-guard` |

---

## 2. Quản lý Chứng chỉ mTLS X.509 & Thu hồi Chứng chỉ (CRL)

### 2.1. Sinh CA & Chứng chỉ cho Site Gateway và Worker Guard
```bash
cd apps/site-gateway
go run ./cmd/pki -out /etc/aicf/pki -workers pc-001,pc-002,pc-003 -gateway-hosts 127.0.0.1,gateway.internal
```
Tệp sinh ra đều có quyền `0600` cho khóa bí mật (`*.key`):
- `/etc/aicf/pki/ca.crt`, `/etc/aicf/pki/ca.key`
- `/etc/aicf/pki/gateway.crt`, `/etc/aicf/pki/gateway.key`
- `/etc/aicf/pki/pc-001.crt`, `/etc/aicf/pki/pc-001.key`

### 2.2. Khởi chạy `site-gateway` và `worker-guard` với mTLS
```bash
# Site Gateway
./site-gateway \
  -grpc 0.0.0.0:7443 \
  -http 0.0.0.0:8080 \
  -tls-cert /etc/aicf/pki/gateway.crt \
  -tls-key /etc/aicf/pki/gateway.key \
  -tls-ca /etc/aicf/pki/ca.crt \
  -roles-file /etc/aicf/roles.json \
  -tenants-file /var/lib/aicf/tenants.json \
  -audit-log /var/log/aicf/audit.jsonl \
  -vram-sidecar-url http://127.0.0.1:8091 \
  -agents-url http://127.0.0.1:8092 \
  -studio-url http://127.0.0.1:8093

# Worker Guard trên máy trạm
./worker-guard \
  --worker-id pc-001 \
  --gateway gateway.internal:7443 \
  --tls-cert /etc/aicf/pki/pc-001.crt \
  --tls-key /etc/aicf/pki/pc-001.key \
  --tls-ca /etc/aicf/pki/ca.crt \
  --llama-server /opt/aicf/bin/llama-server \
  --model /var/lib/aicf/models/Qwen3-8B-Q4_K_M.gguf \
  --headroom-mib 1024
```

### 2.3. Thu hồi Chứng chỉ Site trên Global Control Plane (`gcp-server`) không cần Restart
1. Lấy số Serial (thập phân) của chứng chỉ Site cần thu hồi:
   ```bash
   openssl x509 -in site_b.crt -noout -serial
   ```
2. Ghi số Serial (dạng thập phân) vào tệp `--revoked-file` (ví dụ `/etc/aicf/gcp/revoked_serials.txt`, mỗi dòng 1 serial).
3. `gcp-server` kiểm tra tệp này trong hàm `VerifyPeerCertificate` của mọi bắt tay TLS 1.3 mới mà không cần khởi động lại tiến trình.

---

## 3. Kiểm tra Toàn vẹn Nhật ký Kiểm toán (`AuditLog`) & Sổ cái (`Ledger`)

### 3.1. Nhật ký Kiểm toán Admin API (`audit.jsonl`)
- Mỗi thao tác trên `site-gateway` được nối vào tệp `--audit-log` kèm băm SHA-256 dây chuyền (`prev` -> `hash`).
- Khi `site-gateway` khởi động, hàm `OpenAudit` tự động chạy `VerifyAudit(path)`. Nếu bất kỳ dòng nào bị sửa, xóa hoặc đảo thứ tự, `site-gateway` **từ chối khởi động** và báo chính xác số dòng bị hỏng.

### 3.2. Sổ cái Đối soát Liên Site (`ledger.jsonl` trên `gcp-server`)
- Kiểm tra tính toàn vẹn chuỗi băm của sổ cái bất kỳ lúc nào:
  ```bash
  ./gcp-server --verify-ledger --ledger-file /var/lib/aicf/gcp/ledger.jsonl
  ```
- Kết quả chuẩn: `OK entries=<N> head=<sha256>`. Nếu tệp bị can thiệp trái phép, lệnh trả về mã thoát `!= 0`.

### 3.3. Xác minh Chữ ký SBOM (`Cosign`)
```bash
COSIGN=/path/to/cosign bash deploy/security/verify_sboms.sh
```

---

## 4. Xử lý Sự cố Vận hành (Incident Response)

### 4.1. Sự cố: Máy trạm không giải phóng VRAM kịp (`G-RECLAIM > 2000 ms`)
1. **Triệu chứng:** Metric `aicf_gateway_reclaim_p95_ms > 2000` hoặc trạng thái worker chuyển sang `reclaiming` quá 2 giây.
2. **Cơ chế tự bảo vệ:**
   - `worker-guard` gửi `SIGTERM` tới tiến trình con `llama-server`, chờ tối đa `GracePeriod` (mặc định `800 ms`), sau đó gửi `SIGKILL` (`TerminateProcess` trên Windows) và tiếp tục đo `nvidia-smi` cho tới khi VRAM giảm thực tế về mức nền.
   - `site-gateway` tự động thu hồi mọi `Lease` trên worker đó (`sweepLocked -> revoked`) và chuyển các yêu cầu đang chờ sang worker khác (`failovers_total`).
3. **Thao tác của Operator:**
   - Mở giao diện `index.html`, nhấp vào ô worker trên Heatmap để mở ngăn kéo 6-Bucket Inspector, bấm **Tạm dừng AI trên máy này** (`POST /api/workers/{id}/pause`).
   - Trên máy trạm Windows, kiểm tra dịch vụ:
     ```powershell
     powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -Action status
     ```

### 4.2. Sự cố: Mất kết nối WAN giữa Site và Global Control Plane
1. **Triệu chứng:** `Site.Sync()` báo lỗi kết nối tới `gcp-server`.
2. **Hành vi hệ thống:**
   - Site tiếp tục phục vụ 100% lưu lượng nội bộ độc lập (`Autonomous Site Operation`).
   - Mọi bản ghi tiêu thụ tài nguyên (`Usage`) được ghi vào sổ cái cục bộ (`Site.Local`) với số thứ tự tăng dần `(Site, Seq)`.
3. **Khôi phục:** Khi đường truyền WAN thông suốt trở lại, `Site.Sync()` tự động đẩy các lô bản ghi chưa xác nhận lên `gcp-server`. Nhờ khóa chống trùng lặp `(Site, Seq)`, việc gửi lại nhiều lần hoàn toàn bất biến (`idempotent`) và không bao giờ tính trùng cước.

---

## 5. Sao lưu & Khôi phục Thảm họa (Backup & Disaster Recovery)

| Thành phần Dữ liệu | Vị trí Lưu trữ | Phương thức Sao lưu | Quy trình Khôi phục (Restore) |
|---|---|---|---|
| **Danh sách Tenant & Hash API Key** | `--tenants-file` (`tenants.json`, quyền `0600`) | Sao lưu định kỳ tệp JSON (chỉ chứa SHA-256 hash, không chứa khóa thô) | Khôi phục tệp `tenants.json` với quyền `chmod 600` trước khi bật `site-gateway` |
| **Cơ sở dữ liệu PostgreSQL (`pgvector` + LangGraph + DAG + Studio)** | PostgreSQL 16+ (`aicf_rag_chunks`, `aicf_dag`, `aicf_results`, `aicf_exec`, `checkpoints`) | `pg_dump --format=custom --file=aicf_pg.dump "$AICF_PG_DSN"` | `pg_restore --clean --if-exists --dbname="$AICF_PG_DSN" aicf_pg.dump` |
| **Hàng đợi NATS JetStream** | Thư mục `--store_dir` của `nats-server` (Stream `AICF_TASKS`) | `nats stream backup AICF_TASKS /backup/nats_aicf_tasks.tgz` | `nats stream restore /backup/nats_aicf_tasks.tgz` |
| **Kho Model Offline & Adapter LoRA** | Thư mục Model Registry & `--data-dir` của `studio` | Đồng bộ `rsync -a` kèm tệp `manifest.json` và chữ ký provenance | Chạy `core/model_registry/security.py` và `provenance.py` để xác minh SHA-256 trước khi nạp lại |
| **Sổ cái Metering & Audit Log** | `audit.jsonl`, `ledger.jsonl`, `usage.jsonl` | Sao lưu nối thêm (append-only) + lưu hash đỉnh (`head`) ra kho độc lập | Chạy `VerifyAudit` / `--verify-ledger` / `VerifyLedgerFile` trước khi mở lại dịch vụ |
| **Trạng thái Serverless (`store.py`, `Volume`, khóa HMAC)** | `--data-dir` của `serverless` (`calls/`, `deployments/`, `volumes/`) và `--secret-file` (`0600`) | Sao lưu định kỳ thư mục `--data-dir` (atomic JSON/file) và tệp khóa HMAC (`0600`) | Khôi phục thư mục `--data-dir` và tệp khóa HMAC với quyền `chmod 600` trước khi khởi động `serverless` |

---

## 6. Vận hành Serverless GPU/Python & Xử lý Sự cố (Pha 7)

### 6.1. Khởi chạy Dịch vụ `serverless` và `serverless_worker` (mặc định tắt, bật có kiểm soát)
```bash
# 1. Sinh tệp khóa ký HMAC-SHA256 và khóa proxy (quyền 0600 bắt buộc)
umask 077
head -c 32 /dev/urandom | base64 > /etc/aicf/serverless_hmac.key
head -c 32 /dev/urandom | base64 > /etc/aicf/serverless_proxy.key
head -c 32 /dev/urandom | base64 > /etc/aicf/serverless_evict.key

# 2. Khởi chạy dịch vụ serverless (chỉ lắng nghe loopback 127.0.0.1:18110)
python -m serverless \
  --host 127.0.0.1 --port 18110 \
  --nats-url nats://127.0.0.1:4222 \
  --secret-file /etc/aicf/serverless_hmac.key \
  --proxy-secret-file /etc/aicf/serverless_proxy.key \
  --lease-gateway-url http://127.0.0.1:8080 \
  --data-dir /var/lib/aicf/serverless \
  --gpu-enabled

# 3. Khởi chạy serverless_worker trên máy trạm Linux có GPU
python -m agents.serverless_worker \
  --worker-id pc-001 \
  --nats-url nats://127.0.0.1:4222 \
  --secret-file /etc/aicf/serverless_hmac.key \
  --evict-secret-file /etc/aicf/serverless_evict.key \
  --evict-port 18111 \
  --store-dir /var/lib/aicf/sl-store \
  --gpu-enabled --gpu-index 0 --max-vram-mb 8192
```

### 6.2. Sổ tay Chẩn đoán & Xử lý 6 Kịch bản Sự cố Serverless

#### Kịch bản A: Lease GPU bị kẹt (`stuck lease`)
- **Triệu chứng:** `GET /api/serverless/v1/_lease/active` hoặc thanh 6-Bucket trên UI báo VRAM Serverless bị giữ dù không còn container nào đang chạy.
- **Chẩn đoán:**
  1. Kiểm tra danh sách container ấm: `curl -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8080/api/serverless/v1/containers`
  2. Kiểm tra tiến trình worker còn sống trong KV `AICF_AGENTS` hay đã chết đột ngột (`SIGKILL`).
- **Xử lý:**
  - Tự động: Mỗi lease Serverless chỉ có `TTL = 30 s` (D3) và chỉ được `tick_leases()` gia hạn khi worker còn nhịp tim trong `AICF_AGENTS`. Khi worker chết, `StartSweeper` trên `site-gateway` tự động thu hồi lease sau tối đa `30 s` `[ĐO]`.
  - Cưỡng bức tức thì (< 1 s): Bấm nút **Force Evict** trên bảng Serverless tại `index.html` hoặc bật **Drain** (`POST /api/serverless/v1/admin/state` với `{"draining": true}`), dịch vụ sẽ gọi `ReleaseLease` ngay lập tức.

#### Kịch bản B: Container mồ côi (`sl_orphan_containers > 0`)
- **Triệu chứng:** Cảnh báo Prometheus `sl_orphan_containers > 0` kéo dài > 2 phút hoặc `docker ps --filter label=aicf.sl=1` thấy container không nằm trong `WarmDockerPool`.
- **Chẩn đoán:**
  ```bash
  docker ps --filter label=aicf.sl=1 --format '{{.ID}} {{.Names}} {{.Label "aicf.tenant"}} {{.RunningFor}}'
  ```
- **Xử lý:**
  - Tự động: `_warm_runner.py` trong container có luồng `_watch_stdin` (`POLLHUP | POLLERR`) tự thoát `os._exit(0)` trong `0,5 s` ngay khi tiến trình cha đóng ống `stdin` `[ĐO]`. Ngoài ra `WarmDockerPool.cleanup_orphans()` tự quét và xóa mọi container mang nhãn `aicf.sl=1 --filter label=aicf.worker=<id>` khi worker khởi động.
  - Thủ công (chỉ xóa đúng container Serverless):
    ```bash
    docker ps -q --filter label=aicf.sl=1 | xargs -r docker kill
    ```

#### Kịch bản C: Cold-start chậm (`sl_cold_starts_total` tăng cao, độ trễ > SLO)
- **Triệu chứng:** `sl_call_duration_seconds` tăng vọt ở lượt đầu tiên hoặc `sl_env_build_seconds` cao.
- **Chẩn đoán:**
  1. Kiểm tra `env_cache` có đang dựng lại venv do bị dọn LRU không (`ls -lh /var/lib/aicf/sl-store/envs`).
  2. Kiểm tra `/var/lib/aicf/sl-store/cache` và `/var/lib/aicf/sl-store/envs` có nằm trên **cùng một filesystem** để `UV_LINK_MODE=hardlink` hoạt động hay không (D-STORE: hardlink chỉ tốn `0,7 MB`/env và `0,58 s` warm build so với `77 MB`/env khi copy `[ĐO]`).
- **Xử lý:**
  - Tăng `--max-cache-bytes` cho `EnvCache` và tăng `keep_warm_s` (ví dụ `300 s`) cho hàm thường gọi.
  - Đảm bảo image `python:3.12-slim` đã được `docker pull` sẵn trên mọi worker.

#### Kịch bản D: Thu hồi GPU chậm (`G-RECLAIM > 2000 ms`)
- **Triệu chứng:** `sl_reclaim_latency_seconds` p99 > 2,0 s hoặc `aicf_gateway_reclaim_p95_ms > 2000`.
- **Chẩn đoán:**
  1. Kiểm tra `worker-guard` có kết nối được tới `http://127.0.0.1:18111/v1/evict` của `serverless_worker` không.
  2. Kiểm tra cờ `--serverless-evict-url` và `--serverless-evict-secret-file` trên `worker-guard`.
- **Xử lý:**
  - Theo thiết kế D10 (`apps/worker-guard/evict.go`), nếu `/v1/evict` phản hồi chậm quá `1,5 s` hoặc lỗi, `HTTPEvictor.Kill()` tự động fallback sang `docker ps -q --filter label=aicf.sl=1 --filter label=aicf.gpu=<idx>` + `docker kill` (kiểm tra regex hex ID nghiêm ngặt), trả lại VRAM trong **p95 387,9 ms / p99 393,9 ms** `[ĐO]`.
  - Nếu Docker daemon bị treo, khởi động lại dịch vụ `docker` trên worker sau khi đã `Pause` worker trên `site-gateway`.

#### Kịch bản E: Đầy đĩa cache `EnvCache` hoặc `Volume` (`ENOSPC` / `QuotaExceeded`)
- **Triệu chứng:** Lỗi `VolumeQuotaExceeded` hoặc `OSError: [Errno 28] No space left on device`.
- **Chẩn đoán:**
  ```bash
  df -h /var/lib/aicf/serverless /var/lib/aicf/sl-store
  ```
- **Xử lý:**
  - `VolumeStore.put()` ghi qua tệp tạm `.tmp` và tự động `unlink` tệp `.tmp` trong khối `except BaseException` khi gặp `ENOSPC`, đảm bảo không bao giờ để lại tệp rác hay làm hỏng tệp đích cũ `[ĐO]`.
  - Chạy `EnvCache.evict_lru(target_bytes=...)` hoặc xóa các volume không còn sử dụng qua `aicf volume ls` / `DELETE /api/serverless/v1/volumes/{vol}/{path}`.

#### Kịch bản F: Vượt Quota Tenant & Quy trình Rollback Khẩn cấp (`< 5 phút`)
- **Triệu chứng:** Tenant nhận lỗi `429 QuotaExceeded` hoặc cần ngắt toàn bộ hệ thống Serverless ngay lập tức để bảo vệ tải LLM.
- **Chẩn đoán:** Chạy `aicf doctor` từ máy trạm của tenant hoặc mở trang `tenants.html` để xem mục **Serverless Quota & Metering (`GPU-seconds`)**.
- **Quy trình Rollback Khẩn cấp (đã diễn tập đạt `11,79 ms` << SLA `5 phút` `[ĐO]`):**
  1. **Bước 1 — Bật chế độ Drain & Tắt Serverless toàn cục (trên Web UI hoặc CLI):**
     ```bash
     curl -X POST http://127.0.0.1:8080/api/serverless/v1/admin/state \
       -H "Authorization: Bearer $ADMIN_TOKEN" \
       -H "Content-Type: application/json" \
       -d '{"draining": true, "serverless_enabled": false}'
     ```
     Lệnh này ngay lập tức:
     - Từ chối mọi lượt gọi Serverless mới (`503 ServiceDraining / ServerlessDisabled`).
     - Cưỡng bức đóng toàn bộ container đang ấm (`WarmDockerPool`) và thu hồi 100% GPU lease trên `site-gateway` (`active_leases = 0`).
  2. **Bước 2 — Xác nhận sạch tài nguyên (< 30 giây):**
     ```bash
     curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8080/api/serverless/v1/containers
     docker ps --filter label=aicf.sl=1
     ```
     Cả hai danh sách phải rỗng (`[]`).

### 6.3. Bảng Mã Lỗi Chuẩn của Serverless (`sdk/python/aicf`)

| Mã lỗi / Ngoại lệ SDK | HTTP Status | Ý nghĩa | Hành động khuyến nghị cho Người dùng / Operator |
|---|---|---|---|
| `AuthenticationError` | `401` | Thiếu hoặc sai `AICF_API_KEY` / chữ ký proxy không hợp lệ | Kiểm tra `AICF_API_KEY` hoặc chạy `aicf doctor` |
| `PermissionDeniedError` | `403` | Tenant chưa được bật `serverless_enabled` hoặc yêu cầu GPU khi `gpu_allowed=false` / `RESTRICTED` | Liên hệ Admin bật quyền trong `tenants.html` |
| `QuotaExceeded` | `429` | Vượt `max_concurrency`, `max_vram_mb` hoặc `max_volume_bytes` | Chạy `aicf ps` và `aicf stop <id>` để giải phóng container ấm |
| `NoCapacity` | `503` | Không còn worker có đủ VRAM trống sau khi chờ hàng đợi | Giảm `vram_mb` yêu cầu hoặc thử lại sau |
| `GpuReclaimed` | `200` (`status=reclaimed`) | Chủ máy trạm dùng lại GPU (`G-RECLAIM` D10) nên container bị thu hồi | Thử lại lượt gọi hoặc đặt `retries=1` cho hàm bất biến |
| `OOMKilled` / `VramExceeded` | `200` (`status=oom` / `oom_vram`) | Hàm dùng vượt `memory_mb` (RAM) hoặc `vram_mb` (VRAM GPU) | Tăng `memory_mb` / `vram_mb` trong `@app.function(...)` |
| `FunctionTimeoutError` | `200` (`status=timeout`) | Hàm chạy quá `timeout_s` | Tăng `timeout_s` (tối đa `600 s`) hoặc dùng `.spawn()` |
| `WorkerLost` | `200` (`status=worker_lost`) | Worker bị mất kết nối sau khi đã nhận lệnh chạy (D8 at-most-once) | Kiểm tra trạng thái thực tế hoặc bật `retries=n` nếu hàm idempotent |
| `RemoteError` | `200` (`status=error`) | Ngoại lệ Python xảy ra bên trong hàm người dùng | Xem `remote_traceback` hoặc chạy `aicf logs <call_id>` |

---

## 7. Vận hành Production: Sao lưu, Phục hồi, Kiểm định Chuỗi băm & Nâng cấp (Pha B)

### 7.1. Kiểm tra Sức khỏe (`/healthz`, `/readyz`) và Preflight
- `GET /healthz`: Kiểm tra tiến trình `site-gateway` còn sống (`200 OK`).
- `GET /readyz`: Kiểm tra `TenantStore` và chuỗi băm `AuditLog` nguyên vẹn (`200 Ready` / `503 Not Ready`).
- Trước khi khởi động lại cụm Production:
  ```bash
  AICF_ENV_FILE=/etc/aicf/site-gateway.env bash deploy/production/preflight.sh
  ```

### 7.2. Sao lưu & Phục hồi Trạng thái (`aicf-admin`)
```bash
# Kiểm tra toàn vẹn dữ liệu & chuỗi băm audit/ledger
aicf-admin verify -data-dir /var/lib/aicf

# Tạo bản sao lưu nén kèm manifest SHA-256
aicf-admin backup -data-dir /var/lib/aicf -out /var/backups/aicf-backup.tar.gz

# Phục hồi an toàn (tự động giải nén vào vùng tạm và verify trước khi áp dụng)
aicf-admin restore -in /var/backups/aicf-backup.tar.gz -data-dir /var/lib/aicf
```

### 7.3. Xử lý Sự cố Khóa Tài khoản do Brute-force hoặc Phê duyệt Tenant Pending
- Khi người dùng nhập sai mật khẩu quá 5 lần, tài khoản/IP bị khóa tạm thời (`HTTP 429 account_locked`). Admin có thể kiểm tra tại `tenants.html` hoặc cấp lại mật khẩu/API key.
- Khi bật `--allow-signup=true` ở chế độ cần phê duyệt (`--signup-auto-approve=false`), tài khoản mới hiển thị nhãn **⏳ Chờ duyệt (Pending)** trên `tenants.html`. Nhấn nút **✓ Duyệt** để kích hoạt.

