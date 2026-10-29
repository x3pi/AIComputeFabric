# KẾ HOẠCH: BEST-OF-N CHO SINH CODE (SINH N BẢN → CHẠY THỬ → CHỌN BẢN ĐÚNG) TRÊN AI COMPUTE FABRIC
*(Tài liệu bàn giao cho Agent thực thi. Soạn 2026-10-29 từ đề xuất "Best-of-N cho 20–100 máy quán Net" của chủ dự án, đã đối chiếu với hệ thống thật. Phần 1 nói rõ chỗ đề xuất gốc KHÔNG khớp với hệ thống hiện có.)*

**Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` theo tài liệu bên ngoài, chưa kiểm · `[SUY LUẬN]` tính toán/ước lượng · `[CHƯA XÁC MINH]` phải thử. Con số không có nhãn `[ĐO]` là **chưa đo**.

---

## PHẦN 0: BRIEF BÀN GIAO (ĐỌC TRƯỚC)

**Mục tiêu:** một dịch vụ "Best-of-N" cho việc viết code: nhận một yêu cầu code, sinh N lời giải bằng model trên cụm, chạy thử từng lời giải trong sandbox với unit test, loại bản lỗi, trả về bản tốt nhất. Phải **đo được** nó có tốt hơn gọi model một lần (pass@1) hay không, trước khi coi là xong.

**Hệ thống hiện có (đừng làm lại):**
- 2 GPU: `pc-001` (Linux `192.168.1.40`, RTX 3060 12 GB, RAM 30 GB, Core Ultra 7 265K) và `pc-win-01` (Windows `192.168.1.49` / cáp `10.42.0.225`, RTX 5070 12 GB, RAM 64 GB). Chế độ A `split` / B `smart_1gpu` / C `merged` đổi bằng `POST /api/cluster/topology` hoặc giao diện web.
- Gateway `apps/site-gateway` (cổng `18080`): API chuẩn OpenAI `/v1/chat/completions`, `GET /v1/models`, khoá API theo tenant, hàng chờ công bằng, `X-Priority: batch`, `X-Deadline-Ms`. **Đã là bộ chia tải** `[ĐỌC MÃ]`.
- Model code đang chạy: `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` gộp 2 card (`deploy/demo/apply_coder_merged.sh`, hồ sơ `deploy/demo/coding_profile.env`: `PARALLEL=1`, `CTX=32768`). `[ĐO]` giải mã **126,7–132,4 tok/s** một luồng (`benchmarks/reports/P3_CODER_30B_GOP_2_CARD.md`).
- Khung agent: `agents/` (`orchestrator.py` DAG có lưu PostgreSQL, `sandbox.py` Docker/Bwrap/Null fail-closed, `tool_registry.py`, `validator.py`). Docker có trên `pc-001` (image `python:3.12-alpine`, `[ĐO]` có sẵn).
- Dữ liệu đo so sánh: `benchmarks/reports/P3_BANG_SO_LIEU_CUOI.md`; bộ nhớ làm việc: `~/.claude/projects/.../memory/`.

**Quy tắc bắt buộc (chủ dự án):**
1. Trả lời và báo cáo bằng **tiếng Việt**; mọi số có nhãn; nói rõ cái CHƯA đo.
2. Không tick mục nào trong `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` khi chưa có bằng chứng. **Kiểm đột biến** mọi cơ chế mới (phá thử cơ chế → test phải đỏ → khôi phục).
3. **Không `git push`**; chỉ commit khi được yêu cầu. Cuối commit thêm dòng đồng tác giả theo hướng dẫn phiên.
4. Dùng `pkill -x <tên>` hoặc `kill <pid>`; **không** `pkill -f`. Không dùng `pgrep -f`/`grep -c` mà mẫu khớp chính lệnh chờ của mình.
5. Pha nào Check Pass không đạt: **dừng và báo cáo**, không hạ tiêu chí.
6. Windows: chỉ ghi vào `C:\aicf\`; không đụng `C:\Users\Fios02\`. Không làm gián đoạn máy (tắt card mạng, khởi động lại dịch vụ) khi chưa hỏi chủ dự án.
7. Khi khởi động lại gateway **phải giữ đủ cờ** (`-studio-url`, `-agents-url`, `-vram-sidecar-url`, `-tenants-file`, `-topology-file`, TLS, `-audit-log`). *(Cập nhật 2026-10-29 theo commit `5ed77e2`: mã nguồn `site-gateway` đã tự lưu/khôi phục `groups[]` qua `-topology-file` [mặc định `<dir-of-tenants-file>/topology.json`] và `worker-guard` có `--state-file`; lúc kiểm tra đầu phiên binary trong `.demo/bin/` được biên dịch lúc `2026-10-28 14:29` trước `5ed77e2` `[ĐO]`, và đến `2026-10-29 15:26` đã được biên dịch lại + khởi động lại trên cụm thật kèm `-topology-file .demo/topology.json`, xác nhận tự khôi phục nhóm gộp 2 GPU về `status: "ready"` `[ĐO]`.)*
8. Mã do model sinh ra là **không tin cậy**: chỉ chạy trong sandbox Docker (không mạng, hệ thống tệp chỉ đọc, bỏ quyền, giới hạn bộ nhớ/CPU/pid/thời gian). Không bao giờ chạy trực tiếp trên máy chủ hay trên máy Windows.

---

## PHẦN 1: ĐỀ XUẤT GỐC SO VỚI HỆ THỐNG THẬT (QUYẾT ĐỊNH THIẾT KẾ)

| Thành phần đề xuất | Đánh giá | Quyết định |
|---|---|---|
| 20–100 máy quán Net, mỗi máy 1 model | Hệ thống hiện có **2 card**, không phải 50 máy. Mọi tính toán "50 request chia cho 50 máy" không áp dụng; thông lượng Best-of-N ở đây bị chặn bởi 2 card. | Thiết kế cho 2 card; giữ giao diện không phụ thuộc số máy để mở rộng sau. |
| Qwen2.5-Coder-14B trên 3060 đạt "45 tok/s" | `[CHƯA XÁC MINH]`. Đã đo cùng card: Qwen3-8B Q4 **63,2 tok/s** (3060) / **110,9** (5070) một luồng; 4 luồng/máy **358,6 tok/s tổng**. Bản 14B Q4 (~9 GB) chắc chậm hơn 8B nhưng chưa đo. | Đo trước (Pha 1). Không dùng con số 45 trong báo cáo. |
| LiteLLM Proxy làm bộ chia tải | **Thừa.** Gateway AICF đã có hàng chờ công bằng, ưu tiên, giới hạn theo tenant, khoá API, `/v1`. Thêm LiteLLM = thêm một lớp, thêm một chỗ lỗi, cổng, khoá riêng. | **Không dùng LiteLLM.** Gọi thẳng gateway. |
| OptiLLM / AlphaCodium | `[TÀI LIỆU]`: OptiLLM có kỹ thuật `bon` nhưng chọn bằng LLM chấm (không chạy test); AlphaCodium là quy trình mẫu (flow), không phải thư viện cắm vào. Giấy phép/phiên bản chưa kiểm. | **Viết bộ điều phối nhỏ trong repo** (Python, ~vài trăm dòng) dùng lại khung `agents/`. Có thể tham khảo ý tưởng, không phụ thuộc gói ngoài. |
| Piston API chạy thử code | Piston cần container có đặc quyền và nhiều runtime; thêm bề mặt tấn công. Repo **đã có** `agents/sandbox.py` (Docker không mạng, bỏ quyền, `no-new-privileges`). Windows hiện là `NullSandbox` (từ chối chạy). | **Dùng `DockerSandbox` có sẵn** trên `pc-001`. Chỉ cân nhắc Piston nếu cần nhiều ngôn ngữ (Pha 6, tuỳ chọn). Chạy sandbox **chỉ trên Linux**. |
| Tự sinh unit test rồi chọn bản qua | **Rủi ro lớn nhất:** test do cùng model sinh có thể sai, làm loại nhầm bản đúng hoặc giữ bản sai. | Dùng test có sẵn khi có (benchmark, người dùng đưa test). Khi phải tự sinh: sinh nhiều bộ test, **chọn bản đồng thuận nhiều nhất** (kiểu CodeT) và báo mức tin cậy. Đo tỉ lệ nhầm ở Pha 3. |
| "Chọn bản chạy nhanh nhất" | Nhanh ≠ đúng. | Thứ tự chọn: (1) qua nhiều test nhất, (2) đồng thuận đầu ra giữa các bản, (3) mới đến thời gian chạy/độ dài. |

---

## PHẦN 2: KIẾN TRÚC ĐÍCH

```
Client (VS Code / API)  ->  Gateway :18080  ->  /api/bestofn/*  (proxy như /api/studio)
                                                  |
                         bestofn service (Python, :18099, repo: bestofn/)
                           1. Planner: tạo N prompt biến thể (nhiệt độ 0,4–1,0; gợi ý hướng giải khác nhau)
                           2. Sampler: N lời gọi /v1/chat/completions tới CHÍNH gateway (tenant riêng, X-Priority: batch),
                              rải theo DANH SÁCH AGENT từ GET /api/compute-agents (Phần 2B) — không biết GPU nào bên dưới
                           3. Test source: test có sẵn  HOẶC  sinh M bộ test
                           4. Runner: DockerSandbox (agents/sandbox.py) chạy N x M, thu kết quả
                           5. Selector: chấm điểm, chọn bản, trả kèm báo cáo (bản nào qua test nào)
```
Nguyên tắc: bestofn **là khách của gateway** (không nói chuyện trực tiếp với worker), nên hưởng sẵn hàng chờ, khoá, đo lường, kiểm toán.

**Giao diện đề xuất (OpenAI-tương thích để dán vào VS Code):** `POST /api/bestofn/v1/chat/completions` với `model: "bon:<tên model gốc>"`; tham số tuỳ chọn `bon: {n, tests, language, timeout_s}`. Phản hồi là một completion bình thường (bản được chọn) + trường mở rộng `bon_report`. `[CHƯA XÁC MINH]` khách IDE chịu được độ trễ hàng chục giây không streaming — kiểm ở Pha 5.

---

## PHẦN 2B: "AGENT TÍNH TOÁN" LÀ ĐƠN VỊ DUY NHẤT MÀ TẦNG ĐIỀU PHỐI NHÌN THẤY (YÊU CẦU CỦA CHỦ DỰ ÁN, 2026-10-29)

**Yêu cầu:** một **cụm gộp** (nhiều card chạy chung một model) hay **một GPU chạy đơn lẻ** đều chỉ là **một agent độc lập**. Bộ điều phối Best-of-N (và mọi bộ điều phối sau này) **không biết và không cần biết** card nào, ghép ra sao, ở máy nào. Thêm card hay đổi gộp/tách chỉ làm danh sách agent thay đổi; phần phân bổ GPU nằm hoàn toàn ở tầng dưới (bộ điều phối topology).

**Định nghĩa.** Agent tính toán = một điểm phục vụ suy luận duy nhất, có định danh ổn định, có thể nhận yêu cầu `/v1/chat/completions`:
- `kind: "single"` = một worker độc lập (chế độ A hoặc B): agent_id = `worker_id`.
- `kind: "group"` = một nhóm gộp (chế độ C): agent_id = id nhóm (= id máy chính, theo `groups[]` đã có). Các máy thành viên RPC **không** là agent riêng, không nhận yêu cầu.

**Đã có sẵn `[ĐỌC MÃ]`/`[ĐO]` — đừng làm lại:**
- `Registry.Routable()` (`apps/site-gateway/registry.go`) đã bỏ các worker có model `rpc://…`, nên **chỉ máy chính của nhóm gộp và các worker độc lập là điểm nhận yêu cầu**. Đúng tinh thần "agent" ngay từ tầng định tuyến.
- `GET /api/cluster/topology` đã có `groups[]` (id, máy chính, thành viên, model, tỷ lệ chia, tổng VRAM), `busy_worker_ids`, và `workers{}` có `group_id`, `role` (master/member/standalone) (`apps/site-gateway/cluster_topology.go`, commit `5d4ee74`). Mỗi nhóm đổi độc lập, khoá theo từng máy.

**Còn thiếu (phải làm — Pha A):**
1. **Mô tả năng lực của agent** (hiện router chỉ biết tên model + số slot): `agent_id`, `kind`, `model`, `ctx_per_slot`, `slots`, `state`, `busy`, `labels` (vd `code`, `general`), `tok_s_ref` (tốc độ tham chiếu ĐÃ ĐO một luồng, lấy từ bảng cấu hình chứ không đoán), `members` (chỉ để hiển thị cho người vận hành, **không** đưa cho bộ điều phối). `ctx_per_slot` hiện không có trong registry `[CHƯA XÁC MINH]`: lấy từ worker (`/props` của llama-server hoặc thêm vào heartbeat) — chọn cách và ghi lại.
2. **API danh sách agent** ẩn phần cứng: `GET /api/compute-agents` (tên khác `/api/agents/` vì đường đó đã là Agent DAG của `agents/`), dựng từ `groups[]` + worker độc lập + `Routable()`. Thêm vào `contracts/openapi/aicf_v1.yaml` và test Go. Yêu cầu khoá API (không lộ cấu trúc nội bộ cho khách ngoài vai trò; cân nhắc rút gọn trường theo vai trò).
3. **Định tuyến theo agent hoặc theo nhãn** trên `/v1`: header `X-AICF-Agent: <id>` (ghim vào một agent) hoặc `X-AICF-Label: code` (bất kỳ agent nào có nhãn đó); không có header thì như cũ (theo tên model). Agent bận/offline → 503 rõ ràng, không rơi sang agent khác khi đã ghim.
4. **Sự kiện thay đổi**: bộ điều phối phải biết agent xuất hiện/biến mất (gộp, tách, máy rớt). Dùng tải lại danh sách theo chu kỳ ngắn (đơn giản) hoặc luồng SSE `/api/compute-agents/stream` (tái dùng cơ chế `/api/fleet/stream`) — chọn và ghi lý do.

**Quy tắc cho bộ điều phối Best-of-N (`bestofn/`):**
- Chỉ import "danh sách agent" qua API trên; **cấm** đọc `workers`, `tensor_split`, `gpu_name`, `vram_*`, hay gọi `/api/cluster/topology` từ `bestofn/` (kiểm bằng test tĩnh/grep trong CI: mutation = thêm một truy cập bị cấm, test phải đỏ).
- **Phân bổ mẫu cho agent theo năng lực** (`slots` × `tok_s_ref`, giới hạn bởi `ctx_per_slot` ≥ độ dài prompt dự kiến), không chia đều mù quáng. 1 agent mạnh có thể nhận nhiều mẫu hơn, nhưng không vượt `slots`.
- **Bất kỳ số agent nào**: chạy được với 1 agent (mọi mẫu tuần tự/theo slot) và với K agent (song song). Kết quả hợp lệ không phụ thuộc K; chỉ thời gian đổi.
- **Chịu mất agent giữa chừng**: mẫu của agent biến mất được sinh lại trên agent khác (giới hạn số lần), báo trong `bon_report`; không lỗi cả yêu cầu vì một agent rớt.
- **Đa dạng mô hình tuỳ chọn**: nếu có nhiều agent khác model/nhãn, cho phép trộn (tăng đa dạng cho Best-of-N). Phải **đo** xem trộn có hơn dùng một model hay không (Pha 2), không giả định.
- `bon_report` ghi theo `agent_id` (không ghi tên card).

**Quy mô hàng chục agent (yêu cầu bổ sung 2026-10-29).** Số agent KHÔNG có giới hạn cố định trong thiết kế; mọi mã phải đúng với K = 1, 2, 10, 50. Hệ thống mới chỉ chạy thật với **2 worker** `[ĐO]`; mọi con số cho K lớn là `[CHƯA XÁC MINH]` cho tới khi có bài đo. Những chỗ KHÔNG tự co giãn, phải xử lý:
1. **Sandbox chạy code là nút cổ chai tập trung:** hiện chỉ `pc-001` có Docker và chạy tối đa ~4 container song song (RAM 30 GB). Với 50 agent sinh mẫu nhưng chỉ 4 chỗ chạy thử thì việc chấm điểm sẽ thành hàng chờ. Thiết kế `runner` thành **một hồ chạy thử** (pool) có số chỗ cấu hình được và có thể gắn thêm máy chạy thử; đo thông lượng chạy thử (bài/giây). Windows vẫn không có sandbox.
2. **Hàng chờ và giới hạn của gateway:** `-max-queue` (64), `-max-wait` (10 s), `tenant-queue`, `max_inflight` của tenant. K agent × slot có thể vượt các giá trị này; không được để lỗi 503/429 do cấu hình mặc định bị hiểu nhầm là agent hỏng.
3. **Danh sách agent:** `GET /api/compute-agents` phải phân trang hoặc lọc theo nhãn; thông báo thay đổi nên dùng luồng sự kiện (SSE) thay vì tải lại toàn bộ khi K lớn. Cache định tuyến (`Router.Refresh`) phải được đo với K lớn.
4. **Chia mẫu:** thuật toán rải mẫu theo `slots × tok_s_ref` phải xử lý agent có tốc độ rất khác nhau (3060 so với nhóm gộp) và không dồn cả vào agent nhanh nhất; thêm giới hạn "tối đa x% mẫu cho một agent" để một agent lỗi không làm hỏng cả lượt. Đo độ lệch kết quả khi bỏ bớt agent.
5. **Điều phối topology:** `TopologyCoordinator` đã khoá theo từng máy nên nhiều nhóm đổi song song được, nhưng chưa đo với nhiều nhóm thật `[CHƯA XÁC MINH]`.

**Cách đo quy mô (không cần có hàng chục GPU thật):** viết **worker giả** (HTTP + heartbeat, trả lời `/v1` với độ trễ mô phỏng, một số chạy như nhóm gộp) dựng K = 10, 25, 50 agent trên cùng một máy, rồi đo: thời gian phân bổ N mẫu, tỉ lệ 429/503, độ trễ định tuyến, thời gian chạy thử với hồ sandbox, hành vi khi 20% agent rớt giữa chừng. Ghi rõ kết quả giả lập **không** thay cho số đo GPU thật (chỉ chứng minh lớp điều phối co giãn, không chứng minh thông lượng sinh).

**Cách kiểm cho Pha A (Check Pass):** (a) dựng 3 trường hợp trên cùng phần cứng — chỉ 2 worker độc lập; 1 nhóm gộp; 1 nhóm gộp + (nếu có thể) 1 worker riêng — và chứng minh **cùng một đoạn mã bestofn không đổi** chạy đúng trong cả ba, chỉ khác danh sách agent; (b) gộp/tách giữa lúc đang chạy một lượt Best-of-N: lượt đó hoàn tất hoặc báo lỗi rõ ràng, không treo, không chạy trên agent đã biến mất; (c) kiểm đột biến cho việc ghim agent (`X-AICF-Agent` trỏ agent bận phải bị từ chối, không rơi sang agent khác) và cho việc loại thành viên RPC khỏi danh sách agent. Test giả (fake worker) dùng khung `cluster_topology_test.go`.

---

## PHẦN 2C: GIẢI PHÁP TỔNG THỂ — "ĐÀN AGENT ĐÀN HỒI" (thêm/bớt agent tuỳ ý, số lượng thay đổi theo thời gian)

**Yêu cầu của chủ dự án (2026-10-29):** điều phối được **nhiều agent với số lượng thay đổi**, thêm bớt agent **dễ dàng, không sửa cấu hình trung tâm**. Best-of-N chỉ là MỘT quy trình chạy trên nền này; nền phải dùng chung được cho các quy trình khác (RAG, OCR, âm thanh, đánh giá model…).

### 2C.1 Hai mặt phẳng, một khái niệm agent
| Mặt phẳng | Dùng cho | Cơ chế | Đã có `[ĐỌC MÃ]` |
|---|---|---|---|
| **Suy luận tương tác** (độ trễ thấp, đẩy) | Chat, IDE, mỗi mẫu của Best-of-N | Gateway `/v1` → `Router` chọn agent đang rảnh | `apps/site-gateway` (đã có hàng chờ công bằng, ưu tiên, khoá tenant) |
| **Tác vụ bền** (kéo, chịu lỗi) | Quy trình nhiều bước: sinh mẫu, chạy thử, chấm điểm, RAG, huấn luyện, đánh giá | Hàng đợi tác vụ NATS JetStream; agent **kéo** việc theo khả năng | `agents/task_bus.py` (`BusExecutor`/`BusWorker`, at-least-once, `ack_wait`, `max_deliver`), `agents/orchestrator.py` (DAG LangGraph + PostgreSQL), `agents/tasks.py` (15 loại tác vụ có kiểu) |

**Agent** = bất kỳ tiến trình nào (a) khai báo khả năng của mình, (b) nhận việc, (c) trả kết quả. Ba họ agent dùng chung hợp đồng:
- **Agent tính toán** (Phần 2B): đơn vị suy luận = 1 GPU hoặc 1 nhóm gộp; ẩn phần cứng.
- **Agent thực thi** (hồ sandbox chạy thử code, OCR, ASR/TTS, embedding…): kiểu `BusWorker` kéo theo loại tác vụ.
- **Agent điều phối** (quy trình Best-of-N, RAG…): đồ thị DAG; **không** biết ai thực thi, chỉ phát tác vụ và chờ kết quả.

### 2C.2 Vì sao dùng mô hình KÉO cho tác vụ bền
Thêm agent = **chạy thêm một tiến trình trỏ vào hàng đợi**; bớt agent = **tắt tiến trình** (việc đang dở tự giao lại). Không có danh sách trung tâm phải sửa, không có cổng/địa chỉ phải đăng ký thủ công, số agent từ 1 đến hàng trăm dùng cùng mã. Đây đúng là điều `BusWorker` đã làm `[ĐỌC MÃ]`: consumer bền `workers-<loại>`, `AckPolicy.EXPLICIT`, `ack_wait` + nhịp tim giữ việc chậm, quá hạn thì NATS giao lại cho agent khác. Hiện **chưa dùng trong luồng thật** (chỉ test `tests/integration/test_task_bus.py`) `[ĐỌC MÃ]`.

### 2C.3 Giao thức vòng đời agent (Pha E phải làm)
1. **Tham gia (join):** agent khởi động → kết nối bus (xác thực, Phần 2C.6) → ghi hồ sơ khả năng vào **sổ agent** (JetStream KV `AICF_AGENTS`, khoá = `agent_id`, giá trị = `{kind, task_types[], labels[], models[], slots, ctx_per_slot, tok_s_ref, version, started_at}`, có TTL) → bắt đầu kéo. Không cần khởi động lại ai khác.
2. **Sống (heartbeat):** agent làm mới khoá KV; hết TTL = coi là mất, việc của nó được giao lại nhờ `ack_wait`.
3. **Rút lui êm (drain):** agent nhận lệnh/tín hiệu → **ngừng kéo việc mới**, hoàn tất việc đang làm, `ack`, xoá khoá KV, thoát. Không mất việc, không báo lỗi giả.
4. **Mất đột ngột (crash/rút dây):** không `ack` → sau `ack_wait` việc giao lại (có thể chạy hai lần: mọi tác vụ phải **idempotent**, đã là quy ước `[ĐỌC MÃ]`).
5. **Đổi phần cứng bên dưới (gộp/tách card):** thuộc **bộ điều phối topology**, không phải điều phối quy trình. Kết quả là agent tính toán cũ rút lui (drain) và agent mới tham gia; quy trình đang chạy không cần biết.

### 2C.4 Định tuyến theo khả năng, không theo địa chỉ
- Tác vụ mang **yêu cầu** (`type`, và tuỳ chọn `labels`, `model`, `min_ctx`, `priority`), không mang địa chỉ agent. Chủ đề NATS phân cấp: `aicf.task.<type>[.<label>]`; agent kéo các chủ đề khớp khả năng của mình. Hiện chỉ có `aicf.task.<type>` `[ĐỌC MÃ]`: **cần thêm** nhãn/model/ngữ cảnh (không vỡ tương thích: nhãn là tuỳ chọn).
- Với suy luận (mặt phẳng tương tác) dùng `X-AICF-Agent` / `X-AICF-Label` của Phần 2B.
- **Ưu tiên:** tác vụ nền (Best-of-N, batch) đứng sau tương tác; thực hiện bằng stream/consumer tách riêng hoặc `priority` trong tác vụ; **chứng minh bằng đo** rằng người dùng IDE không bị đói.

### 2C.5 Co giãn số lượng
- **Quan sát:** độ sâu hàng đợi theo loại (`num_pending` của consumer), số agent đang sống theo loại (KV), thời gian chờ trung vị. Xuất ra `/metrics` (Prometheus, đã có định dạng ở `Inference.Metrics`).
- **Chính sách đơn giản, tách khỏi cơ chế:** một "người giám sát" đọc số liệu trên và **đề xuất/ra lệnh** thêm hoặc bớt agent (với phần cứng cố định như hiện nay: gộp/tách nhóm, đổi chế độ A/B/C qua `POST /api/cluster/topology`; với máy mới: bật tiến trình worker-guard/BusWorker). Lệnh làm thay đổi cụm phải **hỏi con người** (quy tắc Phần 0) cho tới khi có chính sách được duyệt.
- **Chống dao động:** thời gian chờ tối thiểu giữa hai lần đổi, ngưỡng trên/dưới khác nhau.

### 2C.6 An toàn (hiện là LỖ HỔNG, không được bỏ qua)
`task_bus.py` tự khai báo **chưa có** xác thực/TLS cho NATS, chủ đề theo tenant, hạn mức, nhân bản stream, chịu tải ngược khi không có agent `[ĐỌC MÃ]` *(xác minh lại 2026-10-29 `[ĐO]`: container `aicf-nats` chạy `nats:2.10-alpine -js` trên `127.0.0.1:54222` không cờ xác thực/TLS; `BusWorker`/`BusExecutor` chỉ xuất hiện trong `agents/task_bus.py`, `tests/integration/test_task_bus.py`, `tests/integration/_bus_worker.py`; `agents/tasks.py` đúng có 15 `TaskType` và `Task.data_class` `PUBLIC/INTERNAL/RESTRICTED`)*. Với "đàn agent mở", ai kết nối được vào bus là nhận được việc/dữ liệu. Phải làm trước khi cho máy khác tham gia:
- NATS: TLS + xác thực (NKey/JWT hoặc mTLS); **mỗi agent có danh tính riêng** (liên hệ với mTLS worker đã làm ở commit `f603ecf` cho gateway — `verifyWorkerPeer` trong `apps/site-gateway/server.go` đã khóa `WorkerId == cert.Subject.CommonName`, đồng thời `proxy.go` chỉ mở allowlist `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` `[ĐỌC MÃ]`); quyền publish/subscribe theo loại tác vụ.
- Phân lớp dữ liệu: tác vụ có `data_class` (`PUBLIC/INTERNAL/RESTRICTED`) `[ĐỌC MÃ]`: agent chỉ được kéo lớp dữ liệu mình được phép.
- Hạn mức theo tenant, kích thước tác vụ tối đa, hết hạn tác vụ (TTL), chịu tải ngược (từ chối nhận khi hàng đợi quá sâu, không để tăng vô hạn).
- Mã do model sinh chỉ chạy trong sandbox; agent thực thi phải tự bảo vệ (không tin dữ liệu từ bus).

### 2C.7 Best-of-N trên nền này
Quy trình DAG: `plan` → `fan-out N × SAMPLE` (mỗi mẫu là một tác vụ TEXT_GENERATE; số mẫu N quyết định lúc chạy, **không** phụ thuộc số agent) → `fan-in` → `N × M RUN_TEST` (TOOL_EXECUTE vào hồ sandbox) → `SELECT`. Thêm agent tính toán hoặc agent sandbox = thông lượng tăng, mã quy trình **không đổi**. Mất agent giữa chừng = giao lại tự động. Cần **mở rộng `Orchestrator`** để hỗ trợ fan-out động `[CHƯA XÁC MINH]` (hiện `Step` là bước tĩnh; kiểm cách LangGraph `Send` hoặc sinh bước lúc chạy) và phải đo xem PostgreSQL ghi checkpoint có thành nút cổ chai với N lớn không.

### 2C.8 Các quyết định cần chốt và ghi vào `docs/adr/` (đừng tự quyết im lặng)
| Mã | Câu hỏi | Lựa chọn | Tiêu chí |
|---|---|---|---|
| D-FAB-1 | Mặt phẳng tác vụ bền dùng NATS (đã có) hay tự xây trên gateway? | Giữ NATS JetStream | Đã có mã, test; đo độ trễ thêm so với gọi thẳng `/v1` |
| D-FAB-2 | Mẫu Best-of-N đi qua bus hay gọi thẳng gateway? | Gọi thẳng gateway cho mẫu (độ trễ thấp), bus cho chạy thử/chấm | Đo độ trễ + độ phức tạp; ghi số liệu |
| D-FAB-3 | Sổ agent: JetStream KV hay bảng PostgreSQL? | KV có TTL | Cần heartbeat/hết hạn tự nhiên; PostgreSQL đã có cho DAG |
| D-FAB-4 | Co giãn tự động hay chỉ đề xuất? | Chỉ đề xuất cho tới khi được duyệt | Quy tắc Phần 0 (không gián đoạn máy khi chưa hỏi) |
| D-FAB-5 | Xác thực bus | mTLS/NKey, danh tính theo agent | Thống nhất với mTLS worker của gateway |

---

## PHẦN 3: RÀNG BUỘC ĐÃ BIẾT TỪ HỆ THỐNG (PHẢI XỬ LÝ)

1. **Chỉ 1 slot hiện nay.** Hồ sơ `coding_profile.env` đặt `PARALLEL=1`, `CTX=32768`. Gửi nhiều yêu cầu liên tiếp `[ĐO 2026-10-29]` bị `503 no capacity within the wait limit` (mặc định `-max-wait 10s`) và `429 estimated wait exceeds your deadline`. Best-of-N sẽ gặp đúng lỗi này nếu không đổi cấu hình. Chọn một trong: (a) `PARALLEL=N` (ngữ cảnh mỗi slot = `CTX`, llama-server chạy `-c CTX*N`; **VRAM cho KV tăng**, có thể chạm vách của RTX 5070 — phải đo lại tỷ lệ), (b) gửi tuần tự/ít song song và tăng `-max-wait`, (c) chế độ A với model nhỏ (xem Pha 1).
2. **Tenant của bestofn** cần khoá riêng, `max_inflight ≥ N`, `weight` thấp hơn người dùng tương tác, luôn gửi `X-Priority: batch`, không gửi `X-Deadline-Ms`. Khoá VS Code hiện tại (`vscode-coder`) chỉ `max_inflight=2`.
3. **Vách VRAM của RTX 5070 (Windows WDDM không báo OOM mà tràn sang RAM chung → tốc độ sụp):** tại 8k vách ở >60%, tại 32k ở >52–54% `[ĐO]`. Tăng số slot hoặc ngữ cảnh kéo vách xuống. Dịch vụ Windows `AICFWorkerGuard` đang đặt `--headroom-mib 300` (hạ từ 1536 để gộp chạy được); đừng đổi khi chưa hỏi.
4. **Lưu/khôi phục nhóm gộp khi khởi động lại gateway** *(cập nhật 2026-10-29 theo commit `5ed77e2`)*: Mã nguồn `site-gateway` đã thêm cờ `-topology-file` (mặc định `<dir-of-tenants-file>/topology.json`) và `worker-guard` thêm `--state-file` để tự động lưu/khôi phục `groups[]` và trạng thái engine qua khởi động lại `[ĐỌC MÃ]`. Đã biên dịch lại `.demo/bin/` lúc `2026-10-29 15:25` và khởi động lại `site-gateway` với `-topology-file .demo/topology.json`, xác minh tự khôi phục nhóm gộp 2 GPU (`pc-001` + `pc-win-01`) về `status: "ready"` mà không cần gọi lại `apply_coder_merged.sh` `[ĐO]`.
5. **RAM Linux 30 GB đang căng** (hệ thống đã tự dừng một lệnh nền vì thiếu RAM). Mỗi container sandbox cần giới hạn bộ nhớ nhỏ (≤128 MB mặc định trong `SandboxLimits`), chạy tối đa K container song song (đề xuất K=4).
6. **Windows không có sandbox** (`NullSandbox` từ chối) → mọi việc chạy code diễn ra trên `pc-001`.

---

## PHẦN 4: CÁC PHA THỰC HIỆN

Mỗi pha có **Check Pass**; không đạt thì dừng báo cáo.

### Pha 0 — Đo nền (trước khi viết mã)
- 0.1 Tải bộ đề có test sẵn, nhỏ, chạy offline: HumanEval (164 bài) và MBPP-sanitized. Kiểm giấy phép; ghi nguồn + SHA256. Đặt dưới `benchmarks/data/` **chỉ nếu giấy phép cho phép**, nếu không thì hướng dẫn tải. `[CHƯA XÁC MINH]` nguồn tải và dung lượng trên Wi-Fi ~1 MB/s.
- 0.2 Viết `benchmarks/hardware/code_eval.py`: gọi một model qua gateway, chạy test trong `DockerSandbox`, in pass@1 theo bài. Chạy với `Qwen3-Coder-30B-A3B` (hiện đang chạy) ở nhiệt độ 0 → **đường cơ sở pass@1**.
- **Check Pass:** có số pass@1 lặp lại được (chạy 2 lần, chênh ≤ 2 bài), kèm thời gian trung bình mỗi bài. Sandbox không chạm mạng (kiểm đột biến: bài cố mở socket phải thất bại).

### Pha 1 — Chọn cấu hình sinh mẫu (đo trước khi chốt)
Câu hỏi: để có N bản, dùng **1 model lớn gộp 2 card** hay **model nhỏ hơn chạy song song trên từng card (chế độ A)**? Best-of-N cần thông lượng, chế độ A có tổng tok/s cao hơn nhiều `[ĐO]` (Qwen3-8B 358,6 tok/s tổng so với ~130 của gộp).
- 1.1 Đo thông lượng thật: Coder-30B gộp với `PARALLEL=1,2,4`; và Qwen2.5-Coder-14B / Qwen3-8B (hoặc bản Coder tương đương có trên máy) ở chế độ A. Ghi tok/s tổng, độ trễ trung bình, VRAM, có chạm vách hay không.
- 1.2 Với từng cấu hình chạy `code_eval.py` ở pass@1.
- **Check Pass:** bảng "cấu hình × (pass@1, thông lượng tổng, thời gian tạo N=8 bản)" có dữ liệu thật; chọn cấu hình bằng số, không bằng cảm tính. Mọi cấu hình đã thử phải có dòng, kể cả thất bại.

### Pha A — Lớp "agent tính toán" ẩn phần cứng (làm TRƯỚC Pha 2; chi tiết ở Phần 2B)
- A.1 Mô tả năng lực agent + `GET /api/compute-agents` (single = worker độc lập, group = nhóm gộp; thành viên RPC không phải agent), OpenAPI, test Go.
- A.2 Định tuyến `X-AICF-Agent` / `X-AICF-Label` trên `/v1`; sự kiện hoặc thăm dò thay đổi danh sách agent.
- A.3 Khách Python `bestofn/agents_client.py` chỉ trả `[{agent_id, model, ctx_per_slot, slots, tok_s_ref, labels, state}]`.
- A.4 Bài đo quy mô bằng worker giả K = 10/25/50 (Phần 2B, "Quy mô hàng chục agent"); báo số liệu và điểm bắt đầu hỏng.
- **Check Pass:** ba kịch bản của Phần 2B (2 worker độc lập / 1 nhóm gộp / hỗn hợp nếu có) cùng một mã khách, và chạy đúng với K = 10/25/50 agent giả; gộp-tách giữa lúc chạy không treo; kiểm đột biến cho ghim agent và loại thành viên RPC; test tĩnh cấm `bestofn/` chạm tới chi tiết GPU.

### Pha E — Nền "đàn agent đàn hồi" (Phần 2C; làm song song với Pha A, TRƯỚC khi đưa Best-of-N lên bus)
- E.0 Chốt D-FAB-1..5 bằng số đo/lý do, ghi `docs/adr/ADR-00x-*.md`; **dừng và hỏi chủ dự án** nếu một quyết định đổi hạ tầng (NATS, PostgreSQL) hoặc cần mở cổng ra mạng.
- E.1 Sổ agent (`AICF_AGENTS` KV có TTL) + giao thức join/heartbeat/drain/crash (Phần 2C.3) cho `BusWorker`; thêm khả năng khai báo (`labels`, `models`, `slots`) và chủ đề `aicf.task.<type>[.<label>]` tương thích ngược.
- E.2 Bảo mật bus (Phần 2C.6): TLS + xác thực, danh tính theo agent, quyền theo loại tác vụ, `data_class`, hạn mức và chịu tải ngược. Mở rộng test an ninh trong `tests/security/`.
- E.3 Số liệu: độ sâu hàng đợi, số agent sống theo loại, thời gian chờ → `/metrics`; "người giám sát" ở chế độ chỉ-đề-xuất.
- E.4 Quy mô: K = 1, 10, 50, 200 `BusWorker` giả; thêm/bớt agent giữa lúc chạy (ví dụ tăng từ 5 lên 50 rồi giảm về 8), 20% agent bị kill; đo thông lượng, thời gian giao lại, số việc chạy trùng, tải của NATS/PostgreSQL.
- **Check Pass:** (a) thêm agent giữa chừng làm thông lượng tăng mà **không** sửa cấu hình/khởi động lại ai; (b) drain không mất và không trùng việc; kill đột ngột → việc được giao lại, không mất; (c) agent không có chứng chỉ hợp lệ **bị từ chối** (kiểm đột biến: tắt kiểm xác thực → test phải đỏ); (d) agent không đủ quyền không kéo được loại tác vụ/`data_class` cấm; (e) hàng đợi quá sâu → từ chối có kiểm soát; (f) báo cáo nêu rõ điểm bắt đầu hỏng của K lớn, kể cả khi giả lập không thay cho GPU thật.

### Pha 2 — Bộ điều phối tối thiểu (không test tự sinh)
- 2.1 Gói `bestofn/` (Python, theo phong cách `agents/`): `planner.py`, `sampler.py` (**chỉ làm việc với agent**: rải mẫu theo `slots`×`tok_s_ref`, sinh lại mẫu khi agent biến mất; gọi gateway bằng HTTP, đồng thời tối đa theo cấu hình, thử lại có kiểm soát khi 429/503), `runner.py` (dùng `pick_sandbox(require_network_isolation=True)`; **từ chối chạy nếu không có sandbox cô lập mạng**), `selector.py`.
- 2.2 Chế độ "có test sẵn": chọn bản qua nhiều test nhất; hoà → đồng thuận đầu ra → ngắn hơn.
- 2.3 Kiểm đột biến: (a) bản cố đọc tệp ngoài/mở mạng/ghi đĩa phải bị chặn; (b) vòng lặp vô hạn bị kill đúng thời hạn; (c) in nhiều dữ liệu bị cắt đúng `max_output_bytes`; (d) selector bỏ qua bản lỗi biên dịch.
- **Check Pass:** trên HumanEval với N=8: báo **pass@best-of-8** so với đường cơ sở pass@1 của Pha 0, cùng điều kiện. Báo cả khi con số **không** cải thiện.

### Pha 3 — Test tự sinh (trường hợp thật: người dùng không đưa test)
- 3.1 Sinh M bộ test (đề xuất M=3) từ đề bài; lọc test chạy được trên ít nhất một bản; chọn bản theo đồng thuận.
- 3.2 Đo **tỉ lệ chọn nhầm**: so với kết quả chạy test thật của benchmark (bản được chọn có thật sự qua test thật không).
- **Check Pass:** có số tỉ lệ nhầm; nếu tệ hơn việc lấy bản ngẫu nhiên thì **không** bật tính năng này mặc định, ghi vào báo cáo.

### Pha 4 — Dịch vụ + tích hợp gateway
- 4.1 Dịch vụ HTTP `bestofn` (cổng `18099`), proxy trong gateway như `StudioProxy` (`apps/site-gateway/admin.go`, cờ `-bestofn-url`); thêm vào `contracts/openapi/aicf_v1.yaml` (repo yêu cầu mọi route `/api/` có trong OpenAPI); test Go cho proxy.
- 4.2 Quyền: chỉ tenant có quyền; nếu không có khoá thì 401. Giới hạn `n ≤ 16`, thời gian tối đa, kích thước đầu vào.
- 4.3 Thêm vào `deploy/demo/run_demo.sh` (chỉ khi có Docker) + tài liệu khởi động; không phá cách chạy cũ.
- **Check Pass:** `go test ./...` và test Python mới đều xanh; gọi bằng `curl` có khoá, không khoá, khoá sai; kiểm đột biến cho kiểm quyền.

### Pha 5 — Dùng thử trong IDE
- 5.1 Cấu hình Continue/Cline trỏ vào `bon:<model>`; đo độ trễ từ lúc gửi đến lúc có kết quả với N=4/8.
- 5.2 Ghi các giới hạn thật (IDE hết thời gian chờ? streaming?).
- **Check Pass:** một kịch bản thật chạy được hết từ IDE, kèm số liệu và giới hạn.

### Pha 6 — Tuỳ chọn
Nhiều ngôn ngữ (Piston hoặc thêm image Docker cho Go/JS), giao diện web hiển thị `bon_report`, mở rộng số máy.

---

## PHẦN 5: TIÊU CHÍ HOÀN THÀNH VÀ BÁO CÁO

- Báo cáo `benchmarks/reports/P7_BEST_OF_N.md` gồm: bảng cấu hình (Pha 1), pass@1 so với pass@best-of-N theo N=1,2,4,8 trên HumanEval/MBPP, tỉ lệ chọn nhầm với test tự sinh, thời gian/chi phí mỗi bài, những gì **không** cải thiện.
- Kết luận phải trả lời được: Best-of-N có đáng không **trên đúng phần cứng này**, N bao nhiêu thì lợi ích bão hoà, và nên chạy ở cấu hình nào.
- Mã mới có test; cơ chế an toàn (sandbox, quyền) có kiểm đột biến; không có bí mật (khoá API, mật khẩu) trong repo.

## PHẦN 6: RỦI RO

| Rủi ro | Hậu quả | Cách giảm |
|---|---|---|
| Test tự sinh sai | Chọn nhầm hoặc loại nhầm | Dùng test có sẵn khi có; đồng thuận nhiều bộ test; đo tỉ lệ nhầm |
| Mã sinh ra độc hại / thoát sandbox | Mất dữ liệu hoặc xâm nhập | Chỉ Docker không mạng, `--cap-drop ALL`, người dùng 65534, giới hạn tài nguyên; từ chối chạy khi không có sandbox |
| N bản chiếm cả cụm, người dùng khác bị đói | Giao diện tương tác chậm | Tenant riêng, `X-Priority: batch`, giới hạn song song |
| Tăng slot đẩy RTX 5070 qua vách VRAM | Tốc độ sụp (47 so với 132 tok/s ở 62%) | Đo lại tỷ lệ mỗi khi đổi `PARALLEL`/`CTX` |
| Lớp agent làm lộ chi tiết phần cứng ngược lên (bộ điều phối bắt đầu phụ thuộc vào GPU) | Mất khả năng mở rộng, phải sửa mỗi lần đổi phần cứng | Test tĩnh cấm `bestofn/` chạm tới trường GPU/topology; ba kịch bản cùng một mã (Pha A) |
| Danh sách agent đổi giữa lúc chạy (gộp/tách, máy rớt) | Mẫu mất, kết quả thiếu | Sinh lại mẫu trên agent khác, giới hạn số lần, ghi trong `bon_report` |
| Trộn nhiều model không cải thiện hoặc làm tệ | Phức tạp vô ích | Chỉ bật khi đo được lợi ích (Pha 2) |
| Tốn thời gian mà pass@N không hơn pass@1 | Công sức vô ích | Pha 0–2 đo sớm; được phép kết luận "không đáng" |
| Hết RAM trên `pc-001` | Hệ thống tự dừng tiến trình | Giới hạn container và số container song song |
