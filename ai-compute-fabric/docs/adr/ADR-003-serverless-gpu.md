# ADR-003 — Nền tảng Serverless GPU/Python (kiểu Modal) trên AI Compute Fabric (D1..D10)

- **Trạng thái:** ACCEPTED cho Linux (`pc-001`); Windows **chưa chấp nhận** (xem D-WIN)
- **Ngày:** 2026-11-05
- **Nguồn:** `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md` (v2) + số đo Pha 0 tại `benchmarks/reports/p_serverless_0_probe.json` (script `benchmarks/serverless/p0_probe.py`).

## Quyết định
- **D1** Dịch vụ Python `serverless/` (cổng `18110`, đã kiểm còn trống) đứng sau reverse-proxy của `site-gateway`; Go không nhận thêm client NATS. Đúng mẫu `bestofn` (`BestOfNProxy`).
- **D2** `serverless/` chỉ tin `X-Aicf-Tenant` khi có `X-Aicf-Proxy-Secret` đúng và lắng nghe `127.0.0.1`.
- **D3** Lease theo *container* (TTL 30 s, gia hạn 10 s khi worker còn nhịp tim); đường nóng không gọi LeaseService.
- **D4** Dựng môi trường trong container builder cùng base image; chỉ wheel (`uv pip install --only-binary :all:`), lock có băm, index allowlist.
- **D5** Base image runtime: **`python:3.12-slim` (glibc)**; torch/CUDA đến từ env cache gắn `:ro`. Không dùng `python:3.12-alpine`.
- **D6** `data_class` `RESTRICTED` bị từ chối trên GPU dùng chung.
- **D7** Object Store chỉ cho bundle/artifact **≤ 8 MiB**; tệp lớn hơn đi qua Volume cục bộ của worker (staging qua HTTP gateway) và `ModelCache` chỉ đọc do admin nạp.
- **D8** At-most-once sau khi hàm bắt đầu chạy; `retries=n` là opt-in.
- **D9** `invoke` đồng bộ ≤ 60 s + `spawn` bất đồng bộ.
- **D10** `worker-guard` là nơi duy nhất quyết định evict: Serverless trước, LLM sau.
- **D-LEASE-HTTP** (mới, từ Pha 0) `serverless/` truy cập `LeaseManager` qua **cầu nối HTTP nội bộ trong gateway** (`/api/serverless/v1/_lease/{request,extend,release}`, chỉ nhận `X-Aicf-Proxy-Secret`), **không** thêm `grpcio` vào Python: `contracts/gen/python/aicf/v1` chỉ có `*_pb2.py`, không có `*_pb2_grpc.py`, và `grpcio` không có trong `.venv` `[ĐO]`.
- **D-DOCKER** (mới) GPU vào container bằng **`--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=<idx> -e NVIDIA_DRIVER_CAPABILITIES=compute,utility`**, **không** `--gpus` (lỗi trên host này, Docker dùng chế độ CDI mà chưa có `/etc/cdi`) `[ĐO]`. `no-new-privileges` không chạy được trên host này (AppArmor) ⇒ giữ cơ chế dò `_nnp_works()` sẵn có `[ĐO]`.
- **D-STORE** (mới) Cache `uv` và các env phải nằm **dưới cùng một mount cha** (`/store/cache`, `/store/envs/<hash>`), `UV_LINK_MODE=hardlink`: mỗi env thêm ≈ 0,7 MB thay vì 77 MB `[ĐO]` (host chỉ còn ~7 GB trống, đĩa 98 %). Runtime gắn riêng `/store/envs/<hash>` ở chế độ `:ro`.
- **D-WIN** Windows (`pc-win-01`) **không** chạy Serverless trong phạm vi hiện tại. Lý do: chưa có bằng chứng WSL2 Docker + `--gpus` + thu hồi PDH `< 2 s`, và quy tắc dự án cấm chạm vào Windows host ngoài `C:\aicf\` khi chưa được duyệt. Pha 8 giữ ở trạng thái "chưa chấp nhận" cho tới khi chủ dự án đồng ý khảo sát.

## Bằng chứng (đều `[ĐO]` trên `pc-001`, RTX 3060, driver 580.178.04, torch 2.11.0+cu130, Docker runtime `nvidia`)
- Container hardening đầy đủ (`--network none --read-only --cap-drop ALL --user 65534:65534 --pids-limit --memory`) + GPU: `import torch` + tensor 1024² trên GPU xong trong **p50 1,96 s / p95 2,04 s** (n = 30) so với container Python rỗng **p50 0,34 s**.
- Runner bền (stdin/stdout length-prefixed): sẵn sàng sau **1,5 s** (gồm `import torch` + khởi tạo CUDA); mỗi lượt gọi `noop` **p50 0,06 ms / p95 0,26 ms**, matmul 1024² trên GPU **p50 0,65 ms / p95 1,27 ms** (n = 300). Đây là đường ống thuần túy, chưa gồm NATS/HTTP.
- Dựng env bằng `uv` trong builder: cache lạnh **1,76 s**, cache ấm **0,58 s** (numpy+pillow+cloudpickle, 77 MB); gói chỉ có sdist bị từ chối rõ ràng với `--only-binary :all:`; env gắn `:ro` vào container runtime import được; cùng venv trên `python:3.12-alpine` **không import được** (`numpy._core._multiarray_umath`).
- Gán VRAM theo container từ host: `nvidia-smi --query-compute-apps` + `/proc/<pid>/cgroup` ánh xạ đúng **10/10**, tìm thấy sau **p50 22 ms**. `docker kill` đến khi VRAM trả về nền: **p50 266 ms / p95 296 ms** (1 container, n = 10); **384 ms** cho 3 container × 1 GiB. Rất xa ngưỡng 2 s.
- Object Store (nats-py 2.16, loopback): 1 MiB put/get ≈ 144/281 MB/s; 8 MiB get chỉ **16 MB/s**; 64 MiB get **12 MB/s** (44 MB/s với `writeinto`); 512 MiB get mất **389 s** ⇒ Object Store không dùng cho tệp lớn (D7).
- Kế toán VRAM: `NewBuckets` tính `Allocatable = Total − (OSUser+Headroom+Weights+KV+Compute)` và `OSUser = totalUsed − (weights+kv+compute của engine)` `[ĐỌC MÃ]` ⇒ VRAM thực của mọi container Serverless **đã nằm trong `OSUser`** nên `Allocatable` giảm theo mức dùng thực; nếu `LeaseManager` lại trừ reservation, VRAM đã vật chất hóa bị tính hai lần. Pha 3 phải để worker-guard loại VRAM các pid thuộc container `aicf.sl=1` khỏi `OSUser` (bucket riêng "Serverless") và để Lease trừ reservation, không trừ mức dùng.
- Một wheel torch cu130 chứa kernel `sm_75, sm_80, sm_86, sm_90, sm_100, sm_120` ⇒ một `env_hash` chạy được cả RTX 3060 (sm_86) và RTX 5070 (sm_120). *Chưa chạy trên RTX 5070.*
- Người dùng `pi` thuộc nhóm `docker` ⇒ quyền root tương đương; Docker không ở chế độ rootless `[ĐO: docker info]`. Rủi ro R2 được ghi nhận, giảm bằng `argv` cố định.

## Hệ quả
- Giới hạn payload bus `max_payload_bytes = 256 KiB` ⇒ inline ≤ 64 KiB, lớn hơn dùng `object_ref` ≤ 8 MiB, lớn hơn nữa dùng Volume.
- Dung lượng đĩa là ràng buộc cứng: cần hạn mức env cache + dọn LRU, và cảnh báo khi < 5 GB trống.
- Thư mục scratch của test phải nằm trên đĩa thật (`.aicf-work/`), không phải `/tmp` của tiến trình chạy lệnh: daemon Docker không thấy `/tmp` đó `[ĐO]`.
