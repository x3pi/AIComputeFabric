# Báo Cáo Pha B — Hoàn Thiện Cấp Độ Production (Production Readiness)

- **Thời điểm hoàn thành:** `2026-11-05`
- **Phạm vi:** Pha B (`B1` → `B7`) theo kế hoạch `KE_HOACH_HOAN_THIEN_PRODUCTION_EDITOR_VA_BAO_MAT_AICF.md`.

---

## 1. Kết quả Thực hiện theo Từng Hạng mục (`B1` – `B7`)

### B1. Xác thực, Đăng ký, Mật khẩu & Quản lý Phiên (Auth & Account Lifecycle)
- **B1(a) Tắt tự đăng ký công khai mặc định (`--allow-signup=false`):** `[ĐỌC MÃ]` `apps/site-gateway/main.go:52`, `apps/site-gateway/admin.go:420-430`. Không còn coi kết nối không có Bearer token là `isAdmin=true` trong `/api/serverless/v1/signup` và `/api/serverless/v1/auth/register`.
- **B1(b) Không tự động xoay khóa của Tenant đã tồn tại:** `[ĐỌC MÃ]` `apps/site-gateway/admin.go:462-470`. Khi tên Tenant đã tồn tại, `/signup` trả `HTTP 409 Conflict` (`tenant_exists`) trừ khi người gọi có token Admin hợp lệ và truyền `"rotate_existing": true`.
- **B1(c) Giới hạn tốc độ đăng ký (`SignupRateLimiter`):** `[ĐỌC MÃ]` `apps/site-gateway/tenants.go:110-197`. Token bucket kép theo IP (`5 req/phút`) và toàn cục (`20 req/phút`), trả `HTTP 429 rate_limited` kèm `Retry-After: 60`.
- **B1(d) Trạng thái chờ phê duyệt (`Pending`):** `[ĐỌC MÃ]` `apps/site-gateway/tenants.go:39-49`, `apps/site-gateway/web/tenants.html`, `apps/site-gateway/web/tenants.js`. Tài khoản tự đăng ký khi `--signup-auto-approve=false` có `Pending: true`, bị chặn gọi API/đăng nhập (`403 account_pending_approval`) cho đến khi Admin bấm **✓ Duyệt**.
- **B1(e) Băm mật khẩu chuẩn OWASP (`Argon2id`) & Độ mạnh mật khẩu:** `[ĐỌC MÃ]` `apps/site-gateway/tenants.go:69-108, 304-370`. Sử dụng `golang.org/x/crypto/argon2.IDKey` (`m=19456 KiB, t=2, p=1`), yêu cầu mật khẩu `>= 12` ký tự, từ chối mật khẩu phổ biến và tự động nâng cấp một chiều từ định dạng băm cũ khi đăng nhập.
- **B1(f) Chống vét cạn mật khẩu & Đồng nhất thông báo lỗi:** `[ĐỌC MÃ]` `apps/site-gateway/tenants.go:530-605`. Khóa tạm thời theo tài khoản và IP sau `5` lần thất bại liên tiếp (`HTTP 429 account_locked`), luôn chạy `dummyVerifyPassword` khi tài khoản không tồn tại để chống dò tên tài khoản qua thời gian phản hồi.
- **B1(g–h) Quản lý Phiên (`aicf_sess_*`) & Đổi mật khẩu:** `[ĐỌC MÃ]` `apps/site-gateway/tenants.go:51-67, 628-785`, `apps/site-gateway/admin.go:539-660`. Chỉ lưu `SHA-256(session_token)` trong bộ nhớ/đĩa, áp dụng cả `ExpiresAt` (tuyệt đối) và `IdleTimeout` (nhàn rỗi), hỗ trợ `GET /api/serverless/v1/auth/sessions`, `DELETE /api/serverless/v1/auth/sessions/{id|all}`, `POST /api/serverless/v1/auth/change-password`, và tự động thu hồi toàn bộ phiên khi xoay API key, đổi mật khẩu, vô hiệu hóa hoặc xóa Tenant.
- **B1(i) Chặn Token Mặc định khi Bind ngoài Loopback:** `[ĐỌC MÃ]` `apps/site-gateway/rbac.go:68-109`. Hàm `ValidateListenAuth` từ chối khởi động nếu bind ngoài `127.0.0.1`/`::1`/`localhost` mà dùng token mặc định (`demo-admin-token`, ...) hoặc ngắn hơn 16 ký tự.

