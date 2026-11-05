# ADR-004: Trình Soạn Thảo Mã Nguồn Trực Tiếp Trên Web (Browser Code Editor)

- **Trạng thái:** Đã chấp thuận (Accepted)
- **Ngày:** 2026-11-05
- **Liên quan:** `ADR-003-serverless-gpu.md`, `apps/site-gateway/web/serverless.html`, `serverless/deployments.py`

---

## 1. Bối cảnh & Vấn đề

Trong bảng điều khiển `serverless.html`, nhà phát triển cần viết, chỉnh sửa, kiểm tra cú pháp (lint), chạy thử nhanh (`Ctrl+Enter`), so sánh phiên bản (`diff`) và triển khai các ứng dụng Serverless Python/CUDA (gồm cả ứng dụng nhiều tệp `.py`).

Các ràng buộc kỹ thuật bắt buộc của **AI Compute Fabric**:
1. **Air-gapped / LAN nội bộ (`0` CDN ngoài):** Môi trường văn phòng hoặc cụm GPU riêng có thể chặn hoàn toàn kết nối Internet ra ngoài. Trình soạn thảo phải hoạt động 100% từ các tệp tĩnh phục vụ nội bộ bởi `site-gateway`.
2. **Tuân thủ `Content-Security-Policy` nghiêm ngặt (`script-src 'self'`):** Không dùng `'unsafe-inline'`, không dùng `'unsafe-eval'`, không phụ thuộc Web Worker khởi tạo từ `blob:` bị chặn bởi CSP.
3. **Hiệu năng & Hỗ trợ thiết bị di động (`375x812` → `2560x1440`):** Tải nhanh, hỗ trợ cảm ứng, bàn phím tắt chuẩn IDE và tự động suy giảm duyên dáng (graceful degradation) về `<textarea>` nếu trình duyệt tối giản.
4. **An toàn máy chủ (`Host Safety`):** Kiểm tra cú pháp/lint (`POST /api/serverless/v1/lint`) tuyệt đối không thực thi (`import`/`exec`/`eval`) mã người dùng trên host; phải chạy trong tiến trình con có giới hạn `RLIMIT_CPU <= 2s` và `RLIMIT_AS <= 256 MiB`.

---

## 2. Các Phương án Đánh giá

| Tiêu chí | Phương án A: `<textarea>` thuần | Phương án B: Monaco Editor | Phương án C: CodeMirror 6 / AicfEditor (Bundle nội bộ) |
|---|---|---|---|
| Kích thước gói tải | `0 KB` | `~2.5 – 4.0 MB` (+ Web Workers) | `< 60 KB` (nội bộ, không phụ thuộc ngoài) |
| Yêu cầu CSP / Worker | `script-src 'self'` | Cần `worker-src blob:` và CSS động phức tạp | Hoạt động 100% với `script-src 'self'` |
| Hỗ trợ Mobile `375x812` | Tốt nhưng thiếu tính năng IDE | Kém trên màn hình cảm ứng hẹp | Rất tốt, bố cục linh hoạt |
| Tô màu cú pháp Python, số dòng, khớp ngoặc, gợi ý SDK (`@app.*`, `aicf_gpu.*`) | Không có | Có | Có đầy đủ |
| Khả năng đồng bộ ngược với `<textarea id="sl-code-editor">` | Tự nhiên | Phức tạp | Đồng bộ 2 chiều tức thì + fallback tự động |

---

## 3. Quyết định Kiến trúc

Chúng tôi chọn **Phương án C**: Xây dựng mô-đun trình soạn thảo nội bộ `apps/site-gateway/web/vendor/codemirror6.bundle.js` (cung cấp API kiểu mô-đun CodeMirror 6 `EditorState`/`EditorView` cùng lớp bọc `window.AicfEditor`) kết hợp đồng bộ 2 chiều với `<textarea id="sl-code-editor">`:

1. **Tự chứa 100% (`0` CDN ngoài, `script-src 'self'`):**
   - Tệp `apps/site-gateway/web/vendor/codemirror6.bundle.js` được đóng gói và phục vụ trực tiếp từ `site-gateway`.
   - Hiển thị cột số dòng (`gutter`), đánh dấu dòng có lỗi cú pháp/lint (`🔴`/`🟡`), tô màu cú pháp Python + từ khóa SDK (`@app.function`, `@app.cls`, `@app.web_endpoint`, `aicf_gpu.*`), tự đóng ngoặc/nháy, `Tab` = 4 dấu cách, `Shift+Tab` lùi đầu dòng, `Ctrl+/` đóng/mở chú thích, `Ctrl+F` tìm kiếm, `Ctrl+Space` gợi ý hàm SDK, và `Ctrl+Enter` chạy thử hàm.
2. **Kiểm tra tĩnh an toàn (`POST /api/serverless/v1/lint`):**
   - Chạy trong tiến trình con cô lập với `resource.setrlimit(RLIMIT_CPU, (2, 2))` và `RLIMIT_AS = 256 MiB`, timeout cứng `2.0s`.
   - Phân tích cây cú pháp trừu tượng (`ast.parse`) và kiểm tra tĩnh (phát hiện biến chưa định nghĩa, import thừa, thiếu hàm entrypoint) mà **không bao giờ chạy mã người dùng**.
3. **Đa tệp (`files: dict[str, str]`), Lưu nháp (`localStorage`) & So sánh Phiên bản (`Diff`):**
   - Hỗ trợ thanh tab nhiều tệp `.py` (`user_app.py`, `helpers.py`, ...); khi triển khai, mọi tệp trong `files` được đóng gói và giải nén vào thư mục làm việc của container sandbox trước khi nạp `entrypoint`.
   - Tự động lưu bản nháp vào `localStorage` kèm cảnh báo khi chưa lưu và công cụ so sánh khác biệt (`Diff`) theo từng dòng giữa mã đang sửa với các phiên bản đã triển khai (`v-...`).

