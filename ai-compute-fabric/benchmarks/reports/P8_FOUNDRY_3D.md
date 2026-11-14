# Báo cáo Kiểm định AICF 3D Asset Foundry — Mode M1 (`Pha 0 -> Pha 1 -> Pha 2 + Pha 5`)

- **Ngày báo cáo:** `2026-11-14` (cập nhật sau khi chạy xong `prop20` và `articulated12`)
- **Phạm vi thực thi:** `Pha 0 -> Pha 1 -> Pha 2 + Pha 5` theo kế hoạch [KE_HOACH_PIPELINE_SAN_XUAT_3D_BLENDER_AICF.md](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/KE_HOACH_PIPELINE_SAN_XUAT_3D_BLENDER_AICF.md), tạm dừng trước `Pha 3–4 (Mode M2)`.
- **Quy ước gắn nhãn bằng chứng:** `[ĐO]` = đo/chạy trực tiếp trong phiên; `[ĐỌC MÃ]` = đọc trực tiếp từ mã nguồn; `[CHƯA XÁC MINH]` = chưa thể đo do thiếu kết nối phần cứng/mạng node RPC.

---

## 1. Tài nguyên Hệ thống, GPU VRAM & Ngưỡng Đĩa An toàn

| Hạng mục | Kết quả | Ngưỡng an toàn | Nhãn |
| --- | --- | --- | --- |
| VRAM GPU (`NVIDIA GeForce RTX 3060`) | `384 MiB / 12288 MiB` (không nạp thêm model 3D/GPU nào) | `+0 MiB` VRAM thêm ở Mode M1 | `[ĐO]` |
| Phân vùng `/` (`/dev/nvme0n1p5`) | Còn trống `1.8G` | `>= 1.8 GiB` | `[ĐO]` |
| Phân vùng workspace (`/dev/nvme0n1p1`) | Còn trống `2.5G` (`2484 MiB`) | `>= 2.2 GiB` | `[ĐO]` |
| Phân vùng `/mnt/Data` (`/dev/nvme0n1p2`) | Còn trống `108G` | — | `[ĐO]` |
| Thư mục `/mnt/Data/aicf-3d-foundry` | Chiếm `1.5G` | `<= 25 GiB` | `[ĐO]` |

---

## 2. Pha 0 — Runtime Blender 4.2.23 LTS, Sandbox & Render 6 Góc CPU Cycles

- **Runtime cục bộ tại `/mnt/Data/aicf-3d-foundry/runtime/`:** Blender `4.2.23 LTS` Linux x64, Khronos `glTF-Validator`, và `70` thư viện Debian Trixie (`sysroot`) chạy trong container `python:3.12-slim` với `--network=none --read-only --cap-drop=ALL --user 65534:65534` và cờ `-Y` (tắt auto-exec Python trong `.blend`) `[ĐO]` / `[ĐỌC MÃ]`.
- **Hiệu năng đo được từ [p_3d_0_probe.json](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/reports/p_3d_0_probe.json):**
  - `render_6views_512_p50_ms`: **`2075.7 ms`** (lần probe 17:16 ngày 13/11; số `894 ms` cũ trong bản báo cáo trước đã lỗi thời) `[ĐO]`
  - `peak_rss_mb`: **`347.43 MiB`** (`<= 1536 MiB`); cold start `3211 ms`, warm ping p50 `0.7 ms` `[ĐO]`
- **Lỗi thực tế đã phát hiện và sửa:**
  1. [render_6views.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/blender_scripts/render_6views.py): Sửa lỗi `to_track_quat("-Z", "Z")` làm camera 4 góc ngang bị lật nghiêng `90°` thành `up_local = "Y" if abs(direction.z) < 0.98 else "X"`, nâng góc nhìn trước/sau/cạnh lên `0.24 * dist`, tăng `samples >= 12` và tắt nảy sáng gián tiếp (`max_bounces = 1`, `diffuse_bounces = 0`, `glossy_bounces = 1`) `[ĐỌC MÃ]` (đã kiểm chứng chạy trong sandbox `[ĐO]`; phân loại lại bằng VLM live `[CHƯA XÁC MINH]`).
  2. [_blender_daemon.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/blender_scripts/_blender_daemon.py): Khi nhận `op == "shutdown"`, dọn `/work/calls` và `/work/.env_tmp` dưới UID `65534` trước khi thoát `[ĐỌC MÃ]`.

