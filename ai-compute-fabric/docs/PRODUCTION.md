# Hướng Dẫn Triển Khai & Vận Hành Production — AI Compute Fabric (AICF)

Tài liệu này quy định kiến trúc triển khai, cấu hình bảo mật bắt buộc, sao lưu/phục hồi dữ liệu (`RPO/RTO`), chính sách xóa dữ liệu Tenant và quy trình nâng cấp không gián đoạn cho môi trường Production của **AI Compute Fabric**.

---

## 1. Kiểm tra Tiền triển khai (`deploy/production/preflight.sh`)

Trước khi khởi chạy bất kỳ dịch vụ nào ở môi trường Production, bắt buộc chạy:
```bash
sudo AICF_ENV_FILE=/etc/aicf/site-gateway.env \
     AICF_DATA_DIR=/var/lib/aicf \
     bash deploy/production/preflight.sh
```
Script kiểm tra 7 điều kiện bắt buộc:
1. `AICF_ADMIN_TOKEN` khác rỗng, dài `>= 16` ký tự và không nằm trong danh sách token mặc định bị cấm (`demo-admin-token`, `admin`, `changeme`, ...).
2. Tệp cấu hình bí mật `/etc/aicf/*.env` có quyền `0600` hoặc `0400`.
3. Chứng chỉ TLS (`AICF_TLS_CERT`) và khóa riêng (`AICF_TLS_KEY`, quyền `0600`/`0400`) tồn tại, hợp lệ.
4. `worker-guard` (`:8099`) và `ggml-rpc` (`:50052`) không bind `0.0.0.0` tự do.
5. Docker daemon hoạt động và đã đăng ký `nvidia` container runtime.
6. Dung lượng đĩa trống trên `/var/lib/aicf` đạt tối thiểu `>= 2048 MiB`.
7. Các cờ an toàn Production: `AICF_ALLOW_SIGNUP=false`, `AICF_SIGNUP_AUTO_APPROVE=false`, `AICF_ALLOW_UNAUTHENTICATED_ADMIN=false`.

---

## 2. Dịch vụ `systemd` & Phân tách Mạng (`deploy/production/`)

### 2.1. Các đơn vị `systemd` (`deploy/production/systemd/`)
Tất cả 6 dịch vụ chạy dưới tài khoản hệ thống không đặc quyền `aicf:aicf` với `NoNewPrivileges=yes`, `ProtectSystem=strict`, `PrivateTmp=yes`:
- `aicf-site-gateway.service` — Cổng điều phối trung tâm & HTTPS/gRPC (`--allow-signup=false` mặc định).
- `aicf-worker-guard.service` — Giám sát 6-Bucket VRAM & thu hồi GPU (`127.0.0.1:8099`).
- `aicf-serverless.service` — Thực thi hàm Serverless Python/CUDA trong Docker rootless/hardened (`127.0.0.1:18110`).
- `aicf-studio.service` — RAG, Prompt Registry, LoRA & Audio AI (`127.0.0.1:18090`).
- `aicf-bestofn.service` — Best-of-N Verification Sidecar (`127.0.0.1:18095`).
- `aicf-agents.service` — Multi-Agent Bus & DAG Orchestrator (`127.0.0.1:18100`).

### 2.2. Tường lửa Mạng (`deploy/production/firewall/`)
- **Cổng `8099/tcp` (`worker-guard`)**: Chỉ cho phép loopback (`127.0.0.1`) và IP của `site-gateway`.
- **Cổng `50052/tcp` (`ggml-rpc`)**: Giao thức tensor-split của `llama.cpp` không mã hóa, **chỉ được phép** đi qua dải mạng nối dây trực tiếp (`10.42.0.0/24`), chặn hoàn toàn từ LAN/WAN chung.
- Áp dụng bằng `nftables` hoặc `ufw`:
  ```bash
  sudo nft -f deploy/production/firewall/nftables.conf
  # hoặc:
  sudo GATEWAY_IP=10.42.0.1 RPC_SUBNET=10.42.0.0/24 bash deploy/production/firewall/ufw-rules.sh
  ```

---

## 3. Xác thực, Mật khẩu (`Argon2id`) & Quản lý Phiên (`aicf_sess_*`)

1. **Tự đăng ký (`--allow-signup=false` mặc định)**:
   - Ở chế độ Production mặc định, chỉ Admin mới có quyền tạo Tenant hoặc phê duyệt tài khoản.
   - Khi bật `--allow-signup=true` mà không bật `--signup-auto-approve`, tài khoản mới ở trạng thái `pending=true` (chờ Admin nhấn **✓ Duyệt** trên `tenants.html`).
   - Giới hạn tốc độ đăng ký: Token Bucket theo từng IP (`5 req/phút`) và toàn cục (`20 req/phút`).