### B2. Đóng gói & Triển khai Production (`deploy/production/`)
- **6 tệp `systemd`:** `deploy/production/systemd/aicf-{site-gateway,worker-guard,serverless,studio,bestofn,agents}.service` chạy dưới user `aicf` không đặc quyền với `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`.
- **Tường lửa mạng (`ufw` & `nftables`):** `deploy/production/firewall/nftables.conf` và `ufw-rules.sh` khóa cổng `:8099` (`worker-guard`) chỉ cho loopback + gateway, và `:50052` (`ggml-rpc`) chỉ cho dải nối dây trực tiếp `10.42.0.0/24`.
- **Kiểm tra tiền triển khai (`deploy/production/preflight.sh`):** `[ĐO]` Kiểm tra đủ 7 điều kiện; chạy thử với cấu hình sai trả mã thoát `1` (`PREFLIGHT_FAILED (4 error(s))`) và với cấu hình đúng trả `0` (`PREFLIGHT_OK (7/7 checks passed)`).
- **Nâng cấp không gián đoạn & Quay vòng log:** `deploy/production/upgrade_rollback.sh` và `deploy/production/logrotate.d/aicf`.

### B3. Độ bền Dữ liệu, Sao lưu, Phục hồi & Kiểm thử `kill -9`
- **Ghi nguyên tử kèm `fsync`:** `[ĐỌC MÃ]` `apps/site-gateway/tenants.go:263-295`, `serverless/deployments.py:405-425`, `serverless/quota.py:110-128`, `serverless/ledger.py:161-170`.
- **Công cụ CLI `aicf-admin`:** `[ĐỌC MÃ]` `apps/site-gateway/cmd/aicf-admin/main.go` (`aicf-admin verify|backup|restore`).
- **Kiểm thử `kill -9` & Phục hồi (`tests/resilience/test_backup_restore_resilience.py`):** `[ĐO]` `2/2 PASS` (`0.81s`).

### B4. Quan sát, Cảnh báo & Bảo mật HTTP (`Strict CSP`)
- **Health/Readiness & Bảo vệ `/metrics`:** `[ĐỌC MÃ]` `/healthz`, `/readyz` (`apps/site-gateway/admin.go:93-114`), `/metrics` yêu cầu `RoleViewer` (`apps/site-gateway/main.go:129`).
- **Tách toàn bộ JS nội tuyến & Bật `Content-Security-Policy: script-src 'self'`:** `[ĐO]` Tách 8 tệp `.js` (`index.js`, `serverless.js`, `tenants.js`, `studio.js`, `audio.js`, `agents.js`, `chat.js`, `guide.js`), loại bỏ 18 thuộc tính `onclick=` tĩnh và `@import` Google Fonts ngoài. Chạy `node tests/e2e/serverless_e2e.mjs` đạt **`25/25 PASS`**, `0` lỗi CSP, `0` lỗi JS console.
- **Grafana Dashboard & Alertmanager:** `deploy/monitoring/grafana_aicf_dashboard.json`, `deploy/monitoring/alertmanager.yml`, `deploy/monitoring/prometheus.yml`.

### B5. Đồng bộ OpenAPI & Mã hóa `secrets` ở Trạng thái Nghỉ
- **Mã hóa `secrets` (`enc:v1:...`):** `[ĐỌC MÃ]` `serverless/quota.py:15-61` (SHA256-CTR + HMAC-SHA256 EtM). Giá trị bí mật không bao giờ xuất hiện dạng rõ trên đĩa hay trong API metadata (`list_secrets_metadata`).
- **OpenAPI Conformance (`tests/integration/test_openapi_conformance.py`):** `[ĐO]` `7/7 PASS` (`2.56s`).

### B6 & B7. Cập nhật CI (`.github/workflows/ci.yml`) & Tài liệu Vận hành
- Đã cập nhật `.github/workflows/ci.yml`, `docs/PRODUCTION.md`, `docs/SRE_RUNBOOK.md`, `docs/TESTING.md`.

---

## 2. Kết quả Kiểm thử Đột biến (Mutation Testing) Pha B `[ĐO]`

| Mã đột biến | Thành phần bị đột biến | Kết quả | Bằng chứng |
|---|---|---|---|
| `M-B1-1` | Bỏ giới hạn tốc độ trong `SignupRateLimiter.Allow` (`return true`) | **KILLED** | `TestB1SignupDisabledByDefaultAndRateLimited` FAIL `[ĐO]` |
| `M-B1-2` | Coi request không có token là `isAdmin = true` trong `/signup` | **KILLED** | `TestB1SignupDisabledByDefaultAndRateLimited` FAIL `[ĐO]` |
| `M-B1-3` | Bỏ so sánh hằng thời gian `subtle.ConstantTimeCompare` trong `verifyPasswordHash` | **KILLED** | `TestB1Argon2idPasswordStrengthLockoutAndMigration` FAIL `[ĐO]` |