---

## 3. Pha 1 & Pha 2 — Bàn cân Toán học 6 Cổng (`G1..G6`), Đột biến & Hoạt cảnh Khớp nối

- **Các thành phần đã triển khai:**
  - Bộ đặc tả & rà soát ngoại lệ [schemas.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/schemas.py): Bắt buộc `exception_reasons` khi bật `floating_ok`, `intentional_intersections`, `uv_mirror_overlap_ok` hoặc `unlit_black_ok` `[ĐỌC MÃ]`.
  - Hậu xử lý [postproc_pipeline.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/blender_scripts/postproc_pipeline.py): Bổ sung `_obj_uv_has_self_overlap` (lưới raster `48x48`) để ép `smart_project` lại khi nhiều khối ghép chồng UV `[ĐỌC MÃ]`.
  - Bàn cân 3D [evaluator_3d.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/blender_scripts/evaluator_3d.py): Quét `G1..G6`, trong đó `_eval_g6_articulated_sweep` kiểm tra va chạm xuyên qua **mọi** vật thể không thuộc danh sách miễn trừ trong cảnh (`other_objs`) `[ĐỌC MÃ]`.
  - Đồ thị LangGraph 6 chặng + chốt chống tụt lùi `R9` tại [pipeline_graph.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/pipeline_graph.py) `[ĐỌC MÃ]`.
- **Kết quả kiểm thử Blender thật + Đột biến (`pytest -v tests/foundry3d/`):** **`15/15 PASSED` trong `8.77s`** `[ĐO]`, gồm:
  - `8/8` bài đột biến Pha 1 tại [test_pha1_mutations.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/tests/foundry3d/test_pha1_mutations.py) (vật lơ lửng `12 cm`, mặt bàn tách `8 cm` + kiểm tra nghịch đảo ngoại lệ, giao cắt `35%`, vượt poly budget `2x`, sandbox chặn mạng & tệp host, cờ `-Y` chặn `register` auto-exec `.blend`, [artifact_store.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/artifact_store.py) chặn symlink escape, và cổng `MemAvailable` chặn spawn container) `[ĐO]`.
  - `3/3` bài khớp nối NLA Pha 2 tại [test_pha2_articulated.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/tests/foundry3d/test_pha2_articulated.py) (cửa bản lề xuất NLA hợp lệ, chặn đặt sai tâm khớp, chặn va chạm khi quét `90°`) `[ĐO]`.

---

## 4. Pha 5 — Dịch vụ HTTP, Gateway Proxy, OpenAPI, WebGL2 Viewer & Cổng Phát hành

- **Kiến trúc triển khai:**
  - Dịch vụ HTTP cô lập nội bộ (`127.0.0.1:18120`) tại [service.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/service.py) (xác thực `hmac.compare_digest` qua `X-Aicf-Proxy-Secret`, cô lập `X-Aicf-Tenant`, SSE `/v1/jobs/{id}/events`, tải `.glb` & `qa_report.json`) `[ĐỌC MÃ]`. Đã vá thêm lỗi race condition ghi đồng thời temp blob giữa các luồng trong [artifact_store.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/foundry3d/artifact_store.py) `[ĐỌC MÃ]` / `[ĐO]`.
  - Proxy `site-gateway` tại [foundry3d.go](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/foundry3d.go) (mặc định `-foundry3d-enabled=false`, trần `-foundry3d-max-inflight=2`) `[ĐỌC MÃ]`.
  - Trình xem WebGL2 `.glb` v2 nội bộ tại [foundry3d.html](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/web/foundry3d.html) + [foundry3d.js](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/apps/site-gateway/web/foundry3d.js) tuân thủ CSP `script-src 'self'` và hợp đồng OpenAPI tại [aicf_v1.yaml](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/contracts/openapi/aicf_v1.yaml) `[ĐỌC MÃ]`.
