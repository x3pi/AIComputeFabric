# KẾ HOẠCH: PIPELINE SẢN XUẤT TÀI SẢN 3D TRỌN GÓI TRÊN BLENDER & AI COMPUTE FABRIC (AICF 3D FOUNDRY)

*(Tài liệu thiết kế kiến trúc & bàn giao triển khai — biên soạn ngày `2026-11-13` để phục vụ phản biện/kiểm toán kỹ thuật độc lập bởi Agent khác trước khi viết mã. Đối chiếu trực tiếp với `ADR-001`, `ADR-002`, `ADR-003`, `KE_HOACH_BEST_OF_N_CODE.md`, `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md`, `BAO_CAO_DANH_GIA_KIEN_TRUC_VA_DO_THUC_TE_CODER_30B.md` và `ai-compute-fabric/docs/API_CLIENT.md`. Trạng thái: **KẾ HOẠCH — chưa sửa dịch vụ đang chạy**.)*

**Quy ước nhãn bằng chứng xuyên suốt tài liệu:**
- **`[ĐO]`**: Đã đo trực tiếp trên cụm máy thật của dự án (ghi kèm ngày hoặc báo cáo gốc).
- **`[ĐỌC MÃ]`**: Đã xác minh trực tiếp trong mã nguồn hiện tại của kho `ai-compute-fabric/`.
- **`[TÀI LIỆU]`**: Số liệu/tính chất từ tài liệu chính thức hoặc bài báo khoa học bên ngoài, **chưa đo trên máy này**.
- **`[SUY LUẬN]`**: Ước lượng kỹ thuật suy ra từ kiến trúc và số đo lân cận.
- **`[CHƯA XÁC MINH]`**: Giả thuyết bắt buộc phải đo/kiểm chứng thực tế ở **Pha 0** trước khi khóa thiết kế.
*(Mọi con số không mang nhãn `[ĐO]` đều được coi là chưa đo trên phần cứng dự án.)*

**5 Tiêu chí "Chuẩn Production" bắt buộc của kế hoạch (mỗi tiêu chí đều gắn cổng nghiệm thu định lượng):**
1. **An toàn cô lập tuyệt đối (`Fail-Closed`):** Mã Python `bpy` do LLM sinh ra và tệp `.blend` bên ngoài là **dữ liệu không tin cậy** — chỉ thực thi trong container cô lập không mạng, tắt `autoexec` (`-Y`), chặn thoát thư mục qua symlink, không bao giờ chạy trần trên host Linux hay Windows.
2. **Khớp thực tế tài nguyên vật lý (Đĩa / RAM / 12 GB VRAM):** Thiết kế tôn trọng tuyệt đối trạng thái thật của cụm máy (phân vùng gốc `/` chỉ còn `2.0 GiB` trống, RAM `pc-001` còn `2.7 GiB` khả dụng, VRAM `RTX 3060` còn `996 MiB` khi chạy gộp `Qwen3.8-27B` `[ĐO 2026-11-13]`). Không giả định viển vông rằng có thể nạp đồng thời LLM 27B và các mô hình đúc 3D 10 GB trên cùng một card.
3. **Kiểm định Toán học Cứng trước, Thị giác VLM sau:** Mọi tài sản 3D phải vượt qua **Bàn cân Hình học & Khung xương xác định (`evaluator_3d.py`)** (đếm tam giác, chạm sàn `Z = 0`, kiểm tra đâm xuyên `BVHTree`, UV overlap, kiểm tra rách lưới khi gập khớp) trước khi gọi Giám khảo Thị giác đa góc nhìn (`VLM 6-View Critic`).
4. **Không xâm phạm dịch vụ LLM đang phục vụ:** Thứ tự ưu tiên tài nguyên bất biến: **Chủ máy > LLM tương tác (`/v1/chat/completions`) > Serverless > Foundry3D (`X-Priority: batch`)**. Bảo toàn cam kết thu hồi GPU `G-RECLAIM < 2 s` `[ĐO: KE_HOACH_SERVERLESS]`.
5. **Tăng dần từng lát cắt chạy thật (Incremental End-to-End):** Chạy thông lát cắt `0 MiB VRAM thêm` (tài sản tham số & lắp ráp trên Headless Blender CPU + `Qwen3.8-27B-Vision` sẵn có) ở Pha 1–2 trước khi bật đúc lưới hữu cơ (`Hunyuan3D-2mini`) và gắn xương tự động (`Make-It-Animatable` / `UniRig`) ở Pha 3–4.

---

## PHẦN 0: BRIEF BÀN GIAO, SỰ THẬT HỆ THỐNG ĐÃ ĐO (`2026-11-13`) & QUY TẮC BẮT BUỘC

### 0.1. Mục tiêu cốt lõi và Ngoài phạm vi v1

**Mục tiêu:** Xây dựng phân hệ **`foundry3d/` (AICF 3D Asset Foundry)** sản xuất tài sản 3D trọn gói từ đầu vào là mô tả văn bản hoặc ảnh tham chiếu kèm hồ sơ ngân sách kỹ thuật (`AssetSpec`: nhóm vật thể, kích thước thật tính bằng mét, trần đa giác `LOD0`, hệ khung xương `static | mechanical | humanoid | creature`, hồ sơ Engine đích `godot4 | unity | ue5 | web_glb`). Đầu ra là gói tài sản đã kiểm định tự động gồm:
- `asset.glb` (chuẩn Khronos glTF 2.0, đạt `0 errors` qua `glTF-Validator`) và/hoặc `asset.fbx`;
- `source.blend` sạch (tách rõ các Collection: `HighPoly`, `LOD0`, `LOD1`, `LOD2`, `Rig`, `UCX_Collider`);
- Bộ bản đồ vật liệu PBR chuẩn (`BaseColor`, `Normal`, `Roughness`, `Metallic`, `AO` ở `1024x1024` hoặc `2048x2048`);
- Dải chuyển động cơ bản đóng gói trong `NLA Tracks` (`Idle`, `Walk`, `Run`, `Attack`) đối với tài sản có xương;
- Biên bản kiểm định định lượng `qa_report.json` + ảnh xem trước 6 góc.

**Ngoài phạm vi v1 (ghi rõ để chống phình phạm vi):**
- Mô phỏng vật lý nặng (chất lỏng, khói lửa, vải vóc `Cloth`, tóc sợi `Hair Dynamics`);
- Kết xuất phim ảnh đường đi tia sáng dài hạn (Cycles Path-Tracing nhiều phút mỗi khung hình);
- Sinh địa hình thế giới mở nhiều km² hoặc cảnh hàng nghìn vật thể vượt ngưỡng RAM `1.5 GiB` của worker;
- Chạy mã `bpy` do mô hình sinh ra trên máy Windows `pc-win-01` (vi phạm quy tắc `D-WIN`);
- Tự ý thay đổi tham số vận hành của `llama-server` / `site-gateway` đang chạy khi chưa được chủ dự án phê duyệt.

---

### 0.2. Sự thật Hạ tầng & Mã nguồn Đã Kiểm Chứng (Reviewer Agent cần đối chiếu trực tiếp)

#### A. Điểm nghẽn Vật lý Sống trên `pc-001` (`[ĐO 2026-11-13]`)
1. **Dung lượng đĩa trên phân vùng hệ thống và phân vùng repo đang chạm mức báo động đỏ (`100% Use%`):**
   - `/dev/nvme0n1p5` (mount tại `/`, chứa Docker root `/var/snap/docker/common/var-lib-docker`): tổng `310 GiB`, đã dùng `295 GiB`, **chỉ còn trống `2.0 GiB`** `[ĐO 2026-11-13]`.
   - `/dev/nvme0n1p1` (mount tại `/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d`, chứa kho mã nguồn): tổng `331 GiB`, đã dùng `312 GiB`, **chỉ còn trống `2.5 GiB`** `[ĐO 2026-11-13]`.
   - `/dev/nvme0n1p2` (mount tại `/mnt/Data`, quyền sở hữu `drwxrwxrwx pi:pi`): tổng `274 GiB`, đã dùng `152 GiB`, **còn trống `109 GiB` (`59% Use%`)** `[ĐO 2026-11-13]`.
   - **Hệ quả thiết kế bắt buộc:**
     - **CẤM TUYỆT ĐỐI** chạy `docker pull` hoặc `docker build` tạo các image mới nặng nhiều GB vào `/var/snap/docker/common/var-lib-docker` vì sẽ làm đầy cứng `/` (`0 byte` trống), gây sập PostgreSQL, NATS và hệ điều hành.
     - Trong cache Docker hiện tại của `pc-001` **đã có sẵn** hai base image `glibc` gọn nhẹ là `python:3.12-slim` (`180 MB`) và `ubuntu:22.04` (`119 MB`) `[ĐO 2026-11-13]`. *(Lưu ý kỹ thuật: không được dùng `python:3.12-alpine` vì `musl libc` không chạy được tệp thực thi chính thức của Blender Linux x64.)*
     - Toàn bộ bộ chạy portable `blender-4.2.x-linux-x64` (~`1.1 GiB` sau giải nén), thư viện đồ họa mềm đi kèm (theo đúng mô hình đóng gói `.aicf-work/eda/` đã làm `[ĐỌC MÃ]`), trọng số mô hình 3D và kho `ArtifactStore` phải đặt tại `/mnt/Data/aicf-3d-foundry/` **sau khi xin phép chủ dự án ở Pha 0**, rồi bind-mount chỉ-đọc (`:ro`) vào container sẵn có.

