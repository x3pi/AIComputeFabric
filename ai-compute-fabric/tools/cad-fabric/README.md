# cadfabric

Tác tử CAD không phụ thuộc phần mềm CAD cụ thể: một mô hình ngôn ngữ (chạy cục bộ qua gateway tương thích OpenAI) viết mã **CadQuery**, kernel đo chi tiết sau **mỗi** bước, và một **kế hoạch có công thức thể tích** (do code tính, không phải do mô hình tính) dùng để kiểm tra từng bước và kết quả cuối.

Chạy headless trên Linux, Windows, macOS. Không cần SolidWorks, không cần GPU ở máy chạy CAD.

## Chiến thuật dựng cảnh 3D (0.8.x, thay vòng lặp CadQuery cũ)

Từ 0.8.0 mặc định cadfabric dựng **cảnh nhiều chi tiết, có chuyển động, trong Blender** (cần Blender 4.x; tìm theo biến `BLENDER`, khoá `"blender"` trong cấu hình, `PATH` rồi các chỗ cài thông dụng). Vòng lặp CadQuery từng bước cũ **không còn là đường chạy mặc định**; mã vòng lặp cũ đã được xoá khỏi 0.9.9 (chỉ còn phần xem/xuất STEP dùng CadQuery). Đầu ra là `<tên>.blend` và thư mục `<tên>.scene/` (`plan.json`, `parts/*.py`) trong thư mục dự án; xuất STEP chính xác chưa có trong chiến thuật này.

Phân vai (mục tiêu: dùng model mạnh ít nhất có thể mà không giảm chất lượng):

| Vai | Ai | Làm gì |
|---|---|---|
| Cố vấn (director) | cố vấn chọn trên giao diện (Claude, Codex, hoặc Qwen) | lập kế hoạch **một lần** (chi tiết, khớp, vùng tiếp xúc cho phép), chỉ được gọi lại khi worker bế tắc, và duyệt **một lần** cuối đối chiếu với lời người dùng. Không thấy mã hay ảnh trong đường thường |
| Worker | Qwen cục bộ | viết từng chi tiết bằng `bpy/bmesh`, tự sửa lỗi; chi tiết cùng loại có thể viết chung một yêu cầu (`group`) |
| Cổng kiểm | mã, không dùng model | schema kế hoạch; `ast`/regex kiểm mã worker; dựng trong Blender; bộ kiểm độc lập (chuyển động cứng quanh trục đã khai, chi tiết quay đồng trục, va chạm giữa chi tiết không kề nhau, tỉ lệ đối tượng, chi tiết sai đơn vị) |

**Kỹ năng (skill)**: kỹ năng đã cài **và được bạn duyệt** đều dùng được ở chế độ này. Cố vấn thấy danh mục (tên + mô tả) và có thể chọn tối đa 3 kỹ năng đưa cho Qwen (`"skills"` trong kế hoạch, nội dung giới hạn 14000 ký tự để không tràn ngữ cảnh); gõ `$tên-kỹ-năng` để ép dùng cho cả cố vấn lẫn Qwen. Có sẵn kỹ năng mẫu `bpy-bmesh-recipes` (vành/lốp, bo cạnh, vỏ loft, nan hoa, nghiêng; đã chạy thử trong Blender 4.2): nhập từ thư mục `skills` rồi duyệt.

**Ảnh**: kéo thả tối đa 3 ảnh (png/jpg/gif/webp, mỗi ảnh dưới 1,4 MB) vào chat để cố vấn nhìn (chỉ cố vấn **Claude Code**; Qwen không nhận ảnh nên cố vấn phải ghi điều nhìn thấy vào mô tả từng chi tiết). Ở lượt duyệt cuối, cố vấn Claude còn nhận 3–4 ảnh dựng thử (iso, ngang, trên, và iso ở cuối chuyển động; mỗi chi tiết một màu) để bắt lỗi chi tiết thiếu/lơ lửng/sai tỉ lệ. Trên Windows, ảnh được lưu ra thư mục tạm cho cố vấn đọc bằng công cụ Read vì CLI báo lỗi với ảnh base64.

Cùng một lỗi hai lần liên tiếp hoặc 4 lần thử thì chi tiết đó **kẹt** và được báo lên cố vấn (sửa bằng ghi chú cho worker, hoặc tự viết mã chi tiết). Lỗi của cặp chi tiết (va chạm) hay của khớp không bao giờ đưa cho worker. Nhắn tiếp ("làm bánh to hơn") sẽ nạp lại kế hoạch đã lưu và chỉ viết lại chi tiết bị đổi. Mỗi lượt báo chi phí thật: Claude CLI trả token và USD; cố vấn khác chỉ ước lượng.

Đã đo (2 đề do chính chúng tôi viết: 12 chi tiết/2 khớp, 15 chi tiết/1 khớp; mỗi đề 2 lần, chấm bằng `oracle_check.py` theo chữ của đề): tỷ lệ đạt và chi phí xem `docs/PLAN-assembly-spec-hybrid-animation.md` (mục 1k) trong repo `poly-agent`. Chưa đo trên bộ đề đa miền, và chưa so với lối CadQuery cũ ở đề đơn chi tiết (lối cũ đã đo 68% trên Holdout đơn chi tiết). Nếu bạn chủ yếu cần một chi tiết CAD chính xác xuất STEP, hãy dùng `CADFABRIC_LEGACY_AGENT=1`.

