# BÁO CÁO PHA A — CHỐT NỀN (BASELINE)

- **Ngày thực hiện:** `2026-11-05` `[ĐO]`
- **Tham chiếu kế hoạch:** `KE_HOACH_HOAN_THIEN_PRODUCTION_EDITOR_VA_BAO_MAT_AICF.md` (Pha A: A1–A5) và `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md` (Phần 0, Phần 4, Sổ rủi ro).
- **Trạng thái cổng nghiệm thu:** **ĐẠT CHECK PASS A** (`0` test đỏ sau khi xử lý 2 lỗi hồi quy nhỏ ở mức lớp/test; `0` container mồ côi; `0` lease kẹt; dịch vụ đang chạy trên `:18080`, `:18110`, `:8099` được giữ nguyên không gián đoạn) `[ĐO]`.

---

## 1. A1 — Kiểm kê cây làm việc & các commit gần nhất (`[ĐO]`, `[ĐỌC MÃ]`)

### 1.1. Giải trình về 8 tệp chưa commit được nêu tại mục 1.1 của Kế hoạch

Tại thời điểm soạn thảo `KE_HOACH_HOAN_THIEN_PRODUCTION_EDITOR_VA_BAO_MAT_AICF.md`, cây làm việc có 8 tệp đang sửa dở (`agents/_warm_runner.py`, `agents/sandbox.py`, `apps/site-gateway/admin.go`, `apps/site-gateway/tenants.go`, `serverless/{app,deployments,service,volumes}.py`). Đó chính là các thay đổi thuộc phiên làm việc ngay trước đó (bổ sung hệ thống tài khoản Cloud Auth `register/login/logout/rotate-key` và thực thi kernel CUDA PTX thật trên GPU `aicf_gpu` kèm gắn kết `/mnt/volumes`) và đã được hoàn tất, kiểm thử xanh, đóng gói vào commit **`16b6357`** (`2026-11-05 17:18:05 +0700`) `[ĐO]`.

### 1.2. Bảng kiểm kê các commit trên nhánh `main` sau `f8b4992` (`[ĐỌC MÃ]`)

| Commit | Ngày giờ | Nội dung tóm tắt | Các tệp chính thay đổi | Quyết định (A1) |
|---|---|---|---|---|
| `7d13db5` | `2026-11-05` | Giao diện Developer Login/Signup, Cloud Python IDE (`.py` upload), tách chế độ User vs Admin, trung tâm tài liệu Cloud Docs 4 tab | `apps/site-gateway/web/serverless.html`, `tests/e2e/serverless_e2e.mjs` | **Giữ nguyên** — sẽ được nâng cấp lên CodeMirror 6 + CSP `'self'` ở Pha B4 & Pha C |
| `98a4e32` | `2026-11-05` | Bổ sung giao diện SDK tương thích `beam-cloud/beta9` (`@aicf.endpoint`, `@aicf.task_queue` + `.put()`, `aicf.Sandbox`, `aicf configure`, `aicf serve`) | `sdk/python/aicf/{__init__,app,cli,sandbox}.py`, `tests/serverless/test_p4_sdk.py` | **Giữ nguyên** — giữ tương thích 100% API SDK |
| `16b6357` | `2026-11-05 17:18` | Cloud Account Auth (`POST /api/serverless/v1/auth/{register,login,logout,rotate-key}`) + mô-đun `aicf_gpu` chạy CUDA Driver API (`libcuda.so.1`) & kernel PTX (`saxpy_kernel`, `matmul_kernel`) + mount `/mnt/volumes` trong GPU container | `agents/_warm_runner.py`, `agents/sandbox.py`, `apps/site-gateway/{admin,tenants}.go`, `serverless/{app,deployments,service,volumes}.py`, `apps/site-gateway/web/serverless.html`, `tests/serverless/test_p5_http_volumes_metering.py`, `tests/e2e/serverless_e2e.mjs` | **Giữ nguyên** — các lỗ hổng mở của cơ chế Auth/Session vừa thêm (G1, G2, G3) sẽ được siết chuẩn production ở **Pha B1** |

### 1.3. Trạng thái `git status` & `git diff --stat` sau khi vá 2 lỗi baseline A2 (`[ĐO]`)

| Tệp trong `ai-compute-fabric/` | Thay đổi | Mục đích | Kết quả quyết định |
|---|---|---|---|
| `agents/sandbox.py` | `+2` dòng | Khai báo thuộc tính mức lớp `volumes_root: Optional[Path] = None` và `model_cache_root: Optional[Path] = None` trên `GpuDockerSandbox` để các lớp giả lập trong unit test (`_Sb` tại `test_core_unit.py` ghi đè `__init__` không gọi `super().__init__()`) không gặp `AttributeError` | **Giữ lại** (sửa lỗi hồi quy từ `16b6357`) |
| `tests/serverless/test_gpu_real.py` | `+1/-1` dòng | Chờ `not rig.mon.last` ở cuối mỗi vòng lặp trong `test_hung_evict_endpoint_falls_back_to_docker_kill_within_two_seconds` để tránh đọc nhầm số đo VRAM cũ của vòng trước trước khi `GpuMonitor.tick()` kịp lấy mẫu lại | **Giữ lại** (loại bỏ race condition trong test GPU thật) |
| `docs/reports/PHA_A_BASELINE.md` | Tệp mới | Báo cáo chốt nền Pha A | **Giữ lại** |