2. **Áp lực VRAM và RAM hệ thống khi cụm đang chạy `Qwen3.8-27B` gộp 2 GPU (`[ĐO 2026-11-13]`):**
   - Trên `pc-001` (`NVIDIA GeForce RTX 3060`, tổng `12,288 MiB`): đang chiếm **`10,915 MiB`**, **chỉ còn trống `996 MiB`** `[ĐO 2026-11-13]`.
   - Bộ nhớ RAM trên `pc-001`: tổng `30 GiB`, đang dùng **`28 GiB`**, **chỉ còn `2.7 GiB` `available`** (`Swap` đang dùng `18 GiB / 31 GiB`) `[ĐO 2026-11-13]`.
   - Cấu hình LLM đang phục vụ thực tế (`aicf-guard.service`, `engine-pc001.log`, `docs/API_CLIENT.md`): chạy `llama-server-vision` với mô hình `Qwen3.8-27B-UD-Q4_K_M.gguf` (`16.46 GiB` `[ĐO]`) gộp 2 card (`pc-001` RTX 3060 + `pc-win-01` RTX 5070 `12,227 MiB`), ngữ cảnh hợp nhất `98,304` token (`-np 2` slot), bật giải mã suy đoán `draft-mtp` đạt **`~40.18 tok/s`** khi sinh mã và **nhận sẵn đầu vào thị giác tối đa 8 ảnh/request** qua chuẩn OpenAI `image_url` base64 `[ĐO]`/`[ĐỌC MÃ]`.
   - Trong `.aicf-work/` trên đĩa đã tải sẵn mô hình thị giác gọn nhẹ `Qwen3VL-8B-Instruct-Q4_K_M.gguf` (`5.03 GiB`) kèm `mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` (`752 MiB`) `[ĐO 2026-11-13]`.
   - Công cụ hiển thị ảo `/usr/bin/xvfb-run` **đã có sẵn** trên `pc-001`; gói `blender` **chưa được cài đặt** `[ĐO 2026-11-13]`.

