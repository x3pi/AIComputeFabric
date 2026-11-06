# Hướng dẫn sử dụng

Tài liệu này mô tả các chức năng của giao diện web `site-gateway` và API suy luận. Giá trị trong `<...>` là giá trị thay thế; không dùng token mặc định của bản demo ở môi trường thật (xem [PRODUCTION.md](PRODUCTION.md)).

## 1. Bắt đầu

1. **Giám sát cụm** — trang *Cụm GPU & VRAM*: kiểm tra worker trực tuyến và VRAM từng máy.
2. **Cấp khóa** — trang *Tenants & Khóa API*: tạo tenant, chọn trọng số WFQ, sao chép khóa `aicf_...` (chỉ hiển thị một lần).
3. **Kiểm thử suy luận** — trang *Playground & Tải*: dán khóa, gửi prompt, xem tok/s và TTFT.
4. **Tùy biến** — *AI Studio* (RAG, Prompt, LoRA) và *AI Agent*.

Thao tác quản trị cần admin token (nhập qua nút token trên thanh trên cùng; chỉ lưu trong `sessionStorage`).

## 2. Mô hình VRAM 6 ngăn

VRAM mỗi GPU chia thành 6 ngăn, tính bằng phép trừ bão hòa (không âm):

| Ngăn | Ý nghĩa |
|---|---|
| OSUser | VRAM của hệ điều hành và ứng dụng chủ máy (đo qua `nvidia-smi`) |
| Weights | Trọng số các layer đặt trên GPU |
| KVState | Bộ đệm ngữ cảnh: `2 × layers_attn × n_kv × head_dim × ctx × parallel × bytes_kv` |
| Compute | Bộ đệm tính toán GGML + CUDA workspace |
| Headroom | Vùng đệm an toàn cho chủ máy (khuyến nghị 512–1536 MiB) |
| Allocatable | Phần còn lại; quyết định trạng thái GREEN / YELLOW / RED |

Chế độ thực thi được chọn tự động:

- `LOCAL_FULL` — toàn bộ model nằm trên 1 GPU.
- `LOCAL_HYBRID` — một phần layer chuyển sang RAM hệ thống (`MaxGPULayers`).
- `DISTRIBUTED_MODEL` — chia layer qua nhiều GPU khi `gpu_count > 1`.

## 3. API suy luận

Endpoint tương thích OpenAI: `POST /v1/chat/completions`.

```bash
curl -N "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $AICF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<model>","stream":true,"messages":[{"role":"user","content":"Xin chào"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url=f"{GATEWAY}/v1", api_key=AICF_API_KEY)
resp = client.chat.completions.create(
    model="<model>",
    messages=[{"role": "user", "content": "Xin chào"}],
)
```

Header điều khiển:

| Header | Tác dụng |
|---|---|
| `Authorization: Bearer aicf_...` | Khóa API của tenant |
| `X-Session-ID` | Định tuyến hội thoại nhiều lượt về worker đang giữ KV prefix cache |
| `X-Priority: batch` | Đánh dấu tác vụ nền, nhường tác vụ tương tác |
| `X-Deadline-Ms` | Hủy sớm nếu thời gian chờ dự kiến vượt hạn |

## 4. AI Studio

Dữ liệu được cô lập theo tenant (row-level security).

- **Kho tri thức (RAG)** — tải `.txt`, `.md`, `.html`, `.pdf`, `.docx` kèm mức mật `PUBLIC` / `INTERNAL` / `RESTRICTED`; câu trả lời có chỉ mục `[n]` và trích dẫn gốc.
- **Prompt** — mỗi lần lưu tạo phiên bản bất biến (`v1`, `v2`, …); có khung so sánh zero-shot và few-shot.
- **LoRA / QLoRA** — dữ liệu `JSONL` (`{"question","answer"}`) hoặc CSV, tối thiểu 20 dòng; bước *Kiểm tra dữ liệu* lọc trùng và tách train/validation; kết quả tải về dạng Safetensors `.zip` hoặc `.gguf` (`llama-server --lora`).

## 5. Audio AI

| Chức năng | Mô tả |
|---|---|
| ASR | Nhận dạng tiếng Việt và tiếng Anh, có phân đoạn VAD |
| TTS | Văn bản sang `.wav` 16-bit PCM; từ chối nhân bản giọng nói |
| VAD / khử nhiễu | Ngưỡng tương đối −30 dB so với đỉnh; Wiener spectral gate |
| Đánh giá | Chuẩn hóa số, ngày, dấu thanh trước khi tính WER/CER |

## 6. Phân quyền

| Thao tác | viewer | operator | admin |
|---|:-:|:-:|:-:|
| Xem cụm, VRAM, DAG, tenant | ✓ | ✓ | ✓ |
| Tạm dừng / chạy lại worker | — | ✓ (site được giao) | ✓ (toàn cụm) |
| Tải tài liệu RAG, tạo prompt, chạy LoRA | — | ✓ | ✓ |
| Xóa tài liệu, prompt, job | — | — | ✓ |
| Tạo tenant, sửa trọng số WFQ, đổi khóa | — | — | ✓ |

Quyền được kiểm tra ở server; giao diện chỉ ẩn/hiện điều khiển tương ứng.

## 7. Bảo vệ máy trạm (Worker Guard)

`worker-guard` trên mỗi máy đo VRAM trống mỗi 250 ms. Khi VRAM trống thấp hơn `--headroom-mib` (chủ máy mở ứng dụng nặng), worker nhường GPU: container Serverless bị thu hồi trước, sau đó mới tới `llama-server`. Khi máy nhàn rỗi ổn định trở lại, worker tự nạp lại model. Số đo và SLO: xem [SRE_RUNBOOK.md](SRE_RUNBOOK.md) và `docs/reports/`.

Giao diện cục bộ của worker-guard: `http://127.0.0.1:8099/` (chỉ loopback).
