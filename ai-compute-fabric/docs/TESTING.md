# Hướng dẫn test thử (Linux, 1 máy có GPU NVIDIA)

Chưa test được trên Windows (xem cuối file). Mọi thứ dưới đây đã chạy thật trên Linux + RTX 3060.

## 0. Chuẩn bị
- Go ≥ 1.22, Python 3, `nvidia-smi`, một `llama-server` (llama.cpp có CUDA) và một file model `.gguf` (đã dùng Qwen3-4B Q4_K_M).
- Chạy kiểm tra tự động (không cần GPU): `make verify-dev`

## 1. Chạy demo một lệnh
```
deploy/demo/run_demo.sh /đường/dẫn/llama-server /đường/dẫn/model.gguf
```
Script tự build, tạo CA + chứng chỉ (mTLS giữa worker và gateway), bật xác thực API-key và admin token
(mặc định `demo-admin-token`; cổng gateway 18080, gRPC 17443, đổi bằng `HTTP_PORT`, `GRPC_PORT`, đổi bằng `AICF_ADMIN_TOKEN`). Đợi ~10–30 s để model nạp.

## 2. Các trang web
| Trang | Địa chỉ | Việc cần thử |
|---|---|---|
| Đội máy (fleet) | http://127.0.0.1:18080/ | Thấy worker `pc-001` chuyển `running`; nhập admin token; bấm Pause/Resume |
| Tenant | http://127.0.0.1:18080/tenants.html | Tạo tenant (vd. `sales`), **copy API key hiện một lần**; đặt trọng số/giới hạn |
| Chat | http://127.0.0.1:18080/chat.html | Dán API key, chat; xem token stream và worker xử lý |
| Worker | http://127.0.0.1:8099/ | VRAM, trạng thái engine |

## 3. Thử cơ chế nhường GPU (tính năng chính)
1. Fleet page: worker `running`.
2. Chiếm VRAM như người dùng thật (vd. `python3 -c "import torch;x=torch.empty(int(10e9),dtype=torch.uint8,device='cuda');input()"`
   hoặc mở game). Khi VRAM trống < 512 MiB (demo) worker chuyển `paused`, llama-server tắt (đo được < 1 s).
3. Tắt chương trình đó; sau ~10 s ổn định worker tự chạy lại.

## 4. Thử nhiều người dùng / nhiều request
```
curl -s -H "Authorization: Bearer <API_KEY>" -H 'Content-Type: application/json' \
  -d '{"stream":true,"max_tokens":50,"messages":[{"role":"user","content":"Xin chào"}]}' \
  http://127.0.0.1:18080/v1/chat/completions
```
- Không có/sai key → 401. Quá tải → 429 kèm `Retry-After`. Header `X-Priority: batch` hạ ưu tiên; `X-Deadline-Ms: 2000` từ chối sớm nếu chờ không kịp.
- Tải thử: `apps/site-gateway/cmd/inferload` (xem `-h`), số liệu: http://127.0.0.1:18080/metrics.

## 5. E2E trình duyệt tự động
`cd tests/e2e && npm install && node dashboard_e2e.mjs --gateway <site-gateway bin> --guard <worker-guard bin> --server <llama-server> --model <gguf> --out report.json` (cần Google Chrome; tự dựng gateway+worker riêng, không dùng demo ở bước 1).

## 6. Chưa kiểm chứng (đừng coi là xong)
- **Mọi thứ trên Windows** (WDDM, PDH, Windows service, CUDA): code biên dịch được nhưng chưa từng chạy. Cần 1 PC Windows có GPU NVIDIA:
  chạy `benchmarks/hardware/probe_windows.ps1`, `benchmarks/hardware/reclaim_poc.py`, `deploy/windows/install-service.ps1`.
- Nhiều PC thật, fleet thật quy mô lớn (mới mô phỏng 5.000 worker), NATS, PostgreSQL, HA gateway, thu hồi chứng chỉ mTLS.
- Con số hiệu năng trong plan là của 1 GPU RTX 3060; 2 worker trong benchmark affinity dùng chung 1 GPU.
