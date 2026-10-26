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
| **Sổ cái Metering & Audit Log** | `audit.jsonl`, `ledger.jsonl` | Sao lưu nối thêm (append-only) + lưu hash đỉnh (`head`) ra kho độc lập | Chạy `VerifyAudit` / `--verify-ledger` trước khi mở lại dịch vụ |
