# Serverless GPU / Python — Hướng dẫn lập trình viên

Thiết kế: [ADR-003](adr/ADR-003-serverless-gpu.md). Giao diện web: trang *Serverless GPU* (soạn code, container ấm, volume, nhật ký kiểm toán).

## 1. Cài đặt

```bash
pip install -e sdk/python
export AICF_GATEWAY_URL="https://<gateway>"
export AICF_TENANT_ID="<tenant>"
export AICF_API_KEY="<api-key>"
```

Trang web cho phép tải sẵn tệp `aicf.env` chứa ba biến trên.

## 2. Ví dụ tối thiểu

```python
import aicf

app = aicf.App("text-analyzer")

@app.function(gpu=None, timeout_s=30, keep_warm_s=60)
def classify(text: str) -> dict:
    return {"length": len(text)}

@app.local_entrypoint()
def main():
    print(classify.remote(text="xin chào"))
```

```bash
aicf run my_app.py        # chạy một lần
aicf deploy my_app.py     # triển khai phiên bản bất biến
```

## 3. SDK

| API | Tham số chính | Hành vi |
|---|---|---|
| `@app.function(...)` | `gpu`, `vram_mb`, `memory_mb`, `cpus`, `timeout_s`, `keep_warm_s`, `retries`, `image`, `secrets`, `volumes` | Đóng gói hàm bằng `cloudpickle`; chạy trong container cô lập (cấp GPU lease khi `vram_mb > 0`). Hỗ trợ `.remote()`, `.local()`, `.map()`, `.starmap()`, `.spawn()`, `.put()` |
| `@app.cls(...)`, `@aicf.enter()`, `@aicf.exit()` | như `function` | Khởi tạo một lần mỗi container (ví dụ nạp model), tái sử dụng qua các lần gọi ấm |
| `@aicf.endpoint(...)`, `@app.web_endpoint()` | `name`, `gpu`, `vram_mb`, `memory`, `keep_warm_seconds`, `method`, `requires_auth`, `cors_origins` | Xuất hàm thành `POST /api/serverless/v1/web/{tenant}/{app}/{fn}` (chỉ JSON) |
| `@aicf.task_queue(...)` | `retries`, `keep_warm_seconds` | Hàng đợi bất đồng bộ: `fn.put(...)` / `fn.spawn(...)` trả `FunctionCall` (`get()`, `logs()`, `cancel()`) |
| `aicf.Sandbox(...)` | `gpu`, `vram_mb`, `memory`, `keep_warm_seconds` | `create()`, `run_code()`, `process.exec()`, `terminate()` |
| `aicf.Image.debian_slim()` | `.pip_install()`, `.env()`, `.add_local_python_source()` | Môi trường bất biến theo `env_hash`; wheel được kiểm tra hash qua `uv` trong builder tách biệt |
| `aicf.Volume.from_name(name)` | `.put()`, `.get()`, `.ls()`, `.rm()` | Lưu trữ theo tenant; gắn qua `volumes={"/data": vol}`, truy cập tại `/mnt/volumes/{name}` |
| `aicf_gpu` | `device_info`, `matmul`, `saxpy`, `cosine_search`, `vram_stress` | Mô-đun CUDA Driver API dựng sẵn trong container |

## 4. CLI

```text
aicf run <file.py>                       chạy hàm
aicf deploy <file.py>                    triển khai phiên bản bất biến
aicf app list | rollback <app> | stop <app>
aicf container list | stop <id>
aicf volume put <volume> <path> <file>   tải lên, kiểm tra SHA-256
aicf volume ls <volume>
aicf secret set <name> KEY=VALUE
```

## 5. REST API

Mọi request gửi `Authorization: Bearer <api-key | session-token>`. Tiền tố: `/api/serverless/v1`.

| Phương thức & đường dẫn | Quyền | Mô tả |
|---|---|---|
| `POST /auth/register` | công khai | Tạo tài khoản; trả `api_key` và `session_token` |
| `POST /auth/login` | công khai | Đăng nhập bằng `identifier`+`password` hoặc `api_key`; cấp `aicf_sess_...` |
| `POST /auth/logout` | session / key | Thu hồi session, giữ nguyên API key |
| `POST /auth/rotate-key` | session / key | Cấp key mới, vô hiệu key cũ ngay |
| `POST /signup` | công khai | Tạo workspace `{"name"}`; có thể bị tắt (`--allow-signup=false`) |
| `POST /deploy` | tenant key | Triển khai bằng `source_code`+`entrypoint` hoặc `bundle_b64`; trả `active_version` |
| `POST /apps/{app}/rollback` | tenant key | Về phiên bản trước hoặc `{"version"}` |
| `POST /invoke` | tenant key | Gọi đồng bộ |
| `POST /spawn` | tenant key | Gọi bất đồng bộ; `202 {"call_id"}`; tra cứu `GET /v1/calls/{id}`, log `GET /v1/calls/{id}/logs` (SSE) |
| `POST /web/{tenant}/{app}/{fn}` | tenant key | Gọi web endpoint, JSON thuần |
| `PUT/GET/DELETE /volumes/{vol}/{path}` | tenant key | Tải lên / tải xuống (kèm `X-Aicf-Sha256`) / xóa |

Đặc tả đầy đủ: `contracts/openapi/aicf_v1.yaml`.

## 6. Mã lỗi

| HTTP | `error.code` | Nguyên nhân / xử lý |
|---|---|---|
| 400 | `syntax_error`, `entrypoint_not_found` | Lỗi cú pháp (kèm số dòng) hoặc thiếu entrypoint |
| 401 | `unauthorized` | Thiếu hoặc sai bearer token |
| 403 | `serverless_disabled`, `gpu_denied` | Tenant chưa bật Serverless, hoặc yêu cầu GPU khi `gpu_allowed=false` |
| 429 | `concurrency_exceeded` | Vượt `max_concurrency` |
| 503 | `no_capacity`, `serverless_draining` | GPU đang nhường cho chủ máy hoặc LLM; thử lại sau `Retry-After` giây |

## 7. Web IDE

| Thao tác | Phím |
|---|---|
| Lưu & triển khai | `Ctrl/Cmd+S` |
| Chạy thử (triển khai bản nháp và gọi endpoint) | `Ctrl/Cmd+Enter` |
| Tìm / thay thế | `Ctrl/Cmd+F` |
| Gợi ý SDK / `aicf_gpu.*` | `Ctrl+Space` |
| Thụt lề / bỏ thụt lề | `Tab` / `Shift+Tab` |
| Thoát focus bàn phím (WCAG 2.1.2) | `Esc` rồi `Tab` |

Giới hạn: tệp tối đa 512 KiB, UTF-8, không chứa NUL (BOM được tự bóc). Nếu `vendor/codemirror6.bundle.js` không tải được, trình soạn thảo chuyển sang `<textarea>` thuần (ADR-004).
