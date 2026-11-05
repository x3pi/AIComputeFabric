# Báo cáo Khắc phục & Kiểm định Bảo mật Toàn diện — Pha D (`SECURITY_REMEDIATION.md`)

- **Thời điểm thực hiện:** `2026-11-05` `[ĐO]`
- **Commit cơ sở trước khi vá (D0):** `ad98cf81ecd73878131924ca93964dbe7ed63f3c` (`ad98cf8`) `[ĐO]`
- **Hồ sơ kiểm toán `.agents/skills/security-audit`:** [`/home/pi/security-audit-skill/AIComputeFabric/run-2/`](file:///home/pi/security-audit-skill/AIComputeFabric/run-2/REPORT.md) (`PASS: 5 coverage units valid`, `PASS: 6 findings valid`) `[ĐO]`
- **Quy trình khắc phục 7 bước (D4):** `PoC RED -> Root-cause Fix -> PoC GREEN -> Regression Test -> Mutation Test (100% KILLED) -> Rescan` `[ĐO]`

---

## 1. Tổng hợp 6 Phát hiện Bảo mật (`D-F1` .. `D-F6`) & Kết quả Khắc phục

| Mã | Mức độ | Fingerprint | Bề mặt | CWE / OWASP ASVS L2 | PoC RED | Fix & PoC GREEN | Mutant Kill | Trạng thái |
|---|---|---|---|---|---|---|---|---|
| **D-F1** | **MEDIUM** | `aicf:serverless:web-endpoint-cross-tenant-idor-and-oracle` | `S3` (`serverless/service.py:invoke_web`) | `CWE-639` / `CWE-203` (ASVS V4.2.1) | `404 != 403` & cross-tenant invoke `[ĐO]` | Kiểm tra `caller_tenant == target_tenant` TRƯỚC `resolve_function` (`PASS`) `[ĐO]` | `M-DF1`: **KILLED** `[ĐO]` | **FIXED** |
| **D-F2** | **MEDIUM** | `aicf:serverless:tenant-metrics-exposure-and-reclaim-poisoning` | `S2 / S8 / S12` (`serverless/app.py`, `admin.go`) | `CWE-285` / `CWE-200` (ASVS V4.1.3) | `GET /v1/metrics` & `POST /v1/internal/reclaim-metric` trả `200` cho `tenant` `[ĐO]` | Chặn `role == "tenant"` với `403`, strip `X-Aicf-Target-Tenant` (`PASS`) `[ĐO]` | `M-DF2`: **KILLED** `[ĐO]` | **FIXED** |
| **D-F3** | **LOW** | `aicf:site-gateway:cwd-relative-static-fileserver` | `S8` (`apps/site-gateway/server.go:HTTPHandler`) | `CWE-427` / `CWE-59` (ASVS V14.5.3) | Phục vụ `./web/index.html` từ CWD thay vì `embed.FS` `[ĐO]` | Chỉ phục vụ từ `fs.Sub(webFS, "web")` (`PASS`) `[ĐO]` | `M-DF3`: **KILLED** `[ĐO]` | **FIXED** |
| **D-F4** | **LOW** | `aicf:site-gateway:mtls-empty-cn-bypass` | `S9` (`apps/site-gateway/server.go:verifyWorkerPeer`) | `CWE-295` (ASVS V9.2.1) | Cert có `CommonName == ""` trả `<nil>` thay vì `PermissionDenied` `[ĐO]` | Chặn `workerID == "" \|\| cn == "" \|\| cn != workerID` (`PASS`) `[ĐO]` | `M-DF4`: **KILLED** `[ĐO]` | **FIXED** |
| **D-F5** | **MEDIUM** | `aicf:supply-chain:pypdf-cve-2026-119203-to-119209` | `S14` (`pyproject.toml`, `.venv`) | `CVE-2026-119203..119209` (ASVS V14.2.1) | `pypdf 6.21.2` có 7 CVE trong `pip-audit` `[ĐO]` | Nâng cấp `pypdf==6.21.4` (`0` CVE trong `pip-audit`) `[ĐO]` | `test_df5`: **PASS** `[ĐO]` | **FIXED** |
| **D-F6** | **LOW** | `aicf:agents:warm-runner-predictable-tmp-dir` | `S5` (`agents/_warm_runner.py:_materialize_files`) | `CWE-377` / Ruff `S108` (ASVS V12.3.2) | Dùng đường dẫn cố định `/tmp/aicf_files` không `O_NOFOLLOW` `[ĐO]` | Cô lập `tempfile.mkdtemp(prefix="aicf_files_")` + `O_NOFOLLOW` (`0600`) (`PASS`) `[ĐO]` | `M-DF6`: **KILLED** `[ĐO]` | **FIXED** |

> **Xác minh 3 phát hiện từ `run-1` trước đó (`D1`):**
> - `aicf:site-gateway:grpc-worker-id-impersonation`: Đã vá trong [`apps/site-gateway/server.go:35-65`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/server.go#L35-L65) và được gia cố thêm bởi `D-F4` (`TestMTLSVerifiesWorkerIDMatchesCertificateCN` + `TestPhaD_DF4` PASS) `[ĐO]`.
> - `aicf:site-gateway:sequential-lease-id-unverified-release`: Đã vá trong [`apps/site-gateway/leases.go:130`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/leases.go#L130) bằng `newSecret(16)` (`TestLeaseIDsAreCryptographicallyRandomCapabilities` PASS) `[ĐO]`.
> - `aicf:site-gateway:unrestricted-v1-subpath-forwarding`: Đã vá trong [`apps/site-gateway/proxy.go:85-95`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/proxy.go#L85-L95) (`TestInferenceRejectsUnsupportedV1Subpaths` PASS) `[ĐO]`.

---

## 2. Chi tiết Quy trình 7 Bước cho Từng Phát hiện (`D-F1` .. `D-F6`)

### 2.1. `D-F1` — Cross-Tenant IDOR & Enumeration Oracle trên Web Endpoint (`S3`)
- **Bước 1 (PoC RED):** `test_df1_web_endpoint_blocks_cross_tenant_idor_and_enumeration_oracle` trong [`tests/security/test_pha_d_remediation.py`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/tests/security/test_pha_d_remediation.py#L54-L80) thất bại trên `ad98cf8`:
  `AssertionError: assert 404 == 403 where 404 = ServiceError("app 'non-existent-app' is not deployed for tenant 't_victim'").status` `[ĐO]`.
- **Bước 2 (Root-cause Fix):** Trong [`serverless/service.py:559-565`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/serverless/service.py#L559-L565), kiểm tra `if caller_role not in ("tenant", "admin", "operator") or (caller_role == "tenant" and caller_tenant != target_tenant): raise ServiceError(403, "forbidden", ...)` ngay đầu `invoke_web`, trước khi gọi `self.deployments.resolve_function(...)`. Đồng thời sửa `stop_app` dùng `evict_reason = "admin" if role in ("admin", "operator") else "idle"` `[ĐỌC MÃ]`.
- **Bước 3 & 4 (PoC GREEN & Regression):** `pytest tests/security/test_pha_d_remediation.py -k test_df1` → **PASSED**; `pytest tests/serverless/test_p5_http_volumes_metering.py` → **PASSED** `[ĐO]`.
- **Bước 5 (Mutation Test `M-DF1`):** Đổi điều kiện chặn đầu hàm `invoke_web` thành `if False:` → `test_df1` **FAILED (`exit=1`, `M-DF1 KILLED`)** → khôi phục mã nguồn `[ĐO]`.

### 2.2. `D-F2` — Lộ Metrics Đa Tenant & Đầu độc Reclaim Metric (`S2 / S8 / S12`)
- **Bước 1 (PoC RED):** `test_df2_tenant_cannot_scrape_global_metrics_or_poison_reclaim_metrics` trong [`tests/security/test_pha_d_remediation.py`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/tests/security/test_pha_d_remediation.py#L82-L133) thất bại trên `ad98cf8`: `Failed: DID NOT RAISE HTTPError` khi `X-Aicf-Role: tenant` gọi `GET /v1/metrics` `[ĐO]`.
- **Bước 2 (Root-cause Fix):**
  - Trong [`serverless/app.py:105-108`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/serverless/app.py#L105-L108): trả `403 forbidden` khi `role == "tenant"` gọi `GET /v1/metrics`.
  - Trong [`serverless/app.py:246-248`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/serverless/app.py#L246-L248): trả `403 forbidden` khi `role in ("tenant", "viewer")` gọi `POST /v1/internal/reclaim-metric`.
  - Trong [`serverless/app.py:356-358`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/serverless/app.py#L356-L358): yêu cầu `role in ("tenant", "admin", "operator")` trên `DELETE /v1/calls/*`.
  - Trong [`apps/site-gateway/admin.go:556`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/admin.go#L556): xóa header `X-Aicf-Target-Tenant` từ phía client `[ĐỌC MÃ]`.
- **Bước 3 & 4 (PoC GREEN & Regression):** `pytest tests/security/test_pha_d_remediation.py -k test_df2` → **PASSED** `[ĐO]`.
- **Bước 5 (Mutation Test `M-DF2`):** Vô hiệu hóa nhánh kiểm tra `if role == "tenant":` trên `/v1/metrics` → `test_df2` **FAILED (`exit=1`, `M-DF2 KILLED`)** → khôi phục mã nguồn `[ĐO]`.

### 2.3. `D-F3` — Phục vụ Tệp Tĩnh Phụ thuộc Thư mục Làm việc CWD (`S8`)
- **Bước 1 (PoC RED):** `TestPhaD_DF3_StaticFileServerIgnoresUntrustedCWDWebFolder` trong [`apps/site-gateway/security_pha_d_test.go`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/security_pha_d_test.go#L22-L55) thất bại trên `ad98cf8`:
  `D-F3 VULNERABILITY: HTTPHandler served untrusted CWD ./web/index.html instead of embedded webFS!` `[ĐO]`.
- **Bước 2 (Root-cause Fix):** Trong [`apps/site-gateway/server.go:141-143`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/server.go#L141-L143), loại bỏ `os.Stat` / `http.Dir` theo CWD và luôn phục vụ từ `fs.Sub(webFS, "web")` (`embed.FS` nhúng lúc biên dịch) `[ĐỌC MÃ]`.
- **Bước 3 & 4 (PoC GREEN & Regression):** `go test -v -run TestPhaD_DF3 ./apps/site-gateway` → **PASS** `[ĐO]`.
- **Bước 5 (Mutation Test `M-DF3`):** Đổi về `http.Dir("web")` → `TestPhaD_DF3` **FAILED (`exit=1`, `M-DF3 KILLED`)** → khôi phục mã nguồn `[ĐO]`.

### 2.4. `D-F4` — Bỏ qua Kiểm tra `CommonName` Rỗng trong mTLS Worker (`S9`)
- **Bước 1 (PoC RED):** `TestPhaD_DF4_VerifyWorkerPeerRejectsEmptyCommonNameAndEmptyWorkerID` trong [`apps/site-gateway/security_pha_d_test.go`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/security_pha_d_test.go#L57-L83) thất bại trên `ad98cf8`:
  `D-F4 VULNERABILITY: expected PermissionDenied for empty certificate CommonName, got <nil>` `[ĐO]`.
- **Bước 2 (Root-cause Fix):** Trong [`apps/site-gateway/server.go:44-47`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/server.go#L44-L47), sửa điều kiện thành `if workerID == "" || cn == "" || cn != workerID` trả `codes.PermissionDenied` `[ĐỌC MÃ]`.
- **Bước 3 & 4 (PoC GREEN & Regression):** `go test -v -run TestPhaD_DF4 ./apps/site-gateway` → **PASS** `[ĐO]`.
- **Bước 5 (Mutation Test `M-DF4`):** Đổi điều kiện về `if cn != "" && cn != workerID` → `TestPhaD_DF4` **FAILED (`exit=1`, `M-DF4 KILLED`)** → khôi phục mã nguồn `[ĐO]`.

### 2.5. `D-F5` — 7 Lỗ hổng CVE trong `pypdf==6.21.2` (`S14`)
- **Bước 1 (PoC RED):** `.venv/bin/python -m pip_audit --local` phát hiện 7 CVE (`CVE-2026-119203` .. `CVE-2026-119209`) trong `pypdf 6.21.2`; `test_df5_pypdf_version_has_zero_known_cves` **FAILED** `[ĐO]`.
- **Bước 2 (Root-cause Fix):** Nâng cấp `pypdf==6.21.4` trong [`pyproject.toml:21`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/pyproject.toml#L21) và `.venv` `[ĐO]`.
- **Bước 3 & 4 (PoC GREEN & Rescan):** `test_df5` → **PASSED**; `.venv/bin/python -m pip_audit --local` → **`No known vulnerabilities found` (`0` CVEs)** `[ĐO]`.

### 2.6. `D-F6` — Đường dẫn Tạm Cố định trong `_warm_runner._materialize_files` (`S5`)
- **Bước 1 (PoC RED):** `test_df6_warm_runner_uses_isolated_mkdtemp_instead_of_fixed_tmp_dir` **FAILED** trên `ad98cf8`; Ruff `S108` cảnh báo `root = "/tmp/aicf_files"` `[ĐO]`.
- **Bước 2 (Root-cause Fix):** Trong [`agents/_warm_runner.py:469-498`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/agents/_warm_runner.py#L469-L498), tạo thư mục cô lập riêng cho từng bundle bằng `tempfile.mkdtemp(prefix="aicf_files_")`, từ chối symlink và ghi tệp với cờ `os.O_NOFOLLOW` cùng quyền `0600` `[ĐỌC MÃ]`.
- **Bước 3 & 4 (PoC GREEN & Regression):** `test_df6` → **PASSED**; `.venv/bin/python -m ruff check --select S108 agents/_warm_runner.py` → **`All checks passed!`**; `pytest tests/serverless/test_p4_sdk_cli.py` → **PASSED** `[ĐO]`.
- **Bước 5 (Mutation Test `M-DF6`):** Đổi `tempfile.mkdtemp(prefix="aicf_files_")` về `"/tmp/aicf_files"` → `test_df6` **FAILED (`exit=1`, `M-DF6 KILLED`)** → khôi phục mã nguồn `[ĐO]`.

---

## 3. Ma trận Kiểm định 14 Bề mặt Bắt buộc (`S1` .. `S14`)

| Bề mặt | Thành phần | Biện pháp Bảo vệ & Kiểm chứng Tự động | Kết quả |
|---|---|---|---|
| **S1** | Web IDE & Frontend (`serverless.js`, `codemirror6.bundle.js`, `admin.js`, `studio.js`) | `esc()` escape toàn bộ dữ liệu động trong `innerHTML`; CM6 dùng `textContent` / DOM API thuần; CSP chặn inline script & SRI `sha384-qgZUSn0j6z/clXjzpVyhbHPlSHdYNrGAhxm1pi6J9vqU5JhxvyaR7ZkXUXID11Kg` | **PASS** (`editor_e2e.mjs` + `serverless_e2e.mjs`) `[ĐO]` |
| **S2** | Auth & Session (`tenants.go`, `rbac.go`, `admin.go`, `app.py`) | Argon2id (`t=3, m=64MiB, p=4`), brute-force lockout, `subtle.ConstantTimeCompare`, xóa `X-Aicf-*` từ client, chặn `tenant` đọc `/v1/metrics` (`D-F2`) | **PASS** (`security_b1_b4_test.go` + `test_df2`) `[ĐO]` |
| **S3** | Serverless Deploy / Invoke / Web Endpoints (`service.py`, `deployments.py`) | JSON-only trên `/v1/web/*` (`T10`), kiểm tra quyền sở hữu tenant trước `resolve_function` (`D-F1`), giới hạn `512 KiB`, từ chối NUL byte & path traversal | **PASS** (`test_df1` + `test_serverless_threats.py`) `[ĐO]` |
| **S4** | AST Linter (`POST /v1/lint`, `lint_python_source`) | Chạy subprocess `python3 -I -S` với `RLIMIT_AS=256MiB`, `RLIMIT_CPU=2s`, cô lập `env={}`, không bao giờ fallback sang `ast.parse` trên tiến trình chủ | **PASS** (`test_editor_lint_multifile.py` 5/5) `[ĐO]` |
| **S5** | Sandbox & Container (`agents/sandbox.py`, `_warm_runner.py`) | `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--network=none`, `--pids-limit=256`, `mkdtemp(prefix="aicf_files_")` + `O_NOFOLLOW` (`D-F6`) | **PASS** (`test_agent_tool_sandbox_and_injection.py` + `test_df6`) `[ĐO]` |
| **S6** | Serialization (`cloudpickle`, HMAC-SHA256) | `cloudpickle.loads` chỉ tồn tại duy nhất trong `agents/_warm_runner.py` (`T2`); mọi gói tác vụ ký HMAC-SHA256 + chống phát lại (`T3`) | **PASS** (`test_t2`, `test_t3`) `[ĐO]` |
| **S7** | Volumes & Model Cache (`serverless/volumes.py`) | `VolumeStore.resolve()` chuẩn hóa đường dẫn, chặn `..` và từ chối mọi symlink (`is_symlink()`); `put_admin` chỉ cho phép `role == "admin"` | **PASS** (`test_t4`, `test_p5_http_volumes_metering.py`) `[ĐO]` |
| **S8** | Reverse Proxy & Header Spoofing (`admin.go`, `proxy.go`, `server.go`) | Strip `X-Aicf-User/Role/Tenant/Target-Tenant/Proxy-Secret`; `MaxBytesReader` `10 MiB`; allowlist `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`; `embed.FS` tĩnh (`D-F3`) | **PASS** (`TestPhaD_DF3`, `TestServerlessClientCannotForgeTenantOrSecret`) `[ĐO]` |
| **S9** | gRPC Control Plane, mTLS & NATS (`server.go`, `mtls.go`, `task_bus.py`) | TLS 1.3 `RequireAndVerifyClientCert`, ràng buộc `cn == workerID` và chặn `cn == ""` (`D-F4`), NATS mTLS + ACL theo subject | **PASS** (`TestPhaD_DF4`, `test_task_bus_security.py`) `[ĐO]` |
| **S10** | Secrets & Audit Log (`quota.py`, `audit.go`, `ledger.py`) | Secret chỉ truyền qua `docker run -e` từ memory, không bao giờ ghi ra audit/metrics/overview; Audit Log băm chuỗi SHA-256 (`prev_hash -> hash`) | **PASS** (`test_secret_injected_into_container_and_never_logged_or_audited`, `TestAuditChainDetectsEditDeleteAndReorder`) `[ĐO]` |
| **S11** | Studio & Best-of-N (`studio/app.py`, `bestofn/service.py`) | Bắt buộc `X-Aicf-Proxy-Secret`, cô lập dữ liệu theo tenant, chống SSRF/path traversal khi nạp tài liệu | **PASS** (`TestStudioProxyRolesIdentityHeadersAndBodyCap`) `[ĐO]` |
| **S12** | DoS, Quota & Concurrency (`quota.py`, `service.py`, `app.py`) | Giới hạn `max_concurrency`, `max_vram_mb`, `queue_Wait`, chặn `tenant` đầu độc `/v1/internal/reclaim-metric` (`D-F2`) | **PASS** (`test_t6_t7`, `test_df2`) `[ĐO]` |
| **S13** | Python SDK & CLI (`sdk/python/aicf/cli.py`, `client.py`) | Lưu token với quyền `0o600`, kiểm tra `SAFE_IDENT` và truyền mảng đối số cố định (`shell=False`) | **PASS** (`test_t8_t9`, `test_t10_t11`) `[ĐO]` |
| **S14** | Supply Chain & Dependencies (`pyproject.toml`, `go.mod`, SBOM) | `pypdf==6.21.4` (`D-F5`), `.venv/bin/python -m pip_audit --local` báo `0` lỗ hổng, `pinned --hash=sha256:...` cho image requirements | **PASS** (`pip_audit` 0 CVEs, `test_df5`) `[ĐO]` |