#### B. Thành phần Nền tảng Có Sẵn trong `ai-compute-fabric/` (Tái sử dụng, KHÔNG viết lại — `[ĐỌC MÃ]`)
- **Cổng `apps/site-gateway` (Go, cổng `:18080`):** Đã có cơ chế xác thực khoá theo `tenant`, RBAC (`viewer | operator | admin`), hàng chờ công bằng (`X-Priority: batch`), mẫu reverse-proxy xoá `Authorization` và đính kèm `X-Aicf-Tenant` + `X-Aicf-Proxy-Secret` (đã kiểm chứng ở `/api/bestofn/` và `/api/serverless/`), API chuyển đổi cấu hình cụm `POST /api/cluster/topology` và quản lý đặt chỗ VRAM `LeaseManager` (`leases.go`) `[ĐỌC MÃ]`. Đường dẫn `/v1/` dành riêng cho suy luận LLM $\Rightarrow$ mọi API mới phải đặt dưới `/api/foundry3d/*` và khai báo đầy đủ trong `contracts/openapi/aicf_v1.yaml` để qua bài kiểm tra `test_openapi_conformance.py`.
- **Lớp Sandbox & Serverless (`agents/sandbox.py`, `serverless/`, `apps/worker-guard`):** Đã có `DockerSandbox`, `GpuDockerSandbox` (bắt buộc dùng `--runtime=nvidia` vì cờ `--gpus` lỗi trên host `[ĐO: KE_HOACH_SERVERLESS]`), `WarmDockerPool` (độ trễ gọi ấm p50 `3.9 ms` `[ĐO]`), và cơ chế thu hồi VRAM tức thời `G-RECLAIM` (p95 `387.9 ms < 2 s` `[ĐO]`).
- **Điều phối & Hàng đợi (`agents/orchestrator.py`, `agents/task_bus.py`, `pyproject.toml`):** Đã cài sẵn `langgraph==1.2.17`, `langgraph-checkpoint-postgres==3.2.0`, `psycopg-pool==3.3.3`, `nats-py==2.16.0`, `pydantic==2.14.1`, `pillow==13.0.0` `[ĐỌC MÃ]`. Lưu ý giới hạn cứng đã đo: gói tin `NATS JetStream` tối đa `256 KiB` `[ĐỌC MÃ]` và `NATS Object Store` chỉ hiệu quả cho tệp `<= 8 MiB` (`get` tệp `512 MiB` mất `389 s` `[ĐO: KE_HOACH_SERVERLESS]`).
- **Đã có sẵn một thử nghiệm Blender + Qwen (KHÔNG thuộc kho mã, chỉ là nền tham chiếu) `[ĐO 2026-11-11]`:** máy Windows riêng `fi-ai03` (`192.168.1.175`, RTX 3060, Blender 5.1/5.2 đã cài) chạy `blender-mcp` + REPL `blender_agent.py` + web `127.0.0.1:8787` trong `C:\aicf-blender`, gọi Gateway qua khoá tenant riêng `blender-fi-ai03`. Đo thật: "làm cái bàn gỗ" → mô hình tự sửa chân bàn bị rời, kiểm bbox độc lập đạt (chân z 0–0.75, mặt z 0.75–0.85 chạm nhau), ~`97 s`/1 bàn. Đây là **mốc so sánh `pass@1` thô** cho Pha 1 và là lý do Pha 6 nên dùng lại máy này thay vì `pc-win-01`. Bài học đã đo cần mang sang: mô hình viết `bpy` trong khối văn bản `<<<PY ... >>>` (JSON tool-args cho mã dài hay vỡ); công cụ chỉ được bật sau khi đã chạy ít nhất 1 khối mã, nếu không mô hình lặp vòng gọi `look`. Lưu ý máy đó là **máy khác** `pc-win-01`; `fi-ai03` không áp dụng chính sách `D-WIN` mặc định nên phải hỏi chủ dự án trước khi chạy mã tự sinh ở đó.
- **Ràng buộc máy phụ Windows (`pc-win-01`, `10.42.0.225`):** GPU `RTX 5070 12 GB` (WDDM có hiện tượng sụp tốc độ khi vượt vách VRAM ~`54%–60%` ở ngữ cảnh lớn `[ĐO]`) + `64 GB DDR5`; hiện áp dụng chính sách `D-WIN`: `NullSandbox` từ chối chạy mã không tin cậy, chỉ đọc/ghi trong `C:\aicf\` `[ĐỌC MÃ]`.

---

### 0.3. Mười Quy tắc Bắt buộc đối với Agent Thực thi

1. **Ngôn ngữ & Nhãn bằng chứng:** Toàn bộ tài liệu, chú thích vận hành và báo cáo viết bằng **tiếng Việt**; mọi con số phải gắn đúng nhãn `[ĐO]`, `[ĐỌC MÃ]`, `[TÀI LIỆU]`, `[SUY LUẬN]` hoặc `[CHƯA XÁC MINH]`.
2. **Bảo vệ dịch vụ đang chạy:** Không tự ý tắt, khởi động lại hoặc đổi cấu hình `site-gateway`, `worker-guard`, `llama-server`, `aicf-nats`, `aicf-pg` hay đổi topology cụm khi chưa hỏi chủ dự án. Các thử nghiệm tích hợp chạy trên cổng, container và tiền tố `aicf-3d-test-` cô lập.
3. **Xin phép trước khi ghi ra `/mnt/Data`:** Dù `/mnt/Data` có quyền ghi của `pi` và trống `109 GiB` `[ĐO]`, nó nằm ngoài thư mục workspace — ở đầu Pha 0 phải xác nhận với chủ dự án đường dẫn `/mnt/Data/aicf-3d-foundry/` và hạn mức dung lượng tối đa được phép dùng (đề xuất trần `25 GiB`).
4. **Bảo vệ phân vùng gốc `/` (`2.0 GiB` trống):** Cấm build/pull image Docker nặng vào `/var/snap/docker/...`. Trước và sau mỗi pha phải chụp `df -h / /mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d` làm bằng chứng không gây rò rỉ đĩa.
5. **Kiểm đột biến (`Mutation Testing`) bắt buộc:** Mọi cơ chế an toàn (sandbox không mạng, chặn `autoexec` `.blend`, chống symlink traversal, xác thực `X-Aicf-Proxy-Secret`, cô lập `tenant`) và mọi cổng kiểm định 3D (`evaluator_3d.py`) đều phải có bài kiểm đột biến: chủ động phá điều kiện kiểm tra $\rightarrow$ test bắt buộc **ĐỎ** $\rightarrow$ khôi phục $\rightarrow$ test **XANH**.
6. **Quản lý tiến trình an toàn:** Chỉ dùng `pkill -x <tên>` hoặc `kill <pid>`; tuyệt đối cấm `pkill -f`.
7. **Quy tắc máy Windows (`pc-win-01`):** Chỉ đọc/ghi trong `C:\aicf\`; không đụng thư mục cá nhân của chủ máy; cấm gửi bất kỳ đoạn mã Python/`bpy` tự sinh nào sang chạy trên Windows.
8. **Không `git push`:** Chỉ `git commit` khi được yêu cầu rõ ràng.
9. **Cổng nghiệm thu cứng (`Check Pass`):** Pha nào chưa đạt đủ tiêu chí `Check Pass` thì dừng lại báo cáo nguyên nhân trung thực, tuyệt đối không tự hạ ngưỡng kiểm định để lấy kết quả giả.
10. **Công tắc an toàn mặc định TẮT:** Thêm cờ `-foundry3d-enabled=false` trên gateway; khi cờ tắt, hành vi và hiệu năng của toàn bộ hệ thống hiện hữu không thay đổi 1 bit.

---

## PHẦN 1: LỰA CHỌN MÃ NGUỒN MỞ, 3 CHẾ ĐỘ PHẦN CỨNG & QUYẾT ĐỊNH KIẾN TRÚC (ADR)

### 1.1. Đánh giá & Chốt Bộ Mã Nguồn Mở Theo Từng Công Đoạn

| Công đoạn | Giải pháp Mã nguồn mở Đánh giá | Giấy phép & Tài nguyên `[TÀI LIỆU]` | Đánh giá Kỹ thuật & Quyết định Chốt |
| :--- | :--- | :--- | :--- |
| **0. Điều phối Multi-Agent** | **`LangGraph`** (`langchain-ai/langgraph`) vs `CrewAI` / `AutoGen GroupChat` | MIT (`0 VRAM`, **đã cài sẵn** `v1.2.17` `[ĐỌC MÃ]`) | **CHỌN `LangGraph` (`StateGraph` có cạnh điều kiện).** Cho phép vòng lặp sửa lỗi có trần cứng (`max_repairs = 3`), lưu checkpoint PostgreSQL bền vững, **0% token hội thoại xã giao thừa**. Loại bỏ `CrewAI`/`AutoGen GroupChat` vì tốn ngữ cảnh và dễ lặp vô hạn. |
| **1. Cầu nối Thực thi Blender** | **Warm Headless Blender Pool** (kiểu `agentblender` / `Blender-AI-Agent`) vs `ahujasid/blender-mcp` | GPL-3.0 / MIT (`CPU` hoặc `EEVEE`, RAM `~0.8–1.5 GiB` `[SUY LUẬN]`) | **CHỌN Warm Headless Worker, viết `WarmBlenderPool` MỚI (KHÔNG dùng nguyên `WarmDockerPool`: lớp hiện có nhận `GpuDockerSandbox` + `GpuAdmission`, khoá theo `(tenant, env_hash, fn_id)` `[ĐỌC MÃ agents/sandbox.py:382]`; `DockerSandbox` hiện chỉ mount 1 script `:ro` và `/tmp` `8m noexec` `[ĐỌC MÃ]` nên không đủ cho Blender, cần mount runtime `:ro` + `/work` riêng)** cho dây chuyền tự động Pha 1–5 (giữ ấm tiến trình `blender --background --factory-startup -Y`, tránh mất `2–3s` khởi động lại mỗi bước). Để `ahujasid/blender-mcp` làm tùy chọn tương tác trực tiếp trên GUI Windows ở Pha 6. |
| **2A. Dựng Khối Cứng & Lắp Ráp** | **`Blender 4.2 LTS Python API` (`bpy`, `bmesh`, `Geometry Nodes`)** + kiến trúc RAG API của **`threedle/ll3m`** | GPL-3.0 / Mở (`0 VRAM`) | **CHỌN.** Các vật thể cơ khí, bàn ghế, kiến trúc, đạo cụ cạnh sắc viết bằng `bpy` + chỉ mục hàm chuẩn `Blender 4.2 LTS` (tránh lỗi gọi hàm cũ đã bị xóa của Blender 2.8/3.x) cho lưới sạch và đúng kích thước milimet. |
| **2B. Sinh Ảnh Chuẩn Hóa Tạo Hình** | **`Flux.1-schnell` (Q4/NF4)** hoặc **`SDXL-Lightning`** + **`BiRefNet` / `rembg`** | Apache-2.0 / MIT (`~6–8 GiB VRAM` `[TÀI LIỆU]`) | **CHỌN (chỉ bật ở `Mode M2/M3`).** Sinh 1 ảnh chuẩn góc 3/4 hoặc A-pose nền trắng phẳng, không bóng đổ gắt rồi lọc qua VLM trước khi đúc lưới 3D giúp giảm mạnh lỗi cụt chi/dính khối so với Text-to-3D trực tiếp `[TÀI LIỆU]`. |
| **2C. Đúc Lưới Hữu Cơ (Organic Mesh)** | **`Tencent/Hunyuan3D-2mini-Turbo`** vs **`Microsoft/TRELLIS` (`fp16` low-VRAM)** vs `Hunyuan3D-2.1 Full` | Mở / MIT (`2mini`: `~5.5–6.5 GiB`; `TRELLIS-fp16`: `~8–11 GiB`; `2.1 Full`: `24–32 GiB` `[TÀI LIỆU]`) | **CHỌN `Hunyuan3D-2mini-Turbo` làm mặc định local** cho card `12 GB`; đo đối chứng `TRELLIS` bản tối ưu `fp16` ở Pha 3. **CẢNH BÁO `[CHƯA XÁC MINH]`:** theo hiểu biết hiện tại `Hunyuan3D-2mini` chỉ sinh **hình khối (shape), không có texture**; màu cần mô hình "paint" riêng (thêm VRAM) hoặc `TRELLIS` (ra mesh có màu). Pha 3 phải chứng minh nguồn `BaseColor` trước khi hứa "Bake PBR High-to-Low", nếu không thì phương án dự phòng là vẽ màu bằng chiếu ảnh chuẩn hóa/vertex color và ghi rõ giới hạn. **BÁC BỎ** chạy `Hunyuan3D-2.1 Full PBR` trực tiếp trên card `12 GB` (chỉ gọi qua `MeshRigProvider` ngoài ở `Mode M3`). |
| **3. Retopology, UV & Bake PBR** | **Bộ công cụ gốc `Blender 4.2 LTS`** (`Quadriflow`, `Planar/Collapse Decimate`, `Smart UV` / `xatlas`, `Cycles Baker`, `convex_hull`) | GPL-3.0 / BSD (`CPU` hoặc `GPU`) | **CHỌN.** Đã tích hợp sẵn bên trong binary Blender 4.2 LTS, không phát sinh biên dịch C++ ngoài hay phụ thuộc dễ gãy; đủ tạo `LOD0..2`, lưới va chạm `UCX_*` và bake 5 bản đồ PBR. |
| **4. Gắn Xương & Trọng Số Da (Auto-Rig)** | **`jasongzy/Make-It-Animatable` (MIA)** *(CVPR 2025)* + **`VAST-AI-Research/UniRig`** *(SIGGRAPH 2025)* + `Blender Rigify` | Apache-2.0 / MIT (`~4–6 GiB VRAM` `[TÀI LIỆU]`) | **CHỌN KẾT HỢP (ở `Mode M2/M3`):** `MIA` cho nhân vật 2 chân chuẩn xương Mixamo/Humanoid (`< 1s` `[TÀI LIỆU]`); `UniRig` cho sinh vật 4 chân, quái vật, đồ vật có khớp bất kỳ; giữ `Rigify` (`bpy`) làm phương án dự phòng xác định. |
| **5. Kiểm Định Chuẩn Xuất Xưởng** | **`KhronosGroup/glTF-Validator`** + `evaluator_3d.py` nội bộ | Apache-2.0 (`CLI nhẹ < 20 MB`, `0 VRAM`) | **CHỌN.** Bắt buộc mọi tệp `.glb` xuất xưởng phải đạt `numErrors == 0` theo chuẩn công nghiệp của Khronos trước khi trả về cho người dùng. |

---

### 1.2. Ba Chế Độ Vận Hành Phần Cứng (`M1 / M2 / M3`) — Giải Quyết Trung Thực Bài Toán 12 GB VRAM

Vì `pc-001` hiện chỉ còn `996 MiB` VRAM và `2.7 GiB` RAM khả dụng khi đang chạy `Qwen3.8-27B` gộp 2 card `[ĐO 2026-11-13]`, hệ thống **không cố nhồi mọi mô hình vào cùng một lúc** mà định nghĩa **3 chế độ vận hành tường minh**:

| Chế độ | Khi nào sử dụng | Phân bổ trên `pc-001` (Linux, RTX 3060 12GB) | Phân bổ trên `pc-win-01` (Windows, RTX 5070 12GB) | Khả năng tạo tài sản 3D |
| :--- | :--- | :--- | :--- | :--- |
| **`Mode M1`**<br>*Procedural & Kitbash Zero-VRAM*<br>**(Mặc định Pha 1–2)** | Chạy **24/7 song song** mà không cần hạ hay đổi cấu hình `Qwen3.8-27B` (98k ctx) đang phục vụ. | - Giữ nguyên `Qwen3.8-27B-Vision` (`:18080`).<br>- Tối đa **`1` container** `WarmBlenderCPU` (`--memory=1536m`, `--cpus=4`; tự xếp hàng chờ nếu `MemAvailable < 2.0 GiB`). | Giữ nguyên `ggml-rpc-server` phục vụ `Qwen3.8-27B`. | Đạo cụ Hard-Surface, nội thất, kiến trúc, tài sản lắp ghép từ thư viện mô-đun (`Kitbashing`), khớp xoay cơ học, UV + PBR thủ tục, kiểm định 6 góc nhìn VLM. |
| **`Mode M2`**<br>*Split Local 3D Foundry*<br>**(Chạy theo Phiên / Ca ở Pha 3–4)** | Khi cần sản xuất lô tài sản **Hữu cơ + Nướng PBR + Gắn xương tự động** 100% nội bộ không tốn phí đám mây. | - Chuyển topology có kiểm soát (`POST /api/cluster/topology`): nạp `Qwen3VL-8B-Instruct-Q4_K_M` (`~6.2 GiB` VRAM `[SUY LUẬN]` — đo thật ở Pha 0, giải phóng mạnh RAM & VRAM).<br>- Chạy `WarmBlenderWorker` (`1–2` slot). | Giải phóng `RTX 5070 12 GB + 64 GB DDR5` (hoặc dùng `GpuDockerSandbox` qua Lease trên Linux): nạp **tuần tự từng mô hình cố định** (`Concept` $\rightarrow$ `Hunyuan3D-2mini` $\rightarrow$ `MIA / UniRig`), xả VRAM ngay khi xong chặng (`keep_warm_s = 60s`). | Đầy đủ 100% dây chuyền 6 chặng: Nhân vật, sinh vật, đạo cụ điêu khắc, Retopo, Bake PBR High-to-Low, Auto-Rig + NLA Animation. |
| **`Mode M3`**<br>*Hybrid Burst Provider*<br>**(Tùy chọn mở rộng)** | Giữ `Qwen3.8-27B` gộp 2 card tại chỗ nhưng cần đúc mẫu chất lượng cao (`24–48 GB VRAM`). | Giữ nguyên như `Mode M1` (Headless Blender làm hậu kỳ, UV, kiểm định, xuất tệp). | Giữ nguyên `ggml-rpc-server`. | Gọi qua giao diện trừu tượng `MeshRigProvider` tới worker GPU lớn bên ngoài cho riêng bước `Hunyuan3D-2.1 Full PBR` / `TRELLIS Full`. |

---

### 1.3. Sáu Quyết Định Kiến Trúc Cốt Lõi (Lập thành `docs/adr/ADR-005-3d-asset-foundry.md` ở Pha 0)

- **`D-3D-1` — Kho hiện vật `ArtifactStore` đặt trên `/mnt/Data`, truyền theo tham chiếu băm `SHA-256`:**
  Một tài sản 3D đi qua 6 chặng sinh ra `20–150 MiB` dữ liệu trung gian (`raw_highpoly.glb`, 5 ảnh PBR PNG, `.blend`, `.glb`, `.fbx`, 6 ảnh chụp VLM). Do NATS giới hạn `256 KiB`/bản tin `[ĐỌC MÃ]` và NATS Object Store nghẽn nặng trên `8 MiB` `[ĐO: KE_HOACH_SERVERLESS]`, mọi nút trong `LangGraph` chỉ truyền cấu trúc JSON nhẹ chứa `artifact_uri = "sha256:<hex>"` trỏ tới `/mnt/Data/aicf-3d-foundry/artifacts/`, kèm chính sách dọn rác TTL cho tệp trung gian khi job hoàn tất.
- **`D-3D-2` — Ranh giới bảo mật kép cho thực thi Blender (`Sandbox` + `-Y` + Kiểm tra Đường dẫn Xuất):**
  Mã `bpy` do LLM sinh ra chỉ được chạy bên trong `DockerSandbox` / `WarmDockerPool` trên Linux `pc-001` với `--network=none`, `--cap-drop=ALL`, user `65534:65534`, hệ thống tệp gốc chỉ-đọc, chỉ mở ghi trên `/work` (`tmpfs` hoặc thư mục job cô lập giới hạn dung lượng `512 MiB`). Mọi lệnh gọi Blender bắt buộc có cờ `--disable-autoexec` (`-Y`) để vô hiệu hóa mã nhúng trong tệp `.blend`. Trước khi thu hồi tệp xuất về `ArtifactStore`, trình chạy từ chối mọi symlink (`Path.is_symlink()`) hoặc đường dẫn thoát khỏi `/work`.
- **`D-3D-3` — Giao diện Công việc Bất đồng bộ (`Async Job + SSE Stream`):**
  Thời gian hoàn thành 1 tài sản 3D dao động từ `~15 s` (đạo cụ tham số đơn giản) đến `~180–300 s` (nhân vật hữu cơ có nướng PBR và gắn xương) `[SUY LUẬN]`, vượt ngưỡng chờ HTTP đồng bộ thông thường. Dịch vụ cung cấp `POST /api/foundry3d/jobs` (trả ngay `job_id`), `GET /api/foundry3d/jobs/{id}`, luồng sự kiện tiến độ `GET /api/foundry3d/jobs/{id}/events` (SSE), `DELETE /api/foundry3d/jobs/{id}` (hủy việc & thu hồi container), và `GET /api/foundry3d/jobs/{id}/artifacts/{filename}` (kiểm tra đúng chủ sở hữu `tenant` trước khi stream tệp).
- **`D-3D-4` — Tôn trọng tuyệt đối Bộ Bảo Vệ Tài Nguyên (`MemAvailable` Gate & `worker-guard` Lease):**
  Mọi lời gọi LLM/VLM từ `foundry3d/` tới Gateway `:18080/v1/chat/completions` đều dùng khoá `tenant` riêng với `X-Priority: batch`. Trước khi nhận chặng hậu kỳ Blender trên `pc-001`, bộ điều phối đọc `/proc/meminfo`: nếu `MemAvailable < 2,048 MiB` thì giữ job ở trạng thái `queued_low_mem` thay vì ép chạy gây `systemd-oomd`. Khi chạy container GPU ở `Mode M2`, bắt buộc xin Lease ngắn hạn (`TTL = 30 s`) qua cầu nối HTTP của `site-gateway` giống hệt `serverless/` (`D3` trong `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md`).
- **`D-3D-5` — Quy ước Hệ tọa độ Trung gian Duy nhất (`Canonical Spatial Convention`):**
  Để dứt điểm lỗi lộn trục giữa các mô hình AI 3D và Game Engine, toàn bộ Chặng 1–5 khóa cứng quy ước nội bộ chuẩn Blender: **`1.0 unit = 1.0 mét`, trục đứng `Z-up`, mặt trước vật thể hướng về `-Y`, trục phải `+X`, điểm gốc `Origin (0, 0, 0)` nằm chính xác tại tâm mặt đáy hộp bao chạm mặt sàn `z_min = 0.000`, `Rotation = (0,0,0)`, `Scale = (1,1,1)`**. Riêng Chặng 6 (`exporter.py`) chịu trách nhiệm chuyển đổi ma trận trục chuẩn xác theo từng đích (`glTF 2.0` tự chuyển sang `Y-up` chuẩn Khronos cho Godot 4 / Three.js; `FBX` cấu hình trục đích riêng cho Unity và Unreal Engine 5).
- **`D-3D-6` — Tách biệt Tuyệt đối Người Làm và Bàn Cân Chấm Điểm:**
  Tương tự bài học từ `KE_HOACH_BEST_OF_N_CODE.md`, LLM không bao giờ được tự sinh mã kiểm thử để tự chấm mình. Bộ kiểm định toán học hình học (`evaluator_3d.py`) là thư viện cố định do kỹ sư viết, có bộ kiểm thử đơn vị và kiểm thử đột biến bảo vệ.

---

## PHẦN 2: KIẾN TRÚC CHI TIẾT DÂY CHUYỀN 6 CHẶNG & HỢP ĐỒNG KIỂM ĐỊNH (`foundry3d/`)

### 2.1. Sơ đồ Luồng Tổng thể

```text
Khách (Web Studio / CLI / API)
  │  POST /api/foundry3d/jobs (Authorization: Bearer <tenant_key>)
  ▼