## Ứng dụng chat (khuyên dùng)

`cadfabric app` mở một ứng dụng chat trong trình duyệt (giao diện kiểu trợ lý lập trình): gõ mô tả bằng tiếng Việt, xem kế hoạch kiểm tra, từng bước code CadQuery (bấm để mở), thể tích đo thật sau mỗi bước, bản xem trước 3D, bảng số đo và nút tải STEP. Tin nhắn tiếp theo chỉnh sửa chính chi tiết đó; `/new` bắt đầu chi tiết mới, `/stop` dừng.

Lần đầu chạy, hộp **Cài đặt** tự mở:

| Mục | Ý nghĩa |
|---|---|
| URL API Qwen | endpoint tương thích OpenAI (ví dụ `http://192.168.1.40:18080/v1/chat/completions`) |
| Tên model | để trống nếu máy chủ tự chọn; nút **Kiểm tra kết nối** liệt kê model có sẵn |
| Khoá API | lưu vào file riêng `key.txt` trong thư mục cấu hình của người dùng (quyền 0600), **không bao giờ** vào file JSON và không bao giờ gửi ngược lại trình duyệt |
| Cố vấn | có sẵn `off`, `nothink`, `think`, `codex`, `codex-relay` (xem bảng bên dưới) và các cố vấn bạn tự thêm. Cố vấn gửi nội dung ra ngoài luôn hỏi xác nhận |

### Thêm cố vấn của riêng bạn (URL hoặc CLI)

Trong **Cài đặt → + Thêm cố vấn** có hai loại, đều chỉ nhận văn bản (cố vấn không có công cụ, không đọc được file):

| Loại | Điền | Ghi chú |
|---|---|---|
| **API (URL)** | kiểu `OpenAI-compatible` hoặc `Anthropic`, URL, model, khoá | khoá lưu file riêng `key-<id>.txt` (0600), không vào JSON; URL ở mạng LAN thì mặc định không tính là "gửi ra ngoài" |
| **CLI** | chọn `Claude Code`, `Codex` (ứng dụng báo đã thấy chương trình ở đâu) hoặc lệnh tự đặt; với Claude/Codex chọn thêm **model** (yếu/nhanh như `haiku`, hay mạnh như `opus`) và **mức suy nghĩ** (`--effort` của Claude: low…max; `model_reasoning_effort` của Codex: low/medium/high) | chạy không qua shell, trong thư mục tạm rỗng, lời nhắc qua stdin (hoặc tham số `{prompt}`) |

Với cố vấn API, ô Model chọn model của dịch vụ đó. Đã đo thật với Claude CLI: `haiku` 3,6 s, `sonnet` 2,6 s, `opus` (effort low) 3,8 s, model sai cho lỗi đọc được.

Mỗi cố vấn có nút **Kiểm tra** (hỏi một câu vô hại và báo kết quả + số giây), cờ "gửi ra ngoài" (hỏi xác nhận mỗi lần dùng) và nút Sửa/Xoá. Xoá một cố vấn cũng xoá file khoá của nó. Nếu cố vấn lỗi giữa chừng, ứng dụng rơi về `nothink` (chạy ở máy chủ của bạn) và **ghi rõ điều đó** trong thẻ kế hoạch. Từ dòng lệnh: `cadfabric configure --advisor-cli claude`, `cadfabric run --planner claude-cli "..."`; cài hàng loạt: `install.ps1 -AdvisorCli claude -Planner claude-cli`.

Cố vấn được chọn ngay trên ô nhập từng lần gửi. Ứng dụng chỉ lắng nghe `127.0.0.1`, mỗi lần chạy có token riêng; chỉ máy chạy ứng dụng mới sửa được cài đặt.

### Xem tệp trong dự án

Tab **Tệp** ở panel phải liệt kê thư mục dự án; bấm một tệp để mở trong tab cạnh khung chat (mở nhiều tab, cuộn ngang, chuột phải vào tab để đóng). Cách hiển thị: mã có tô màu và số dòng (highlight.js), Markdown (marked + DOMPurify), CSV/TSV dạng bảng, ảnh, và STEP dạng **3D xoay được** (máy chủ chia lưới bằng CadQuery, trình duyệt vẽ bằng three.js), hoặc hình nét 2D. **Chuột phải** vào tệp: mở trong tab, đính kèm vào chat, mở trong chương trình CAD, mở bằng chương trình mặc định (chỉ loại tệp không chạy mã), hiện trong thư mục, sao chép đường dẫn. Thư viện giao diện nằm trong gói (`src/cadfabric/static`, giấy phép trong `static/licenses`), không tải gì từ Internet.

### Làm việc nhiều bước (sổ làm việc, nén ngữ cảnh, chống kẹt)

