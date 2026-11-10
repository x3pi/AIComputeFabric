# AI Compute Fabric — Tài liệu API cho bên tích hợp

API tương thích **OpenAI Chat Completions** (cũng có cổng tương thích Ollama). Mô hình hiện chạy: `Qwen3.8-27B-UD-Q4_K_M.gguf`
(chữ + ảnh vào, chữ ra; context **65 536 token**). Dùng bất kỳ SDK OpenAI nào: chỉ đổi `base_url` và `api_key`.

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

## 5. Nhớ hội thoại dài (nén hội thoại tự động)

### 5.1 Vấn đề và cách giải quyết
API là *stateless*: client gửi lại toàn bộ `messages` ở mỗi lượt. Khi lịch sử dài hơn context của model (**65 536 token**),
engine từ chối với lỗi 400 `exceed_context_size_error`. Gateway xử lý việc này giúp client: **tóm tắt phần cũ của hội thoại
thành một "ghi chú trí nhớ" ngắn, rồi gửi lại và trả lời bình thường**. Client không phải đổi code.

### 5.2 Ba chế độ

| Chế độ | Khi nào nén | Ghi chú |
|---|---|---|
| `overflow` (**mặc định**) | Chỉ khi engine đã từ chối request vì quá dài | Request vừa context hoàn toàn không bị can thiệp. Nếu không nén, request đó vốn sẽ nhận lỗi 400 |
| `auto` hoặc `auto:T` | Nén **trước khi gửi** khi ước lượng prompt vượt `T` × context (mặc định `T`=0,7; hợp lệ 0,3–0,95); vẫn nén khi bị từ chối | Giảm độ trễ/tải prefill cho hội thoại rất dài. Phụ thuộc vào việc gateway biết độ dài context của engine; nếu chưa biết thì chỉ còn tác dụng như `overflow`. Ước lượng thô (~3 ký tự/token, mỗi ảnh ~1 500 token) |
| `off` | Không bao giờ | Nhận lại đúng lỗi 400 gốc của engine |

Cách chọn chế độ cho từng request (cách nào cũng được; header ưu tiên hơn trường body):

```bash
# 1) Header
-H "X-AICF-Compact: off"                 # hoặc: overflow | auto | auto:0.8
-H "X-AICF-Compact-Keep: 6"              # số tin nhắn cuối giữ nguyên (2–40, mặc định 4)

# 2) Trường body (gateway gỡ trường này trước khi chuyển cho model)
"aicf_compact": "auto:0.8"
"aicf_compact": {"mode": "auto", "threshold": 0.8, "keep": 6}
```

```python
# Với OpenAI Python SDK
c.chat.completions.create(model=..., messages=...,
    extra_headers={"X-AICF-Compact": "off"})              # tắt nén
c.chat.completions.create(model=..., messages=...,
    extra_body={"aicf_compact": {"mode": "auto", "threshold": 0.8}})
```

Giá trị mặc định toàn hệ thống do bên vận hành đặt (flag `-compact-default`); hiện là `overflow`.

### 5.3 Phần nào được giữ, phần nào bị tóm tắt
- **Giữ nguyên:** toàn bộ các tin nhắn `system`/`developer` ở đầu, và `N` tin nhắn cuối (mặc định 4). Điểm cắt luôn lùi về một
  tin nhắn `user`, nên một lệnh gọi công cụ (`tool_calls`) không bao giờ bị tách khỏi kết quả `tool` của nó. Vì vậy số tin nhắn
  giữ lại có thể nhiều hơn `N` một chút. Tin nhắn `user` mới nhất luôn được giữ nguyên văn.
- **Bị tóm tắt:** mọi thứ nằm giữa. Gateway viết ghi chú bằng cùng ngôn ngữ người dùng, yêu cầu giữ nguyên văn mã, lệnh, đường
  dẫn, định danh, số liệu, quyết định và việc đang dở. Ghi chú được **gắn vào cuối tin nhắn system** (hoặc thành tin nhắn system
  mới nếu chưa có), bắt đầu bằng "Memory of the earlier part of this conversation (compressed automatically by the gateway)".
- **Hội thoại rất dài** được tóm tắt theo từng đoạn (mỗi đoạn ~ nửa cửa sổ context) và gộp dần.
- **Nếu sau khi nén vẫn quá dài** (ví dụ vài tin nhắn gần nhất rất lớn), gateway nén thêm một lần nữa và chỉ giữ 2 tin nhắn cuối.
- **Giới hạn của bản tóm tắt:** ảnh trong phần cũ chỉ còn là ký hiệu `[ảnh]` (nội dung ảnh không được nhớ); kết quả công cụ dài bị
  cắt còn ~1 500 ký tự; tin nhắn đơn lẻ quá lớn chỉ giữ đầu và đuôi; tên hàm gọi công cụ giữ nhưng tham số cắt ~500 ký tự.