- **Kiểm thử bảo mật Pha 5 ([test_pha5_service_security.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/tests/foundry3d/test_pha5_service_security.py)):** **`4/4 PASSED`** (`/healthz` `200`, sai/thiếu secret `401`, sai/thiếu tenant `400`, cô lập chéo tenant `404` & danh sách rỗng, giới hạn đồng thời `429`, SSE `event: done`, tải `asset.glb` magic `b"glTF"`, chặn path traversal, và E2E HTTP qua Blender container thật) `[ĐO]`.
- **Cổng Kiểm định Phát hành (`verify-release-v1` từng bước):**
  - `make test-go`: **PASS 5/5 module Go** (`core/vram-estimator` `93.3%`, `apps/worker-guard` `59.7%`, `apps/site-gateway` `69.8%` trong `8.579s`, `apps/global-control-plane` `90.9%`, `contracts/gen/go`) `[ĐO]`.
  - [test_openapi_conformance.py](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/tests/integration/test_openapi_conformance.py): **PASS `7/7`** `[ĐO]`.
  - `make proto`: **PASS (`0` lỗi)** `[ĐO]`.
  - `make build-cross` (`linux/amd64` + `windows/amd64`): **PASS (`0` lỗi)** `[ĐO]`.
  - `make lint-go` (`golangci-lint`): **`0 issues` trên cả 4 module Go** `[ĐO]`.
  - `make lint-py`: `ruff check .` (`All checks passed!`) + `mypy` (`Success: no issues found in 104 source files`) `[ĐO]`.
  - `deploy/security/verify_sboms.sh` (`cosign`): **`ok` trên cả `5/5` SBOM** `[ĐO]`.
  - `make test-py` (`pytest -q`): **`755 passed, 79 skipped, 1 xfailed`** `[ĐO]`; `1` failure duy nhất nằm ở test có sẵn ngoài `foundry3d` là `tests/serverless/test_pool_docker.py::test_env_build_rejects_sdist_only_package` do đĩa `/dev/nvme0n1p1` đang còn `2484 MiB` trống (`../.aicf-work` chứa `144G` trọng số mô hình) trong khi `EnvCache` đặt trần `min_free_bytes = 3072 MiB` `[ĐO]`.

---

## 5. Kết quả benchmark thật qua `:18080/v1` (Mode M1, `Qwen3.8-27B`, `max_repairs=3`, không bake PBR)

Gateway `:18080/healthz` trả `200` và cạnh Ethernet `enp133s0` đã `UP` khi chạy lại (`[ĐO]` 2026-11-14); mục "bị chặn phần cứng" trong bản báo cáo trước không còn đúng và đã bỏ.

**Pha 0 — probe 10 mẫu cố định** ([p_3d_0_probe.json](p_3d_0_probe.json), 2026-11-13 17:16): `9/10` phân loại đúng (5A + 5B) → `accuracy_ge_9_of_10 = True`; mọi `check_pass` Pha 0 đều `True`. Đĩa `/` trước/sau probe: `2.67 -> 2.37 GB` (probe tiêu thụ ~`0.3 GB` trên `/`; cần giải thích nguồn, xem 6.3). `[ĐO]`

**Pha 1 — `prop20`** ([p_3d_1_prop20.json](p_3d_1_prop20.json)):

| Chỉ số | Kết quả |
| --- | --- |
| `pass@1` | **`5/20` = `25%`** |
| `pass@repair<=3` | **`9/20` = `45%`** |
| Mẫu `needs_human_review` sau 3 vòng | `11/20` |
| `glTF-Validator` `numErrors == 0` trên mẫu đạt | Có (`9/9`) |
| Thời gian job p50 / p95 | `176.9 s` / `337.5 s` |
| RSS đỉnh Blender | `373 MiB` |

**Pha 2 — `articulated12`** ([p_3d_2_articulated12.json](p_3d_2_articulated12.json)), hai lần chạy:

| Chỉ số | Lần 1 (trước khi thêm thư viện trợ giúp `bpy`) | Lần 2 (sau khi thêm `SAFE_HELPERS_BPY`) |
| --- | --- | --- |
| `pass@1` | `0/12` | **`1/12` (`8.3%`)** (`a08_awning_window`) |
| `pass@repair<=3` | `0/12` | **`2/12` (`16.7%`)** (`a08`, `a10_seesaw_plank` sau 1 vòng sửa) |
| `glTF-Validator` `numErrors==0` trên mẫu đạt | — | Có (`2/2`) |
| Thời gian job p50 / p95 | `273.2 s` / `403.4 s` | `201.8 s` / `281.6 s` |
| RSS đỉnh Blender | `403 MiB` | `404 MiB` |

Lần 2 bị gián đoạn ở mẫu 7 (container Blender dừng, không có stderr, không thấy OOM trong log kernel) và được chạy tiếp bằng `--resume` cho mẫu 7–12. Lần 1 chạy một phần bằng `--resume` (mẫu 11–12 chạy sau). `[ĐO]`