2. **Băm mật khẩu chuẩn OWASP (`Argon2id`)**:
   - Thông số: `m=19456 KiB (19 MiB), t=2, p=1`, salt ngẫu nhiên 16 byte.
   - Độ mạnh mật khẩu: tối thiểu `12` ký tự, chặn danh sách mật khẩu phổ biến và chuỗi lặp ký tự.
   - Chống vét cạn (Brute-force): Khóa tạm thời theo tài khoản và theo IP sau `5` lần sai liên tiếp (backoff lũy thừa), thời gian phản hồi đồng nhất (chạy `dummyVerifyPassword` khi tài khoản không tồn tại).
3. **Token phiên ngắn hạn (`aicf_sess_*`)**:
   - Độc lập hoàn toàn với `aicf_*` API key; trên đĩa chỉ lưu mã băm `SHA-256(session_token)`.
   - Có hạn tuyệt đối (`ExpiresAt`, mặc định `12h`) và hạn nhàn rỗi (`IdleTimeout`, mặc định `2h`).
   - Tự động thu hồi toàn bộ phiên khi xoay API key (`rotate-key`), đổi mật khẩu (`change-password`), khóa hoặc xóa Tenant.

---

## 4. Độ bền Dữ liệu, Sao lưu/Phục hồi (`aicf-admin`) & Mục tiêu RPO/RTO

### 4.1. Mục tiêu RPO / RTO
- **RPO (Recovery Point Objective)**: `0` giao dịch đối với các ghi nhận đã hoàn tất trên ổ đĩa cục bộ (nhờ ghi nguyên tử `.tmp` + `fsync(file)` + `os.Rename` + `fsync(dir)`), và `<= 15 phút` khi đồng bộ bản sao lưu `.tar.gz` sang kho lưu trữ ngoài cụm.
- **RTO (Recovery Time Objective)**: `<= 5 phút` sử dụng công cụ `aicf-admin restore` và `deploy/production/upgrade_rollback.sh`.

### 4.2. Công cụ `aicf-admin` (`apps/site-gateway/cmd/aicf-admin`)
```bash
# 1. Kiểm tra tính toàn vẹn JSON và chuỗi băm mật mã (audit.jsonl & usage_ledger.jsonl)
aicf-admin verify -data-dir /var/lib/aicf

# 2. Sao lưu nguyên tử kèm manifest SHA-256
aicf-admin backup -data-dir /var/lib/aicf -out /var/backups/aicf-$(date +%Y%m%d-%H%M%S).tar.gz

# 3. Phục hồi vào thư mục staging, kiểm định chuỗi băm rồi mới ghi đè an toàn
aicf-admin restore -in /var/backups/aicf-latest.tar.gz -data-dir /var/lib/aicf
```

### 4.3. Chính sách Xóa Dữ liệu khi Xóa Tenant (`DELETE /api/tenants/{id}`)
Khi Admin xóa một Tenant:
1. **Thu hồi tức thì (`0 ms`)**: Khóa API (`aicf_*`) và toàn bộ phiên đăng nhập (`aicf_sess_*`) của Tenant bị xóa ngay lập tức khỏi bộ nhớ và `tenants.json`.
2. **Dữ liệu App & Volume**: Các ứng dụng Serverless (`DELETE /api/serverless/v1/apps/{app}`), container ấm (`WarmDockerPool`), tệp Volume (`/var/lib/aicf/serverless/volumes/{tenant}`) và tài liệu AI Studio (`DELETE /api/studio/v1/tenants/{tenant}`) được dọn sạch theo quy trình thu hồi.
3. **Sổ cái Kiểm toán & Định lượng (`audit.jsonl`, `usage_ledger.jsonl`)**: Được **giữ nguyên bất biến** để bảo toàn chuỗi băm mật mã (`prev -> hash`) phục vụ đối soát tài chính và kiểm toán bảo mật.

---

## 5. Nâng cấp Không gián đoạn & Rollback (`deploy/production/upgrade_rollback.sh`)

```bash
# Nâng cấp (bật Drain -> chờ lượt gọi đang chạy -> sao lưu binary cũ -> thay thế nguyên tử -> kiểm tra /healthz & /readyz)
sudo AICF_ADMIN_TOKEN="$ADMIN_TOKEN" deploy/production/upgrade_rollback.sh upgrade /tmp/site-gateway-new

# Rollback khẩn cấp về phiên bản trước
sudo AICF_ADMIN_TOKEN="$ADMIN_TOKEN" deploy/production/upgrade_rollback.sh rollback
```