### 5.4 Cách nhận biết trong phản hồi

| Header | Ý nghĩa |
|---|---|
| `X-AICF-Compacted: 178 messages -> memory note (~2668 tokens), trigger=overflow` | Đã nén: số tin nhắn bị thay, cỡ ghi chú, nguyên nhân (`overflow` hoặc `auto`) |
| `X-AICF-Compact-Cache: hit` | Dùng lại bản tóm tắt đã có, không phải tóm tắt lại |
| `X-AICF-Compact-Failed: <lý do>` | Nén thất bại (ví dụ không đủ tin nhắn cũ để nén, hoặc model tóm tắt lỗi); client nhận lại **đúng lỗi 400 gốc** |

Với streaming (`stream: true`) các header này nằm ở phần header của phản hồi, trước khi dữ liệu đầu tiên được gửi.

### 5.5 Độ trễ và cache — quan trọng khi đặt timeout
- Nén là một lượt gọi model thêm (tóm tắt) **trước** khi trả lời, nên **chậm**. Đo thực tế với lịch sử ~84 000 token:
  **117 giây** ở một lần đo và **7 phút** ở lần đo khác (chưa rõ nguyên nhân dao động). Hãy đặt timeout client **≥ 10 phút**
  cho hội thoại rất dài, hoặc dùng `X-AICF-Compact: off` và tự quản lý lịch sử.
- Bản tóm tắt được **cache theo tiền tố lịch sử** của từng API key (hiệu lực **1 giờ**, tối đa 512 mục, lưu xuống đĩa nên còn
  sau khi gateway khởi động lại). Client gửi lại cùng lịch sử, hoặc lịch sử dài thêm vài tin nhắn, chỉ phải trả phí cho phần mới.
  Đo thực tế: gửi lại cùng request sau khi khởi động lại gateway mất **2,5 giây** (`X-AICF-Compact-Cache: hit`).
- Trong lúc đang nén, luồng sinh duy nhất của hệ thống bị chiếm, nên **request của các client khác phải xếp hàng** và có thể nhận
  429/503 nếu chờ quá 60 giây. Đây là suy ra từ cấu hình 1 luồng, chưa được đo trực tiếp.

### 5.6 Độ tin cậy của "trí nhớ" và khuyến nghị
- Bản tóm tắt là **nén có mất mát**. Trong bài thử trên model thật, tên người dùng và mã dự án đặt ở đầu một hội thoại 83 764 token
  được nhớ đúng; không có gì đảm bảo tuyệt đối cho mọi nội dung.
- Thông tin bắt buộc không được quên (vai trò, quy tắc, mã khách hàng…) nên đặt trong **system prompt** của client: system prompt
  luôn được giữ nguyên.
- Nên tự giữ lịch sử đầy đủ ở phía client; gateway không lưu hội thoại (chỉ cache bản tóm tắt tạm thời, file cache chứa nội dung
  tóm tắt nên được bảo vệ bằng quyền 0600).
- Nén áp dụng cho `POST /v1/chat/completions` ở cổng chính và ở cổng tương thích Ollama. **Chưa áp dụng** cho
  `/v1/responses`, `/v1/completions` và `/api/chat` (giao thức Ollama gốc): các đường này vẫn trả 400 khi tràn context.

### 5.7 Ví dụ
```bash
# Hội thoại dài, để mặc định: tràn context thì tự nén
curl -i <BASE_URL>/v1/chat/completions -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" -d @hoi_thoai_dai.json
# -> HTTP/1.1 200 OK
#    X-Aicf-Compacted: 178 messages -> memory note (~2668 tokens), trigger=overflow

# Giữ hành vi cũ (nhận lỗi 400 khi tràn)
curl -i ... -H "X-AICF-Compact: off" -d @hoi_thoai_dai.json
# -> HTTP/1.1 400   {"error":{"type":"exceed_context_size_error","n_prompt_tokens":83764,"n_ctx":65536,...}}
```

## 6. Mã lỗi

Lỗi trả dạng `{"error":{"message":"...","code":N}}`.

| HTTP | Ý nghĩa | Xử lý |
|---|---|---|
| 400 | Request sai; hoặc `exceed_context_size_error` (chỉ khi nén bị tắt/thất bại) | Sửa request / rút gọn lịch sử |
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
và `/v1/chat/completions`. Cùng API key; nén hội thoại chỉ áp dụng cho `/v1/chat/completions` của cổng này (xem mục 5.6).