Để một bài dài chạy được hàng chục đến hàng trăm bước mà ngữ cảnh của Qwen (KV cache, nằm trong VRAM) không phình ra:
- **Sổ làm việc** (`ledger.py`): mọi bước (mã, kết quả, lỗi, số đo) được ghi ra ngoài lời nhắc, trong RAM và trên đĩa (`<thư mục dữ liệu>/ledger/<phiên>.jsonl`, chỉ người dùng đọc được, xoá cùng lịch sử). Qwen đọc lại một bước cũ bằng công cụ `recall(số bước | 'attachments' | từ khoá)`.
- **Nén ngữ cảnh bằng code**: khi tin nhắn vượt khoảng 30.000 ký tự (đổi bằng `CADFABRIC_COMPACT_CHARS`), phần giữa được thay bằng một **thẻ trạng thái** do chương trình viết từ số liệu đo (số bước, số đo hiện tại, các bước thành công, các ngõ cụt kèm số lần), giữ lại yêu cầu gốc và 6 lượt gần nhất. Không nhờ Qwen tóm tắt, nên một tóm tắt sai không thể ghi vào bộ nhớ. Tệp đính kèm rời khỏi lời nhắc và đọc lại bằng `recall('attachments')`.
- **Chống kẹt**: cùng một đoạn mã 3 lần liền, thể tích không đổi 5 cell liền, 5 cell lỗi liền, hoặc cùng một lỗi 3 lần (dù mã khác) thì Qwen được nhắc một lần; tới 6, 9 hoặc 10 thì lượt chạy kết thúc với lý do rõ ràng và nút **Tiếp tục** thay vì đốt hết số bước.
- **Số bước linh hoạt**: ít nhất 14 bước, cộng 5 bước cho mỗi mốc trong kế hoạch của cố vấn (tối đa 120, `CADFABRIC_HARD_STEPS`); tin nhắn sửa tiếp có ít nhất 20 bước. Hết số bước thì bấm **Tiếp tục** để chạy thêm từ đúng chỗ đang dở.

### Khi nào lượt vẽ dừng

Một lượt dừng khi: (1) Qwen trả lời xong và qua các lớp kiểm tra (mốc kế hoạch, khối hợp lệ, không phải ngay sau một bước lỗi, duyệt cuối của cố vấn tối đa 2 lần; nếu lần 2 vẫn có vấn đề thì câu trả lời được giữ kèm danh sách vấn đề còn mở); (2) bị kẹt (cùng code 6 lần, thể tích không đổi 9 bước, 10 bước lỗi liên tiếp; khi thể tích không đổi, cố vấn được nhờ kết luận trước khi dừng); (3) bạn bấm Dừng; (4) hết ngân sách bước, vốn tự tăng thêm 10 bước mỗi lần khi chi tiết còn thay đổi, tối đa 200. Cùng một đoạn code xuất hiện 2 lần trong 20 bước gần nhất thì lần thứ 3 không được chạy. Phép đo có dòng `connectivity:` khi có nhiều khối, nêu khối nào lơ lửng.

### Logo và icon

Bộ logo và icon vector nằm trong `assets/`: biểu tượng là một khối lập phương đẳng cự có lỗ khoan trên mặt trên (một thao tác CAD), màu cam đất cùng tông với giao diện. Có `cadfabric-icon.svg` (ô vuông bo góc), `cadfabric-icon-small.svg` (bản đơn giản cho 16 đến 32 px), `cadfabric-mark.svg` (chỉ khối), `cadfabric-logo.svg` và `cadfabric-logo-dark.svg` (ngang, chữ đã chuyển thành đường nét nên không cần cài phông), cùng `png/` và `cadfabric.ico` (7 kích thước). Tất cả sinh từ một hình học bằng `python assets/build_assets.py` (cần fonttools, cairosvg, Pillow, phông Inter theo giấy phép OFL). Giao diện dùng khối này làm favicon và logo tiêu đề (đổi màu theo chế độ sáng và tối); trình cài Windows gán `cadfabric.ico` cho shortcut, trình cài Linux dùng bản SVG cho launcher.

### Hỏi cố vấn khi kẹt

Khi bạn chọn Codex, Claude hoặc một cố vấn CLI/API làm cố vấn, và Qwen bị kẹt (lặp mã, thể tích không đổi, lỗi liên tục) hoặc chi tiết cứ lệch kế hoạch, app tự gửi cho cố vấn một bản tóm tắt: đề bài, kế hoạch, 4 ô mã gần nhất kèm kết quả và số đo hiện tại. Cố vấn chẩn đoán nguyên nhân gốc (hiểu sai đề, cắt nhầm trục, kế hoạch sai) và nêu bước tiếp theo bằng văn bản. Lời khuyên hiện thành thẻ "Cố vấn xem lại" và được đưa cho Qwen như gợi ý để tự kiểm lại trên chi tiết thật. Tối đa 2 lần mỗi lượt, cách nhau ít nhất 3 bước. Không chọn cố vấn thì không gửi gì ra ngoài. Cố vấn bên ngoài nhận thêm mã và số đo của lượt đó, nên vẫn nằm trong xác nhận "gửi ra ngoài" đã hỏi khi bạn chọn cố vấn.

### Hỏi "có skill gì?"