apps/site-gateway (:18080, Go)
  │  Xác thực Tenant, RBAC, Rate-limit, gắn X-Aicf-Tenant + X-Aicf-Proxy-Secret
  ▼
foundry3d/service.py (127.0.0.1:18120, Python)
  │  Khởi tạo StateGraph trên LangGraph + Checkpoint PostgreSQL
  ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                   ĐỘNG CƠ DÂY CHUYỀN 6 CHẶNG (LangGraph StateGraph)               │
│                                                                                   │
│  [Chặng 1: Spec & Canonical Concept] ──(gọi :18080/v1, X-Priority: batch)         │
│       │  Xuất AssetSpec JSON (+ Ảnh chuẩn A-pose/3-4 nếu Mode M2/M3 -> VLM duyệt) │
│       ▼                                                                           │
│  [Chặng 2: Hybrid Geometry Synthesis]                                             │
│       ├── Nhánh Tham số/Khối cứng: bpy Coder + RAG Blender 4.2 API                │
│       └── Nhánh Hữu cơ (Mode M2/M3): Hunyuan3D-2mini / TRELLIS-fp16 -> raw.glb    │
│       ▼                                                                           │
│  [Chặng 3: Blender Technical Art Post-Proc] (WarmBlenderPool cô lập trên pc-001)  │
│       │  Chuẩn hóa mét & Z=0 -> Dọn lưới -> LOD0..2 + UCX_Collider -> UV & Bake   │
│       ▼                                                                           │
│  [Chặng 4: Auto-Rigging & Animation] (Bỏ qua nếu static_prop)                     │
│       │  Khớp cơ học bpy HOẶC MIA / UniRig (<=4 xương/đỉnh) + Nạp NLA CC0 clips   │
│       ▼                                                                           │
│  [Chặng 5: Bàn Cân Kiểm Định Kép (evaluator_3d.py)]                               │
│       ├─ 5A. Cổng Toán học Hình học & Biến dạng Xương (0 token, < 0.5s)           │
│       └─ 5B. Cổng VLM 6-View Critic (6 ảnh 512x512 gửi :18080/v1 Vision)          │
│       │                                                                           │
│       ├──(Nếu LỖI & repair_count < 3)──▶ Quay lui ĐÚNG chặng gây lỗi kèm số đo mm │
│       ▼  (Nếu ĐẠT cả 5A & 5B)                                                     │
│  [Chặng 6: Engine-Ready Exporter + Khronos glTF-Validator]                        │
│       └─ Lưu .glb, .fbx, source.blend, PBR maps, qa_report.json vào ArtifactStore │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2. Cấu trúc Thư mục Dự kiến trong Kho Mã Nguồn

```text
ai-compute-fabric/foundry3d/
├── __init__.py
├── schemas.py                      # Pydantic models: AssetSpec, StageArtifact, GeometryReport, VLMVerdict, JobStatus
├── artifact_store.py               # Lưu trữ nội dung băm SHA-256 trên /mnt/Data/aicf-3d-foundry/artifacts/ + cô lập tenant
├── warm_blender.py                 # Quản lý Warm Headless Blender 4.2 container (dựa trên WarmDockerPool) + cổng MemAvailable
├── api_rag_b42.py                  # Chỉ mục tra cứu nhanh cú pháp chuẩn Blender 4.2 LTS (chống gọi hàm cũ đã xóa)
├── pipeline_graph.py               # Đồ thị LangGraph 6 chặng + logic định tuyến sửa lỗi có giới hạn (max_repairs=3)
├── providers/
│   ├── llm_vlm_client.py           # Khách gọi Gateway :18080/v1 (hỗ trợ X-AICF-Thinking on/off + gửi 6 ảnh Base64)
│   ├── mesh_provider.py            # Giao diện trừu tượng + adapter Hunyuan3D-2mini / TRELLIS-fp16 (xin Lease GPU)
│   └── rig_provider.py             # Giao diện trừu tượng + adapter Make-It-Animatable / UniRig / Rigify fallback
├── blender_scripts/                # Các kịch bản cố định đã kiểm toán chạy bên trong Blender Headless (-b -Y)
│   ├── safe_exec_bpy.py            # Trình bọc chạy mã bpy của LLM an toàn, bắt ngoại lệ & kiểm tra đối tượng sinh ra
│   ├── postproc_pipeline.py        # Căn Z=0, Apply Scale, Clean Mesh, Quadriflow/Decimate LOD0..2, Convex Collider, UV, Bake PBR
│   ├── rig_and_nla.py              # Chuẩn hóa <= 4 xương/đỉnh, kiểm tra lem trọng số, gắn NLA tracks từ kho CC0
│   ├── evaluator_3d.py             # Bàn cân Toán học 3D (BVHTree overlap, grounding, UV overlap, khớp xoay 90 độ spike check)
│   ├── render_6views.py            # Dựng 6 camera chuẩn + trục màu XYZ, kết xuất nhanh Workbench/EEVEE ra PNG 512x512
│   └── export_bundle.py            # Xuất .glb / .fbx / .blend theo đúng hồ sơ Godot 4 / Unity / UE5 / Web
└── service.py                      # HTTP/SSE Server (127.0.0.1:18120) nối sau reverse-proxy của site-gateway
```

---

### 2.3. Đặc tả Kỹ thuật Từng Chặng & Cổng Kiểm Định