---

## 2. A2 — Kết quả đo Baseline Test tuần tự trên cổng/container cô lập (`[ĐO]`)

Mọi lệnh kiểm thử được chạy tuần tự, sử dụng cổng cô lập và container tiền tố `aicf-sl-test-`, không chạm vào các dịch vụ đang chạy trên `:18080`, `:18110`, `:8099`.

| # | Bộ kiểm thử / Lệnh thực thi | Kết quả lần 1 | Nguyên nhân gốc của test đỏ (nếu có) & Khắc phục | Kết quả sau khắc phục |
|---|---|---|---|---|
| 1a | `make test-go` (`vram-estimator`, `worker-guard`, `site-gateway`, `global-control-plane`, `contracts/gen/go` với `-race -cover`) | **PASS 100%** `[ĐO]` | Không có lỗi. Coverage: `vram-estimator` `93.3%`, `worker-guard` `60.0%`, `site-gateway` `74.4%`, `global-control-plane` `90.9%` `[ĐO]`. | **PASS 100%** `[ĐO]` |
| 1b | `make proto build-cross lint-go lint-py` (Linux/Windows `amd64`, `golangci-lint`, `ruff`, `mypy`) | **PASS 100%** `[ĐO]` | `golangci-lint`: `0 issues` (4 module Go); `ruff`: `All checks passed!`; `mypy`: `Success: no issues found in 84 source files` `[ĐO]`. | **PASS 100%** `[ĐO]` |
| 2 | `.venv/bin/pytest tests/unit tests/serverless -q` | `6 failed` (`test_core_unit.py`), `1 failed` (`test_gpu_real.py`), `588 passed` `[ĐO]` | **(1)** `test_core_unit.py`: lớp giả lập `_Sb(GpuDockerSandbox)` ghi đè `__init__` không gọi `super().__init__()`, dẫn đến thiếu `self.volumes_root` vừa thêm ở `16b6357` (`agents/sandbox.py:238`). Đã thêm giá trị mặc định mức lớp `volumes_root = None`, `model_cache_root = None`.<br>**(2)** `test_gpu_real.py::test_hung_evict_endpoint_falls_back_to_docker_kill_within_two_seconds`: khi `docker kill` chạy trực tiếp từ `worker-guard` (do `/v1/evict` giả lập bị treo), `rig.mon.last` chưa kịp xóa trước khi vòng lặp `i+1` kiểm tra `sum(rig.mon.last.values()) >= 2048`, khiến vòng `i+1` gọi `_guard_once` trước khi container mới kịp cấp phát VRAM. Đã bổ sung `and not rig.mon.last` vào điều kiện đợi dọn dẹp cuối mỗi vòng. | **PASS 100%** (`53/53 passed` trong `test_core_unit.py` + `test_gpu_real.py` trong `8.26 s`) `[ĐO]` |
| 3 | `.venv/bin/pytest tests/security tests/resilience -q` | **PASS 100%** `[ĐO]` | Không có lỗi (`test_serverless_threats.py` T1–T11 và `test_serverless_chaos.py` đều XANH 100%) `[ĐO]`. | **PASS 100%** `[ĐO]` |
| 4 | `node tests/e2e/serverless_e2e.mjs --gateway /tmp/aicf-site-gateway` (Playwright Chromium headless, cổng cô lập `:18280`/`:18290`) | **PASS 100% (`24/24`)** `[ĐO]` | Không có lỗi (`0` JS console error, kiểm tra đầy đủ RBAC Viewer/Admin, Force Evict, Drain/Disable, Cloud Auth Register/Logout/Login, Python IDE, Volume SHA-256, Developer Docs, responsive `2560x1440` & `375x812`) `[ĐO]`. | **PASS 100% (`24/24`)** `[ĐO]` |

---

## 3. A3 — Kiểm tra thư mục trùng lặp `ai-compute-fabric/ai-compute-fabric/` (`[ĐO]`)

Đã kiểm tra chi tiết bằng `find ai-compute-fabric/ -ls` và `git status -s -- ai-compute-fabric/`:

- **Nội dung bên trong:** Chỉ chứa duy nhất 1 tệp nhị phân cũ:
  - `ai-compute-fabric/ai-compute-fabric/.demo/bin/site-gateway` (`19,180,592 bytes`, ngày sửa `Oct 26 16:01`) `[ĐO]`.
- **Trạng thái Git:** Không được theo dõi bởi Git (`untracked` / bị bỏ qua bởi quy tắc `.demo/` trong `.gitignore`) `[ĐO]`.
- **Nguyên nhân hình thành:** Do một lệnh build thử nghiệm từ ngày `26/10` chạy bên trong thư mục `ai-compute-fabric/` với đường dẫn tương đối `ai-compute-fabric/.demo/bin/site-gateway`.
- **Đề xuất:** Đây là thư mục rác cũ không còn sử dụng. **Xin ý kiến chủ dự án cho phép xóa `ai-compute-fabric/ai-compute-fabric/`** để làm sạch cây thư mục.

