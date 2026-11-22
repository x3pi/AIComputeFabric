# Cài CadFabric trên máy Windows

CadFabric là ứng dụng chat thiết kế CAD: bạn mô tả chi tiết bằng tiếng Việt, Qwen viết mã CadQuery, kernel đo thể tích thật sau mỗi bước, và có thể mở chi tiết trong SolidWorks.

Bộ cài **offline** (không cần Internet), **không cần quyền quản trị**, **không cần cài Python**. Nó mang theo Python 3.12 riêng và không đụng Python sẵn có trên máy.

## Yêu cầu

| Mục | Cần |
|---|---|
| Hệ điều hành | Windows 10 hoặc 11, 64-bit |
| Đĩa trống | khoảng 2 GB |
| Mạng | nối được tới máy chủ Qwen trong LAN (ví dụ `192.168.1.40:18080`) |
| Tuỳ chọn | SolidWorks (để mở chi tiết xem), Claude CLI hoặc Codex CLI đã đăng nhập (để làm cố vấn) |

## Cài đặt

1. Lấy file `cadfabric-windows-<phiên bản>.zip` (khoảng 354 MB) và chép sang máy Windows bằng USB, thư mục chia sẻ, hoặc `scp`.
2. Chuột phải file zip, chọn **Extract All** (giải nén hết ra một thư mục). Đừng chạy trực tiếp trong file zip.
3. Trong thư mục vừa giải nén, bấm đúp **`install.cmd`**. Mất khoảng 1 đến 3 phút.
4. Khi báo `Installed`, trên Start Menu và Desktop có shortcut **CadFabric**.

Ứng dụng được cài vào `%LOCALAPPDATA%\CadFabric`. Cài đặt của người dùng nằm ở `%APPDATA%\cadfabric`, file STEP xuất ra nằm ở `%LOCALAPPDATA%\cadfabric\exports`.

## Dùng lần đầu

1. Bấm shortcut **CadFabric**. Ứng dụng mở cửa sổ riêng (dùng Microsoft Edge WebView2 có sẵn trong Windows 10/11); nếu máy không có WebView2 thì tự mở bằng trình duyệt. Đóng cửa sổ là tắt ứng dụng.
2. Hộp **Cài đặt** tự hiện ở lần chạy đầu. Điền:
   - **URL API Qwen**, ví dụ `http://192.168.1.40:18080/v1/chat/completions`
   - **Tên model** (để trống nếu máy chủ tự chọn). Nút **Kiểm tra kết nối** liệt kê model có sẵn.
   - **Khoá API**. Khoá được lưu vào file riêng của người dùng, không nằm trong file cấu hình.
3. Chọn **cố vấn** (xem bảng dưới), rồi bấm **Lưu**.
4. Gõ yêu cầu, ví dụ: `Đĩa tròn Ø100 dày 10 mm, khoan 6 lỗ xuyên Ø8 đều trên vòng tròn Ø70. Báo thể tích.`

Tin nhắn tiếp theo chỉnh sửa chính chi tiết đó. Gõ `/new` để bắt đầu chi tiết mới, `/stop` để dừng.

### Cố vấn là gì

Cố vấn lập một kế hoạch có công thức thể tích. Chương trình tự tính các công thức đó và đối chiếu với chi tiết thật sau mỗi bước.

| Lựa chọn | Dữ liệu đi đâu | Ghi chú |
|---|---|---|
| Không cố vấn | ở lại máy bạn | nhanh nhất |
| Qwen lập kế hoạch (nhanh) | ở lại máy chủ Qwen của bạn | mặc định |
| Qwen suy nghĩ sâu | ở lại máy chủ Qwen | chậm, có thể quá hạn với đề khó |
| Codex hoặc Claude CLI | **gửi ra dịch vụ bên ngoài** | cần CLI đã cài và đăng nhập; ứng dụng hỏi xác nhận một lần cho mỗi cố vấn trong mỗi lần mở trang |
| API do bạn thêm | tuỳ URL | OpenAI-compatible hoặc Anthropic |

Thêm cố vấn: **Cài đặt, + Thêm cố vấn**, chọn **API (URL)** hoặc **CLI**. Với Claude và Codex chọn thêm model (yếu/nhanh hay mạnh) và mức suy nghĩ. Mỗi cố vấn có nút **Kiểm tra**.

## Thư mục làm việc, đính kèm tệp, tra cứu internet

**Thư mục làm việc:** trong **Cài đặt** bấm **Chọn…** (hoặc gõ đường dẫn) để chọn thư mục dự án. Qwen và cố vấn được **đọc** tệp văn bản trong đó (txt, md, csv, json, dxf, step, py, ...), không ghi đè bất kỳ tệp nào. Tệp bí mật (`.env`, `key*.txt`, `*.pem`, thư mục `.git`, `.ssh`...) và đường dẫn trỏ ra ngoài thư mục luôn bị chặn.
- Bạn có thể nói trong chat: "đọc tệp sub/hop.txt rồi dựng đúng chi tiết đó", "tìm chữ bu lông trong thư mục", "lưu chi tiết thành tệp hop-test" (tạo tệp `.step` mới, không bao giờ ghi đè).
- Nút **📎 Tệp** ở khung chat chọn tệp (tối đa 8) đính kèm vào tin nhắn: nội dung được đưa cho cả Qwen **và cố vấn**. Với cố vấn bên ngoài (Claude, Codex), ứng dụng hỏi thêm một lần vì nội dung tệp rời khỏi máy.