#### Chặng 1: Đặc tả Kỹ thuật & Ảnh Chuẩn Hóa (`Spec & Canonical Concept`)
- **Đầu vào:** Câu lệnh của người dùng (kèm ảnh tham chiếu nếu có) và cấu hình đích (mặc định: `target_engine="godot4"`, `poly_budget_lod0=6000`).
- **Xử lý:**
  1. Gọi `Planner Agent` (`X-AICF-Thinking: on`, `max_tokens=2500`) phân rã yêu cầu thành `AssetSpec` hợp lệ theo `pydantic`:
     - `archetype`: `"static_prop" | "articulated_prop" | "modular_kit" | "humanoid_char" | "creature_char"`.
     - `dimensions_m`: `[width_x, depth_y, height_z]` thực tế ngoài đời (ví dụ ghế gỗ `[0.50, 0.52, 0.88]`).
     - `parts[]`: danh sách bộ phận, mỗi bộ phận ghi rõ phương pháp tạo (`"procedural_bpy"` hay `"neural_mesh"`), hộp bao tương đối và điểm neo tiếp xúc (`anchor_to`, `contact_face`) để chống lỗi bay lơ lửng.
  2. Nếu có bộ phận `"neural_mesh"` (chỉ mở ở `Mode M2/M3`): sinh 1 ảnh chuẩn hóa (nền trắng `#FFFFFF`, ánh sáng đều không đổ bóng đen gắt; tư thế chữ A tách rõ tay chân `30°` nếu là nhân vật), tách nền sạch và qua **VLM Concept Gate** kiểm tra đủ bộ phận trước khi cho phép đúc 3D.

#### Chặng 2: Tổng hợp Hình học Lai (`Hybrid Geometry Synthesis`)
- **Nhánh Tham số / Khối cứng (`procedural_bpy`):**
  - `Blender Coder Agent` (`X-AICF-Thinking: off` để tận dụng tốc độ `draft-mtp` `~40 tok/s` `[ĐO]`, kèm bảng tra cứu nhanh `api_rag_b42.py` giải thích sự thay đổi API của Blender 4.2 như `Principled BSDF` socket names, `shade_auto_smooth` modifier mới thay cho thuộc tính mesh cũ của 3.x).
  - Sinh mã tạo từng bộ phận đặt tên đúng theo `parts[].id` và lắp ghép theo điểm tiếp xúc thay vì đoán mò tọa độ tuyệt đối.
- **Nhánh Hữu cơ (`neural_mesh` ở `Mode M2/M3`):**
  - Gọi `mesh_provider` chạy `Hunyuan3D-2mini-Turbo` (hoặc `TRELLIS-fp16`) xuất `raw_highpoly.glb` (~`150k–400k` tam giác).

#### Chặng 3: Hậu kỳ Kỹ thuật Tự động trong Blender (`postproc_pipeline.py`)
Chạy tự động hoàn toàn bên trong `WarmBlenderPool`:
1. **Chuẩn hóa Không gian:** Gom nhóm, co giãn tỷ lệ khớp đúng `dimensions_m`, dịch chuyển sao cho điểm thấp nhất chạm đúng mặt phẳng `Z = 0.000`, đặt `Origin` về `(0, 0, 0)` và gọi `bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)`.
2. **Vệ sinh Hình học:** Gộp đỉnh trùng (`merge_by_distance = 0.0001`), xóa cạnh/đỉnh mồ côi (`delete_loose`), xóa mảnh rác trôi nổi có thể tích `< 0.5%` tổng thể tích (trừ khi được khai báo trong `parts[]`), vá lỗ thủng nhỏ và đồng bộ pháp tuyến hướng ra ngoài (`recalculate_normals`).
3. **Tạo Đa mức Chi tiết (`LOD0..2`) & Lưới Va chạm (`UCX_`):**
   - Nếu lưới vượt `poly_budget_lod0`: dùng `Quadriflow Remesh` (cho nhân vật/sinh vật cần lưới tứ giác đều để uốn khớp xương mượt) hoặc `Decimate (Planar 5° + Collapse)` (cho đồ vật cứng để giữ phẳng mặt và sắc cạnh) đưa về đúng `LOD0`.
   - Tự động sinh tiếp `LOD1` (`50%` số tam giác của `LOD0`), `LOD2` (`25%`) và khối va chạm vật lý lồi `UCX_<asset_name>` (`<= 64` mặt bằng `convex_hull`).
4. **Trải UV & Nướng Bản đồ Vật liệu PBR (`High-to-Low Cage Baking`):**
   - Với lưới chưa có UV sạch: chạy `Smart UV Project` (`angle_limit=66°`, `island_margin=0.02`, `area_weight=1.0`) hoặc `xatlas`.
   - Tự động nướng chi tiết bề mặt và màu sắc từ bản `HighPoly` sang `LOD0` bằng Cycles (`cage_extrusion` tự tính bằng `1.5%` đường chéo hộp bao) ra 5 tệp PNG chuẩn: `BaseColor`, `Normal` (OpenGL `+Y` cho glTF/Godot, tự đảo kênh `G` sang DirectX `-Y` khi xuất UE5), `Roughness`, `Metallic`, `AO`.

#### Chặng 4: Gắn Xương, Trọng Số Da & Nạp Chuyển Động (`rig_and_nla.py`)
- **`static_prop`**: Bỏ qua Chặng 4.
- **`articulated_prop`** (rương có nắp, cửa có bản lề, xe có bánh quay): Đặt `Empty / Bone` đúng trục bản lề đã khai báo trong `AssetSpec` và tạo 1 NLA clip kiểm thử đóng/mở `0° -> 90°`.
- **`humanoid_char` / `creature_char`**:
  - Gọi `Make-It-Animatable` (nhân vật 2 chân) hoặc `UniRig` (sinh vật 4 chân/quái vật) trên lưới sạch `LOD0`.
  - Chạy bước hậu kiểm bắt buộc trong `bpy`: giới hạn tối đa **4 xương ảnh hưởng trên 1 đỉnh (`limit_total(limit=4)`)** (quy chuẩn bắt buộc của mọi Game Engine thời gian thực), chuẩn hóa tổng trọng số từng đỉnh $\sum w_i = 1.0$ (`normalize_all()`), và ánh xạ bộ chuyển động chuẩn giấy phép **CC0** (`Idle`, `Walk`, `Run`, `Attack`) vào các dải `NLA Tracks`.

#### Chặng 5: Bàn Cân Kiểm Định 3D Kép (`evaluator_3d.py` + `VLM 6-View Critic`)

Mỗi tài sản phải vượt qua **cả 2 tầng kiểm định** mới được xuất xưởng:

1. **Tầng 5A — Cổng Toán học Hình học & Biến dạng Cứng (`Deterministic Gate`, `< 0.5 s`, tốn `0` token):**
   - `G1_PolyBudget`: `0 < tris(LOD0) <= poly_budget_lod0 * 1.05`; `tris(UCX_Collider) <= 128`.
   - `G2_TransformAndGround`: `scale == (1, 1, 1) ± 1e-4`; độ lệch mặt đáy so với sàn `|z_min| <= 0.002 m`; **Phát hiện bay lơ lửng (`Floating Part Check`)**: xây đồ thị kề giữa các bộ phận — mọi bộ phận không chạm sàn (`z_min > 0.005 m`) bắt buộc phải tiếp xúc hoặc giao cắt có chủ đích trong bán kính `<= 0.005 m` với ít nhất một bộ phận đỡ bên dưới; nếu treo lơ lửng giữa không trung $\rightarrow$ **BÁO LỖI KÈM KHOẢNG HỞ MILIMET CHÍNH XÁC**.
   - `G3_IllegalOverlap`: Dùng `mathutils.bvhtree.BVHTree.FromBMesh` kiểm tra tỷ lệ thể tích/mặt đâm xuyên trái phép giữa các khối rời không khai báo ghép nối.
   - `G4_MeshAndUVHealth`: `0` mặt suy biến diện tích bằng `0`; tỷ lệ UV tràn khung `[0, 1]` bằng `0%`; tỷ lệ chồng lấn UV (`UV Island Overlap`) `< 2%`; bản đồ `BaseColor` không bị đen đặc do lỗi nướng ngược tia (`mean_luminance > 0.02` trừ khi vật liệu khai báo màu đen tuyền).
   - `G5_RigDeformationStress` (chỉ áp dụng khi có xương): Số đỉnh không có trọng số xương `unweighted_vertices == 0`; xoay thử các khớp chính (`±60°` ở vai, khuỷu, hông, gối) và đo tỷ lệ kéo giãn cạnh cực đại so với tư thế nghỉ: nếu có đỉnh bị rách văng nhọn hoắt (`max_edge_stretch_ratio > 3.5`) $\rightarrow$ **BÁO LỖI LEM/THIẾU TRỌNG SỐ DA**.

2. **Tầng 5B — Giám khảo Thị giác Đa Góc Nhìn (`VLM 6-View Critic`):**
   - Chỉ gọi sau khi Tầng 5A đã **XANH 100%** (tiết kiệm tối đa băng thông GPU của VLM).
   - Kết xuất 6 tấm ảnh `512x512` trên `xvfb-run` (`Front`, `Right`, `Back`, `Iso 3/4 PBR`, `Iso Wireframe`, và `Mid-Walk Pose Frame` nếu có xương) kèm mũi tên hệ trục màu chuẩn ở góc khung hình.
   - Gửi cả 6 ảnh vào 1 request duy nhất tới `:18080/v1/chat/completions` (nằm gọn trong giới hạn `<= 8 ảnh/request` của Gateway `[ĐỌC MÃ]`). VLM trả về JSON định dạng cứng:
     `{pass: bool, scores: {semantic: 1..5, proportion: 1..5, front_axis_correct: bool, texture_clean: bool}, failed_stage: 1|2|3|4|null, fix_instruction: "..."}`.
   - Nếu trượt Tầng 5A hoặc 5B và `repair_count < 3`: `LangGraph` tự động định tuyến lùi về **đúng chặng gây lỗi** kèm báo cáo sai số milimet của Tầng 5A và nhận xét của Tầng 5B. Nếu hết `3` lượt sửa vẫn chưa qua cổng: đánh dấu `status = "needs_human_review"` kèm đầy đủ hiện vật và biên bản lỗi, không trả hàng lỗi âm thầm.

#### Chặng 6: Đóng gói Xuất xưởng & Cổng Chuẩn Hóa Khronos (`export_bundle.py`)
- Xuất gói tệp theo hồ sơ đích (`godot4`, `unity`, `ue5`, `web_glb`).
- Chạy trình kiểm định độc lập `gltf_validator` trên tệp `asset.glb` vừa xuất: bắt buộc **`issues.numErrors == 0`**.

---

## PHẦN 3: LỘ TRÌNH 6 PHA THỰC HIỆN (CÓ `CHECK PASS` CỨNG & KIỂM ĐỘT BIẾN)

