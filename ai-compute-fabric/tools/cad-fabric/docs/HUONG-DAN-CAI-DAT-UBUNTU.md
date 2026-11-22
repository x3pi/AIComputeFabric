# Cài CadFabric trên Ubuntu

CadFabric là ứng dụng chat thiết kế CAD: bạn mô tả chi tiết bằng tiếng Việt, Qwen viết mã CadQuery, kernel đo thể tích thật sau mỗi bước. Cách dùng giống bản Windows (xem `HUONG-DAN-CAI-DAT-WINDOWS.md` cho các mục Cố vấn, Thư mục làm việc, Lịch sử, Mở trong CAD); tài liệu này chỉ nói phần cài đặt trên Ubuntu.

Bộ cài **offline**, chạy bằng **tài khoản thường (không dùng sudo)**, không đụng Python hay gói nào của hệ thống: nó tạo một môi trường ảo riêng.

## Yêu cầu

| Mục | Cần |
|---|---|
| Hệ điều hành | Ubuntu 22.04 hoặc 24.04, x86_64 (Debian tương đương) |
| Python | 3.10 (Ubuntu 22.04) hoặc 3.12 (Ubuntu 24.04); đã có sẵn trên hệ thống |
| Gói | `python3-venv` (cài một lần nếu thiếu: `sudo apt install python3-venv`) |
| Đĩa trống | khoảng 1,5 GB |
| Mạng | nối được tới máy chủ Qwen trong LAN |
| Tuỳ chọn, cho cửa sổ riêng | `sudo apt install python3-gi gir1.2-webkit2-4.1` (Ubuntu 24.04 thường đã có) |

Python khác (ví dụ 3.11): thêm `--online` để pip tự tải thư viện.

## Cài đặt

```bash
tar xzf cadfabric-linux-<phiên bản>.tar.gz
cd cadfabric-linux-<phiên bản>
./install.sh
```

Mất khoảng 1 đến 2 phút. Xong, tìm **CadFabric** trong menu ứng dụng, hoặc chạy `~/.local/bin/cadfabric app`.

Ứng dụng nằm ở `~/.local/share/cadfabric-app`, cài đặt người dùng ở `~/.config/cadfabric`, lịch sử và file STEP xuất ra ở `~/.local/share/cadfabric`.

Có GTK và WebKit thì CadFabric mở **cửa sổ riêng** (đóng cửa sổ là tắt ứng dụng). Không có thì tự mở bằng trình duyệt, tắt bằng nút **Thoát**.

## Cài hàng loạt (cấu hình sẵn)

```bash
./install.sh --url http://192.168.1.40:18080/v1/chat/completions \
  --model Qwen3.8-27B-UD-Q4_K_M.gguf --key-file ~/key.txt --planner nothink
```

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--url`, `--model`, `--planner` | cấu hình Qwen và cố vấn mặc định |
| `--key-file` | file chứa khoá API; được đọc rồi lưu riêng cho người dùng, không nằm trên dòng lệnh |
| `--advisor-cli claude` hoặc `codex` | thêm sẵn cố vấn CLI (id `claude-cli` hoặc `codex-cli`) |
| `--prefix` | cài vào thư mục khác |
| `--no-shortcuts` | không tạo mục menu |
| `--online` | cho phép tải thư viện từ internet nếu bộ cài không có bản hợp với Python của bạn |

## Nâng cấp và gỡ

- **Nâng cấp:** giải nén bản mới và chạy lại `./install.sh`. Nó tự tắt ứng dụng đang chạy, giữ nguyên cài đặt và lịch sử.
- **Gỡ:** `~/.local/share/cadfabric-app/uninstall.sh` (thêm `--purge` để xoá cả cài đặt, lịch sử và file STEP).

## Dòng lệnh

```
cadfabric doctor          kiểm tra CadQuery, sandbox, kết nối Qwen
cadfabric run "mô tả"     dựng chi tiết, không cần giao diện
cadfabric app             mở ứng dụng (--browser: dùng trình duyệt)
```

## Mở chi tiết xem

Ubuntu không có SolidWorks. Nút **Mở trong CAD** mở file STEP bằng chương trình mặc định của hệ thống (`xdg-open`) nếu bạn đã gán một chương trình cho `.step` (ví dụ FreeCAD: `sudo apt install freecad`). Hoặc chọn **lệnh tự đặt** trong Cài đặt, ví dụ `freecad {file}`.

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| `python3-venv is missing` | `sudo apt install python3-venv` |
| `no matching wheel` / Python 3.11 | chạy lại với `--online` |
| `cadfabric: command not found` | `~/.local/bin` chưa nằm trong PATH; đăng xuất rồi đăng nhập lại, hoặc dùng đường dẫn đầy đủ |
| Mở bằng trình duyệt thay vì cửa sổ riêng | `sudo apt install python3-gi gir1.2-webkit2-4.1` rồi mở lại |
| Nút Mở trong CAD báo không mở được | gán chương trình cho `.step` hoặc đặt lệnh tự đặt (mục trên) |
| Nghi ngờ cài chưa đúng | `cadfabric doctor`: mỗi bước hiện OK hoặc FAIL kèm lý do |
