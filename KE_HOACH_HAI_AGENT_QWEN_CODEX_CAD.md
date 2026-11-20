# Kế hoạch: hai agent Qwen3.8-27B (thực thi) + Codex CLI (cố vấn) cho thiết kế CAD bảo mật

Ngày lập: 2026-11-16. Máy đích: Windows `DESKTOP-L3FNFTO` (192.168.1.33, có SolidWorks 2026, Blender, KiCad).
Gắn nhãn: **[ĐO]** = đã đo/chạy thật; **[THIẾT KẾ]** = chưa chạy; **[CHƯA]** = chưa làm.

## 1. Mục tiêu
1. Qwen3.8-27B chạy local là agent **duy nhất chạm vào dữ liệu thật**, tự làm việc nhiều giờ, sáng ra có báo cáo.
2. Codex (cloud) chỉ **tư vấn** ngắn và thưa, nhằm giữ chất lượng ngang Codex làm một mình mà **không thấy dữ liệu thiết kế**.
3. Giảm token Codex: vòng lặp dài chạy ở Qwen (miễn phí token ngoài).

## 2. Căn cứ đo được từ nhật ký Codex trên .33 [ĐO]
| Phiên | Nội dung | Tổng token | Chủ động |
|---|---|---|---|
| 04/11 | Bảng nhân sự (Sheets) | 35,2 triệu | 81 phút |
| 04–07/11 | Robot CARRIE (Blender, SolidWorks, MuJoCo, BOM) | 54,9 triệu | 216 phút |
| 07/11 | AGV mỏng SYS-05 (SolidWorks COM, MuJoCo) | 86,3 triệu | 91 phút |
| 09/11 | Module 03-EON (SolidWorks MCP) | 4,3 triệu | 10 phút |

- Khoảng 99% token là **đầu vào lặp lại** (cache 96–99%); đầu ra chỉ 18 nghìn–381 nghìn.
- Kết quả lệnh `exec` rất lớn (1,8–20,7 triệu ký tự mỗi phiên). Chi phí đến từ vòng lặp dài, không đến từ suy luận.
- Lỗi chất lượng hiện qua lời bạn sửa: thiếu chi tiết cơ khí, không dùng file CAD gốc, rút gọn hình học quá mức.
- Codex hiện nạp MCP `blender` + `solidworks` + plugin computer-use/browser: **mọi kết quả công cụ và ảnh màn hình đi lên đám mây**. Dự án đã qua Codex coi như đã lộ.

## 3. Hai agent và ranh giới
### Agent A: Qwen3.8-27B, "thợ" (local)
- Chạy trên máy .33 qua gateway AICF (tenant `cad-fiosv-33`, 1 yêu cầu cùng lúc, ngữ cảnh 65536, ~24 tok/s [ĐO]).
- Công cụ: 42/88 công cụ `solidworks-mcp` (COM), Blender qua addon, đọc/ghi **chỉ trong thư mục job**.
- Làm theo `job.json`: chia subtask, mỗi subtask có **điều kiện nghiệm thu chạy máy** (kích thước, thể tích, cây feature, rebuild, interference...).
- Không có internet tới nhà cung cấp AI. Cánh cửa ra ngoài duy nhất là công cụ `ask_advisor`.

