# windows-agent (wagent)

Agent điều khiển máy Windows bằng một model OpenAI-compatible (Qwen local qua llama.cpp/vLLM/SGLang/gateway AICF).
Kế hoạch & nhật ký đo đạc: `../KE_HOACH_APP_DIEU_KHIEN_WINDOWS.md`.

## Mở bằng icon trên desktop (Windows)
Sau khi `scripts/sync-to-windows.sh` (đã gồm `ui_dist`) chạy `powershell -File C:\wagent\scripts\install_shortcut.ps1` một lần → có icon **Wagent** trên Desktop. Bấm vào: tự bật server ẩn và mở trình duyệt vào giao diện (token cố định ở `C:\wagent\web.token`). Build giao diện: `cd ui && npm install && npm run build`.

## Cách hoạt động
`observe (cây UI Automation dạng văn bản) → model trả 1 JSON {"tool","args"} → guard → thực thi → audit`.
Không cần model thị giác. Nội dung đọc từ màn hình/file là **dữ liệu không tin cậy**.

## Cài (Windows)
```
py -3.12 -m venv C:\wagent\venv
C:\wagent\venv\Scripts\pip install uiautomation requests
set PYTHONPATH=<thư mục src>
```
Từ Linux: `scripts/sync-to-windows.sh` (đẩy mã sang `C:\wagent`).

## Chạy
- Một nhiệm vụ: `python -m wagent --url http://HOST:PORT/v1 [--key K] [--model M] "nhiệm vụ"`
- Giao diện web: `python -m wagent --web 8765` → in `http://127.0.0.1:8765/?t=<token>`. Chỉ nghe 127.0.0.1; từ máy khác dùng `ssh -L 18765:127.0.0.1:8765 user@windows`.
- Chọn model: bấm **Tải danh sách** cạnh ô Model (server Wagent gọi `GET <URL>/models`, key gửi kèm nếu có). Engine trực tiếp (`:18090`) không cần key và chỉ chạy một model; gateway (`:18080`) cần API key tenant. Lỗi hiện ngay dưới ô (vd 401 → cần key). Không tải được thì vẫn gõ tay hoặc để trống.
- Duyệt hành động rủi ro: web UI (nút Cho phép/Từ chối) hoặc `--approve ask` (file `C:\wagent\approvals\pending-*.json` → tạo `.allow`/`.deny`). Mặc định `--approve deny`. Hết giờ chờ = từ chối.
- Dừng: nút DUNG trên web, hoặc tạo file `C:\wagent\STOP`.

## Quan trọng: phải chạy trong phiên desktop đã đăng nhập
GUI automation cần phiên tương tác của user (phiên ở trạng thái "Disconnected" vẫn đọc được cây UIA nhưng **SendKeys không hoạt động** → skill dùng UIA pattern `SetValue`/`Invoke`). Cách chạy từ ssh: Scheduled Task `/IT`:
`schtasks /Create /TN wagent_run /TR "C:\wagent\scripts\run_task.cmd \"nhiệm vụ\"" /SC ONCE /ST 23:59 /IT /F` rồi `schtasks /Run /TN wagent_run`; log ở `C:\wagent\run.log`.
Task chạy ở Medium integrity (token lọc UAC), không có quyền admin.

## Công cụ (skill)
`observe, open_app(whitelist: notepad|calc|mspaint), set_text, read_text, click (theo `id` hoặc `name` của nút, vd `{"name":"Eight"}`), close_window, read_file, write_file, list_dir, move_file, copy_file, done`. File chỉ trong sandbox (`C:\wagent\sandbox`), chống `..` và symlink.

## Thêm app cho `open_app` (mặc định chỉ notepad|calc|mspaint)
Tạo `C:\wagent\apps.json` (hoặc đặt biến `WAGENT_APPS_FILE`), mẫu ở `apps.example.json`:
`{"chrome": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"}`.
Chỉ nhận tên `a-z0-9_-` và đường dẫn tuyệt đối `.exe` (không đối số, không shell, không ghi đè app mặc định); mục sai bị bỏ qua. **Mỗi lần mở app thêm đều cần bạn bấm Cho phép.** Sau khi sửa file, mở lại server (icon Wagent). Lưu ý: agent mở trình duyệt thì có thể đọc nội dung web không tin cậy (prompt injection); `close_window` chỉ đóng cửa sổ do agent mở, không đóng Chrome cũ của bạn.

## Bài học (dạy agent nhiệm vụ mới không cần sửa code)
1. Chạy nhiệm vụ trên giao diện, duyệt các bước. Khi **thành công**, bấm **Lưu thành bài học** (đặt tên). Lưu ở `C:\wagent\lessons\<id>.json` (`--lessons-dir`, `""` = tắt).
2. Lần sau: ô **Bài học** = *Tự động* (chỉ dùng khi nhiệm vụ mới trùng ≥5 từ khoá và ≥80% ý chính), *Không dùng*, hoặc chọn một bài. Xoá bài ngay trong panel.
3. Bài học chỉ là **gợi ý** gắn vào tin nhắn nhiệm vụ (không vào system prompt); mọi hành động vẫn qua guard/duyệt. URL, tên file, số, văn bản giống mật khẩu bị loại/thay placeholder khi lưu.
API: `GET /api/lessons`, `POST /api/lessons {"title"}`, `POST /api/lessons/delete {"id"}`; run nhận `"lesson"`. Crash native khi chạy nhiều nhiệm vụ liên tiếp được ghi ở `C:\wagent\fault.log` (faulthandler).

## An toàn
- Tool ngoài whitelist → từ chối. Cần duyệt: click phần tử tên rủi ro (delete/send/pay/install/password…), ghi đè file, văn bản giống mật khẩu.
- Audit JSONL hash-chain (`wagent.audit.verify`). API key chỉ ở bộ nhớ, không ghi log.
- **Qwen không tự kháng prompt injection** (đo thật, GĐ2) — an toàn dựa vào guard/sandbox. Đừng cho agent đọc nội dung không tin cậy rồi cấp quyền rộng.
- Không có cô lập file-system ngoài sandbox cho skill file; cô lập thật cần VM/máy riêng.
- `summary` do model viết có thể sai: kiểm chứng bằng trạng thái thật (benchmark làm đúng như vậy).

## Test
`pip install pytest requests && pytest tests` (chạy được trên Linux, dùng LLM/UIA giả). Benchmark thật: `scripts/run_bench.cmd --n 5` trên Windows → `bench.jsonl`.
