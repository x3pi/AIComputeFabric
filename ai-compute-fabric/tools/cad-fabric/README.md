# cadfabric

Tác tử CAD không phụ thuộc phần mềm CAD cụ thể: một mô hình ngôn ngữ (chạy cục bộ qua gateway tương thích OpenAI) viết mã **CadQuery**, kernel đo chi tiết sau **mỗi** bước, và một **kế hoạch có công thức thể tích** (do code tính, không phải do mô hình tính) dùng để kiểm tra từng bước và kết quả cuối.

Chạy headless trên Linux, Windows, macOS. Không cần SolidWorks, không cần GPU ở máy chạy CAD.

## Cài đặt

Cần Python 3.10 trở lên (đã kiểm tra trên 3.12) và khoảng 2 GB đĩa cho CadQuery.

```bash
# Linux / macOS
./install.sh
# Windows (PowerShell)
.\install.ps1
```

Hai script tạo môi trường ảo riêng (`~/.local/share/cadfabric/venv` hoặc `%LOCALAPPDATA%\cadfabric\venv`), cài gói, ghi cấu hình khởi tạo và chạy `cadfabric doctor`. Hoặc thủ công:

```bash
python -m venv venv && venv/bin/pip install .
venv/bin/cadfabric init --url http://GATEWAY:18080/v1/chat/completions --model TEN_MODEL --key-file /duong/dan/key.txt
venv/bin/cadfabric doctor
```

## Dùng

```bash
cadfabric run "Tao khoi 40 x 30 x 20 mm, khoan lo duong kinh 6 mm xuyen theo chieu dai 40 mm"
cadfabric run --planner codex --step part.step "..."   # xuất STEP
cadfabric tasks                                        # 10 bài kiểm thử, đáp án do kernel tính
cadfabric bench --modes off,codex --reps 3 --tasks cup_side_hole,flange_tube
```

Kết quả `run` gồm: câu trả lời, số đo thật của kernel (thể tích, bbox, tâm khối lượng, số mặt/cạnh/đỉnh, hợp lệ), file STEP và nhật ký `run-*.json` trong thư mục dữ liệu.

## Cấu hình

File đầu tiên tồn tại trong: `$CADFABRIC_CONFIG`, `./cadfabric.json`, `~/.config/cadfabric/config.json` (Windows: `%APPDATA%\cadfabric\config.json`).

| Khóa | Ý nghĩa |
|---|---|
| `url`, `model` | gateway chat-completions và tên mô hình |
| `key_file` | đường dẫn file chứa API key (hoặc biến `CADFABRIC_KEY`). **Khóa không bao giờ được ghi vào cấu hình** |
| `planner` | `off`, `nothink`, `think`, `codex`, `codex-relay` |
| `relay` | `host` (user@máy), `workdir` (thư mục RỖNG trên máy đó), `codex` (đường dẫn codex) |
| `codex_bin`, `data_dir` | tùy chọn |

Biến môi trường ghi đè: `CADFABRIC_URL`, `_MODEL`, `_KEY`, `_KEY_FILE`, `_PLANNER`, `_DATA_DIR`, `_CODEX`, `_PYTHON`, `_MEM_GB`, `_MAX_STEPS`.

### Chọn planner

Planner viết kế hoạch (các bước + công thức thể tích). Chất lượng kế hoạch quyết định kết quả vì verifier chỉ đúng bằng kế hoạch nó nhận.

| Planner | Ưu | Nhược |
|---|---|---|
| `off` | nhanh nhất, không rời máy | không có kiểm tra theo kế hoạch |
| `nothink` | nhanh, dùng cùng gateway | kế hoạch của mô hình nhỏ có thể hiểu sai đề (đã đo: bài cốc 0/5) |
| `think` | kế hoạch kỹ hơn | chậm; trên prompt khó có thể vượt giới hạn yêu cầu của gateway (lỗi 502) |
| `codex` | kế hoạch tốt nhất đã đo (bài cốc 5/5) | cần Codex CLI đã đăng nhập; **nội dung yêu cầu rời khỏi máy** |
| `codex-relay` | như `codex`, chạy trên máy khác qua ssh | cần ssh vào máy đó (khóa ssh, hoặc `SSHPASS` + `sshpass`) |

`codex` và `codex-relay` chạy Codex ở chế độ `read-only` trong thư mục rỗng, chỉ nhận văn bản. Đây là biện pháp giảm rủi ro, **không phải sandbox cứng**: mô hình vẫn có thể thử đọc file theo đường dẫn. Không dùng với yêu cầu chứa dữ liệu mật.

## An toàn

Mã CadQuery của mô hình chạy trong tiến trình con riêng: chỉ cho import `cadquery`, `math`...; chặn `open`, `eval`, `exec`, `os`, `sys`, truy cập `__dunder__`; giới hạn RAM; có timeout. Đây là lớp chặn các đường thoát hiển nhiên, **không phải ranh giới bảo mật** đối với kẻ tấn công cố ý. Nếu mô hình không đáng tin, chạy cả `cadfabric` trong container.

## Kiến trúc

```
cadfabric run ──> agent.run ──> planners.make_plan (công thức)   ──> verifier.parse_spec (code tính số)
                     │
                     └─ vòng lặp: LLM ──execute_cadquery(code)──> Backend.execute ──> Measure
                                         └─ verifier.Tracker.observe(đo thật vs mốc kế hoạch) ─> "HARNESS CHECK"
                                  hết việc ──> Tracker.final_verdict ──> (lệch? quay lại tối đa 2 lần)
```

`backend.py` là giao diện (`execute`, `measure`, `undo`, `export_step`); hiện có backend CadQuery. Backend khác (ví dụ SolidWorks qua MCP) chỉ cần cài đặt cùng giao diện.

## Phát triển

```bash
pip install -e .
python -m unittest discover -s tests
```

Thêm bài benchmark: sửa `src/cadfabric/tasks.py` (văn bản đề + lời giải tham chiếu; đáp án do kernel tính, thêm công thức giải tích nếu có để test đối chiếu).