### Agent B: Codex CLI, "cố vấn" (cloud, cách ly)
- Chỉ nhận **văn bản đã lọc** từ `ask_advisor`; chỉ trả **văn bản**. Không MCP, không plugin, không đọc file, không thấy ảnh.
- Lệnh gọi (cờ đã xác nhận trong `codex exec --help` trên .33, bản 0.169.0-alpha): `codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check -s read-only -C <thư mục rỗng> -o <file>`.
- **Quan trọng [ĐO từ help, THIẾT KẾ cho biện pháp]:** `-s read-only` chỉ chặn *ghi*, **không chặn đọc**. Chạy trong tài khoản của bạn thì Codex vẫn đọc được C:\MCP3D... Vì vậy bắt buộc cách ly bằng một trong hai:
  1. **Backend API** (không có công cụ nào để lạm dụng). Cần khóa API. Khuyến nghị.
  2. **Tài khoản Windows riêng `aicf-advisor`**, không có quyền vào C:\MCP3D, C:\Drfiaidsrm, hồ sơ của FiosSV; có `CODEX_HOME` riêng và đăng nhập riêng; gọi bằng `runas`/tác vụ theo lịch. Backend `codex_cli` trong `advisor.py` **từ chối** chạy khi chưa khai báo `contained_user`.

### Giao thức giữa hai agent
`ask_advisor{kind ∈ api_usage|error_help|strategy|review_abstract, api_names[], error_class, question}`. Chỉ ba thời điểm được gọi:
1. **Đầu nhiệm vụ**: Codex nhận yêu cầu đã trừu tượng hóa, trả kế hoạch phân rã + **bộ kiểm tra nghiệm thu dạng mã chạy được**.
2. **Khi kẹt** sau N lần sửa thất bại: gửi lớp lỗi + tên API.
3. **Rà soát cuối**: chỉ chỉ số đã ẩn danh (đạt/không đạt, mã lỗi), không gửi hình học hay ảnh.
Lời đáp của Codex là **không tin cậy**: mã Python trong đó phải qua `check_code()` (allowlist import, cấm `os/subprocess/open/exec/...`) trước khi Qwen dùng; điều kiện nghiệm thu từ Codex phải viết bằng DSL biểu thức an toàn đã có.

## 4. Phân loại dữ liệu
| Mức | Ví dụ | Quy tắc |
|---|---|---|
| L2 bí mật | hình học, kích thước, BOM, tên khách/dự án/linh kiện, đường dẫn, ảnh, STEP/STL | Không bao giờ ra ngoài |
| L1 trừu tượng | lớp lỗi, tên API, tham số đã ẩn | Qua sanitizer, ghi log |
| L0 chung | cú pháp API, thuật toán | Được gửi |
Mỗi dự án khai báo `denylist` (tên dự án, khách hàng, mã linh kiện) và `pseudonyms` trong job. Chế độ: `off` (mặc định) | `dryrun` | `api` | `codex_cli`.

## 5. Vận hành qua đêm
Điều kiện cần (tất cả phải đúng trước khi bạn đi ngủ):
- SolidWorks **đang mở và phản hồi** (runner không tự mở được: đã treo ở màn hình khởi động, bạn phải mở tay [ĐO]).
- Phiên RDP của FiosSV **còn tồn tại** (ngắt kết nối được, **đăng xuất thì không**); máy không sleep; Gateway và engine Qwen đang chạy.
- Job có `budget` (phút, bước, số lần thử) và bộ kiểm tra thật.
Runner đã có [ĐO]: giới hạn thời gian/bước, phát hiện lặp vô hạn, dừng khi SolidWorks không phản hồi 3 lần, lưu `state.json` để chạy tiếp, `REPORT.md`, `audit.jsonl`.
Còn thiếu [CHƯA]: watchdog khởi động lại engine/gateway, thông báo khi xong, chạy tự động theo lịch, dọn tài liệu mở giữa các subtask, kiểm tra theo **tên tài liệu** thay vì tài liệu đang kích hoạt.

