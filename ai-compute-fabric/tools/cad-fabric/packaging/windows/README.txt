CadFabric - bộ cài Windows (offline)
====================================
Yêu cầu: Windows 10/11 64-bit. Không cần quyền quản trị, không cần cài Python, không cần Internet.

CÀI ĐẶT
  1. Giải nén thư mục này ra ổ đĩa (đừng chạy trực tiếp trong file zip).
  2. Bấm đúp install.cmd. Mất khoảng 1-3 phút. Cài vào %LOCALAPPDATA%\CadFabric
     và tạo shortcut "CadFabric" trên Start Menu và Desktop.
  3. Mở shortcut "CadFabric". Trình duyệt tự mở trang chat. Lần đầu, hộp "Cài đặt" tự hiện:
       - URL API Qwen (ví dụ http://192.168.1.40:18080/v1/chat/completions)
       - Tên model, khoá API (bấm "Kiểm tra kết nối" để thử)
       - Cố vấn: off / Qwen nhanh / Qwen sâu / Codex trên máy này / Codex qua ssh
     Cố vấn Codex gửi nội dung yêu cầu ra dịch vụ bên ngoài; ứng dụng luôn hỏi xác nhận trước.

CÀI HÀNG LOẠT (cấu hình sẵn, người dùng khỏi phải điền)
  powershell -ExecutionPolicy Bypass -File install.ps1 -Url http://192.168.1.40:18080/v1/chat/completions -Model <tên model> -KeyFile \\server\share\key.txt -Planner nothink
  (khoá được đọc từ file, lưu riêng cho từng người dùng, không bao giờ nằm trên dòng lệnh hay trong file cấu hình)

DÙNG DÒNG LỆNH
  %LOCALAPPDATA%\CadFabric\cadfabric.cmd doctor | run "mô tả" | tasks | bench

GỠ CÀI ĐẶT
  powershell -ExecutionPolicy Bypass -File %LOCALAPPDATA%\CadFabric\uninstall.ps1        (thêm -Purge để xoá cả cài đặt và file STEP)

GHI CHÚ
  - Ứng dụng chỉ lắng nghe 127.0.0.1 (máy này), mỗi lần chạy có token riêng.
  - Cần mạng tới máy chủ Qwen. Cố vấn Codex cần Codex CLI đã cài và đăng nhập trên máy đó (hoặc máy ssh đích).
  - Nếu cài báo thiếu msvcp140.dll thì vc_redist.x64.exe trong thư mục này sẽ được chạy (Windows hỏi quyền một lần).
