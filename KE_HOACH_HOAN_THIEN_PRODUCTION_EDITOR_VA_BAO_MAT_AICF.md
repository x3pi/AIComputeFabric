# KẾ HOẠCH: HOÀN THIỆN SẴN SÀNG PRODUCTION · NÂNG CẤP TRÌNH SOẠN THẢO MÃ TRÊN TRÌNH DUYỆT · ĐÁNH GIÁ & VÁ BẢO MẬT TRIỆT ĐỂ
*(Tài liệu bàn giao cho Agent thực thi. Soạn 2026-11-05 sau khi đọc mã thật trong `ai-compute-fabric/`, kế hoạch `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md` (Pha 0–7 ghi "XONG"), phần việc đang dở trong cây làm việc và báo cáo bảo mật cũ. Trạng thái: **KẾ HOẠCH — chưa sửa mã**.)*

**Quy ước nhãn bằng chứng (giữ nguyên của dự án):** `[ĐO]` đã đo trên máy thật · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` theo tài liệu ngoài · `[SUY LUẬN]` suy ra từ kiến trúc · `[CHƯA XÁC MINH]` bắt buộc kiểm ở Pha A trước khi dựa vào. **Người soạn kế hoạch này CHƯA chạy bộ test nào**; mọi con số "xanh/đỏ" phải do Agent đo lại ở Pha A.

**Ba mục tiêu, theo đúng thứ tự thực hiện:**
1. **Pha B — Hoàn thiện tính năng để chạy production** (đóng các khoảng trống vận hành/xác thực/triển khai/kiểm thử còn lại, kể cả phần việc đang dở chưa commit).
2. **Pha C — Trình soạn thảo mã trên trình duyệt tốt hơn** (hiện chỉ là `<textarea>`).
3. **Pha D — Đánh giá lại bảo mật bằng skill `claude-security`, rồi sửa triệt để** (quét → patch → áp dụng → test hồi quy → quét lại tới khi sạch).

Thứ tự bắt buộc: **A → B → C → D → E**. Pha D chỉ bắt đầu khi B và C đã hợp nhất và xanh, vì quét bảo mật phải chạy trên mã cuối cùng.

---

## PHẦN 0: QUY TẮC BẮT BUỘC (ĐỌC TRƯỚC KHI CHẠM VÀO CODE)

Kế thừa nguyên văn `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md` mục 0.3; nhắc lại các điều ảnh hưởng trực tiếp tới kế hoạch này:
1. **Báo cáo bằng tiếng Việt**, mọi con số có nhãn bằng chứng.
2. **Không làm gián đoạn dịch vụ đang chạy** (`site-gateway`, `worker-guard`, `llama-server`, `aicf-nats`, `aicf-pg`) khi chưa hỏi chủ dự án. Thử nghiệm tích hợp chạy trên tiến trình/cổng/stream NATS/container có tiền tố `aicf-sl-test-`. Cổng đã dùng: 18080 (gateway), 18090/18091, 18099 (bestofn), 18110 (serverless), 18199.
3. **Kiểm đột biến bắt buộc** cho mọi cơ chế an toàn/phân quyền/giới hạn: phá điều kiện → test ĐỎ → khôi phục → test XANH; ghi (file, dòng, kết quả) vào báo cáo.
4. Chỉ `pkill -x <tên>` hoặc `kill <pid>`; cấm `pkill -f`.
5. **Không `git push`.** Chỉ commit khi chủ dự án cho phép; riêng Pha D cần commit cục bộ trên nhánh làm việc (xem D0) — phải xin phép trước.
6. **Cổng nghiệm thu cứng:** pha chưa đạt đủ `Check Pass` thì dừng và báo cáo trung thực, không hạ tiêu chí, không tắt test/cảnh báo để "xanh".
7. **Không giữ tương thích ngược:** xóa đường mã cũ thay vì thêm lớp tương thích (trừ khi có dữ liệu đang chạy cần di trú — khi đó viết bước di trú một lần và xóa sau khi chạy).
8. Windows `pc-win-01` vẫn **chỉ chạy LLM** (D-WIN, ADR-003); không đưa Serverless lên Windows trong kế hoạch này.
9. Cờ an toàn mặc định TẮT (`--serverless-enabled=false`, `--serverless-gpu-enabled=false`) phải giữ nguyên trong cấu hình production.

---

## PHẦN 1: HIỆN TRẠNG ĐÃ KIỂM VÀ KHOẢNG TRỐNG

### 1.1. Những gì đã có (không viết lại)
- Serverless Pha 0–7 được ghi "XONG" kèm báo cáo đo trong `benchmarks/reports/` `[ĐỌC MÃ: bảng trạng thái của kế hoạch Serverless]`. Các con số này **chưa được đo lại trong phiên soạn kế hoạch này** `[CHƯA XÁC MINH]`.
- Sau Pha 7 có thêm các commit: canary `pc-001` + alert Prometheus + ép D-WIN (`1dec630`), Developer Login/Signup + Cloud Python IDE (`7d13db5`), SDK kiểu Beam/Beta9 `@endpoint`/`@task_queue`/`Sandbox`/CLI `configure|serve` (`98a4e32`) `[ĐỌC MÃ: git log]`.
- **Cây làm việc đang dở, chưa commit và ĐANG THAY ĐỔI trong lúc soạn kế hoạch** `[ĐỌC MÃ: git status/diff --stat, 2 lần đọc cách nhau vài phút cho kết quả khác nhau]`: lần đọc cuối gồm `agents/_warm_runner.py` (+354), `agents/sandbox.py`, `apps/site-gateway/admin.go` (+94), `apps/site-gateway/tenants.go` (+129), `serverless/{app,deployments,service,volumes}.py` (tổng 8 tệp, +722/−41). Trong đó có route `rotate-key`, `auth/register`, `auth/login`, session `aicf_sess_*`. Tức là **có người/agent khác đang sửa song song** — Agent thực thi tuyệt đối không revert/stash/ghi đè; ở A1 phải xác định ai đang sửa, chờ hoặc phối hợp qua chủ dự án rồi mới chốt baseline.
- CI `.github/workflows/ci.yml` có: `make test-go build-cross`, `golangci-lint`, `govulncheck` cho 4 module Go, `trivy fs` `[ĐỌC MÃ]`. Có `deploy/security/{scan.sh,verify_sboms.sh,cosign.pub}`, `release-manifest-v1.0.yaml`, `docs/{SRE_RUNBOOK,TESTING}.md`, `deploy/monitoring/prometheus_serverless_alerts.yml` `[ĐỌC MÃ]`.
- Trình soạn thảo hiện tại: `<textarea id="sl-code-editor" class="code-editor">` trong `apps/site-gateway/web/serverless.html:425` `[ĐỌC MÃ]`; xử lý phím Tab bằng cách gán lại `codeEditor.value` (dòng ~1210–1218); tìm entrypoint bằng regex `def …(` (dòng ~1221); upload/download `.py`; 5 mẫu; chỉ kiểm cú pháp ở **server khi deploy** bằng `ast.parse` (`serverless/deployments.py: compile_source_bundle`, giới hạn 512 KiB) và lỗi cú pháp chỉ trả dạng chuỗi `"Lỗi cú pháp Python tại dòng X, cột Y"` `[ĐỌC MÃ]`.
- Web console là HTML thuần được nhúng bằng `//go:embed web` (`server.go:21`), **không có toolchain JS/npm cho phần web** (chỉ `tests/e2e/package.json` cho Playwright) `[ĐỌC MÃ]`.

### 1.2. Khoảng trống nhìn thấy khi đọc mã (là giả thuyết cần xác minh, không phải kết luận)
| # | Khoảng trống | Bằng chứng | Hạng mục xử lý |
|---|---|---|---|
| G1 | `POST /api/serverless/v1/signup` **không cần xác thực**; ở chế độ mở (`auth.Open()`) người gọi được coi là admin và khi trùng tên sẽ **xoay khóa của tenant đang tồn tại** (`RotateKey`); không có giới hạn tần suất, xác minh email, hay phê duyệt | `admin.go` khối `/signup` `[ĐỌC MÃ]` | B1 |
| G2 | Mật khẩu băm bằng SHA-256 lặp 4096 lần tự viết (`hashPassword`), mật khẩu tối thiểu 6 ký tự, chưa thấy chống dò mật khẩu/khóa tài khoản | `tenants.go` `[ĐỌC MÃ]` | B1, D |
| G3 | Demo bật `--default-enabled` (mọi tenant dùng được Serverless) kèm `--canary-gpu-allowed`; kết hợp với G1 = ai đăng ký cũng chạy mã Python trên máy | `deploy/demo/run_demo.sh` `[ĐỌC MÃ]` | B1 |
| G4 | Chỉ có kịch bản **demo** (`run_demo.sh`, token mặc định `demo-admin-token`, HTTP không TLS bind `0.0.0.0`); chưa có đơn vị systemd/compose cho production, hướng dẫn TLS cho cổng HTTP, quản lý bí mật, sao lưu/khôi phục | `deploy/` chỉ có `canary, demo, monitoring, security, windows` `[ĐỌC MÃ]` | B2, B3 |
| G5 | Kho tenant là tệp JSON (`-tenants-file`) trên một gateway duy nhất: chưa có sao lưu/khôi phục/kiểm tra toàn vẹn được kiểm thử | `main.go`, `tenants.go` `[ĐỌC MÃ]` | B3 |
| G6 | Chưa có header bảo mật web (CSP, X-Frame-Options, nosniff, Referrer-Policy, HSTS); trang dùng script inline rất nhiều; khóa admin/tenant lưu ở `sessionStorage` | `grep` không thấy `Content-Security-Policy`; `serverless.html` `[ĐỌC MÃ]` | B4, D |
| G7 | Route mới (signup/register/login/rotate-key, SDK `@endpoint`/`@task_queue`) có thể chưa nằm trong OpenAPI và `tests/integration/test_openapi_conformance.py` | — `[CHƯA XÁC MINH]` | B5, B6 |
| G8 | CI chạy Go; chưa thấy job chạy bộ test Python (`tests/serverless`, `tests/security`, `tests/resilience`), Playwright e2e, kiểm đột biến | `ci.yml` chỉ đọc 53 dòng đầu `[CHƯA XÁC MINH]` | B6 |
| G9 | `/metrics` của gateway nằm ngoài lớp xác thực admin (ghi chú hardening của lần quét cũ) | `REPORT.md §6` của run-1 `[ĐỌC MÃ]` | B4 |
| G10 | Báo cáo bảo mật cũ (`run-1`, profile `quick`, commit `5cdaf75`) chỉ phủ `apps, studio, agents, core`; **không phủ `serverless/`, `sdk/`, phần web, phần mã mới sau commit đó**. 3 phát hiện (1 Medium, 2 Low) + 1 lead cần xác minh; chưa rõ đã vá chưa | `run-metadata.json`, `REPORT.md` `[ĐỌC MÃ]` | D |

---

## PHA A: CHỐT NỀN (BASELINE) — KHÔNG ĐƯỢC BỎ QUA

**Mục đích:** biết chính xác điểm xuất phát, không chồng thêm tính năng lên cây làm việc chưa rõ.

| ID | Việc | Cách làm |
|---|---|---|
| A1 | Kiểm kê cây làm việc | `git status`, `git diff --stat`, đọc từng tệp đang sửa; phân loại: *hoàn chỉnh – cần test* / *dở dang – cần hoàn tất* / *thử nghiệm – bỏ*. Lập bảng trong báo cáo Pha A. **Dừng và hỏi** chủ dự án nếu có thay đổi không rõ nguồn (có thể là việc của người khác — tuyệt đối không revert/stash). |
| A2 | Đo baseline test | Chạy tuần tự (ghi số liệu): `make verify-dev`; `pytest tests/unit tests/serverless -x -q`; `pytest tests/security tests/resilience -q`; `go test ./...` từng module; `node tests/e2e/serverless_e2e.mjs`. Test cần Docker/GPU chạy trong cổng cô lập `aicf-sl-test-`. Ghi test nào đỏ/bỏ qua và **vì sao**. |
| A3 | Kiểm tra trùng lặp cấu trúc | Thư mục `ai-compute-fabric/ai-compute-fabric/` (lồng nhau, mtime cũ) có phải bản sao cũ không? Nếu là rác: báo chủ dự án để xóa; nếu cần: ghi vai trò. |
| A4 | Đối chiếu lại số đo Pha 7 | Chạy lại nhanh các cổng quan trọng: G-RECLAIM (≤ 2 s), `docker ps --filter label=aicf.sl=1` = 0 sau test, lease kẹt = 0. Số lệch > 20 % so với báo cáo cũ ⇒ báo cáo và điều tra trước khi sang Pha B. |
| A5 | Nhánh làm việc | Đề xuất tên nhánh `prod-readiness-2026-11` (cục bộ). Chỉ tạo khi chủ dự án đồng ý. |

**Check Pass A:** có `docs/reports/PHA_A_BASELINE.md` (hoặc mục trong báo cáo chung) với bảng A1, số liệu A2/A4; mọi test đỏ có nguyên nhân gốc được ghi; chủ dự án đã xác nhận cách xử lý phần mã dở.

---

## PHA B: HOÀN THIỆN TÍNH NĂNG ĐỂ SẴN SÀNG PRODUCTION

Mỗi hạng mục có: **Việc → Cổng nghiệm thu**. Mọi thay đổi cơ chế an toàn kèm kiểm đột biến (Phần 0 mục 3).

### B1. Tài khoản, đăng ký, phiên (đóng G1, G2, G3)
**Việc:**
1. Bỏ quyền admin ngầm ở chế độ mở trong `signup`. Production **bắt buộc** có `--admin-token`/`--roles-file` (cổng đã từ chối bind ngoài loopback khi mở — giữ và thêm test). Xoay khóa của tenant có sẵn **chỉ** qua route admin/`rotate-key` có xác thực, không bao giờ qua `signup`.
2. Thêm cờ `--allow-signup` (mặc định **tắt**). Khi bật: giới hạn tần suất theo IP + toàn cục (token bucket), tenant tạo qua signup ở trạng thái `pending` (hoặc quota CPU rất thấp, **không GPU, không Serverless**) cho tới khi admin duyệt trên `tenants.html`; ghi Audit mọi lần đăng ký/duyệt.
3. Mật khẩu: thay `hashPassword` tự chế bằng `argon2id` (hoặc `scrypt`) từ thư viện chuẩn đã có trong module/`golang.org/x/crypto` — **kiểm `go.mod` trước khi thêm phụ thuộc**; tham số theo OWASP; mật khẩu tối thiểu 12 ký tự; chặn mật khẩu phổ biến; băm có phiên bản để nâng cấp; di trú một lần cho tài khoản cũ khi đăng nhập thành công.
4. Đăng nhập: giới hạn thử sai theo (tài khoản, IP) có khóa tạm và backoff, thông báo lỗi không phân biệt "sai email" / "sai mật khẩu", thời gian xử lý đều (không lộ tài khoản tồn tại).
5. Phiên `aicf_sess_*`: hết hạn tuyệt đối + hết hạn do không hoạt động, thu hồi khi đổi mật khẩu/xoay khóa, API đăng xuất, liệt kê và thu hồi phiên; token chỉ lưu dạng băm phía server.
6. Đổi mặc định demo: `run_demo.sh` không dùng `demo-admin-token` cố định ngoài loopback; gateway **từ chối khởi động** nếu token là giá trị mặc định đã biết khi bind ngoài loopback.

**Cổng:** test Go cho từng nhánh (signup tắt → 404/403; bật → rate-limit 429; duplicate không xoay khóa; brute-force bị khóa; phiên hết hạn/thu hồi); **đột biến**: bỏ giới hạn tần suất / bỏ kiểm admin / bỏ so sánh hằng thời gian ⇒ test ĐỎ; e2e Playwright: người dùng viewer/tenant không thấy hành động admin.

### B2. Đóng gói và triển khai production (đóng G4)
**Việc:** tạo `deploy/production/` gồm:
- Đơn vị `systemd` cho `site-gateway`, `worker-guard`, các dịch vụ Python (`serverless`, `studio`, `bestofn`, `agents`), chạy bằng **user riêng không root**, `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, giới hạn tài nguyên, `Restart=on-failure`; file môi trường `0600` do root sở hữu; bí mật không nằm trong `argv`.
- Cấu hình TLS cho cổng HTTP: hoặc cờ `--http-tls-cert/--http-tls-key` trong gateway, hoặc mẫu reverse proxy (Caddy/nginx) kèm HSTS; mTLS gRPC giữ như cũ. Mẫu cấu hình tường lửa (`ufw`/`nftables`): `8099` (worker-guard) chỉ nhận loopback + IP gateway; `50052` (ggml-rpc) chỉ trên giao diện liên kết riêng — đây là **lead NEEDS-VALIDATION của lần quét cũ**.
- Script kiểm tra trước khi chạy `deploy/production/preflight.sh`: token không mặc định, quyền tệp khóa `0600`, TLS hợp lệ, cổng không lộ, Docker runtime `nvidia`, dung lượng đĩa cache, cờ an toàn mặc định.
- Quy trình nâng cấp/rollback có phiên bản (`release-manifest-v1.0.yaml` → cập nhật thành phiên bản mới), kèm chữ ký (`cosign.pub`) và `verify_sboms.sh`.
- Xoay vòng log (`logrotate`/journald), log không chứa khóa API/mật khẩu/payload người dùng.

**Cổng:** triển khai thử trên **cổng và thư mục cô lập** (không đụng dịch vụ đang chạy), `preflight.sh` đỏ khi cố tình cấu hình sai (token mặc định, `0644`, mở cổng 8099 ra LAN) và xanh khi đúng; khởi động lại từng dịch vụ không làm mất tenant/call state; diễn tập rollback ghi thời gian.

### B3. Dữ liệu, sao lưu, khôi phục (đóng G5)
**Việc:** (a) kho tenant/topology/audit/`sl_store` ghi nguyên tử + `fsync`, khóa tệp đơn tiến trình; (b) lệnh `aicf-admin backup|restore|verify` (tenants, topology, audit chain, serverless data-dir, ledger metering) kèm kiểm `VerifyLedgerFile` và chuỗi băm audit; (c) tài liệu rõ gateway hiện là **điểm lỗi đơn** và RPO/RTO mục tiêu (đề xuất RPO ≤ 15 phút, RTO ≤ 30 phút — chủ dự án chốt); (d) chính sách lưu giữ và xóa dữ liệu tenant (khi xóa tenant: dữ liệu Studio/Volume/log có xóa không — hiện thông báo là "giữ lại", cần quyết định).

**Cổng:** kịch bản `kill -9` giữa lúc ghi → khởi động lại không hỏng; backup → xóa dữ liệu → restore → tenant đăng nhập được, ledger/audit hợp lệ; test tự động trong `tests/resilience/`.

### B4. Quan sát được và header bảo mật (đóng G6, G9)
**Việc:** (a) `GET /healthz` (sống) và `/readyz` (phụ thuộc NATS/Docker/worker) cho mọi dịch vụ; (b) đặt `/metrics` sau `RoleViewer` hoặc cổng riêng; (c) bổ sung dashboard Grafana (JSON) cho các metric `sl_*` đã định nghĩa trong Phần 4 kế hoạch Serverless và quy tắc Alertmanager (route tới kênh chủ dự án chọn); (d) log có cấu trúc kèm `call_id`/`tenant`, không bí mật; (e) middleware header cho mọi phản hồi web: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, HSTS khi qua TLS, và **CSP**. CSP chặt cần bỏ script inline: chuyển script trong `*.html` sang tệp `.js` riêng hoặc dùng nonce do gateway sinh mỗi phản hồi — **chọn một hướng và áp dụng thống nhất** (khuyến nghị tệp riêng + `script-src 'self'`); (f) `Cache-Control: no-store` cho API, ETag/immutable cho tài sản tĩnh có băm.

**Cổng:** test Go kiểm header trên mọi route (kể cả 404/lỗi); `tests/e2e` mở tất cả trang với CSP bật, **0 lỗi CSP trong console**; alert thử (tạo container mồ côi giả) kích hoạt đúng.

### B5. Hoàn tất tính năng Serverless đang dở và hợp đồng API (đóng G7)
**Việc:** (a) hoàn tất/loại bỏ phần dở ở A1; (b) cập nhật OpenAPI và `test_openapi_conformance.py` cho mọi route mới (signup, auth/register, auth/login, rotate-key, `_lease` nội bộ không công bố, endpoint lint mới của Pha C); (c) rà `@endpoint`, `@task_queue.put`, `Sandbox`, CLI `configure|serve` của SDK: test đơn vị + e2e thật, tài liệu `sdk/python/README.md`, bảng mã lỗi; (d) rà quản lý `secrets` (trường `secrets` có trong `ALLOWED_REQ`): mã hóa khi lưu? ghi vào log? hiển thị ra UI? — nếu chưa mã hóa/che thì sửa; (e) giữ nguyên các hạn chế đã chốt: không Internet từ sandbox, không build Dockerfile tùy ý, không cron — **không mở rộng phạm vi**.

**Cổng:** `make verify-release-v1` xanh; OpenAPI khớp 100 % route công bố; SDK e2e xanh với Docker thật.

### B6. Kiểm thử và CI (đóng G8)
**Việc:** thêm vào CI (hoặc workflow riêng chạy trên runner tự host có Docker/GPU, nightly): job `pytest` (unit + serverless không cần GPU), `tests/security`, `tests/resilience` (bản không phá hệ thống), Playwright e2e với Chromium, kiểm đột biến bắt buộc (`tests/mutation/*`), `pip-audit`, SBOM Python (`cyclonedx-bom`), `bandit` (mức cao). Test cần GPU thật gắn nhãn riêng và chạy ở runner có GPU.

**Cổng:** pipeline xanh trên nhánh; một PR cố tình đưa lỗi (ví dụ bỏ kiểm tenant ở Volume) làm pipeline ĐỎ.

### B7. Tài liệu vận hành
**Việc:** cập nhật `docs/SRE_RUNBOOK.md` (đăng ký bị lạm dụng, khóa tài khoản, xoay khóa, backup/restore, hết hạn chứng chỉ TLS/mTLS, lỗi CSP), `docs/TESTING.md` (cách chạy từng bộ), hướng dẫn production `docs/PRODUCTION.md` (mục B2), tài liệu trình soạn thảo cho người dùng cuối (Pha C). Không tạo tài liệu thừa ngoài các tệp trên.

**Check Pass Pha B:** mọi cổng B1–B7 đạt; báo cáo `docs/reports/PHA_B.md` kèm số đo; không còn mục `[CHƯA XÁC MINH]` của G1–G9 mà chưa được xác nhận hoặc bác bỏ bằng bằng chứng.

---

## PHA C: TRÌNH SOẠN THẢO MÃ TRÊN TRÌNH DUYỆT

### C0. Vấn đề của bản hiện tại `[ĐỌC MÃ]`
Chỉ là `<textarea>`: không tô màu cú pháp, không số dòng, không tự thụt lề, không tìm/thay thế, không gập, không gợi ý; phím Tab bị chiếm cứng (**bẫy bàn phím — vi phạm WCAG 2.1.2**) và gán lại `.value` nên thường làm mất lịch sử Ctrl+Z `[CHƯA XÁC MINH trên Chromium/Firefox — đo ở C1]`; lỗi cú pháp chỉ biết sau khi bấm Deploy; chỉ một tệp; không chạy thử tại chỗ; không so sánh phiên bản; không có bản nháp tự lưu; entrypoint dò bằng regex (nhận nhầm hàm lồng/hàm phụ).

### C1. Quyết định công nghệ (ADR ngắn `docs/adr/ADR-004-web-code-editor.md`)
- **Chọn CodeMirror 6**, không chọn Monaco. Lý do `[SUY LUẬN, xác minh kích thước bằng đo thật]`: Monaco nặng hàng MB, cần web worker và dễ vướng CSP chặt, kém trên màn hình nhỏ; CodeMirror 6 theo mô-đun, vài trăm KB sau tối giản, hỗ trợ cảm ứng và truy cập tốt hơn. Nếu đo cho thấy CM6 không đạt ngân sách C9, ghi vào ADR và cân nhắc lại.
- **Không dùng CDN.** Hệ thống chạy trong LAN/ngoại tuyến và có CSP `script-src 'self'`. Cố định phiên bản, **vendor** bản đã build vào `apps/site-gateway/web/vendor/` kèm `LICENSE`, băm SHA-384 (SRI) và ghi nguồn.
- Kịch bản build một lần `tools/build_editor/` (npm + esbuild, khóa `package-lock.json`), đầu ra `web/vendor/aicf-editor.<hash>.js` + `.css`. Nằm ngoài đường chạy runtime (người dùng cuối không cần npm). CI kiểm bản vendor khớp bản build lại từ nguồn khóa (tái lập được).
- **Cải tiến lũy tiến:** nếu bundle không tải được, giữ `<textarea>` hoạt động như cũ. Tạo lớp `AicfEditor` với giao diện `getValue()/setValue()/onChange()/focus()/setReadOnly()/setDiagnostics()`; thay toàn bộ chỗ dùng `codeEditor.value` (`serverless.html` ~dòng 1215, 1228, 1241, 1258, 1430, 1666, 1670) bằng lớp này.

### C2. Chức năng lõi (làm trước, giao được ngay)
| ID | Chức năng | Ghi chú |
|---|---|---|
| C2.1 | Tô màu cú pháp Python, số dòng, tô dòng hiện tại, khớp ngoặc, tự đóng ngoặc/nháy, tự thụt lề, gập khối | `@codemirror/lang-python` |
| C2.2 | Tab thụt lề 4 khoảng trắng nhưng **không bẫy bàn phím**: `Esc` rồi `Tab` để thoát khỏi editor; Shift-Tab bỏ thụt lề; giữ nguyên lịch sử Ctrl+Z/Y | khắc phục C0 |
| C2.3 | Tìm/thay thế (Ctrl+F/H), nhảy tới dòng (Ctrl+G), chú thích dòng (Ctrl+/), nhân bản dòng, đa con trỏ | |
| C2.4 | Giao diện sáng/tối theo biến CSS của `common.css`; cỡ chữ chỉnh được; chế độ chỉ đọc khi xem phiên bản cũ | |
| C2.5 | Chuẩn hóa nội dung: CRLF→LF, loại BOM, từ chối byte NUL, chặn > 512 KiB ở client (hiển thị số byte còn lại) khớp giới hạn server | |
| C2.6 | A11y: `aria-label` tiếng Việt, thông báo lỗi qua vùng `aria-live`, điều khiển hoàn toàn bằng bàn phím, tương phản đạt WCAG AA | |
| C2.7 | Responsive: dùng được ở `375×812` (không tràn ngang, bàn phím ảo không che con trỏ) và `2560×1440` | |

### C3. Chẩn đoán lỗi ngay khi gõ (lint phía server, không chạy mã)
- **API mới:** `POST /api/serverless/v1/lint` với `{source_code, entrypoint?}` → `{ok, diagnostics:[{line, col, end_line, end_col, severity, code, message}], functions:[{name, line, params}], suggested_entrypoint}`. Cần xác thực tenant như `deploy`, có quota riêng và giới hạn tần suất.
- **Nguồn chẩn đoán:** `ast.parse` (lỗi cú pháp có vị trí chính xác từ `SyntaxError.lineno/offset/end_*`), kiểm entrypoint có tồn tại và là hàm ở cấp module, chữ ký nhận `payload`/`**inputs` đúng kiểu mẫu, và `pyflakes` (tên chưa định nghĩa, import thừa). `pyflakes` chỉ phân tích AST, không import mã người dùng.
- **BẮT BUỘC an toàn:** phân tích mã không tin cậy **trên host** (`ast.parse` có thể bị tấn công từ chối dịch vụ bằng biểu thức lồng sâu/rất lớn). Chạy lint trong **tiến trình con** có `RLIMIT_CPU`/`RLIMIT_AS`, thời gian tối đa (ví dụ 2 s), giới hạn độ sâu/kích thước; hết hạn ⇒ trả `lint_timeout`, không làm sập dịch vụ. **Cấm** dùng `jedi`, `importlib`, `exec`, `eval`, hay bất kỳ thứ gì import mã người dùng trên host.
- Phía trình duyệt: debounce ~400 ms, hủy yêu cầu cũ bằng `AbortController`, gạch chân đỏ/vàng đúng ký tự, bảng "Vấn đề" bấm để nhảy tới dòng. Hiển thị thông điệp bằng `textContent`, không bao giờ `innerHTML`.
- `deploy` dùng lại **cùng** hàm lint (một nguồn sự thật); lỗi cú pháp trả về dạng có cấu trúc thay vì chỉ chuỗi.
- Gợi ý tự động hoàn thành: tĩnh, từ danh sách API SDK `aicf` (`@app.function`, `gpu=`, `vram_mb=`, `keep_warm_s=`, `Image`, `Volume`…), built-in Python và danh từ có trong tài liệu hiện tại. Không gọi dịch vụ ngoài.

### C4. Chạy thử, nhật ký, phiên bản (biến editor thành IDE nhỏ)
- **Chạy thử tại chỗ:** nút "Chạy thử" (Ctrl+Enter) gọi cơ chế gọi hàm sẵn có với ô payload JSON; hiển thị kết quả, `stdout/stderr`, thời gian, cold/warm, VRAM; nhật ký chảy qua SSE (`calls/{id}`, đã có theo D9) hoặc thăm dò khi SSE không sẵn; nút Hủy. Hiển thị bằng `textContent`, cắt nhật ký ở ngưỡng cấu hình, tô màu ANSI an toàn (bỏ chuỗi điều khiển lạ).
- **Bản nháp tự lưu:** `localStorage` khóa theo `tenant+app`, debounce; **không** lưu khóa API/bí mật; xóa khi đăng xuất; khi phiên bản active trên server đã đổi so với lúc mở (so băm nội dung/version) ⇒ hỏi "dùng bản nháp / bản server / so sánh".
- **So sánh phiên bản:** dùng `@codemirror/merge` để diff bản đang sửa với bản active và giữa hai phiên bản trong lịch sử; nút "Khôi phục bản này vào editor"; ghi chú thay đổi khi Deploy; xác nhận trước khi deploy đè lên bản active.
- **Phím tắt:** Ctrl+S = lưu nháp, Ctrl+Shift+Enter = deploy (có hộp xác nhận), Ctrl+Enter = chạy thử, `Esc`,`Tab` = thoát focus; bảng phím tắt mở bằng `?` (đã có `openHelpDrawer`).
- **Entrypoint thông minh:** dùng `functions[]` từ lint (AST) thay cho regex; cho chọn trong danh sách hàm; cảnh báo khi hàm không nhận `payload`.

### C5. Nhiều tệp (làm sau C2–C4 nếu đạt ngân sách thời gian)
- Cây tệp/tab (`main.py` + `helpers.py` + `config.json`), tối đa 20 tệp và tổng 512 KiB, ánh xạ vào trường `files` có sẵn trong `bundle_doc`.
- **Server phải kiểm đường dẫn nghiêm ngặt** (tương tự zip-slip): chỉ tên tương đối, không `..`, không tuyệt đối, không ký tự điều khiển/NUL, không symlink, chỉ phần mở rộng cho phép (`.py .json .txt .md .yaml`), không trùng sau chuẩn hóa chữ hoa/thường và Unicode. Tệp được dựng trong sandbox tại `/tmp/aicf_files/` — test tràn đường dẫn bắt buộc.
- Panel `requirements`: ánh xạ vào `Image` với kiểm tra chỉ-wheel, băm khóa, index allowlist (đúng D4/T9 của kế hoạch Serverless). Nếu chưa đủ thời gian: **không làm nửa vời**, ghi vào "ngoài phạm vi".

### C6. Bảo mật riêng của trình soạn thảo
Không dùng `innerHTML`/`document.write` với mã, log, tên hàm, thông báo lỗi; nội dung tải lên không được diễn giải như HTML; vendor có SRI và CSP `script-src 'self'` (không `unsafe-inline`/`unsafe-eval`); lint/diff không gửi mã sang bên thứ ba; bản nháp không chứa bí mật; kiểm tra `Content-Type` khi upload; tên tệp tải về đã chuẩn hóa (đã có `replace(/[^A-Za-z0-9_.-]/g, "-")`).

### C7. Kiểm thử
- **Playwright (`tests/e2e/editor_e2e.mjs`):** gõ mã → có token tô màu; Tab thụt lề và **Ctrl+Z hoàn tác đúng**; Esc-Tab thoát focus; lỗi cú pháp ở dòng N hiện đúng dòng N và bấm bảng "Vấn đề" nhảy đúng; deploy → chạy thử → thấy kết quả; diff hiển thị đúng; upload `.py` quá lớn/NUL/BOM bị xử lý; dán payload XSS (`<img src=x onerror=…>`) vào mã, log và tên hàm ⇒ **không thực thi** và 0 lỗi CSP; viewport `375×812` và `2560×1440` (`overflowX = 0`); chạy chỉ bằng bàn phím; bundle chặn tải ⇒ rơi về `<textarea>` vẫn deploy được.
- **Python:** test endpoint lint (cú pháp, entrypoint thiếu, vị trí `col` đúng với ký tự Unicode/tab, mã 512 KiB, biểu thức lồng sâu bị `lint_timeout` mà tiến trình chính còn sống, đồng thời không có tệp/tiến trình sót); test ranh giới tenant và quota.
- **Go:** route mới đi qua `Allowed()`/RBAC và proxy xóa/gán lại `X-Aicf-*` đúng.
- **Đột biến:** bỏ giới hạn thời gian lint, bỏ kiểm đường dẫn tệp, bỏ kiểm tenant, bỏ escape khi hiển thị ⇒ test ĐỎ.

### C8. Tài liệu người dùng
Mục "Trình soạn thảo" trong tab Cloud Docs của `serverless.html` + `sdk/python/README.md`: phím tắt, cách chạy thử, giới hạn, xử lý sự cố khi bundle không tải.

### C9. Ngân sách và tiêu chí đo được
| Chỉ tiêu | Mục tiêu (đề xuất; chủ dự án chốt) |
|---|---|
| Kích thước bundle editor (nén gzip) | ≤ 300 KiB (nếu vượt: ghi ADR, cắt tính năng, không bỏ qua) |
| Thời gian tải + khởi tạo editor (LAN) | ≤ 300 ms p95 trên `pc-001` `[ĐO]` |
| Độ trễ gõ phím tệp 5.000 dòng | không có khung hình > 50 ms `[ĐO]` |
| Độ trễ chẩn đoán sau khi dừng gõ | ≤ 800 ms p95 `[ĐO]` |
| Lint tối đa thời gian CPU/yêu cầu | ≤ 2 s cứng, RSS tiến trình con ≤ 256 MiB |

**Check Pass Pha C:** C2 đầy đủ; C3 và C4 đạt cổng C7; C5 hoàn tất hoặc ghi rõ hoãn; C9 đo đạt; `editor_e2e.mjs` 100 % PASS; ADR-004 hoàn chỉnh; bản vendor tái lập được; không có lỗi CSP; báo cáo `docs/reports/PHA_C.md`.

---

## PHA D: ĐÁNH GIÁ LẠI BẢO MẬT BẰNG SKILL `claude-security` VÀ SỬA TRIỆT ĐỂ

### D-1. Công cụ và điều kiện `[ĐỌC MÃ]`
- Skill: plugin **`claude-security`** (`~/.claude/plugins/marketplaces/claude-plugins-official/plugins/claude-security/skills/claude-security/SKILL.md`). Gọi bằng `/claude-security` (hoặc skill `claude-security`). Ba việc: **Scan codebase** (toàn repo hoặc phạm vi hẹp; mức `low|medium|high`), **Scan changes** (diff nhánh/PR/commit), **Suggest patches**.
- Cách hoạt động theo tài liệu skill `[TÀI LIỆU]`: mỗi phát hiện được một nhóm verifier độc lập thử bác bỏ trước khi vào báo cáo; thư mục kết quả `CLAUDE-SECURITY-<timestamp>/`; patch là tệp `.patch` do người dùng tự áp dụng — **skill không tự commit/push/mở PR**. Patch được ghi là "verified by a panel of agents", **không phải "đã test"**: Agent phải tự viết test (D4).
- Skill thích chạy ở auto mode (`claude --permission-mode auto`); thông báo này chỉ nói một lần.
- Lần quét cũ: `~/security-audit-skill/AIComputeFabric/run-1/` (profile `quick`, commit `5cdaf75`, 6 đơn vị, phạm vi `apps, studio, agents, core`). Phát hiện cũ cần tra trạng thái ở D2:
  1. **Medium** `aicf:site-gateway:grpc-worker-id-impersonation` — gRPC `Register/Heartbeat` không buộc `CN` chứng chỉ mTLS với `WorkerId` (`server.go`).
  2. **Low** `…:sequential-lease-id-unverified-release` — `LeaseId` tuần tự `lease-N`, release/extend không kiểm chủ sở hữu (`leases.go`).
  3. **Low** `…:unrestricted-v1-subpath-forwarding` — `POST /v1/*` chuyển tiếp tùy ý tới `llama-server` (`proxy.go`).
  4. **Needs-validation** `…:worker-guard:lan-control-and-rpc-network-isolation` — cô lập mạng cổng `8099`/`50052` là việc của hạ tầng (chuyển thành `preflight.sh` ở B2).
  5. Ghi chú hardening: `/metrics` ngoài lớp admin (đã vào B4).
- Nếu Agent thực thi **không có plugin** `claude-security`: dừng và báo chủ dự án. Phương án dự phòng (chỉ khi chủ dự án chấp thuận): rà thủ công theo danh sách D2 + OWASP ASVS L2 + `gosec`, `bandit`, `semgrep`, `govulncheck`, `pip-audit`, và ghi rõ báo cáo **không phải** kết quả của skill.

### D0. Điều kiện vào Pha D
Pha B và C đạt Check Pass; `make verify-release-v1` xanh; cây làm việc sạch. **Xin phép chủ dự án commit cục bộ trên nhánh làm việc** (không push) để "Scan changes" và phục hồi dễ dàng. Không bắt đầu D khi còn thay đổi chưa commit mà chủ dự án chưa quyết.

### D1. Quét toàn bộ
1. `/claude-security` → **Scan codebase** → *toàn repository*, mức **`high`** (lần đầu; chấp nhận thời gian và token lớn — hỏi chủ dự án nếu skill yêu cầu xác nhận chi phí). Lưu thư mục `CLAUDE-SECURITY-<ts>/` (đừng commit; skill tự thêm `.gitignore`).
2. Chạy **Scan changes** cho khoảng từ `5cdaf75` đến HEAD để bảo đảm phần mã mới (serverless, SDK, web, tài khoản) được phủ riêng, vì lần quét cũ chưa phủ.
3. Quét bổ sung bằng công cụ có sẵn trong dự án: `govulncheck` (4 module), `trivy fs`, `pip-audit`, `bandit -ll`; ghi kết quả vào cùng báo cáo.
4. **Kiểm tra độ phủ:** đối chiếu `coverage-ledger` của lần quét với danh sách bắt buộc D2; mục nào chưa được phủ ⇒ chạy lại *Scan codebase* **phạm vi hẹp** vào đúng thư mục đó.

### D2. Danh sách bắt buộc phải được xem xét (giả thuyết từ việc đọc mã — chưa kết luận)
| # | Bề mặt | Cần chứng minh |
|---|---|---|
| S1 | `signup`, `auth/register`, `auth/login`, `rotate-key`, phiên `aicf_sess_*` | xác thực/ủy quyền, chiếm tài khoản, chống dò, hết hạn/thu hồi, băm mật khẩu, enumeration, thời gian so sánh |
| S2 | Cách gateway xác định `Principal` và gán `X-Aicf-Tenant/User/Role` tới dịch vụ Python | giả mạo header khi gọi thẳng cổng dịch vụ (`127.0.0.1:18110`) từ tiến trình cục bộ khác; bí mật proxy `X-Aicf-Proxy-Secret` của cầu lease |
| S3 | Web endpoint `/api/serverless/v1/web/{tenant}/{app}/{fn}` | tenant trong URL có bị tin cậy không; IDOR giữa tenant; rò dữ liệu qua tên app/fn |
| S4 | Deploy bằng `source_code`/`bundle_b64`/`files` | `cloudpickle.loads` chỉ trong container; `ast.parse` DoS trên host; đường dẫn tệp; kích thước; `compile_source_bundle` import `cloudpickle` trên host với mã đã đăng ký theo giá trị |
| S5 | Sandbox/Docker (`sandbox.py`, `_warm_runner.py`, `GpuDockerSandbox`) | thoát container, `argv` cố định, mount, `--privileged`/capability, mạng, tmpfs, người dùng không root, rò VRAM giữa tenant |
| S6 | `volumes.py` | path traversal, symlink, tenant isolation, quota, tên tệp |
| S7 | Web UI (`web/*.html`, `common.js`) | XSS (render log/tên/lỗi), lưu khóa admin ở `sessionStorage`, CSRF, clickjacking, CSP/headers |
| S8 | `/metrics`, `/healthz`, `/api/whoami`, static file server (`http.FileServer` với thư mục tương đối theo CWD) | lộ thông tin, đọc tệp ngoài `web/` |
| S9 | gRPC `FleetGateway`/`LeaseService`, mTLS `CN`↔`WorkerId`, `LeaseId` | tái xác minh 3 phát hiện cũ còn tồn tại hay đã vá |
| S10 | `worker-guard` control API (`8099`) và evict | xác thực lệnh điều khiển, DoS (evict), ranh giới mạng |
| S11 | NATS/JetStream chữ ký `SERVERLESS_EXEC` (`sl_security.py`) | replay, `nonce/exp`, quyền tệp khóa, xoay vòng |
| S12 | Secrets (`secrets` trong yêu cầu), audit log, ledger | lộ ra log/UI/metrics; chuỗi băm audit có thể bị ghi đè |
| S13 | Studio/BestOfN/Agents qua proxy | upload tài liệu, SSRF qua URL `*_url`/`gguf`/`vram-sidecar`, prompt injection → tool |
| S14 | Chuỗi cung ứng | lock file, `uv.lock`, vendor editor, image runtime, chữ ký `cosign` |

### D3. Tạo patch
`/claude-security` → **Suggest patches** cho các phát hiện đã xác nhận (chọn tất cả Critical/High/Medium trước; Low sau). Kết quả: `CLAUDE-SECURITY-<ts>/patches/{PATCHES.md, F<n>.patch, F<n>.md}`. Phát hiện bị "declined" ⇒ sửa tay ở D4 theo phân tích nguyên nhân gốc của skill.

### D4. Áp dụng và vá **triệt để** — định nghĩa "triệt để"
Với **mỗi** phát hiện (kể cả Low) phải đủ 7 điều sau, ghi vào bảng theo dõi `docs/reports/SECURITY_REMEDIATION.md`:
1. **Tái hiện trước:** viết test/PoC an toàn (trong cổng `aicf-sl-test-`) chứng minh lỗi tồn tại ở HEAD ⇒ test **ĐỎ**. Nếu không tái hiện được, ghi rõ lý do và phản biện lại phát hiện (không vá mù).
2. **Áp patch:** `git apply --check` rồi `git apply F<n>.patch`; nếu patch cũ vì cây đã đổi ⇒ chỉnh tay giữ ý nghĩa. **Không tin patch chỉ vì đã được "verified"** — đọc diff, kiểm không đổi hành vi hợp lệ, không thêm phụ thuộc vô cớ.
3. **Sửa tận gốc:** sửa lớp nguyên nhân, không chỉ vị trí báo cáo. Tìm *mọi điểm tương tự* bằng `search` (ví dụ nếu thiếu kiểm `CN`↔`WorkerId` ở `Register` thì kiểm cả `Heartbeat`, các RPC khác và mọi nơi lấy `worker_id` từ client). Không dùng danh sách chặn/regex vá víu khi có thể dùng allowlist/ràng buộc cấu trúc.
4. **Test hồi quy XANH** và nằm lâu dài trong `tests/security/` hoặc test Go cùng gói; mỗi cơ chế vá kèm **kiểm đột biến** (gỡ bản vá ⇒ ĐỎ).
5. **Không gỡ cảnh báo/ không tắt test** để qua; không thêm ngoại lệ "bỏ qua" cho scanner mà không có lý do viết ra và chủ dự án duyệt.
6. **Phòng thủ nhiều lớp khi hợp lý** (ví dụ ngoài kiểm tenant ở gateway còn kiểm ở dịch vụ Python); cập nhật tài liệu (`SRE_RUNBOOK`, mô hình đe dọa T1–T11 → bổ sung T12+).
7. **Xác minh bởi quét lại** (D5).

Ưu tiên theo mức độ: Critical/High → Medium → Low. Hạn mức tự đặt: không ngừng ở Medium; Low chỉ được **chấp nhận rủi ro** khi chủ dự án ký duyệt bằng văn bản kèm lý do và điều kiện giảm thiểu.

Quy tắc thực thi: mỗi phát hiện một thay đổi nhỏ riêng (dễ review, dễ hoàn tác); chạy bộ test liên quan sau từng bản vá và toàn bộ `make verify-release-v1` sau mỗi lô; thay đổi `worker-guard`/`leases.go`/proto giữ golden test (Phần 0 mục 9) hoặc `-update` có chủ đích và có review.

### D5. Vòng quét lại tới khi sạch
1. Sau mỗi lô vá: *Scan changes* trên diff của lô (kiểm bản vá không tạo lỗi mới).
2. Khi xong tất cả: *Scan codebase* **toàn repository, mức `high`** lần 2 trên mã cuối cùng. Lặp D3→D4→D5 cho tới khi **0 phát hiện Critical/High/Medium đã xác nhận** và mọi Low hoặc đã vá hoặc có chấp nhận rủi ro có chữ ký.
3. Giới hạn: tối đa 4 vòng; nếu vòng 3 vẫn còn phát hiện mới cùng một họ lỗi ⇒ dừng, làm phân tích nguyên nhân kiến trúc (không vá điểm) và báo chủ dự án.
4. Một bản quét sạch chỉ là "triage, không phải bằng chứng không có lỗi" (đúng như skill tuyên bố) — báo cáo phải nói đúng điều này.

### D6. Kiểm thử động bổ sung (không thay thế skill)
Chạy lại `tests/security` (T1–T11) và thêm: lạm dụng signup, brute-force đăng nhập, IDOR web endpoint/Volume giữa hai tenant, giả mạo header `X-Aicf-*` trực tiếp vào dịch vụ, XSS qua log/tên hàm trong Pha C, DoS `ast.parse`, tràn đường dẫn tệp đa tệp, replay `SERVERLESS_EXEC`, giả `WorkerId` qua gRPC mTLS bằng chứng chỉ worker khác. Trên máy cô lập: xác minh `ufw/nftables` đúng mẫu B2 cho cổng `8099`/`50052`.

### D7. Báo cáo bảo mật
`docs/SECURITY_ASSESSMENT_2026-11.md`: phạm vi + commit đã quét, danh sách phát hiện (mã định danh của skill), trạng thái từng phát hiện (đã vá / chấp nhận có ký / không khai thác được), tham chiếu test hồi quy và đột biến, kết quả quét lại, rủi ro tồn dư, và kiểm tra độ phủ đối chiếu D2. Không chép bí mật/PoC có hại vào tài liệu.

**Check Pass Pha D:** quét lần cuối không còn Critical/High/Medium đã xác nhận; mọi phát hiện có test hồi quy + đột biến; S1–S14 đều có kết luận kèm bằng chứng; `make verify-release-v1` và CI xanh; báo cáo D7 hoàn chỉnh.

---

## PHA E: CỔNG PHÁT HÀNH VÀ ROLLOUT

1. `make verify-release-v1` + toàn bộ bộ test Python/e2e/mutation xanh; số liệu SLO (warm p50/p95, G-RECLAIM, A/B LLM) **đo lại** sau mọi thay đổi (không dùng số cũ).
2. Cập nhật `release-manifest-v1.0.yaml` → bản mới, chạy `deploy/security/{scan.sh,verify_sboms.sh}`, ký artifact (`cosign`).
3. Soak ≥ 24 h trên cổng cô lập với tải hỗn hợp + editor thật (Playwright lặp) + A/B LLM: RSS, FD, container mồ côi = 0, lease kẹt = 0.
4. Rollout theo canary đã có (1 tenant nội bộ, CPU → GPU → mở rộng, ≥ 48 h/bước), tiêu chí dừng tự động giữ như Phần 4 kế hoạch Serverless; **rollback = tắt cờ + Drain** đã diễn tập lại với bản mới.
5. Bàn giao: báo cáo cuối `docs/reports/PHA_E_RELEASE.md` liệt kê mọi cổng với bằng chứng, các điểm hoãn và rủi ro tồn dư. **Không push, không triển khai lên dịch vụ đang chạy khi chưa có chấp thuận.**

---

## PHẦN F: BẢNG PHỤ THUỘC, ƯỚC LƯỢNG THỨ TỰ VÀ RỦI RO

| Pha | Phụ thuộc | Có thể song song | Ghi chú |
|---|---|---|---|
| A | — | A2 ∥ A3 | bắt buộc đầu tiên |
| B1, B2, B3 | A | B1 ∥ B2 ∥ B3 | B1 trước khi mở Pha D |
| B4 | B2 (TLS) | — | CSP ảnh hưởng mọi trang ⇒ làm **trước** C để editor được viết sẵn tương thích CSP |
| B5, B6, B7 | B1–B4 | B6 ∥ B7 | |
| C1–C2 | B4 | — | ADR rồi lõi editor |
| C3 | C1 | C3 ∥ C2 | có thể chia việc backend/frontend |
| C4 | C2, C3 | — | |
| C5 | C4 | — | tùy chọn, có thể hoãn |
| C7–C9 | C2–C5 | — | |
| D | B, C xong | — | không quét trên mã chưa ổn định |
| E | D | — | |

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Quét `high` toàn repo tốn thời gian/token, repo có thư mục lớn (`third_party`, `.aicf-work`, venv) | TB | Đặt phạm vi loại trừ thư mục không phải mã của dự án; hỏi chủ dự án trước khi chạy tốn kém |
| CSP chặt làm vỡ các trang HTML nhiều script inline | Cao | Làm B4 sớm, test e2e mọi trang, chuyển script ra tệp |
| Bundle editor vượt ngân sách hoặc lỗi giấy phép | TB | ADR-004, kiểm giấy phép MIT của CM6 và phụ thuộc, build tái lập |
| Lint trên host bị DoS | Cao | Tiến trình con + rlimit + timeout (C3), test DoS |
| Vá bảo mật làm đổi hành vi LLM đang chạy | TB | Golden test, cờ mặc định, kiểm A/B LLM sau thay đổi `leases.go`/`worker-guard` |
| Patch của skill sai ngữ cảnh | TB | Luôn tự tái hiện + review diff + test hồi quy (D4) |
| Phạm vi trôi (cron, GPU đa node, Windows) | Thấp | Giữ nguyên danh sách "ngoài phạm vi" ở kế hoạch Serverless |

---

## PHẦN G: DANH SÁCH KIỂM CUỐI CÙNG (Agent đánh dấu khi có bằng chứng)

- [x] A: baseline + bảng cây làm việc + xác nhận của chủ dự án (`docs/reports/PHA_A_BASELINE.md`, `CLEANED_A3_OK` `[ĐO]`)
- [x] B1 tài khoản/đăng ký/phiên an toàn; signup mặc định tắt; argon2id (`apps/site-gateway/tenants.go`, `security_b1_b4_test.go` PASS `[ĐO]`)
- [x] B2 `deploy/production/` + `preflight.sh` + TLS + systemd; không còn token mặc định (`deploy/production/preflight.sh` PASS `[ĐO]`)
- [x] B3 backup/restore/verify có test `kill -9` (`cmd/aicf-admin/main.go`, `tests/resilience/test_backup_restore_resilience.py` 4/4 PASS `[ĐO]`)
- [x] B4 healthz/readyz, `/metrics` có xác thực, Grafana/Alertmanager, header + CSP, 0 lỗi CSP (`security_b1_b4_test.go`, `serverless_e2e.mjs` 25/25 PASS `[ĐO]`)
- [x] B5 OpenAPI khớp, SDK e2e, `secrets` an toàn (`test_openapi_conformance.py` 3/3 PASS, `test_p4_sdk_cli.py` PASS `[ĐO]`)
- [x] B6 CI chạy Python/e2e/mutation/pip-audit/bandit; PR cố tình lỗi làm ĐỎ (`.github/workflows/ci.yml`, `Makefile` `[ĐO]`)
- [x] B7 runbook/testing/production docs (`docs/PRODUCTION.md`, `docs/SRE_RUNBOOK.md`, `docs/TESTING.md`, `docs/reports/PHA_B.md` `[ĐO]`)
- [x] C1 ADR-004 + bundle vendor tái lập + SRI (`docs/adr/ADR-004-web-code-editor.md`, `codemirror6.bundle.js` `7,116 B` gzip + SRI `sha384` `[ĐO]`)
- [x] C2 lõi editor (highlight, số dòng, Tab không bẫy, undo đúng, tìm/thay, a11y, responsive) (`editor_e2e.mjs` 16/16 PASS `[ĐO]`)
- [x] C3 API lint an toàn + chẩn đoán ngay khi gõ (`POST /v1/lint`, `RLIMIT_AS=256MiB`, `test_editor_lint_multifile.py` 5/5 PASS, `lintMs=96ms` `[ĐO]`)
- [x] C4 chạy thử + log SSE + nháp + diff phiên bản (`serverless.js`, `editor_e2e.mjs` PASS `[ĐO]`)
- [x] C5 đa tệp (`user_app.py`, `helpers.py`, `requirements.txt`) · C7 e2e 100 % (`16/16 PASS`) · C9 ngân sách đạt (`keystroke5kMs=35.2ms <= 50ms`) (`docs/reports/PHA_C.md` `[ĐO]`)
- [x] D1 quét toàn bộ + scan changes + công cụ bổ sung + kiểm độ phủ S1–S14 (`~/security-audit-skill/AIComputeFabric/run-2/`, `pip-audit` `0` CVEs `[ĐO]`)
- [x] D3/D4 mỗi phát hiện (`D-F1`..`D-F6`): tái hiện RED → vá tận gốc → hồi quy GREEN → đột biến (`M-DF1`..`M-DF6` `5/5 KILLED 100%` `[ĐO]`)
- [x] D5 quét lại sạch (0 Crit/High/Med/Low mở) · D6 test động (`330 passed`) · D7 báo cáo bảo mật (`docs/reports/SECURITY_REMEDIATION.md`, `docs/SECURITY_ASSESSMENT_2026-11.md` `[ĐO]`)
- [x] E: release gate, soak/SLO re-measurement (`warm p95 = 1.97ms`, `cold = 407.16ms`), canary rollback (`1.77ms`) & backup/restore (`11.42ms`) diễn tập, bàn giao (`docs/reports/PHA_E_RELEASE.md` `[ĐO]`)

## PHẦN H: LỆNH KHỞI ĐỘNG CHO AGENT THỰC THI

> Đọc hết `KE_HOACH_HOAN_THIEN_PRODUCTION_EDITOR_VA_BAO_MAT_AICF.md` và `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md` (đặc biệt Phần 0, Phần 4, mục rủi ro). Bắt đầu từ **Pha A**; không sang pha sau khi pha trước chưa đạt Check Pass. Báo cáo tiếng Việt, mọi con số kèm nhãn bằng chứng. Không restart dịch vụ đang chạy, không `git push`, không commit khi chưa được cho phép, không `pkill -f`. Pha D dùng skill `claude-security` (`/claude-security`: Scan codebase → Suggest patches → áp dụng → test hồi quy + đột biến → Scan lại); nếu plugin không có, dừng và hỏi chủ dự án.
