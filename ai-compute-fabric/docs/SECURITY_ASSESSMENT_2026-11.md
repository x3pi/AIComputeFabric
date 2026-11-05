# Đánh giá Bảo mật Toàn diện Hệ thống AI Compute Fabric (`SECURITY_ASSESSMENT_2026-11.md`)

- **Ngày đánh giá:** `2026-11-05` `[ĐO]`
- **Phạm vi:** Toàn bộ các phân hệ `apps/site-gateway`, `apps/worker-guard`, `apps/global-control-plane`, `core/vram-estimator`, `serverless/`, `agents/`, `studio/`, `bestofn/`, `sdk/python/`, và giao diện Web (`index.html`, `serverless.html`, `studio.html`, `admin.html`, `codemirror6.bundle.js`).
- **Tiêu chuẩn đối chiếu:** OWASP ASVS 4.0.3 Level 2, CWE Top 25, Threat Model `T1`–`T11` + `S1`–`S14`.

---

## 1. Tóm tắt Điều hành (Executive Summary)

Qua 2 vòng kiểm toán chuyên sâu (`run-1` và `run-2` theo quy chuẩn `.agents/skills/security-audit`):
- **Tổng số lỗ hổng xác nhận đã phát hiện và khắc phục triệt để:** **9 phát hiện** (`3` từ `run-1` + `6` từ `run-2` `D-F1`..`D-F6`), bao gồm **0 Critical**, **0 High**, **4 Medium**, **5 Low** `[ĐO]`.
- **Trạng thái hiện tại sau khắc phục Pha D:**
  - **0 lỗ hổng mở** thuộc mức Critical / High / Medium / Low trong mã nguồn `[ĐO]`.
  - **0 CVE** trong toàn bộ cây phụ thuộc Python (`.venv/bin/python -m pip_audit --local` → `No known vulnerabilities found`) `[ĐO]`.
  - **100% bài kiểm thử bảo mật tự động vượt qua:** `330 passed` trong `pytest tests/serverless tests/security` và `100% PASS` trong `go test -race ./...` `[ĐO]`.
  - **100% tỷ lệ tiêu diệt đột biến (Mutation Kill Rate):** `5/5` đột biến bảo mật Pha D (`M-DF1`..`M-DF6`) và `3/3` đột biến Pha C (`M-C3-1`, `M-C3-2`, `M-C5-1`) đều bị tiêu diệt (`KILLED`) `[ĐO]`.

---

## 2. Đánh giá Tuân thủ OWASP ASVS Level 2