## 6. Hiện trạng
| Hạng mục | Trạng thái |
|---|---|
| Qwen điều khiển SolidWorks (khối 40×20×10, 2 lần: 2,2 và 3,4 phút, qua 9 điều kiện) | **[ĐO]** |
| Qwen tự phục hồi lỗi lưu file | **[ĐO]** |
| Runner không báo thành công giả (đã sửa lỗi `COMPLETE` giả) | **[ĐO]** |
| Sanitizer: 27 ca rò rỉ ngụy trang + 4 ca phải chặn = 0 lọt; 8/9 quy tắc chứng minh cần thiết | **[ĐO]** |
| Hai lỗ rò thật tìm ra và sửa (che số trước danh sách cấm; mã giữ chỗ chứa số) | **[ĐO]** |
| Bộ rà soát Qwen local 6/6 mẫu (quá ít để tin) | **[ĐO, yếu]** |
| `ask_advisor` gắn vào runner, chế độ dryrun | **[ĐO]** |
| Backend API / Codex CLI gọi thật | **[CHƯA], chưa gửi gì ra ngoài** |
| Tài khoản cách ly cho Codex CLI | **[CHƯA]** |
| Công thức khai thác từ script cũ của Codex | **[CHƯA]** |
| So sánh 3 nhánh (Qwen / Qwen+cố vấn / Codex một mình) | **[CHƯA], chưa có số nào** |
| Job vừa tầm nhiều subtask, chạy qua đêm | **[CHƯA]** |

## 7. Lộ trình, mỗi bước có tiêu chí qua (không ước lượng thời gian)
1. **Sửa runner**: kiểm tra theo tên tài liệu; đóng tài liệu sau subtask; watchdog. *Qua khi*: test đơn vị + chạy lại t1-box xanh.
2. **Cách ly Codex**: chọn API (cần khóa) hoặc tạo `aicf-advisor`. *Qua khi*: từ tài khoản cách ly, đọc thử `C:\MCP3D` thất bại; một lệnh gọi thử với câu hỏi chung (không dữ liệu thật) trả lời được.
3. **Khai thác công thức** từ script cũ của Codex trên .33 thành thư viện hàm tham số hóa cho Qwen (không thêm rủi ro rò rỉ vì dữ liệu đó đã nằm trên đám mây). *Qua khi*: 3–5 hàm chạy lại được trên dữ liệu giả.
4. **Bài thử vừa tầm**: một đoạn của SYS-05 (đã có đáp án và số token Codex từ nhật ký), có điều kiện nghiệm thu thật.
5. **So sánh 3 nhánh**: tỉ lệ qua nghiệm thu, số vòng sửa, **token Codex**, số lần canary lọt. Mục tiêu "ngang Codex một mình" chỉ được tuyên bố nếu số đo cho thấy.
6. **Chạy qua đêm có giới hạn** (job ngắn) rồi xem báo cáo sáng; chỉ nâng lên nhiều giờ sau khi bước 4–5 đạt.

## 8. Rủi ro và giới hạn
- Bộ lọc văn bản **không chứng minh được không rò**: lỗ hổng còn lại gồm số viết bằng chữ, diễn đạt lại bí mật không có trong danh sách cấm, mã hóa lạ, và thông tin nằm ở *chuỗi nhiều câu hỏi* được phép.
- Qwen có thể yếu ở API SolidWorks mức khó; chưa có số đo.
- Dự án đã qua Codex trước đây không thể thu hồi.
- Dữ liệu có ràng buộc hợp đồng: đọc điều khoản nhà cung cấp.
- Máy .33 dùng mật khẩu `123` (rất yếu) và RDP mở trong LAN: nên đổi.

## 9. Quyết định cần bạn
1. Đường cố vấn: **khóa API** hay **tài khoản Windows cách ly** cho Codex CLI.
2. Cho phép đọc thiết kế SYS-05 trên .33 làm bài thử (chỉ đọc, không gửi ra ngoài).
3. Dọn các tác vụ theo lịch trên .33 (`AICF-CAD-t1`, `AICF-CAD-win`, `AICF-SW-launch`) và thư mục thử khi xong.

## 10. Mã nguồn
`ai-compute-fabric/tools/cad-agent/`: `sw_agent.py`, `job_runner.py`, `advisor.py`, `canary_test.py`, `jobs/t1-box.json` (commit `621f947`, chưa đẩy lên remote).
