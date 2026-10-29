# ADR-002 — Đàn Agent Đàn Hồi trên NATS JetStream: Bus Tác Vụ, Sổ Agent KV, Giám Sát Chỉ-Đề-Xuất và Bảo Mật mTLS (D-FAB-1..5)

- **Trạng thái:** ACCEPTED (đã xác minh bằng số đo thực nghiệm ngày 2026-10-29 ở Pha E)
- **Ngày:** 2026-10-29
- **Quyết định (D-FAB-1..5):**
  - **D-FAB-1 (Mặt phẳng tác vụ bền):** Giữ **NATS JetStream** (`agents/task_bus.py`, stream `AICF_TASKS`, `RetentionPolicy.WORK_QUEUE`) làm mặt phẳng kéo tác vụ bền thay vì tự viết hàng đợi bền trong `apps/site-gateway`.
  - **D-FAB-2 (Đường đi của Best-of-N):** Gọi **thẳng HTTP `/v1/chat/completions` qua `site-gateway`** cho bước sinh mẫu (tận dụng bộ chia tải công bằng, `X-Priority: batch`, `X-AICF-Agent`/`X-AICF-Label`); dùng **bus NATS JetStream** cho các tác vụ thực thi bền/chạy thử hộp cát (`TOOL_EXECUTE`, `RESULT_VALIDATE`, DAG đa bước).
  - **D-FAB-3 (Sổ đăng ký agent):** Dùng **JetStream KV bucket `AICF_AGENTS` có TTL** (mặc định `10s`, nhịp tim `TTL/3`) cho vòng đời `join / heartbeat / drain / crash`; giữ PostgreSQL (`aicf_dag`, `aicf_exec`, `aicf_results`, `checkpoints`) cho trạng thái bền của đồ thị DAG.
  - **D-FAB-4 (Cơ chế co giãn):** Bộ giám sát (`ElasticSupervisor`) chạy ở chế độ **CHỈ ĐỀ XUẤT (`propose_only=True`)** kèm chống dao động (hysteresis hai ngưỡng + `cooldown_s`), tuyệt đối không tự động thay đổi cấu hình cụm hay tắt/bật máy khi chưa có phê duyệt của con người (Quy tắc Phần 0).
  - **D-FAB-5 (Bảo mật và định danh trên bus):** Áp dụng **mTLS (`verify_and_map: true`)** với chứng chỉ X.509 riêng cho từng agent (`Subject.CommonName == agent_id`, đồng bộ với cơ chế `verifyWorkerPeer` của `site-gateway` ở commit `f603ecf`) kết hợp phân quyền publish/subscribe theo `TaskType` và `data_class` (`PUBLIC`, `INTERNAL`, `RESTRICTED`), giới hạn kích thước gói tin (`max_payload_bytes`), thời hạn sống tác vụ (`ttl_s`), hạn mức theo tenant (`TenantQuota`), và chịu tải ngược (`QueueFullError` + `DiscardPolicy.NEW`).

## Bằng chứng

- **D-FAB-1 & D-FAB-2 (Độ trễ HTTP trực tiếp so với NATS JetStream):**
  - `[ĐO]` Trên `pc-001` (`127.0.0.1`), một lời gọi HTTP POST rỗng vòng lặp nội bộ có độ trễ trung vị **0,44 ms** (p95 **1,15 ms**), trong khi một vòng tác vụ đầy đủ qua NATS JetStream (`BusExecutor` publish $\rightarrow$ `BusWorker` pull $\rightarrow$ ack & reply) có độ trễ trung vị **1,15 ms** ở chế độ thường (p95 **2,61 ms**) và **1,17 ms** ở chế độ mTLS (p95 **2,17 ms**).
  - `[SUY LUẬN]` Phụ phí **+0,71 ms** của NATS JetStream là không đáng kể so với tác vụ chạy hộp cát hoặc suy luận dài (100–5.000 ms), đổi lại có cơ chế giao lại tự động (`ack_wait`, `in_progress` heartbeat) khi worker chết giữa chừng. Ngược lại, với bước sinh mẫu tương tác/streaming `/v1/chat/completions`, `site-gateway` (`[ĐỌC MÃ]` `apps/site-gateway/proxy.go`, `scheduler.go`) đã có sẵn hàng chờ công bằng theo tenant, ưu tiên `X-Priority: batch` và kiểm toán, nên gọi thẳng gateway tránh nhân đôi bộ lập lịch.
