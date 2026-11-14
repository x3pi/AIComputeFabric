# ADR-005: Phân Hệ Sản Xuất Tài Sản 3D Trọn Gói (`foundry3d` — AICF 3D Asset Foundry)

- **Trạng thái:** Đang triển khai (Pha 0–2 & Pha 5 ở `Mode M1`; Pha 3–4 ở `Mode M2` chờ duyệt cửa sổ đổi topology GPU)
- **Ngày lập:** `2026-11-13`
- **Tham chiếu liên quan:** `ADR-002`, `ADR-003`, `ADR-004`, `KE_HOACH_PIPELINE_SAN_XUAT_3D_BLENDER_AICF.md`

## 1. Bối cảnh Hạ tầng & Vấn đề Cần Giải Quyết (`[ĐO 2026-11-13]`)

1. **Giới hạn đĩa trên `pc-001`:** Phân vùng gốc `/` (chứa Docker root `/var/snap/docker/common/var-lib-docker`) chỉ còn `1.9–2.0 GiB` trống; phân vùng workspace `/mnt/2d47...` còn `2.5 GiB` trống; phân vùng `/mnt/Data` còn `109 GiB` trống. Không thể kéo/dựng image Docker mới nhiều GB.
2. **Giới hạn RAM & VRAM khi chạy `Qwen3.8-27B` gộp 2 GPU:** Trên `pc-001` (`RTX 3060 12 GiB`), `llama-server-vision` chiếm `10,915 MiB / 12,288 MiB` (còn `996 MiB` trống), RAM khả dụng dao động `2.7–4.4 GiB`.
3. **Nhu cầu kỹ thuật:** Tự động hóa tạo tài sản 3D sạch chuẩn Game Engine (`godot4`, `unity`, `ue5`, `web_glb`) từ mô tả/ảnh với vòng tự sửa có kiểm soát (`max_repairs = 3`), cổng toán học xác định chạy trước VLM, và cô lập tuyệt đối mã `bpy` / tệp `.blend` không tin cậy.

## 2. Ba Chế độ Vận hành (`M1 / M2 / M3`)

- **`Mode M1` (Mặc định Pha 0–2 & 5, `0 MiB` VRAM thêm):** Giữ nguyên `Qwen3.8-27B-Vision` trên `:18080`. Chạy tối đa `1` container `WarmBlenderPool` trên CPU (`--memory=1536m --memory-swap=2048m --cpus=4`, cổng `MemAvailable >= 2048 MiB`), tạo đạo cụ cứng, lắp ráp và đạo cụ khớp cơ học bằng `bpy` + `bmesh`.
- **`Mode M2` (Pha 3–4, chạy theo ca bảo trì được duyệt):** Chuyển topology có kiểm soát sang `Qwen3VL-8B` để nhường VRAM cho `Hunyuan3D-2mini-Turbo` / `Make-It-Animatable` / `UniRig` nạp tuần tự từng chặng.
- **`Mode M3` (Mở rộng lai):** Giữ LLM 27B tại chỗ, ủy quyền bước đúc lưới/xương nặng cho GPU worker ngoài qua giao diện `MeshRigProvider`.

## 3. Sáu Quyết định Kiến trúc Cốt lõi (`D-3D-1..6`)

1. **`D-3D-1` — Kho hiện vật `ArtifactStore` theo SHA-256 trên `/mnt/Data/aicf-3d-foundry/artifacts/`:** Giới hạn trần `25 GiB`, tách thư mục theo `tenant_id`, từ chối mọi symlink và thoát đường dẫn `/work`, chỉ truyền tham chiếu `sha256:<hex>` qua `LangGraph` (không đẩy tệp > 8 MiB qua NATS).
2. **`D-3D-2` — Sandbox kép cho Blender (`WarmBlenderPool` mới + `-Y`):** Không tái sử dụng nguyên `WarmDockerPool` (do gắn chặt `GpuAdmission` và `/tmp` `8m noexec`). Dùng base `glibc` sẵn có (`python:3.12-slim`) + mount `:ro` runtime `Blender 4.2 LTS` & sysroot trên `/mnt/Data/aicf-3d-foundry/runtime/`, `--network=none`, `--read-only`, `--cap-drop=ALL`, `--pids-limit=256`, `--ulimit cpu=180`, `--ulimit fsize=536870912`, `--user=65534:65534`, cờ `-Y` (`--disable-autoexec`), đặt `HOME`, `TMPDIR`, `XDG_*`, `BLENDER_USER_*`, `MESA_SHADER_CACHE_DIR`, `PYTHONPYCACHEPREFIX` vào `/work/.env_tmp` để rò rỉ đĩa ngoài `/mnt/Data` bằng `0 byte`.
3. **`D-3D-3` — Bất đồng bộ HTTP + SSE (`:18120`) & công tắc tắt mặc định:** Dịch vụ `foundry3d.service` lắng nghe `127.0.0.1:18120` sau `site-gateway`, bắt buộc `X-Aicf-Proxy-Secret` (`hmac.compare_digest`), cô lập `X-Aicf-Tenant`, và mặc định TẮT trên gateway (`-foundry3d-enabled=false`).
4. **`D-3D-4` — Cổng bảo vệ tài nguyên `MemAvailable` & `X-Priority: batch`:** Không khởi chạy/giao việc Blender khi `MemAvailable < 2,048 MiB` (`queued_low_mem`); mọi lệnh gọi LLM/VLM dùng `X-Priority: batch`.
5. **`D-3D-5` — Hệ quy chiếu không gian & xuất Engine thống nhất:** Nội bộ Chặng 1–5 khóa `1 unit = 1 m`, `Z-up`, `-Y forward`, `+X right`, `Origin (0, 0, 0)` tại tâm đáy `z_min = 0.000`, `Scale = (1, 1, 1)`. Chặng 6 đóng gói `ORM` (`R=AO, G=Roughness, B=Metallic`), xương gốc `root`, đảo kênh `G` Normal khi xuất `ue5`, nạp lại `.glb` đối chiếu hộp bao (`round-trip bbox`), và bắt buộc `0 errors` trên Khronos `glTF-Validator`.
6. **`D-3D-6` — Tách biệt Người làm & Bàn cân Kiểm định + Cờ ngoại lệ kiểm soát:** `evaluator_3d.py` chạy Tầng 5A xác định trước Tầng 5B (`VLM 6-View Critic`). Các ngoại lệ hình học (`floating_ok`, `intentional_intersections`, `uv_mirror_overlap_ok`, `unlit_black_ok`) phải khai báo tường minh kèm lý do trong `AssetSpec`, chỉ miễn trừ đúng bộ phận được nêu, và bản sửa lỗi chỉ được nhận khi số lỗi Tầng 5A không tăng.