Hỏi trong chat "bạn có skill gì?" hoặc "liệt kê skill": CadFabric tự trả lời từ kho skill và bài học (skill dùng được, skill chỉ gọi bằng `$tên`, skill chờ duyệt, số bài học), không qua Qwen nên không bịa. Hỏi "**cố vấn** có skill gì?", "Codex có skill gì?" hoặc "Claude có skill gì?": cố vấn **đang chọn** ở ô Cố vấn được hỏi bằng một câu cố định (không gửi gì của dự án) và liệt kê skill của chính nó (Codex liệt kê skill của Codex, Claude liệt kê skill của Claude); với Claude CLI, app chỉ bật công cụ `Skill` cho câu hỏi này. Nếu nêu tên một cố vấn khác với cố vấn đang chọn thì không gọi, và câu trả lời bảo chọn cố vấn đó trước (tránh gửi dữ liệu tới dịch vụ chưa xác nhận). Cố vấn API không có skill; Qwen làm cố vấn thì chưa có cố vấn bên ngoài để hỏi.

### Skill từ GitHub

Tab **Skill** cài các skill định dạng [Agent Skills](https://agentskills.io) (thư mục có `SKILL.md`) từ link GitHub (`github.com/chủ/repo` hoặc `.../tree/nhánh/thư-mục`) hoặc từ thư mục trên máy. Skill chỉ là **tài liệu hướng dẫn**: chỉ phần chỉ dẫn được dùng, `scripts/` **không bao giờ chạy**, `allowed-tools` bị bỏ qua. Skill mới ở trạng thái chờ; app hiện cảnh báo (có scripts, URL, lệnh shell, nhắc khoá bí mật) và bạn bấm **Đọc** rồi **Duyệt**. Qwen chỉ thấy tên và mô tả một dòng của skill đã duyệt, khi thấy hợp mới gọi công cụ `use_skill` để đọc nội dung (và các tệp tham khảo kèm theo), nội dung được gói là "dữ liệu tham khảo" để không ghi đè yêu cầu của bạn. Cách dùng theo kiểu Codex: **chỉ định** bằng `$tên-skill` trong chat (gõ `$` để chọn từ danh sách; skill chưa duyệt bị từ chối kèm thông báo), hoặc để Qwen **tự chọn** theo mô tả (tắt riêng từng skill bằng nút "Tự động", hoặc skill có `agents/openai.yaml` với `allow_implicit_invocation: false`). Danh sách trong lời nhắc giới hạn 8000 ký tự, mô tả được rút ngắn dần khi có nhiều skill. App tự tìm skill trong `.agents/skills` và `.codex/skills` của dự án và của thư mục người dùng, đưa vào ở trạng thái chờ duyệt. `/skills` mở tab Skill. Tải về giới hạn 25 MB, chỉ từ codeload.github.com, chặn đường dẫn thoát thư mục và liên kết tượng trưng. Lưu trong `skills/` ở thư mục dữ liệu.

### Bài học (rút kinh nghiệm có duyệt)

Sau cuộc trò chuyện có lỗi, cảnh báo của chương trình kiểm tra hoặc lưu ý của cố vấn, app nhờ Qwen rút ra **bài học chung** (tab **Bài học** ở panel phải; nút "Rút bài học từ cuộc này" để rút thủ công). Bài học là quy tắc về phương pháp (cách dựng, cách dùng CadQuery, cách kiểm tra, cách hiểu đề mơ hồ), **không** chứa kích thước, tên chi tiết hay số liệu của một bài cụ thể: lời nhắc yêu cầu như vậy, và một bộ lọc trong code từ chối mọi bài học có số đo, đơn vị, kích thước, phép gán mã, đường dẫn hay URL, hoặc trùng ý bài học đã có (kể cả bài đã bị bỏ). Bài học mới ở trạng thái **chờ duyệt**; chỉ bài **đã duyệt** mới được đưa vào lời nhắc hệ thống của Qwen, và chỉ ở cuộc trò chuyện mới. Dùng linh hoạt như skill: mỗi bài có tên ngắn, gõ `#tên` trong chat để chỉ định (gõ `#` để chọn từ danh sách), nút **Tự động bật/tắt** cho từng bài (bài tắt chỉ dùng khi gõ `#`), lượt sửa tiếp cũng được nạp lại bài hợp với yêu cầu mới (không lặp bài đã đưa), khi có hơn 20 bài đã duyệt thì lời nhắc chỉ mang danh sách tên ngắn và Qwen đọc bài cần bằng công cụ `use_lesson`, và nút **Gộp thành skill** xuất các bài đã duyệt thành một `SKILL.md` (chờ duyệt) dùng được với Codex/Claude. Có thể sửa (sửa xong phải duyệt lại), tạm dừng, xoá, và đặt phạm vi "chỉ dự án này" hoặc "mọi dự án". Lưu trong `lessons.json` ở thư mục dữ liệu (chỉ người dùng đọc được). Tắt tự đề xuất trong Cài đặt.

## Cài đặt

### Ubuntu 22.04 / 24.04 (x86_64, offline)

Dùng Python 3 của hệ thống (3.10 trở lên) với venv riêng trong `~/.local/share/cadfabric-app`, không cần sudo. Tạo bộ cài (cần mạng một lần để tải wheel):

```bash
./packaging/build_linux_bundle.sh          # -> dist/cadfabric-linux-<phiên bản>.tar.gz (khoảng 760 MB, wheel cho Python 3.10 và 3.12)
```

Trên máy Ubuntu: giải nén, chạy `bash install.sh` (tuỳ chọn `--url --model --planner --key-file --advisor-cli`), mở **CadFabric** từ menu ứng dụng hoặc `~/.local/bin/cadfabric app`. Gỡ bằng `uninstall.sh` (`--purge` xoá cả dữ liệu). Cửa sổ riêng cần `gir1.2-webkit2-4.1` và `python3-gi`; thiếu thì tự mở bằng trình duyệt. Hướng dẫn đầy đủ: `docs/HUONG-DAN-CAI-DAT-UBUNTU.md`. Đã chạy thật trên Ubuntu 24.04 (Python 3.12): cài, `doctor` 6/6, dựng chi tiết bằng Qwen, mở cửa sổ (Xvfb), gỡ. Chưa thử Python 3.10 và Ubuntu 22.04.

### Windows (nhiều máy, offline, không cần quyền quản trị)

Bộ cài mang theo Python 3.12 riêng và toàn bộ thư viện, không đụng Python sẵn có. Tạo bộ cài trên Linux/macOS:

```bash
./packaging/build_windows_bundle.sh        # -> dist/cadfabric-windows-<phiên bản>.zip (khoảng 350 MB)
```

Trên mỗi máy Windows 10/11 64-bit: giải nén zip, bấm đúp `install.cmd`. Ứng dụng được cài vào `%LOCALAPPDATA%\CadFabric`, có shortcut **CadFabric** trên Start Menu và Desktop. Cài hàng loạt với cấu hình sẵn:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Url http://GATEWAY:18080/v1/chat/completions -Model TEN_MODEL -KeyFile \\server\share\key.txt -Planner nothink
```

Cài đè bản mới nâng cấp tại chỗ và giữ nguyên cài đặt người dùng (nhớ tăng `version` trong `pyproject.toml`, pip bỏ qua cùng số phiên bản). Gỡ: `%LOCALAPPDATA%\CadFabric\uninstall.ps1` (`-Purge` xoá cả cài đặt).

Đã kiểm chứng thật trên một máy Windows 10 sạch (PowerShell 5.1): cài mất khoảng 107 s, nâng cấp 16 s, `cadfabric doctor` OK, ứng dụng không console dựng chi tiết và thoát sạch.

### Linux / macOS

Cần Python 3.10 trở lên (đã kiểm tra trên 3.12) và khoảng 2 GB đĩa cho CadQuery.

```bash
./install.sh
```

Script tạo môi trường ảo riêng (`~/.local/share/cadfabric/venv`), cài gói, ghi cấu hình khởi tạo và chạy `cadfabric doctor`. Hoặc thủ công:

```bash
python -m venv venv && venv/bin/pip install .
venv/bin/cadfabric configure --url http://GATEWAY:18080/v1/chat/completions --model TEN_MODEL
venv/bin/cadfabric app
```

## Dùng

```bash
cadfabric run "Tao khoi 40 x 30 x 20 mm, khoan lo duong kinh 6 mm xuyen theo chieu dai 40 mm"
cadfabric run --planner codex "..."   # cố vấn Codex thay cho Claude
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

## Kết quả đo (đã đo, 3 lần mỗi ô, kernel CadQuery tự chấm thể tích)

5 bài khó (`bolt_circle_plate`, `rounded_block`, `open_box`, `keyway_shaft`, `flange_tube`), cổng Qwen3.8-27B chạy tuần tự:

| Cố vấn | Đạt | Ghi chú |
|---|---|---|
| `off` (Qwen tự làm) | 10/15 | `keyway_shaft` 0/3 |
| `codex` | 13/15 | `keyway_shaft` 3/3; mất 2 lần ở `flange_tube` |

`flange_tube` thất bại ở cả hai chế độ **vì đề bài mơ hồ**: "ống cao 40 mm" có thể là 40 trên mặt bích (tổng 45) hoặc 40 tổng cộng. Codex đã nêu đúng điều này trong mục lưu ý của kế hoạch; ứng dụng hiển thị các lưu ý đó trong khung màu vàng để người dùng kiểm tra đề bài trước khi tin kết quả. Bài `cup_side_hole`: `off` 0/5, `nothink` 0/5, `codex` 5/5; `think` bị cổng trả HTTP 502 ở bước lập kế hoạch (5/5 lần). Thời gian phụ thuộc tải của cổng Qwen dùng chung: cùng một yêu cầu đã đo từ 13 s đến hơn 200 s khi cổng bận.

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

## Lịch sử

Mỗi cuộc trò chuyện tự lưu vào `<data dir>/history/<id>.json` (0600, tối đa 100). Nút **Lịch sử** mở lại: chat, ngữ cảnh của model và chi tiết (dựng lại bằng chạy lại các cell đã lưu). Chỉ dùng được khi app gắn với máy này (không `--allow-remote`). Đã đo: mở lại sau khi khởi động lại app, sửa tiếp thêm lỗ Ø6, thể tích 28 994,69 → 28 429,20 mm³ (giảm đúng 565,49 = π·3²·20).

## Thư mục làm việc, tệp đính kèm, tra internet, cửa sổ riêng (0.5.0)

- `workspace` (Cài đặt): thư mục chỉ-đọc cho Qwen (`list_files`, `read_file`, `search_files`) và `save_step` (chỉ tạo tệp mới). Chặn `..`, đường dẫn tuyệt đối, symlink ra ngoài, tệp bí mật; kiểm thử đột biến cả ba luật.
- Đính kèm tệp (`files` trong `/api/send`, nút 📎): nội dung vào cả lời nhắc của Qwen và của cố vấn. Đo thật (đề: đĩa Ø60 dày 8, lỗ Ø12, đọc từ tệp): planner `off` đúng 1/3 lần, `nothink` 3/3, Claude (haiku) 3/3, thể tích đúng 21714,69 mm³.
- Cố vấn Claude/Codex có tuỳ chọn `web` (Claude: `--tools WebSearch,WebFetch`; Codex: `--search`). Đã đo Claude CLI tra được M8 theo bảng Optimas trong 18,6 s. Codex `--search` chưa chạy thử đến cùng (Codex trên máy Linux này báo model mặc định không hỗ trợ).
- `cadfabric app` mở cửa sổ riêng bằng pywebview (WebView2 trên Windows); `--browser` để dùng trình duyệt. Đã xem tận mắt cửa sổ trên Windows 10 (.33) và nút Thoát đóng sạch tiến trình. Linux/macOS: cần cài thêm `pip install cadfabric[desktop]` và GTK/Qt; chưa thử.

## 0.9: Blender nhiều chi tiết, skill theo chuẩn mở, số liệu đo

**Chiến lược (0.8–0.9).** Cố vấn mạnh lập kế hoạch một lần và duyệt một lần; Qwen viết từng chi tiết; các cổng cứng kiểm tra (ast, dựng Blender, bộ kiểm tra cảnh, cổng hình học tâm/kích thước, bộ kiểm tra chuyển động). Qwen có thể xuất công thức JSON (thư viện `bxlib`: box, cylinder, ring, disc, beam, sphere, loft, spokes) để chương trình dựng tất định. Sửa lỗi có cổng hồi quy (điểm tăng thì hoàn tác). Cảnh dở dang vẫn được lưu. Mỗi chi tiết có `color` (#RRGGBB) và `finish` (matte, gloss, metal, rubber, emissive). Công tắc: `BX_RECIPES`, `BX_GUARD`, `BX_COLORS`, `BX_PLANCACHE` (0 để tắt).

**Skill theo chuẩn Agent Skills** (agentskills.io; cùng định dạng với Claude Code và OpenAI Codex): thư mục `SKILL.md` có frontmatter `name`, `description` (tối đa 1024 ký tự) và tùy chọn `license`, `compatibility`, `metadata`, `disable-model-invocation`; `scripts/` không bao giờ được chạy; phải duyệt trước khi dùng.
- Một kho dùng chung. Phân vai bằng `metadata: {audience: director|worker|both}` trong frontmatter (mặc định `both`); đổi nhanh trong tab Skill ("Dành cho").
- Cố vấn thấy danh mục (tên và mô tả) của skill dành cho cố vấn và có thể chọn tối đa 3 skill cho Qwen. `$tên` gọi thẳng cho vai trò được phép. Qwen chỉ nhận skill dành cho Qwen; nếu dài hơn 14000 ký tự thì giữ các đoạn liên quan nhất tới chi tiết đang làm, theo thứ tự gốc, và nêu tiêu đề các đoạn bỏ.
- Tự tìm trong `.agents/skills`, `.codex/skills`, `.claude/skills` (dự án và thư mục home); luôn vào ở trạng thái chờ duyệt. Xuất skill đã duyệt sang `~/.agents/skills` hoặc `~/.claude/skills` bằng nút "Xuất sang Claude/Codex".
- Cảnh báo khi lệch chuẩn (tên khác thư mục, mô tả quá dài, SKILL.md quá 500 dòng).

**Đo A/B** (cùng máy, Qwen cục bộ, cố vấn Claude; N=2–3 mỗi ô, chỉ mang tính chỉ báo): bài xe moto 42 chi tiết có màu, bản 0.9 so với 0.8.1: oracle 2/2 cả hai; coverage 1.0 so với 0.965; chi phí cố vấn 0.145 so với 0.466 USD; thời gian 183 so với 1100 giây; điểm giám khảo ảnh (mô hình, nhiễu) 4.0 so với 3.8 trên 10, tức độ đẹp chưa khác biệt rõ.

**0.9.3 - chống lỗi hình học từ gốc, không mã hóa cứng theo từng lỗi.** (1) Cố vấn đưa hình học dạng công thức (`ops`, cùng định dạng `bxlib`) ngay trong kế hoạch cho chi tiết đơn giản: chương trình dựng tất định, không qua Qwen. (2) Mã của Qwen chạy qua lớp bảo vệ chung `bxguard`: sau MỌI lệnh `bmesh.ops` kiểm tra kích thước lưới (ngân sách `BX_PART_VERTS`, mặc định 150000, và hệ số tăng `BX_GROWTH`, mặc định 8), nên thao tác nào nổ ở vòng lặp nào cũng bị dừng đúng chỗ, không cần danh sách lệnh bị cấm; lỗi sai tham số được kèm chữ ký thật của lệnh lấy từ Blender. (3) Mỗi chi tiết mã thô được dựng thử riêng trong tiến trình Blender có giới hạn bộ nhớ (`BX_PART_GB`, mặc định 3; `BX_PROBE=0` để tắt) nên lỗi được gán đúng chi tiết; cả cảnh dựng với `BX_MEM_GB` (mặc định 10; chỉ Linux/macOS). Báo cáo cũ không còn bị nhầm là mới khi Blender sập.

**0.9.5 - ít đỉnh mà vẫn đẹp.** Một cơ chế chung cho mọi chi tiết, ai viết cũng vậy: (1) chia đoạn thích ứng trong `bxlib` theo sai số dây cung (`BX_TESS_MM`, mặc định 0,4 mm): chốt nhỏ 16 đoạn, lốp lớn khoảng 56; bo cạnh chỉ ở cạnh thật sự gấp khúc, không bo các đường nối của mặt cong; (2) tối ưu không mất hình: gộp đỉnh trùng, hòa tan đỉnh nằm trên mặt phẳng; (3) giảm đỉnh có chặn sai số: giảm tới mức bề mặt còn trong `BX_LOD_MM` (mặc định 0,5 mm) so với bản gốc theo cả hai chiều, chỉ với lưới trên `BX_LOD_MIN_VERTS` (300) đỉnh; (4) tô bóng mịn có giữ cạnh sắc ở góc lớn hơn `BX_SHARP_DEG` (35 độ) nên ít đỉnh vẫn ra bề mặt sạch. `BX_OPTIMIZE=0` để tắt. Đo trên bài H1 vàng: 3788 xuống 2748 đỉnh (-27%) và oracle vẫn đạt; một lốp mã thô 1920 xuống 161 đỉnh (-92%).

**0.9.6 - cổng không xuyên sàn.** Bộ kiểm tra chuyển động báo `ground_penetration` khi một chi tiết đang chạm sàn lúc nghỉ (z=0) bị chuyển động đẩy xuống dưới sàn hơn `BX_GROUND_TOL_MM` (mặc định 5 mm); lỗi này đi tới cố vấn để sửa khớp. Công tắc `BX_DIRECTOR_OPS=0` cho Qwen viết công thức thay vì cố vấn (cố vấn rẻ hơn, kém ổn định hơn). Đo hồi quy 0.9.5 (N=2 mỗi bài): H1 và M1 đều đạt oracle, độ phủ 0,95–1,0; chi phí cố vấn H1 0,31 USD (bản cũ 0,47), M1 0,24 USD (bản cũ 0,12), thời gian 125–155 giây.

**0.9.7.** Hai op công thức mới: `tube` (ống tròn gấp khúc, mối nối mượt) và `panel` (tấm vỏ cong mỏng có độ dày, thay cho khối loft đặc). Bước tối ưu phẳng nay cũng chỉ được chấp nhận khi bề mặt còn trong `BX_LOD_MM` theo cả hai chiều (trước đó nó làm hỏng tấm mỏng).

**0.9.8 - cơ chế chung, không gắn với một loại vật thể.** (1) Kiểm tra chi tiết lơ lửng (`floating_part`): mỗi chi tiết phải chạm sàn hoặc nằm trong `BX_FLOAT_MM` (mặc định 10 mm) so với một chi tiết khác, nếu không báo cố vấn sửa; chi tiết cố ý treo tự do khai báo `"free": true`. (2) Kế hoạch có thể khai báo `mirror_of` (bản phản chiếu qua mặt phẳng, mặc định y=0) và `array` (n bản lặp), chương trình khai triển xác định. (3) Sau lần gọi đầu, cố vấn nhận kế hoạch với công thức đã rút gọn thành một dòng dấu; muốn đổi hình học chỉ cần gửi lại `ops` của chi tiết đó. Đo trên kế hoạch xe moto 112 chi tiết: 29 cặp đối xứng chính xác (khoảng 8% văn bản kế hoạch) và kế hoạch gửi lại nhỏ hơn 15%; đây là mức giảm khiêm tốn.

**0.9.9 - phân tầng chi tiết, vá kế hoạch cục bộ và kiểm tra lơ lửng 2 chiều.** Cố vấn chỉ viết `ops` cho thân vỏ/ống cong khó và bỏ trống `ops` ở chi tiết cơ khí thường để Qwen dịch sang `bxlib`; thêm hành động `patch_plan` sửa cục bộ thay vì gửi lại toàn bộ `plan`; giảm `BX_REVIEWS` mặc định còn 1; sửa `connectivity.floating` đo bề mặt 2 chiều trên đỉnh + trung điểm cạnh + tâm mặt.

**0.9.10 - danh sách model thống nhất cho Cố vấn/Thực thi, tự động co giãn theo số chi tiết, cổng hình học Python thuần, Best-of-N thích ứng, đối xứng/mảng cho Qwen và phục hồi kế hoạch lớn.** (1) **Danh sách Model thống nhất**: mọi model cấu hình (URL Qwen/OpenAI mặc định, URL API thêm vào, Anthropic API, Claude/Codex/Custom CLI) đều nằm chung trong một danh sách, có thể chọn linh hoạt làm **Cố vấn** (`--planner`) lẫn **Thực thi** (`--worker`) ngay trên thanh nhập hoặc trong Cài đặt. (2) `bxlib.recipe_bbox` + `bxlib.check_plan_part` kiểm tra hộp bao, độ rỗng `shape.kind="ring"` và hướng đầu-cuối `shape.endpoints` ngay trong Python trước khi mở Blender; `loft`/`panel` chấp nhận lát cắt `x` đơn điệu tăng hoặc giảm. (3) `BX_BESTOFN` (mặc định 2) lấy thêm phương án từ model thực thi khi mẫu đầu trượt cổng tĩnh hoặc lệch hộp bao > 4 mm và chọn bản sai số nhỏ nhất. (4) `mirror_of` và `array` hỗ trợ chi tiết do worker viết (không cần `ops` trong kế hoạch) và tự khai triển `array` trong `joints`/`allowed_contacts`. (5) Va chạm ở tư thế nghỉ (`overlap_mm`) cho worker tự sửa 1 vòng cục bộ trước khi leo thang; `plan_delta_view` chỉ gửi vùng lân cận và `review_text` gom tóm tắt khi cảnh lớn. (6) `scale_policy(N)` tự co giãn `group_max`, luồng worker song song (`1/2/3`), `max_issues`, bán kính delta (`60/55/45 mm`), ngân sách `eff_max` (`10/12/15`) và ngân sách đỉnh/tessellation cho cảnh > 55 chi tiết; `clip_prompt` giữ đầu+đuôi lời nhắc, `jparse` chống ngoặc nhiễu và giữ JSON trước khi sửa lỗi kế hoạch.

**0.9.11 - tự động phát hiện model mới, Antigravity CLI (`agy`) và chọn nhanh Model con / Effort cho mọi vai trò.** (1) Tự động đọc danh mục model và mức suy nghĩ mới nhất từ Claude Code (`~/.claude/cache/model-catalog`, bao gồm `opus`, `sonnet`, `fable`, `haiku` và mọi dòng mới phát hành), **Antigravity CLI (`agy`)** (quét động `agy models` gồm Gemini 4 Neon/Carbon/Hydrogen, Gemini Flash, Claude 5.5, GPT-OSS, kèm khoá quyền không chạy lệnh/sửa tệp qua `--settings` và nhận ảnh qua `view_file`/`read_file` trong thư mục tạm), Codex CLI (`~/.codex/models_cache.json`, `config.toml` và `codex app-server` `model/list`), cùng chuẩn `GET /v1/models` cho cả OpenAI-compatible lẫn Anthropic API. (2) Ngay cạnh ô chọn **Cố vấn** và **Thực thi** trên thanh chat (và qua CLI `cadfabric models`, `--planner claude:sonnet@medium`, `--planner agy:gemini-3.8-flash@low`, `--planner-model`, `--planner-effort`, `--worker-model`, `--worker-effort`) có thể đổi model con, mức suy nghĩ và `--fallback-model` cho Claude mà không cần tạo lại cấu hình.

**0.9.12 - bộ chọn model dạng cây phân tầng gọn nhẹ trong khung chat.** Thu gọn thanh công cụ chat về hai nút pill tối giản (`Cố vấn: … ▾` và `Thực thi: … ▾`) mở popover neo theo viewport (lật tự động không tràn mép), phân tầng theo nhà cung cấp (`Qwen`, `Claude`, `Antigravity CLI`, `Codex`, API/CLI tuỳ chỉnh) kèm nhánh con thu gọn cho các phiên bản phụ/biến thể, ô lọc/nhập model mới, nút quét `↻` tại chỗ và thanh chọn `Effort` ở chân popover.

**0.9.13 - bài học khép kín theo vai trò cho Blender/CadQuery, lọc trùng & chống bài học ngược.** (1) Bài học đã duyệt (và `#tên` người dùng gõ) được đưa thẳng vào luồng `scene3d` phân theo vai trò `Cố vấn` / `Thực thi` (`director` / `worker` / `both`). (2) Chỉ tự rút bài học trên phần sự kiện mới của lượt vừa chạy khi lượt kết thúc sạch và lỗi đã được xử lý (`auto_signals`), tối đa 2 ý mới mỗi lượt kèm danh sách bài đã biết để tránh đề xuất lặp. (3) Chặn bài học lách cổng kiểm, ngắt câu theo ranh giới từ, khử trùng theo tiêu đề/slug + nội dung, giới hạn tối đa 25 bài chờ duyệt mỗi dự án, và thêm ô lọc + nút `Lọc trùng` / `Xoá chờ duyệt` trên tab **Bài học**.