- **D-FAB-3 (JetStream KV `AICF_AGENTS` so với bảng PostgreSQL cho nhịp tim agent):**
  - `[ĐO]` Ghi nhịp tim vào JetStream KV (`kv.put`) đạt độ trễ trung vị **0,11 ms** (đọc danh sách `list_live_agents`: **0,80 ms**), nhanh hơn **5,9 lần** so với `INSERT ... ON CONFLICT DO UPDATE` vào PostgreSQL trên `127.0.0.1:55432` (**0,65 ms**).
  - `[ĐO]` Container `aicf-pg` có `max_connections = 100` (`superuser_reserved_connections = 3`, đang dùng `9` kết nối cho `studio` và `dag_api`, chỉ còn **88** kết nối trống). Nếu K = 50–200 agent gửi nhịp tim trực tiếp vào PostgreSQL, số kết nối sẽ cạn kiệt và gây tranh chấp WAL với `PostgresSaver` của `Orchestrator`. JetStream KV xử lý TTL tự động trên bộ nhớ/luồng NATS mà không tốn kết nối PostgreSQL.
- **D-FAB-4 (Chế độ chỉ đề xuất):**
  - `[ĐỌC MÃ]` Phù hợp tuyệt đối với Quy tắc Phần 0 của dự án: cụm đang chạy model gộp phục vụ người dùng thật và `AICFWorkerGuard` trên Windows có ngưỡng VRAM nhạy cảm; mọi hành động gộp/tách card hoặc thêm/bớt tiến trình phải do con người duyệt. `ElasticSupervisor(propose_only=False)` bị khóa cứng bằng `PermissionError` (`[ĐO]` kiểm đột biến xanh).
- **D-FAB-5 (Bảo mật mTLS và phân lớp `data_class`):**
  - `[ĐO]` Bật mTLS (`TLSv1.2+`, ECDSA P-256, `verify_and_map: true`) chỉ làm tăng độ trễ trung vị từ **1,15 ms** lên **1,17 ms** (**+0,02 ms**).
  - `[ĐỌC MÃ]` + `[ĐO]` Phân tách chủ đề `aicf.task.<type>[.<label>][.dc.<data_class>]` cùng ACL của `nats-server` đảm bảo worker chỉ có quyền `PUBLIC`/`INTERNAL` không thể đăng ký consumer hoặc kéo dữ liệu `RESTRICTED` ngay từ tầng broker.

## Chưa được kiểm chứng

- Chưa bật mTLS trực tiếp trên container `aicf-nats` (`127.0.0.1:54222`) đang chạy chung vì quy tắc cấm chạm vào hạ tầng đang chạy khi chưa hỏi chủ dự án; toàn bộ bài kiểm thử bảo mật và đo quy mô chạy trên tiến trình `nats-server v2.10.29` cô lập ở cổng riêng.
- Chưa đo độ trễ mTLS của NATS qua mạng LAN/Wi-Fi thật giữa `pc-001` (`192.168.1.40`) và `pc-win-01` (`192.168.1.49` / `10.42.0.225`).
- Cụm NATS hiện chạy 1 nút (`num_replicas = 1`); chưa kiểm thử JetStream Raft cluster 3 nút.

## Điều kiện đảo quyết định

- **Đảo D-FAB-1 / D-FAB-3:** Nếu quy mô vượt quá `K > 500` worker đồng thời chờ trên cùng một consumer mà không chia nhỏ theo nhãn (`ConsumerConfig.max_waiting = 512` `[ĐO]`), hoặc khi cần truy vấn lịch sử kiểm toán vòng đời agent dài hạn bằng SQL $\Rightarrow$ bổ sung đồng bộ bất đồng bộ từ KV sang bảng lịch sử PostgreSQL và tăng `max_waiting` / phân mảnh consumer.
- **Đảo D-FAB-4:** Khi chủ dự án phê duyệt chính sách tự động co giãn cho riêng nhóm `BusWorker` chạy hộp cát không chạm tới GPU $\Rightarrow$ mở cờ thực thi có danh sách trắng cho riêng `kind="executor"`.

