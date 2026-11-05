# AICF Serverless Python SDK (`aicf`)

SDK Python chuẩn kiểu Modal.com cho nền tảng **AI Compute Fabric Serverless GPU/CPU**. Cho phép định nghĩa môi trường container, hàm từ xa, lớp giữ trạng thái trên GPU (`@app.cls`), xử lý song song có kiểm soát ngược dòng (`.map()`), lưu trữ bền vững (`Volume`), bộ đệm trọng số mô hình cục bộ (`ModelCache`), và điểm cuối HTTP (`@app.web_endpoint`) hoàn toàn bằng mã Python thuần.

---

## 1. Cài đặt & Cấu hình

Yêu cầu **Python 3.12** (khớp phiên bản minor với image runtime `python:3.12-slim` để đảm bảo tương thích bytecode `cloudpickle`).

```bash
pip install -e sdk/python
export AICF_GATEWAY_URL="http://127.0.0.1:18080"
export AICF_API_KEY="aicf_<khóa_tenant_của_bạn>"
aicf doctor
```

---

## 2. Ví dụ Nhanh (`@app.function`, `.remote()`, `.map()`, `.spawn()`)

```python
import aicf

image = (
    aicf.Image.debian_slim("3.12")
    .uv_pip_install("numpy==2.2.6")
    .add_local_python_source("my_helper_module")
)

app = aicf.App("demo-serverless", image=image)

@app.function(gpu="RTX 3060", vram_mb=2048, timeout_s=30, keep_warm_s=60)
def square(x: int) -> int:
    return x * x

if __name__ == "__main__":
    # 1. Gọi đồng bộ 1 phần tử
    print(square.remote(12))  # -> 144

    # 2. Gọi song song .map() có backpressure (cửa sổ 2 * max_concurrency)
    results = list(square.map(range(200), max_concurrency=16, retries=1))

    # 3. Gọi bất đồng bộ .spawn() cho tác vụ dài (tối đa 600s)
    fc = square.spawn(99)
    print("Call ID:", fc.object_id)
    print("Result:", fc.get(timeout=60))
```

---

## 3. Lớp Giữ Trạng Thái trên GPU (`@app.cls` + `@aicf.enter()`)

Nạp mô hình vào VRAM **đúng 1 lần duy nhất** khi container khởi chạy (`@aicf.enter()`), tái sử dụng qua hàng trăm lượt gọi `.remote()` mà không tốn chi phí nạp lại:

```python
import aicf

app = aicf.App("llm-embedder")

@app.cls(gpu="RTX 3060", vram_mb=4096, keep_warm_s=300)
class Embedder:
    @aicf.enter()
    def load_model(self):
        # Chạy đúng 1 lần khi khởi tạo container ấm
        self.proj = [0.25, 0.5, 0.75]

    @aicf.method()
    def encode(self, vec: list[float]) -> float:
        return sum(a * b for a, b in zip(vec, self.proj))

    @aicf.exit()
    def cleanup(self):
        self.proj = []
```

---

## 4. `Volume`, `ModelCache`, `Secret` và `@app.web_endpoint`

- **`aicf.Volume("my-vol")`**: Lưu trữ đọc/ghi cô lập theo từng `tenant_id` (chặn tuyệt đối `..` và symlink thoát, kiểm tra SHA-256 và hạn mức `max_volume_bytes`).
- **`aicf.ModelCache("qwen3-weights")`**: Kho trọng số chỉ đọc đối với tenant (do Admin nạp sẵn trên đĩa cục bộ để tránh nghẽn mạng).
- **`aicf.Secret.from_name("hf-secret")`**: Tiêm biến môi trường bí mật vào container tại thời điểm chạy; giá trị bí mật bị lọc hoàn toàn khỏi nhật ký kiểm toán (`ServerlessAudit`) và Prometheus metrics.
- **`@app.web_endpoint()`**: Phơi bày hàm đã `deploy` thành HTTP POST endpoint chỉ nhận/trả `application/json` (chống RCE qua pickle trên giao diện web — Threat T10) tại `/api/serverless/v1/web/<tenant>/<app>/<fn>`.

---

## 5. Tham chiếu Lệnh CLI (`aicf`)

| Lệnh | Mô tả |
|---|---|
| `aicf doctor` | Kiểm tra toàn diện 5 hạng mục: kết nối Gateway, khóa API tenant, phiên bản Python 3.12, quyền bật Serverless và hạn mức đồng thời |
| `aicf run <file.py>[::fn] [--json-args '...']` | Chạy hàm trực tiếp từ máy trạm và in kết quả |
| `aicf deploy <file.py>` | Đóng gói và triển khai phiên bản bất biến (`version = v-<sha256>`) lên Gateway |
| `aicf rollback <app_name> [--version v-...]` | Quay lui ngay lập tức về phiên bản ổn định trước đó mà không ngắt các lượt đang chạy |
| `aicf ps` | Liệt kê các container ấm đang giữ lease và các ứng dụng đã triển khai |
| `aicf stop <app_name>` | Dừng ứng dụng và thu hồi toàn bộ container ấm của ứng dụng đó |
| `aicf logs <call_id>` | Xem luồng `stdout`/`stderr` của một lượt gọi `.spawn()` hoặc `.remote()` |
| `aicf volume put/get/ls <vol> ...` | Tải lên, tải xuống hoặc liệt kê tệp trong `aicf.Volume` của tenant |

---

## 6. Bảng Mã Lỗi Hành Động Được (`aicf.errors`)

| Ngoại lệ Python | Mã HTTP / Code | Nguyên nhân & Cách xử lý |
|---|---|---|
| `ConfigError` | Local | Thiếu `AICF_GATEWAY_URL` hoặc `AICF_API_KEY` trong biến môi trường |
| `PythonVersionMismatch` | Local | Python cục bộ khác `3.12`; chạy lại qua `uv run --python 3.12 ...` |
| `AuthError` | `401` / `403` (`serverless_disabled`) | Khóa API sai/đã xoay vòng, hoặc tenant chưa được Admin bật `serverless_enabled=true` |
| `QuotaExceeded` | `403` / `429` (`concurrency_exceeded`, `gpu_denied`, `vram_exceeded`, `volume_quota_exceeded`) | Vượt hạn mức đồng thời, VRAM, dung lượng Volume hoặc truy cập `data_class="RESTRICTED"` trên GPU dùng chung |
| `NoCapacity` | `503` (`no_worker`, `no_vram_lease`, `serverless_draining`) | Không có worker thỏa nhãn GPU hoặc hết VRAM khả dụng; thử lại sau `retry_after_s` giây |
| `FunctionTimeout` | `200` (`status="timeout"`) | Hàm chạy quá `timeout_s`;container đã bị `docker kill` để giải phóng tài nguyên |
| `VramExceeded` | `200` (`status="oom_vram"`) | Tiến trình trong container dùng vượt `vram_mb` đã khai báo và bị `GpuMonitor` cắt |
| `PreemptedByHost` | `200` (`status="evicted"`) | Chủ máy trạm dùng GPU (`G-RECLAIM < 2s` theo D10) hoặc Admin/Operator bấm Force Evict |
| `WorkerLost` | `200` (`status="worker_lost"`) | Worker mất kết nối giữa chừng; theo D8 (at-most-once), chỉ tự chạy lại nếu đặt `retries > 0` |
| `RemoteError` | `200` (`status="error"`) | Ngoại lệ phát sinh trong mã người dùng kèm `remote_type` và `remote_traceback` |

