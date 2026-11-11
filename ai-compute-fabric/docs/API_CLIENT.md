# AI Compute Fabric — Tài liệu API cho bên tích hợp

API tương thích **OpenAI Chat Completions** (cũng có cổng tương thích Ollama). Mô hình hiện chạy: `Qwen3.8-27B-UD-Q4_K_M.gguf`
(chữ + ảnh vào, chữ ra; context **98 304 token**). Dùng bất kỳ SDK OpenAI nào: chỉ đổi `base_url` và `api_key`.

## 1. Kết nối

| Mục | Giá trị |
|---|---|
| Base URL | `<BASE_URL>/v1` — do bên vận hành cấp (mạng LAN: `http://192.168.1.40:18080/v1`) |
| Xác thực | Header `Authorization: Bearer <API_KEY>` — key do bên vận hành cấp, mỗi bên một key |
| Định dạng | JSON, UTF-8 |
| Model | `Qwen3.8-27B-UD-Q4_K_M.gguf` (lấy danh sách và khả năng bằng `GET /v1/models`) |

Chưa có TLS ở đường LAN: không gửi key qua mạng không tin cậy.

## 2. Gọi nhanh

```bash
curl <BASE_URL>/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"Qwen3.8-27B-UD-Q4_K_M.gguf",
       "messages":[{"role":"user","content":"Xin chào"}],
       "max_tokens":512}'
```

```python
from openai import OpenAI
c = OpenAI(base_url="<BASE_URL>/v1", api_key="<API_KEY>")
r = c.chat.completions.create(model="Qwen3.8-27B-UD-Q4_K_M.gguf",
        messages=[{"role":"user","content":"Xin chào"}], max_tokens=512)
print(r.choices[0].message.content)
```

Streaming: thêm `"stream": true` (SSE chuẩn OpenAI, kết thúc bằng `data: [DONE]`). Hỗ trợ `temperature`, `top_p`,
`max_tokens` / `max_completion_tokens`, `stop`, `tools` (function calling).

## 3. Ảnh (vision)

Chuẩn OpenAI: phần tử `image_url` trong `content`, nhận `data:` URI base64 hoặc URL `http(s)` công khai
(gateway tự tải; địa chỉ nội bộ/loopback bị chặn). Tối đa **8 ảnh/request**.

```json
{"model":"Qwen3.8-27B-UD-Q4_K_M.gguf","messages":[{"role":"user","content":[
  {"type":"text","text":"Mô tả ảnh này"},
  {"type":"image_url","image_url":{"url":"https://example.com/a.jpg"}}]}]}
```

## 4. Chế độ suy nghĩ (thinking)

Model có chế độ suy nghĩ. **Mặc định của hệ thống: TẮT** (trả lời nhanh, `content` có ngay). Bật cho từng request:
`"chat_template_kwargs":{"enable_thinking":true}` (hoặc header `X-AICF-Thinking: on`). Khi bật, phần suy nghĩ nằm ở
`reasoning_content` và **tốn max_tokens** — hãy đặt `max_tokens` đủ lớn (≥ 2000), nếu không `content` có thể rỗng với
`finish_reason: "length"`. Header phản hồi `X-AICF-Thinking-Mode: on|off` cho biết chế độ đã áp dụng.

## 5. Hội thoại dài và giới hạn context

API là *stateless*: client gửi lại toàn bộ `messages` ở mỗi lượt và **tự quản lý lịch sử**. Khi prompt vượt context của
model (**98 304 token**), engine trả lỗi 400 `exceed_context_size_error`. Gateway **không** tự nén hay cắt hội thoại: client cần
tự rút gọn (cắt bớt lượt cũ hoặc tự tóm tắt) trước khi gửi. Thông tin bắt buộc không được quên nên đặt trong system prompt.

## 6. Mã lỗi

Lỗi trả dạng `{"error":{"message":"...","code":N}}`.

| HTTP | Ý nghĩa | Xử lý |
|---|---|---|
| 400 | Request sai; hoặc `exceed_context_size_error` (prompt vượt 98 304 token) | Sửa request / rút gọn lịch sử |
| 401 | Thiếu hoặc sai API key | Kiểm tra key |
| 429 | Hàng đợi đầy / ước tính chờ quá lâu / quá nhiều request đang chờ của key | Đọc `Retry-After` (giây), thử lại |
| 502 / 503 | Worker lỗi hoặc không có chỗ trong thời gian chờ tối đa (60 s) | Thử lại sau `Retry-After` |

## 7. Giới hạn và hiệu năng (đo trên cấu hình hiện tại)

- Cấu hình: 1 luồng sinh (slot) chạy trên 2 GPU ghép (RTX 3060 + RTX 5070); **~24 token/s** cho một request.
  Nhiều request đồng thời **xếp hàng** (không chạy song song); số request đồng thời tối đa theo từng key (key tích hợp mẫu: 2).
- Chưa đo tải 50 người dùng đồng thời; không nên giả định hệ thống phục vụ được mức đó.
- Request dài (hàng chục nghìn token) tốn thời gian xử lý prompt đáng kể: đo ~62 s cho 55 800 token.

## 8. Cổng tương thích Ollama (tuỳ chọn)

Bên vận hành có thể cấp cổng Ollama (mặc định `:11434`) cho công cụ dùng giao thức Ollama (`/api/chat`, `/api/tags`, …)
và `/v1/chat/completions`. Cùng API key.