**Giới hạn của so sánh:** `n=12`, mỗi cấu hình chạy 1 lần, LLM không tất định, nên chênh lệch `0 -> 2` mẫu **chưa đủ để kết luận** thư viện trợ giúp là nguyên nhân cải thiện; chỉ có bằng chứng trực tiếp là số lỗi chữ ký hàm `bpy` giảm từ `4` mẫu (lần 1) xuống `1` mẫu (lần 2, một `NameError` do LLM tự gây ở `a03`).

Lý do thất bại ở vòng cuối của lần 2 (`10/12` mẫu, `[ĐO]` từ `failure_reasons`):
- **Va chạm khi quét khớp `G6` (4):** `a01` (15,6% vào khung), `a02` (24,3% vào thân rương), `a06` (20,1% vào bệ), `a09` (93,2% vào thân tủ).
- **Đâm xuyên giữa khối `G3` (2):** `a04`, `a12` (`100%`).
- **Lơ lửng / chìm sàn `G2` (3):** `a07` (cánh quạt hở `292,7 mm`), `a11` (nắp hở `10 mm`), `a05` (`z_min = -0,88 m`).
- **Lỗi thực thi `bpy` (1):** `a03` (`NameError`).

## 6. Kết luận Check Pass & giới hạn trung thực

### 6.1. Check Pass theo kế hoạch
| Pha | Tiêu chí | Kết quả |
| --- | --- | --- |
| 0 | đĩa, RSS, `>= 9/10` | **ĐẠT** (`[ĐO]`), nhưng `/` còn đúng `1.8G` — sát ngưỡng |
| 1 | `8/8` đột biến, `0 errors` validator, báo cáo `pass@1` vs `pass@repair` | **ĐẠT** về cơ chế; tỷ lệ chất lượng thấp (`25%` / `45%`) được báo cáo nguyên trạng |
| 2 | 12 bài khớp cơ học chạy tự động, khớp đúng tâm, `0 errors` | **CHƯA ĐẠT:** `2/12` bài qua cổng (lần chạy mới nhất). Các bài đạt có `0` lỗi validator; `10/12` bài còn lại `needs_human_review` |
| 5 | `go test`, `pytest`, openapi, lint | **ĐẠT** theo mục 4, trừ `1` test serverless có sẵn lỗi vì đĩa (đã ghi ở mục 4) |
| 3, 4 (Mode M2) | — | **Chưa làm** (cần chủ dự án duyệt cửa sổ đổi topology) |

Không hạ ngưỡng nào để lấy kết quả (quy tắc 9).

### 6.2. Giới hạn
- `prop20` chạy với `bake_pbr=False`; chưa chứng minh nướng PBR ở bộ này. Các mẫu có `tris_lod0 = 0` (`p14`, `p19`, `a02`...) nghĩa là Blender lỗi trước khi sinh lưới.
- Không có đối chứng với mốc thủ công `fi-ai03` (bàn gỗ ~`97 s`) trên cùng tiêu chí, nên không kết luận Foundry "tốt hơn/kém hơn" nó.
- Độ chính xác VLM chỉ đo trên `10` mẫu probe; chưa có đo tỷ lệ báo nhầm/bỏ lọt trên `prop20`. Tầng 5B chỉ chạy khi 5A xanh, nên các mẫu thất bại chưa được VLM đánh giá (`vlm_pass = None`).
- Nguồn màu cho `Hunyuan3D-2mini` (shape-only) vẫn `[CHƯA XÁC MINH]` — thuộc Pha 3, chưa chạm tới.

### 6.3. Việc còn lại (đề xuất)
1. **Đã làm:** thư viện hàm trợ giúp `bpy` (+ test Blender thật, có đột biến), dọn thư mục tạm job.
2. Chưa kết luận được cổng `G6` có quá chặt hay không: các mẫu `a01/a02/a06` đâm xuyên `15–24%` ở bước quét `1–4`. Cần xem hình học từng mẫu (bản lề có chừa khe quét không) trước khi sửa cổng; **không** sửa ngưỡng nếu chưa có bằng chứng cổng sai.
3. Giải thích mức `/` giảm (`1.8G -> 1.7G` trong lúc chạy); hiện thấp hơn ngưỡng Pha 0.
4. Hỏi chủ dự án có mở cửa sổ `Mode M2` cho Pha 3–4 hay không.