---

## 4. A4 — Đối chiếu lại số đo Pha 7 & Trạng thái tài nguyên thực tế (`[ĐO]`)

| Chỉ số / Cổng an toàn | Mục tiêu SLO | Giá trị đo thực tế (`[ĐO]`) | Nguồn bằng chứng |
|---|---|---|---|
| **G-RECLAIM (`detect_to_freed_ms`)** (30 vòng, 2 container GPU × 2048 MiB trên RTX 3060) | `p99 < 2000 ms`, `engine_stopped = 0` | `p50 = 295.3 ms`, `p95 = 353.2 ms`, **`p99 = 355.7 ms`** (`max = 355.7 ms`), `engine_stopped = 0` `[ĐO]` | `benchmarks/reports/p_serverless_3_reclaim.json` |
| **G-RECLAIM Fallback (`docker-kill` khi `/v1/evict` treo)** (5 vòng liên tiếp trên RTX 3060) | `< 2000 ms`, `engine_stops = 0` | **5/5 vòng PASS**, `max(tick_ms) < 2000 ms`, `engine_stops = 0` `[ĐO]` | `tests/serverless/test_gpu_real.py::test_hung_evict_endpoint_falls_back_to_docker_kill_within_two_seconds` |
| **Overhead đường nóng (Warm Call qua Gateway)** | `p50 <= 20 ms`, `p95 <= 60 ms` | **`p50 = 2.19 ms`**, **`p95 = 4.06 ms`**, `p99 = 6.37 ms` (`n = 150`) `[ĐO]` | `benchmarks/reports/p_serverless_2_slo.json` |
| **A/B LLM (`Qwen3-4B-Q4_K_M` trên RTX 3060)** khi chạy đồng thời Serverless GPU (`1024 MiB`, 72 lượt gọi ấm) | Suy giảm `<= 5%` | `tok/s p50`: `101.43 → 101.45` (`-0.02%`); `TTFT p95`: `47.15 ms → 42.40 ms` (`-10.07%`) — **ĐẠT** `[ĐO]` | `benchmarks/reports/p_serverless_3_ab_llm.json` |
| **Quy mô `.map(200)` (`K = 10, 25, 50` worker)** & 20% worker rớt mạng | `200/200` hoàn tất, `0` rò lease | `K=10`: `2657.5 ms`; `K=25`: `2981.4 ms`; `K=50`: `3547.4 ms`; rớt 10/50 worker: `200/200` trong `1626.0 ms` `[ĐO]` | `benchmarks/reports/p_serverless_7_scale_e2e.json` |
| **Diễn tập Rollback khẩn cấp (`Drain` + tắt `serverless_enabled`)** | `< 5 phút`, `0` lease kẹt | Thu hồi 12 container ấm và 12 GPU lease về `0` trong **`11.79 ms`** `[ĐO]` | `benchmarks/reports/p_serverless_7_scale_e2e.json` |
| **Container mồ côi sau toàn bộ bài test A2** (`docker ps -a --filter label=aicf.sl=1`) | `= 0` | **`0` container** (`ORPHANS=0`) `[ĐO]` | Đo trực tiếp sau khi chạy `test_gpu_real.py` và `serverless_e2e.mjs` |
| **Lease kẹt trên Gateway sau test** (`GET /api/serverless/v1/_lease/active`) | `= 0` | **`{"leases":[]}` (`0` lease)** `[ĐO]` | Đo trực tiếp trên `http://127.0.0.1:18080/api/serverless/v1/_lease/active` |

---

## 5. A5 — Đề xuất nhánh làm việc & Xin xác nhận chuyển sang Pha B

Theo mục **A5** của `KE_HOACH_HOAN_THIEN_PRODUCTION_EDITOR_VA_BAO_MAT_AICF.md`, xin chủ dự án xác nhận 3 điểm trước khi thực thi **Pha B (Hoàn thiện tính năng Production B1–B7)**:

1. **Xóa thư mục rác lồng nhau (A3):** Cho phép xóa `ai-compute-fabric/ai-compute-fabric/` (chỉ chứa binary `.demo/bin/site-gateway` cũ từ ngày `26/10`).
2. **Nhánh làm việc (A5):** Chủ dự án muốn tạo nhánh cục bộ `prod-readiness-2026-11` từ `HEAD` hiện tại hay tiếp tục làm việc trực tiếp trên nhánh `main`?
3. **Chuyển sang Pha B → C → D → E:** Cho phép tiến hành tuần tự từ **Pha B** (bắt đầu từ **B1**: siết an toàn đăng ký `--allow-signup=false` mặc định, băm mật khẩu `argon2id`/`scrypt`, quản lý phiên `aicf_sess_*` lưu băm có hạn tuyệt đối + idle timeout, chặn `demo-admin-token` ngoài loopback; tiếp theo **B2–B7**).