| Chương ASVS | Yêu cầu Trọng tâm | Cơ chế Thực thi trong AI Compute Fabric | Trạng thái |
|---|---|---|---|
| **V2: Authentication** | Mật khẩu băm bằng thuật toán kháng GPU, chống vét cạn, so sánh hằng thời gian | Argon2id (`time=3, memory=64MiB, threads=4`, salt ngẫu nhiên 16 byte), khóa tạm thời sau 5 lần sai/15 phút (`tenants.go`), `subtle.ConstantTimeCompare` (`rbac.go`, `admin.go`) | **ĐẠT (PASS)** `[ĐO]` |
| **V3: Session Management** | Không lộ khóa quản trị trên trình duyệt khách, thu hồi khóa tức thì | Cổng `/serverless` của khách tách biệt khỏi `/admin`, nút Đăng xuất xóa sạch `sessionStorage`/`localStorage`, `RotateKey` và `DeleteTenant` thu hồi khóa ngay lập tức | **ĐẠT (PASS)** `[ĐO]` |
| **V4: Access Control** | Cô lập đa người thuê (Multi-tenant isolation), chống IDOR và leo thang đặc quyền | Mọi API `/v1/*` đóng gói theo `X-Aicf-Tenant` do cổng xác thực gán; `invoke_web` kiểm tra quyền sở hữu tenant trước khi tra cứu hàm (`D-F1`); chặn `tenant` truy cập `/v1/metrics` và `/v1/internal/reclaim-metric` (`D-F2`) | **ĐẠT (PASS)** `[ĐO]` |
| **V5: Validation & Encoding** | Chống XSS, SQLi/Command Injection, Path Traversal | Không dùng shell (`exec.Command` / `subprocess.run(..., shell=False)`); `esc()` + `textContent` trên Web UI; CSP + SRI `sha384` cho `codemirror6.bundle.js` | **ĐẠT (PASS)** `[ĐO]` |
| **V6: Cryptography** | Ký gói tác vụ, mTLS TLS 1.3, sinh khóa ngẫu nhiên an toàn | HMAC-SHA256 (`agents/sl_security.py`), mTLS TLS 1.3 với kiểm tra `cn != ""` và `cn == workerID` (`D-F4`), `crypto/rand` cho API key và Lease ID | **ĐẠT (PASS)** `[ĐO]` |
| **V7: Error Handling & Logging** | Không rò rỉ stacktrace nội bộ hoặc secret vào log, nhật ký chống giả mạo | Bắt ngoại lệ trả thông điệp chuẩn hóa; secret không bao giờ xuất hiện trong `audit.jsonl` / `metrics`; nhật ký kiểm toán băm chuỗi liên kết SHA-256 (`audit.go`) | **ĐẠT (PASS)** `[ĐO]` |
| **V8: Data Protection** | Cô lập ổ đĩa Volume và bộ nhớ đệm giữa các tenant | `VolumeStore.resolve` khóa chặt theo `<root>/<tenant>/<volume>`, từ chối `..` và symlink; container ấm phân vùng khóa theo `(tenant, fn_id, env_hash, gpu, vram_mb)` | **ĐẠT (PASS)** `[ĐO]` |
| **V12: Files & Resources** | Giới hạn kích thước tải lên, cô lập trình phân tích cú pháp, ghi tệp an toàn | Giới hạn mã nguồn `512 KiB`, từ chối byte NUL; `/v1/lint` chạy subprocess `python3 -I -S` với `RLIMIT_AS=256MiB, RLIMIT_CPU=2s`; `_materialize_files` dùng `mkdtemp` + `O_NOFOLLOW` (`D-F6`) | **ĐẠT (PASS)** `[ĐO]` |
| **V14: Configuration & Supply Chain** | Đóng gói tài nguyên tĩnh bất biến, kiểm toán phụ thuộc | `HTTPHandler` chỉ phục vụ từ `embed.FS` (`D-F3`); `pip-audit` `0` CVE với `pypdf==6.21.4` (`D-F5`); SBOM ký số Cosign | **ĐẠT (PASS)** `[ĐO]` |

---

## 3. Rủi ro Tồn đọng ở Tầng Triển khai Hạ tầng (Deployment Residual Risks)

| Mã | Mô tả | Biện pháp Giảm thiểu Bắt buộc khi Vận hành Production |
|---|---|---|
| **DR-1** | `worker-guard` (`:8099`) và `ggml-rpc-server` (`:50052`) ở chế độ ghép cụm đa máy (Multi-node GGML RPC) không có lớp xác thực ứng dụng riêng trong giao thức GGML RPC gốc `[ĐỌC MÃ]`. | Mặc định `worker-guard` chỉ lắng nghe trên `127.0.0.1:8099`. Khi bật chế độ đa máy, quản trị viên bắt buộc cấu hình tường lửa (`ufw`/`nftables`) chỉ cho phép IP của `site-gateway` gọi tới `:8099` và cô lập `:50052` trên VLAN kết nối điểm-điểm chuyên dụng (`deploy/production/preflight.sh`) `[TÀI LIỆU]`. |
| **DR-2** | Khóa ký SBOM `deploy/security/cosign.pub` trong kho mã là khóa phát triển dùng cho kiểm thử tự động `[ĐỌC MÃ]`. | Trong đường ống phát hành Production, thay thế bằng khóa KMS/HSM của tổ chức trước khi ký phát hành chính thức `[TÀI LIỆU]`. |