*(Quy tắc xuyên suốt: Không vượt qua đủ `Check Pass` và `Kiểm đột biến` của pha trước thì cấm sang pha sau.)*

### Pha 0 — Thăm dò Đĩa, Dựng `WarmBlenderWorker` Cô Lập & Đo Nền VLM (2 ngày)
- **0.1.** Hỏi xác nhận chủ dự án cho phép tạo thư mục `/mnt/Data/aicf-3d-foundry/` (trần dung lượng `25 GiB`). Ghi lại `df -h` ban đầu.
- **0.2.** Tải bản chính thức `Blender 4.2 LTS Linux x64` vào `/mnt/Data/aicf-3d-foundry/runtime/`, kiểm tra mã băm `SHA-256` chính thức từ `blender.org`. Cấu hình chạy thử bên trong container `python:3.12-slim` / `ubuntu:22.04` (có sẵn trong cache Docker `[ĐO]`, tuyệt đối không phình `/` quá `300 MiB`) kèm `xvfb-run` + Mesa `llvmpipe`.
- **0.3.** Viết `benchmarks/foundry3d/p0_probe.py` đo thực tế 30 vòng lặp:
  - Thời gian khởi động lạnh (`cold_start_ms`) so với gọi ấm (`warm_exec_ms`), mức ăn RAM đỉnh (`peak_rss_mb`), và thời gian kết xuất 6 góc `512x512` trên CPU.
  - Tạo bộ **10 mẫu 3D đối chứng cố định** (5 mẫu chuẩn + 5 mẫu gài lỗi thật: chân ghế lơ lửng `15 cm`, hai khối xuyên nhau `40%`, lật ngược mặt trước/sau, bản đồ màu đen xì, gãy nhọn khớp gối khi xoay `60°`). Đo xem `evaluator_3d.py` + `Qwen3.8-27B-Vision` (và `Qwen3VL-8B` nếu được phép thử ở cửa sổ bảo trì) bắt trúng bao nhiêu trên 10 mẫu.
- **0.4.** Chốt các quyết định `D-3D-1..6` thành `docs/adr/ADR-005-3d-asset-foundry.md`.
- **Check Pass Pha 0:**
  - Phân vùng `/` vẫn giữ `>= 1.8 GiB` trống, `/mnt/2d47...` giữ `>= 2.2 GiB` trống `[ĐO]`.
  - Container Blender chạy ở chế độ `--network=none --disable-autoexec` kết xuất ra đủ 6 ảnh PNG hợp lệ với `peak_rss_mb <= 1,536 MiB`.
  - Tổ hợp `(Tầng 5A + Tầng 5B)` phân loại đúng `>= 9/10` mẫu chuẩn/lỗi cố định; lưu số liệu thô vào `benchmarks/reports/p_3d_0_probe.json`.

### Pha 1 — Xưởng Hậu Kỳ Blender, Bàn Cân 3D & Luồng Tài Sản Tham Số `Mode M1` (3 – 4 ngày)
- **1.1.** Hiện thực `schemas.py`, `artifact_store.py`, `warm_blender.py` (tích hợp cổng chặn `MemAvailable < 2,048 MiB`), `api_rag_b42.py`, `postproc_pipeline.py`, `evaluator_3d.py`, `render_6views.py`, `export_bundle.py`.
- **1.2.** Dựng đồ thị `pipeline_graph.py` trên `LangGraph` nối vào Gateway `:18080/v1` ở **`Mode M1` (`0 MiB` VRAM thêm, không đụng chạm `Qwen3.8-27B` đang chạy)** cho nhóm `static_prop` bằng `bpy` + `bmesh` + `Geometry Nodes`.
- **1.3.** Xây bộ đề chuẩn `benchmarks/foundry3d/prop20_suite.json` (20 đạo cụ/nội thất từ dễ đến vừa: bàn, ghế, rương kho báu, đèn đường, kệ sách, thùng gỗ, khiên, búa, cột đá...). Đo tỷ lệ qua cổng toàn diện ở lần sinh đầu (`pass@1`) so với sau tối đa 3 vòng tự sửa lỗi (`pass@repair<=3`).
- **Kiểm đột biến bắt buộc (8/8 đột biến phải bị giết ĐỎ):**
  1. Vật thể đặt tại `z_min = +0.12 m` (bay lơ lửng) $\rightarrow$ `G2_TransformAndGround` phải chặn.
  2. Mặt bàn tách rời khỏi 4 chân bàn `0.08 m` $\rightarrow$ `Floating Part Check` phải chặn.
  3. Hai khối đặc đâm xuyên `35%` thể tích trái phép $\rightarrow$ `G3_IllegalOverlap` phải chặn.
  4. Lưới vượt trần đa giác `2x` $\rightarrow$ `G1_PolyBudget` phải chặn.
  5. Mã `bpy` cố gọi `socket.connect` hoặc đọc `/etc/passwd` $\rightarrow$ Sandbox chặn cứng.
  6. Tệp `.blend` nhúng Text-block `Register` tự động chạy lệnh hệ thống $\rightarrow$ cờ `-Y` chặn cứng (không thực thi).
  7. Mã `bpy` tạo symlink `/work/asset.glb -> /etc/hostname` $\rightarrow$ `artifact_store` từ chối thu hồi.
  8. Ép ngưỡng `MemAvailable` giả lập xuống thấp hơn ngưỡng an toàn $\rightarrow$ job chuyển sang chờ, không mở thêm tiến trình Blender.
- **Check Pass Pha 1:** 8/8 đột biến bị giết; 100% tệp `.glb` qua cổng đạt `0 errors` trên `glTF-Validator`; báo cáo rõ số liệu `pass@1` vs `pass@repair<=3` (kể cả các bài thất bại).

### Pha 2 — Lắp Ráp Đa Bộ Phận & Đạo Cụ Có Khớp Cơ Học (`Modular & Articulated Props`) (3 ngày)
- **2.1.** Mở rộng `Planner` và `bpy Coder` hỗ trợ cây phân cấp cha-con (`Parent-Child Hierarchy`), điểm neo lắp ghép (`Anchor Snapping`) và khớp xoay/trượt cơ học (cửa mở bản lề, bánh xe quay quanh tâm trục, nắp rương mở `90°`, ngăn kéo trượt).
- **2.2.** Bổ sung kiểm tra va chạm động học trong `evaluator_3d.py`: quét góc mở khớp từ `0°` đến `max_angle` theo 5 bước để đảm bảo cánh cửa/nắp rương mở ra không đâm xuyên bản lề hoặc khung bao.
- **Check Pass Pha 2:** Bộ 12 bài đạo cụ có khớp cơ học & bộ mô-đun phòng ốc chạy tự động; các khớp xoay đúng tâm (không bị văng lệch trục do đặt sai `Origin`), đạt `0 errors` trên `glTF-Validator`.

### Pha 3 — Tích Hợp Đúc Lưới Hữu Cơ (`Hunyuan3D-2mini` / `TRELLIS-fp16`) & Nướng PBR High-to-Low ở `Mode M2` (4 ngày)
- **3.1.** Xin phép chủ dự án mở cửa sổ thử nghiệm `Mode M2` (hoặc dùng khe Lease trên `GpuDockerSandbox` `--runtime=nvidia`). Tải trọng số `Hunyuan3D-2mini-Turbo` vào `/mnt/Data/aicf-3d-foundry/models/` (mount `:ro` theo đúng mẫu `ModelCache` `D7` của `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md`).
- **3.2.** Đo thực tế mức chiếm VRAM đỉnh, thời gian đúc 1 khối từ ảnh chuẩn hóa, và **kiểm chứng thu hồi VRAM khẩn cấp (`G-RECLAIM < 2 s`)**: đang đúc dở mà có tín hiệu thu hồi từ `worker-guard` thì container phải nhả VRAM trong `< 2 s` mà không làm treo driver. Nếu `TRELLIS-fp16` vượt vách VRAM `12 GB` gây tràn RAM/chậm sụp, ghi nhận bằng số đo thật và giữ `Hunyuan3D-2mini` làm mặc định duy nhất.
- **3.3.** Khép kín luồng: `Canonical Image -> Raw High-Poly (~300k tris) -> Quadriflow/Decimate LOD0 (6k–10k tris) -> Automated Cage PBR Bake -> 5 PBR PNGs -> VLM 6-View QA -> .glb`.
- **Check Pass Pha 3:** `G-RECLAIM < 2 s` xanh 100% (10 vòng kiểm thử liên tiếp); tỷ lệ texel đen lỗi trên bản đồ `BaseColor/Normal` `< 5%`; đo p50/p95 thời gian và điện năng tiêu thụ (`Wh/asset`) trên 15 mẫu vật thể hữu cơ.

### Pha 4 — Gắn Xương Tự Động (`MIA` + `UniRig`) & Đóng Gói Chuyển Động NLA (4 ngày)
- **4.1.** Tích hợp `Make-It-Animatable` (nhân vật 2 chân) và `UniRig` (thú/quái vật) vào `rig_provider.py` chạy theo cơ chế nạp tuần tự (chỉ nạp sau khi `Hunyuan3D-2mini` đã xả VRAM).
- **4.2.** Chuẩn bị kho 5 chuyển động gốc giấy phép **CC0** đã kiểm chứng nguồn & ghi `SHA-256` (`Idle`, `Walk`, `Run`, `Jump`, `Attack`), viết `rig_and_nla.py` ánh xạ xương tự động, khóa `<= 4` xương/đỉnh và nhúng vào `NLA Tracks` của `.glb` / `.fbx`.
- **4.3.** Kích hoạt cổng `G5_RigDeformationStress` + kiểm đột biến (cố tình xóa trọng số 1 bàn tay và cố tình làm dính trọng số giữa 2 ống chân $\rightarrow$ cổng kiểm tra kéo giãn cạnh `stretch_ratio > 3.5` phải bắt sống ĐỎ).
- **Check Pass Pha 4:** Xuất thành công `.glb` nhân vật/sinh vật có xương chạy được đủ dải animation trong trình đọc chuẩn glTF / Godot 4 mà không văng đỉnh hay sụp lưới.