**Tra cứu internet:** khi thêm cố vấn Claude Code hoặc Codex, tích **Cho phép tra cứu internet**. Cố vấn tìm kiếm và đọc trang web để kiểm tra tiêu chuẩn, kích thước chuẩn rồi nêu nguồn; vẫn không đọc được tệp trên máy bạn. Chỉ nên bật cho cố vấn bạn tin cậy, vì câu hỏi có thể được dùng để tìm kiếm.

## Lịch sử thiết kế

Mỗi cuộc trò chuyện tự lưu sau mỗi lượt, kể cả khi tắt ứng dụng. Bấm **Lịch sử** ở thanh trên để xem danh sách (tên, giờ, số bước, thể tích), **Mở** để dựng lại chi tiết và chat tiếp (Qwen vẫn nhớ ngữ cảnh), **Xoá** hoặc **Xoá tất cả** để dọn. Lưu tối đa 100 mục gần nhất tại `%LOCALAPPDATA%\cadfabric\history` (chỉ người dùng này đọc được). Khi mở lại, chi tiết được dựng lại bằng cách chạy lại các bước đã lưu; nếu có bước không dựng lại được, ứng dụng báo.

## Mở chi tiết trong SolidWorks

Có ba cách:
- Gõ trong chat: `mở bằng SolidWorks cho tôi xem`.
- Bấm nút **Mở trong CAD** ở khung bên phải.
- Gõ `/open`.

Ứng dụng xuất STEP rồi gửi cho SolidWorks (tự tìm trong Program Files). Nếu SolidWorks hỏi tuỳ chọn nhập STEP thì bạn xác nhận. Đổi chương trình xem ở **Cài đặt**: Tự động, SolidWorks, chương trình mặc định cho `.step`, hoặc lệnh tự đặt.

## Cài hàng loạt (cấu hình sẵn)

Để người dùng khỏi phải nhập cấu hình, mở PowerShell trong thư mục đã giải nén:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 `
  -Url http://192.168.1.40:18080/v1/chat/completions `
  -Model Qwen3.8-27B-UD-Q4_K_M.gguf `
  -KeyFile C:\duong\dan\key.txt `
  -Planner nothink
```

| Tham số | Ý nghĩa |
|---|---|
| `-Url`, `-Model`, `-Planner` | cấu hình Qwen và cố vấn mặc định |
| `-KeyFile` | file chứa khoá API; được đọc rồi lưu riêng cho từng người dùng, không nằm trên dòng lệnh |
| `-AdvisorCli claude` hoặc `codex` | thêm sẵn cố vấn CLI (id là `claude-cli` hoặc `codex-cli`) |
| `-Prefix` | cài vào thư mục khác (mặc định `%LOCALAPPDATA%\CadFabric`) |
| `-NoShortcuts`, `-NoDesktopShortcut` | không tạo shortcut |

## Nâng cấp và gỡ

- **Nâng cấp:** chạy lại `install.cmd` của bản mới. Trình cài tự đóng ứng dụng đang chạy và giữ nguyên cài đặt người dùng.
- **Gỡ:** `powershell -ExecutionPolicy Bypass -File %LOCALAPPDATA%\CadFabric\uninstall.ps1`. Thêm `-Purge` để xoá cả cài đặt và file STEP.
- **Tắt ứng dụng:** nút **Thoát** ở góc trên bên phải.

## Dòng lệnh

```
%LOCALAPPDATA%\CadFabric\cadfabric.cmd doctor          kiểm tra CadQuery, sandbox, kết nối Qwen
%LOCALAPPDATA%\CadFabric\cadfabric.cmd run "mô tả"     dựng chi tiết, không cần giao diện
%LOCALAPPDATA%\CadFabric\cadfabric.cmd app             mở ứng dụng
```

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| Trình duyệt không tự mở | Bấm lại shortcut **CadFabric** (nó chỉ mở lại trang, không chạy bản thứ hai). Hoặc xem địa chỉ có token trong `%LOCALAPPDATA%\cadfabric\app.log` rồi dán vào trình duyệt. |
| "Kiểm tra kết nối" báo hết giờ | Máy chủ Qwen đang xếp hàng với người khác. Thử lại sau; yêu cầu thật hiển thị giai đoạn và số giây chờ. |
| "Kiểm tra kết nối" báo không tới được | Máy chưa nối được mạng LAN tới máy chủ, hoặc URL sai. Kiểm tra URL và tường lửa. |
| Cố vấn CLI báo `exit 1` | CLI chưa đăng nhập hoặc tên model sai. Chạy `claude` hoặc `codex` một lần trong Terminal để đăng nhập; lý do lỗi được hiển thị cạnh nút Kiểm tra. |
| Nút Mở trong CAD báo không có chương trình gán cho `.step` | Máy Windows mới thường chưa gán. Chọn **SolidWorks** hoặc lệnh tự đặt ở **Cài đặt**. |
| Cài báo thiếu `msvcp140.dll` | Chạy `vc_redist.x64.exe` có trong thư mục bộ cài (Windows hỏi quyền một lần), rồi cài lại. |
| Nghi ngờ cài chưa đúng | Chạy `%LOCALAPPDATA%\CadFabric\cadfabric.cmd doctor`: mỗi bước tự kiểm tra hiện OK hoặc FAIL kèm lý do. |

## An toàn

- Ứng dụng chỉ lắng nghe `127.0.0.1` (máy này) và mỗi lần chạy có token riêng.
- Mã CadQuery chạy trong tiến trình riêng có sandbox (chặn import ngoài danh sách, chặn file và tiến trình), có giới hạn bộ nhớ và thời gian.
- Cố vấn chỉ nhận văn bản, không có công cụ và không đọc được file của bạn.
- Cố vấn gửi ra ngoài luôn hỏi xác nhận trước khi dùng.