### Pha 5 — Dịch Vụ HTTP, Tích Hợp `site-gateway`, OpenAPI & Xem Trước 3D Trên Web Studio (3 ngày)
- **5.1.** Hoàn thiện `foundry3d/service.py` (`127.0.0.1:18120`), thêm reverse-proxy `/api/foundry3d/*` vào `apps/site-gateway` (kèm cờ `-foundry3d-enabled=false`, kiểm tra `X-Aicf-Proxy-Secret`, giới hạn số job đang chạy theo `tenant`).
- **5.2.** Khai báo toàn bộ schema vào `contracts/openapi/aicf_v1.yaml`; thêm khung xem trước `.glb` trực tiếp trên trình duyệt trong `studio/` (xoay 360°, bật/tắt lưới wireframe, chọn xem thử `LOD0/LOD1/Collider` và chạy thử clip `Walk/Idle`).
- **Kiểm đột biến bảo mật:** (a) Gọi thẳng cổng `18120` không có `X-Aicf-Proxy-Secret` $\rightarrow$ `401`; (b) Dùng khoá của `Tenant B` tải `.glb` hoặc xem SSE của `Tenant A` $\rightarrow$ `403/404`.
- **Check Pass Pha 5:** `go test ./...`, toàn bộ `pytest`, `test_openapi_conformance.py` và `make verify-release-v1` đều **XANH 100%**.

### Pha 6 (Tùy chọn Mở rộng) — Tương Tác Trực Tiếp `blender-mcp` Trên Máy Windows & Tự Nâng Cấp LoRA `bpy`
- **6.1.** Dựng cầu nối tùy chọn dựa trên `ahujasid/blender-mcp` để người dùng mở giao diện đồ họa Blender trên màn hình Windows `pc-win-01` và ra lệnh tương tác từng bước qua Gateway `:18080` (mọi thao tác sửa trực tiếp trên phiên làm việc của chính người dùng có nút Undo rõ ràng).
- **6.2.** Thu thập các cặp đối kháng `(Mã bpy bị lỗi ở vòng 0 -> Mã bpy vượt qua Bàn cân Chặng 5 ở vòng sửa)` lưu thành tập dữ liệu sạch để tinh chỉnh nhẹ bộ điều hợp LoRA chuyên viết `bpy` chuẩn Blender 4.2 cho mô hình trên cụm.

---

## PHẦN 4: TIÊU CHÍ HOÀN THÀNH TOÀN DỰ ÁN & BÁO CÁO ĐO LƯỜNG (`benchmarks/reports/P8_FOUNDRY_3D.md`)

Chỉ được tuyên bố hoàn thành khi xuất bản báo cáo thực nghiệm `benchmarks/reports/P8_FOUNDRY_3D.md` chứa đầy đủ số liệu `[ĐO]` cho các câu hỏi sau:
1. **Hiệu quả của Vòng lặp Kiểm định Kép:** Tỷ lệ tài sản đạt chuẩn kỹ thuật ở lượt đầu (`pass@1`) so với sau vòng tự sửa (`pass@repair<=3`) tách riêng theo 3 nhóm: Đạo cụ tham số (`Mode M1`), Vật thể hữu cơ tĩnh (`Mode M2`), và Nhân vật có xương (`Mode M2`).
2. **Độ trung thực của Giám khảo VLM:** Tỷ lệ VLM bắt đúng lỗi thật so với tỷ lệ VLM "bắt nhầm" (báo lỗi ảo trên mẫu tốt) hoặc "bỏ lọt" — đo đối chiếu giữa `Qwen3.8-27B-Vision` và `Qwen3VL-8B`.
3. **Chi phí Vật lý trên mỗi Tài sản 3D:** Thời gian hoàn thành (p50/p95 theo từng chặng), đỉnh RAM/VRAM thực đo, và điện năng tiêu thụ (`Wh/asset` & chi phí quy đổi VNĐ theo đơn giá `3,000đ/kWh`, đồng bộ phương pháp tính tại `BAO_CAO_DANH_GIA_KIEN_TRUC_VA_DO_THUC_TE_CODER_30B.md`).
4. **Giới hạn trung thực (Những gì hệ thống làm CHƯA TỐT):** Ghi thẳng các dạng vật thể dễ thất bại (ví dụ: vật thể trong suốt phức tạp như thủy tinh khúc xạ, bàn tay 5 ngón tách rời ở ngân sách đa giác cực thấp `< 1,500 tris`, hoặc quần áo nhiều lớp bay phấp phới) để người dùng biết rõ biên năng lực.

---

## PHẦN 5: MA TRẬN 10 RỦI RO SÁT SƯỜN PHẦN CỨNG & CÁCH PHÒNG NGỪA

| # | Rủi ro kỹ thuật | Hậu quả nếu xảy ra | Cơ chế chặn cứng trong thiết kế |
| :---: | :--- | :--- | :--- |
| **R1** | Ghi nhầm Docker layer / cache model / tệp tạm vào `/` (`2.0 GiB` trống `[ĐO]`) hoặc repo (`2.5 GiB` trống `[ĐO]`) | Đầy đĩa `100%`, sập PostgreSQL, NATS và dịch vụ hệ thống | Đặt toàn bộ runtime Blender, model cache, `TMPDIR`, `XDG_CACHE_HOME` và `ArtifactStore` sang `/mnt/Data/aicf-3d-foundry/` (`109 GiB` trống `[ĐO]`); kiểm tra `df -h` tự động ở mọi pha. |
| **R2** | Chạy Blender Headless khi RAM `pc-001` đang căng (`2.7 GiB` khả dụng `[ĐO]`) | Kích hoạt `systemd-oomd` giết tiến trình hoặc đẩy hệ thống vào Swap gây treo cứng | Giới hạn cứng container `--memory=1536m --memory-swap=2048m`, tối đa `1` worker ở `Mode M1`, cộng cổng kiểm tra `/proc/meminfo` (`MemAvailable >= 2,048 MiB`) trước khi nhận việc. |
| **R3** | Binary `blender` chính thức không chạy được trên image `alpine` (`musl`) | Hỏng ngay từ bước khởi chạy worker | Khóa cứng base image thuộc họ `glibc` đã có sẵn trong Docker cache (`python:3.12-slim` hoặc `ubuntu:22.04` `[ĐO]`), xác minh ở Pha 0. |
| **R4** | Gọi hàm `bpy` đã bị xóa hoặc đổi tên trong `Blender 4.2 LTS` (do LLM nhớ cú pháp Blender 2.8/3.x) | Tỷ lệ lỗi biên dịch/chạy `bpy` ở vòng đầu rất cao | Nhúng bảng tra cứu nhanh `api_rag_b42.py` (liệt kê các thay đổi breaking changes của Blender 4.0–4.2) vào system prompt của `Blender Coder Agent`. |
| **R5** | Mã `bpy` độc hại hoặc tệp `.blend` cài cắm script tự chạy (`Autoexec`) | Đọc trộm dữ liệu host, mở kết nối mạng trái phép hoặc xóa tệp | Bắt buộc chạy trong `DockerSandbox` (`--network=none`, `--cap-drop=ALL`, user `65534`), luôn truyền cờ `-Y` (`--disable-autoexec`), kiểm tra chống symlink escape. |
| **R6** | Nướng bản đồ PBR (`Cycles Bake`) bị mảng đen hoặc lem chéo ở khe hẹp (nách, kẽ ngón, hốc lõm) | Tài sản nhìn bẩn, sai màu nghiêm trọng dù lưới đúng | Tự động tính `cage_extrusion` theo kích thước hộp bao + cổng `G4_MeshAndUVHealth` kiểm tra tỷ lệ điểm ảnh đen bất thường + VLM soi 6 góc để tự chạy lại Bake với tham số hiệu chỉnh. |
| **R7** | `Quadriflow Remesh` gặp lưới hở (`non-manifold`) làm sụp hoặc biến mất chi tiết mỏng | Lỗi mất bộ phận (mất kiếm, mất tai, thủng mặt) | Luôn chạy bước vá lỗ `fill_holes` & `voxel_remesh` nhẹ làm kín thể tích trước khi gọi `Quadriflow`, kèm fallback tự động về `Planar + Collapse Decimate` nếu thể tích sau remesh lệch `> 15%`. |
| **R8** | Vách VRAM WDDM trên Windows `pc-win-01` (`RTX 5070 12 GB`) khi chạy `Mode M2` | Không báo lỗi OOM nhưng tràn sang Shared System RAM làm tốc độ tụt 5–10 lần `[ĐO]` | Nạp tuần tự từng mô hình đơn lẻ (`6–8 GiB`), xả sạch `torch.cuda.empty_cache()` / đóng worker giữa các chặng, theo dõi thời gian chạy vượt ngưỡng để ngắt sớm. |
| **R9** | VLM Critic chấm ảo (báo lỗi sai làm vòng lặp sửa phá hỏng mô hình đang đẹp) | Tốn thêm vòng lặp mà chất lượng đi lùi | Luôn lưu snapshot mỗi vòng trong `ArtifactStore`; chỉ chấp nhận bản sửa mới nếu điểm vi phạm Toán học Tầng 5A **giảm hoặc bằng `0`** (không bao giờ đánh đổi đúng đắn hình học lấy lời khen cảm tính của VLM). |
| **R10** | Rủi ro bản quyền từ thư viện vật liệu / chuyển động mẫu đi kèm | Nhiễm giấy phép độc quyền vào tài sản xuất ra của người dùng | Chỉ đóng gói chuyển động/HDRI thuộc **CC0** đã kiểm tra giấy phép từng tệp. Lưu ý: *Poly Haven* chỉ có HDRI/texture/model (không có clip animation); **Mixamo KHÔNG phải CC0** (điều khoản riêng) nên cấm đóng gói; nguồn clip cần tự xác minh (ví dụ gói nhân vật CC0 của Quaternius/Kenney, ứng viên `[CHƯA XÁC MINH]` — đọc giấy phép trực tiếp trước khi dùng), lưu rõ tệp `LICENSE_ASSETS.json` kèm mã băm `SHA-256`. |

---

## PHẦN 6: BẢNG KIỂM ĐỐI CHIẾU NHANH DÀNH CHO AGENT ĐÁNH GIÁ ĐỘC LẬP (REVIEWER CHECKLIST)

*(Mục này được thiết kế riêng để Agent phản biện có thể kiểm chứng chéo tính trung thực và khả thi của kế hoạch chỉ trong 2 phút bằng các lệnh chỉ-đọc trên máy thật.)*

### 6.1. Các lệnh xác minh sự thật hạ tầng đã nêu trong Phần 0.2
Agent đánh giá có thể chạy trực tiếp các lệnh chỉ-đọc sau để kiểm tra không có con số `[ĐO]` nào bị bịa đặt:
1. Kiểm tra dung lượng đĩa thật (`/` còn ~`2.0G`, workspace còn ~`2.5G`, `/mnt/Data` còn ~`109G`) và Docker root dir:
   - `df -h / /mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d /mnt/Data`
   - `docker info --format '{{.DockerRootDir}}' && docker images`
2. Kiểm tra áp lực RAM (`~28/30 GiB`) và VRAM trên `pc-001` (`RTX 3060` đang gánh engine gộp):
   - `free -h && nvidia-smi`
3. Kiểm tra các mô hình `.gguf` thị giác đã có sẵn trên ổ cứng và trạng thái `xvfb-run` / `blender`:
   - `ls -lh /mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work/*VL* /mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work/q38/`
   - `which xvfb-run blender`
4. Kiểm tra các thành phần tái sử dụng trong `ai-compute-fabric/`:
   - `ai-compute-fabric/pyproject.toml` (`langgraph`, `langgraph-checkpoint-postgres`, `nats-py`, `pydantic`)
   - `ai-compute-fabric/agents/sandbox.py` (`DockerSandbox`, `GpuDockerSandbox`, `WarmDockerPool`)
   - `ai-compute-fabric/docs/API_CLIENT.md` (hỗ trợ đa phương thức 8 ảnh/request & cờ `X-AICF-Thinking`)
   - `KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md` (giới hạn `--runtime=nvidia`, NATS Object Store `<= 8 MiB`, `G-RECLAIM < 2 s`, chính sách `D-WIN`).

### 6.2. Sáu câu hỏi trọng tâm gợi ý cho Agent Phản biện Đánh giá Kế hoạch này
1. **Tính an toàn đĩa & bộ nhớ:** Cơ chế đặt dữ liệu sang `/mnt/Data/aicf-3d-foundry/` + tái sử dụng base image `glibc` có sẵn + cổng gác `MemAvailable >= 2,048 MiB` đã chặn triệt để nguy cơ tràn đĩa `/` (`2.0 GiB`) và tràn RAM trên `pc-001` chưa? Còn đường rò rỉ tệp tạm nào của Blender/Cycles/Python (`/tmp`, `~/.config/blender`, `/var/tmp`) bị bỏ sót không?
2. **Tính thực tế của phân bổ VRAM (`Mode M1 / M2 / M3`):** Việc tách bạch `Mode M1` (`0 MiB` VRAM thêm, chạy ngay cạnh `Qwen3.8-27B`) và `Mode M2` (đổi sang `Qwen3VL-8B` + nạp tuần tự `Hunyuan3D-2mini` / `UniRig`) có còn điểm xung đột nào với `worker-guard` (`HeadroomMiB`) hoặc cơ chế khôi phục nhóm gộp `-topology-file` của `site-gateway` không?
3. **Độ chặt của Bàn cân Kiểm định 3D (`evaluator_3d.py`):** 5 cổng toán học ở Tầng 5A (`PolyBudget`, `TransformAndGround` + đồ thị phát hiện bộ phận bay lơ lửng, `BVHTree IllegalOverlap`, `MeshAndUVHealth`, `RigDeformationStress`) có trường hợp biên hình học nào dễ lọt lưới hoặc báo lỗi giả (ví dụ: con chim đang bay, ngọn lửa lơ lửng có chủ đích, hoặc áo choàng chồng UV đối xứng) cần bổ sung cờ ngoại lệ trong `AssetSpec` không?
4. **Ranh giới Bảo mật Sandbox:** Ngoài `--network=none`, `--cap-drop=ALL`, user `65534`, `-Y` (`--disable-autoexec`) và chặn symlink, có cần bổ sung giới hạn `--ulimit cpu` / `RLIMIT_AS` hoặc lọc `seccomp` riêng cho tiến trình `blender` khi xử lý tệp `.glb/.fbx` đầu vào từ người dùng không?
5. **Khả năng tương thích Game Engine thực tế:** Quy trình chuyển đổi hệ trục từ chuẩn nội bộ (`Z-up`, `-Y forward`, mét) sang `Godot 4 (.glb)`, `Unity (.fbx)` và `Unreal Engine 5 (.fbx, DirectX Normal -Y)` ở Chặng 6 còn thiếu quy ước đặt tên xương gốc (`root` bone) hay quy ước đóng gói kênh nhám-kim loại (`ORM packed texture`: Occlusion-Roughness-Metallic chung 1 ảnh PNG để tiết kiệm VRAM trong game) nào không?
6. **Tính tinh gọn của MVP:** Có thể cắt gọn thêm chi tiết nào ở Pha 1–2 để rút ngắn thời gian ra sản phẩm chạy thật đầu tiên mà không hy sinh 5 tiêu chí Production ở đầu bài không?
---

## PHẦN 7: KẾT QUẢ RÀ SOÁT & BỔ SUNG TRƯỚC KHI BÀN GIAO (`2026-11-13`)

### 7.1. Đã đối chiếu lại với máy thật (chỉ-đọc)
`[ĐO 2026-11-13]` đĩa `/` còn `2.0G`, workspace `2.5G`, `/mnt/Data` `109G`; VRAM `10,915/12,288 MiB`; Docker có `python:3.12-slim`, `ubuntu:22.04`; `xvfb-run` có, `blender` chưa; `Qwen3VL-8B` + mmproj có trong `.aicf-work/`. RAM `available` đo lại được `3.0 GiB` (kế hoạch ghi `2.7`, dao động theo thời điểm — agent thực thi phải đo lại, đừng coi là hằng số). `[ĐỌC MÃ]` `Makefile:verify-release-v1`, `contracts/openapi/aicf_v1.yaml`, `tests/integration/test_openapi_conformance.py`, `studio/`, `ADR-001..004` đều tồn tại → `ADR-005` là số kế tiếp hợp lệ; thư mục `foundry3d/` chưa có. Chưa kiểm: bản chạy `llama-server` có đúng `-np 2`/`98304` và `~40 tok/s` hay không (lấy từ tài liệu cũ, không đo lại trong lượt rà soát này).

### 7.2. Lỗi/thiếu đã sửa trực tiếp trong tài liệu
1. Kế hoạch nói "dùng `WarmDockerPool`" — thực tế lớp này gắn với GPU admission và `DockerSandbox` chưa đủ cho Blender → phải viết `WarmBlenderPool` mới (đã sửa ở 1.1).
2. `Hunyuan3D-2mini` nhiều khả năng chỉ ra hình khối, không có màu → luồng "bake BaseColor" có thể không có nguồn màu (đã gắn `[CHƯA XÁC MINH]` ở 1.1, thành câu hỏi bắt buộc của Pha 3).
3. R10: Mixamo không phải CC0, Poly Haven không có clip animation (đã sửa).
4. Thiếu nền đã đo từ thử nghiệm `fi-ai03` (đã thêm ở 0.2).

### 7.3. Trả lời 6 câu của Phần 6.2 (ý kiến rà soát, chưa đo)
1. **Rò rỉ tệp tạm:** có. Container chạy `--read-only`, nên đặt `HOME`, `TMPDIR`, `XDG_*`, `BLENDER_USER_CONFIG/SCRIPTS`, `MESA_SHADER_CACHE_DIR`, `PYTHONPYCACHEPREFIX` vào `/work` (tmpfs có trần). `/tmp` của `DockerSandbox` hiện chỉ `8m` — Cycles bake/`xvfb` sẽ vượt; phải tăng có chủ đích. Pha 0 đo `df` trước/sau 30 vòng để chứng minh không rò.
2. **Xung đột `worker-guard`:** `Mode M2` đổi topology sẽ làm gián đoạn LLM đang phục vụ → chỉ chạy trong cửa sổ bảo trì do chủ dự án duyệt, và phải kiểm tra vòng reconcile không giết `rpc-server` giữa chừng (memory: reconcile loop đã từng kill rpc-server). Mặc định chỉ làm `M1`.
3. **Ca biên của 5A:** thêm cờ ngoại lệ trong `AssetSpec`: `floating_ok: [part_id]` (chim, lửa, đèn treo), `intentional_intersections: [[a,b]]`, `uv_mirror_overlap_ok` (UV đối xứng), `unlit_black_ok`. Mỗi cờ phải do Planner khai báo kèm lý do và ghi vào `qa_report.json`; mutation test cho chiều ngược lại (cờ không được tự mở cổng cho mọi bộ phận).
4. **Sandbox:** nên thêm `--pids-limit`, `--ulimit cpu`, `--ulimit fsize`, giới hạn `/work`, timeout cứng cho mỗi lần gọi; seccomp mặc định của Docker là đủ cho v1 `[SUY LUẬN]`. Tệp `.glb/.fbx` do người dùng đưa vào = dữ liệu không tin cậy, đi qua cùng sandbox; v1 nên **chỉ nhận ảnh**, hoãn nhập mesh ngoài.
5. **Engine:** thêm vào Chặng 6: xương gốc tên `root`; gói `ORM` (R=AO, G=Roughness, B=Metallic) cho glTF/UE; đảo kênh G normal khi xuất UE; scale đơn vị FBX (Unity/UE cm). Kiểm bằng nạp lại tệp xuất (round-trip) và so bbox, không chỉ tin validator.
6. **Cắt MVP:** làm lát cắt dọc nhỏ nhất trước: `1 prop → bpy → postproc (Z=0, scale) → 5A → .glb → glTF-Validator`, chưa cần LOD/UV/bake/VLM/SSE/Studio. Đo `pass@1` trên 20 prop (so với mốc `fi-ai03`), rồi mới thêm VLM và bake. Dời `Pha 5` (gateway/OpenAPI/Studio) và `Pha 6` sau khi `Pha 1` đạt.

### 7.4. Thứ tự việc đề xuất cho agent thực thi
`0` (xin phép `/mnt/Data`, cài Blender, probe) → `1-MVP` (lát cắt dọc ở 7.3.6) → mở rộng `Pha 1` đủ 8 đột biến → `2` → báo cáo chặn lại cho chủ dự án quyết định có làm `M2/M3` (Pha 3–4) hay không. Không có số liệu thời gian/điện/`pass@N` nào trong tài liệu này được đo; mọi con số `[SUY LUẬN]` về thời gian job (`15–300 s`) và RAM Blender (`0.8–1.5 GiB`) phải được thay bằng số đo ở Pha 0 trước khi đưa vào báo cáo.
