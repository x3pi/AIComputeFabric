# KẾ HOẠCH: CẢI THIỆN TẦNG MẠNG PIPELINE-PARALLEL CỦA SGLang (FORK `x3pi/sglang`) VÀ CHUẨN BỊ PR UPSTREAM
*(Bản 2026-11-01. Mục tiêu: **đo, tìm đúng nút thắt, sửa tối thiểu, chứng minh bằng số đo, rồi mới tạo PR**. Không giả định nguyên nhân trước khi đo. Bàn giao cho Agent thực thi.)*

**Quy ước nhãn:** `[ĐO]` đã đo · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` · `[SUY LUẬN]` · `[CHƯA XÁC MINH]` phải thử. Con số không có `[ĐO]` là chưa đo. Báo cáo bằng tiếng Việt.

---

## PHẦN 0: ĐÁNH GIÁ GIẢ THUYẾT "TẦNG MẠNG SGLang YẾU HƠN vLLM"

**Chứng cứ ủng hộ (P11, `Qwen3-8B-AWQ`, PP=2 qua cáp 2,5 GbE + `nccl0`) `[ĐO]`:** decode 1 người: chia lệch (13,23) vLLM **73,24** so SGLang **58,78** tok/s (−20%); chia đều (18,18) vLLM 67,73 so SGLang 44,19 (−35%). Prefill 8k (13,23): vLLM 2.554 so SGLang 2.042 tok/s.

**Chứng cứ chống / chưa kết luận được:**
1. Ở tải cao SGLang **không** yếu hơn: `C=16` (13,23) SGLang **331,25** so vLLM **317,79** (P11, `[ĐO]`). Khoảng cách chỉ lộ ở **độ trễ từng token (tải thấp)**, tức là con đường nhạy với độ trễ, không phải băng thông.
2. **Nguyên nhân chưa được đo.** Độ trễ thấp hơn có thể do mạng (nhiều thông điệp nhỏ qua `gloo`), nhưng cũng có thể do bộ lập lịch Python, đồng bộ CUDA, thiếu CUDA graph khi PP, hay tham số NCCL. **Kế hoạch này bắt đầu bằng đo, không bằng sửa.**
3. Đoạn phân tích dán kèm có **số không truy được** trong dữ liệu: `306,86`, `304,24`, `553,03`, `539,77` (C=16 một card) **không có** trong `P11_WINDOWS_2GPU_SGLANG_VLLM.md` hay `p11_2gpu_sglang_vllm.jsonl`; P10 đo cùng điều kiện trên 3060 cho `C=16`: SGLang **206,96** < vLLM **256,96** `[ĐO]` (mâu thuẫn). `35,60` s (khởi động DP=2 SGLang) cũng không thấy; `15,03`, `28,55`, `61,17` s có trong jsonl. Còn `P11.md` nhắc **TTFT P95 llama.cpp = 12.175 ms** (3 chỗ) trong khi P10 đo **3.693 ms** `[ĐO]`. Cần agent thực thi **rà lại P11 trước khi dùng làm mốc** (Pha S0).
4. Vì vậy câu "SGLang thắng tuyệt đối ở tải cao" chưa đủ căn cứ cho mọi chế độ; phần đáng chắc chắn là: **PP=2 ở `C=1..4` SGLang chậm hơn vLLM** `[ĐO]`. Đó là mục tiêu cải thiện.

---

## PHẦN 1: HIỆN TRẠNG KHO MÃ

- Fork đã clone (blobless, **không** push gì): `/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/sglang`, `origin = https://github.com/x3pi/sglang`, thêm `upstream = https://github.com/sgl-project/sglang`; fork đang trùng `upstream/main`; HEAD `8817f63c2c` (2026-11-01). Tags có `v0.5.19` (**bản đã đo**) đến `v0.5.23`; `main` còn mới hơn. Dung lượng `.git` 155 MiB, ổ dữ liệu còn ~31 GB.
- **Hai nhánh làm việc, đừng trộn:**
  1. `perf/pp-net-baseline` tách từ tag **`v0.5.19`**: để tái lập đúng số đã đo, gắn công cụ đo (không đổi hành vi), so A/B.
  2. `perf/pp-net-upstream` tách từ **`upstream/main`**: chỉ nhánh này mới dùng để PR (PR nhận ở `main`, mã PP trong `main` đã khác nhiều: `python/sglang/srt/managers/scheduler_pp_mixin.py` dài 1.868 dòng). Mọi sửa đổi phải **cherry-pick và đo lại** trên `main`.
- **Đã đọc mã (`[ĐỌC MÃ]`, chưa chạy):** truyền dữ liệu PP đi qua `GroupCoordinator` trong `python/sglang/srt/distributed/parallel_state.py`:
  - đối tượng Python (yêu cầu mới, metadata, kết quả): `send_object`/`recv_object` dùng **`cpu_group` (gloo)**, mỗi lần là **hai thông điệp** (tensor kích thước rồi tensor nội dung, `isend`/`irecv`, dòng ~1626–1710);
  - tensor (hidden states, proxy tensors): `send_tensor_dict`/`recv_tensor_dict` (dòng ~1797, `scheduler_pp_mixin.py`: `_pp_send_dict_to_next_stage`, `_pp_send_proxy_to_next_stage`, `P2PWork`), phần thân tensor qua NCCL (`device_group`), phần metadata qua `cpu_group`.
  - Ứng viên nút thắt **[SUY LUẬN, cần đo]**: với `C=1`, mỗi token cần nhiều thông điệp nhỏ qua mạng (gloo size + payload + NCCL), mỗi cái gánh độ trễ vòng đi vòng về qua cáp/`wg0`.

---

## PHẦN 2: GIẢ THUYẾT CẦN KIỂM (đo để loại trừ, không để chứng minh)

| # | Giả thuyết | Cách kiểm |
|---|---|---|
| H1 | Số thông điệp `gloo` mỗi bước decode quá nhiều (size + payload tách đôi, nhiều đối tượng khác nhau) ⇒ cộng dồn độ trễ | Đếm thông điệp/bước (hook ở `send_object`/`recv_object`/`send_tensor_dict`), đo thời gian chờ từng lời gọi; so với RTT đo được qua `wg0` |
| H2 | Gửi/nhận NCCL hidden states **đồng bộ** hoặc chờ `wait()` sớm, không chồng lấp tính toán với truyền | `torch.profiler`/`nsys` trên 1 bước decode, xem khoảng trống GPU rảnh giữa hai stage |
| H3 | Chi phí bộ lập lịch Python/serialization (`pickle`) và thiếu CUDA graph cho decode PP | Tách thời gian CPU của bước (profile), thử cờ liên quan CUDA graph/`--disable-cuda-graph` để xem ảnh hưởng `[CHƯA XÁC MINH tên cờ trên 0.5.19]` |
| H4 | Tham số NCCL socket chưa tối ưu cho đường 2 nút (`NCCL_SOCKET_NTHREADS`, `NCCL_NSOCKS_PERTHREAD`, `NCCL_BUFFSIZE`, `NCCL_PROTO`, `NCCL_ALGO`) | Thử từng biến (cấu hình, không cần sửa mã); vLLM dùng cùng NCCL nên khác biệt có thể nằm ở đây |
| H5 | Khác biệt do đường truyền (`wg0`, MTU 1380, mã hóa) | Cả vLLM và SGLang đã dùng cùng đường ⇒ ít khả năng giải thích **khác biệt giữa hai engine**; vẫn đo RTT/băng thông làm sàn |
| H6 | Khác biệt do cấu hình vLLM so với SGLang (chunked prefill, batch tối đa, KV) chứ không do mạng | So bảng tham số khởi động hai bên trong P11 log; đưa về cấu hình tương đương |

**Giới hạn lý thuyết cần ghi:** bước decode PP=2 tối thiểu = (tính toán stage 1) + (tính toán stage 2) + ít nhất 1 lần truyền hidden states qua mạng + độ trễ trả token. Ước từ số một card `[ĐO]` rồi trừ ra để biết còn bao nhiêu ms "dư" do tầng mạng và lập lịch, không đoán.

---

## PHẦN 3: CÁC PHA

### Pha S0: Chuẩn bị và rà số liệu (≈ 2 giờ, chỉ đọc + dựng môi trường, **xin phép trước khi chiếm GPU/mạng**)
1. Rà `P11.md`/`jsonl`: sửa/ghi chú các số không truy được (Phần 0.3), lấy **cấu hình khởi động chính xác** của SGLang và vLLM từ log P11 (phiên bản, cờ, biến môi trường).
2. Tạo venv trên **ổ dữ liệu** (không vào `/`), cài SGLang editable từ nhánh `perf/pp-net-baseline` (`v0.5.19`) trên **cả hai nút**: Linux và WSL2. Phiên bản torch/NCCL phải khớp hai nút (đang dùng torch 2.13 + cu130 như P10/P11). Cách đưa mã sang WSL2: `git`/`rsync` qua SSH; **không push lên GitHub nếu chưa được duyệt**.
3. Tái lập số đo gốc: SGLang PP=2 (13,23) `C=1,4,16` ba lần ABAB cùng vLLM, cùng `p10_client.py`; xác nhận lại khoảng cách 58,78 so 73,24 (nhiễu ±3%).
- **Cổng S0:** khoảng cách `C=1` tái lập được (> nhiễu). Nếu không tái lập, **dừng**, báo: vấn đề có thể là nhiễu/cấu hình, không phải mã.

### Pha S1: Đo nền mạng và dụng cụ đo (≈ 3 giờ)
1. **Sàn mạng (không cần SGLang):** mở rộng `benchmarks/hardware/p11_nccl_smoke.py` hoặc viết `p12_net_floor.py`: ping-pong NCCL `send/recv` tensor 16 KB–16 MB, và ping-pong `gloo` `send_object` (nhỏ), qua `wg0`; ghi độ trễ trung vị/p99, so với RTT `ping`. Đây là mức tối thiểu có thể đạt.
2. **Dụng cụ đo trong SGLang (nhánh baseline, mặc định tắt, không đổi hành vi):** biến môi trường `SGLANG_PP_NET_TRACE=1` ghi, cho mỗi bước: số/kích thước thông điệp `gloo`, thời gian từng `send_object`/`recv_object`/`send_tensor_dict`/`recv_tensor_dict`, thời gian chờ `P2PWork.wait()`, thời gian bước forward từng stage. Ghi ra file `jsonl`, tính chi phí < 1% khi bật.
3. Thu `nsys`/`torch.profiler` cho **một** vài bước decode ở `C=1`.
- **Cổng S1:** phân rã thời gian một token decode `C=1` thành: tính toán, thời gian truyền thực, chờ/đồng bộ, CPU lập lịch; tổng khớp thời gian tường trong ±10%.

### Pha S2: Phân tích và chọn mục tiêu (≈ 1 giờ)
- Xếp hạng H1–H6 theo ms/token đóng góp thật. **Chỉ sửa cái nào có phần dư lớn hơn nhiễu** (≥ 2 ms/token hoặc ≥ 5% thời gian token). Nếu phần dư chủ yếu là H4 (cấu hình NCCL), ghi như **khuyến nghị cấu hình**, không phải mã.
- **Cổng S2:** nếu không tìm được nguyên nhân do mã trong SGLang (phần dư nhỏ/không đổi được), kết luận "khác biệt đến từ cấu hình/tổng thể, không có PR đáng làm" là kết quả hợp lệ.

### Pha S3: Cài đặt sửa tối thiểu (≈ 4–8 giờ, tuỳ nút thắt)
Ví dụ **chỉ nếu số đo ủng hộ** (đều đặt sau cờ/biến môi trường, mặc định giữ hành vi cũ khi chưa chứng minh):
- H1: gộp thông điệp `gloo` mỗi bước (một thông điệp thay vì size + payload, hoặc gộp nhiều đối tượng cùng bước thành một buffer), loại bỏ thông điệp trùng.
- H2: đẩy `wait()` muộn nhất có thể, chồng lấp truyền hidden states với tính toán bước kế.
- Kèm **kiểm thử đơn vị** trong `test/` (tính đúng của nhóm thông điệp, hồi quy: hành vi cũ khi cờ tắt, mutation: đảo thứ tự/lỗi truyền).
- Làm trên `perf/pp-net-baseline` (v0.5.19) để đo A/B; sau đó **port sang `perf/pp-net-upstream`** (`main`) và đo lại trên `main`.

### Pha S4: Xác nhận (≈ 3 giờ)
- ABAB (trước/sau, cùng phiên, ≥ 7 lần decode, ≥ 3 lần prefill) trên `Qwen3-8B-AWQ` PP=2 (13,23) và (18,18), `C = 1, 4, 8, 16`, kèm Best-of-8/16; chất lượng 15/15 (`p10_v2_quality.py`); mutation (đảo cờ, đảo tỷ lệ chia tầng); không hồi quy ở `C=16` và DP=2.
- **Tiêu chí thành công:** cải thiện decode `C=1` PP=2 **ngoài nhiễu (±3% hoặc ±3 tok/s)**, lặp lại ở lượt ABAB thứ hai, 15/15, không hồi quy > nhiễu ở `C=16`. Mục tiêu thực tế: thu hẹp khoảng cách với vLLM (58,78 → gần 73,24); **không hứa vượt vLLM**.

### Pha S5: Chuẩn bị PR upstream (≈ 1 ngày, **không tự đẩy PR**)
1. Đọc `CONTRIBUTING.md`, quy ước commit, pre-commit/lint, yêu cầu test (CI) của SGLang; kiểm tra có issue/PR trùng (`gh search prs`) và **mở issue mô tả vấn đề + số đo trước**.
2. Phạm vi PR **tổng quát**: tránh gắn với cấu hình riêng của bạn (WSL2, `wg0`, Windows 10). Mô tả lợi ích trên kịch bản 2 máy qua TCP/gloo phổ biến; chứng cứ phải **tái lập được** bởi người khác (lệnh, phiên bản, số đo, phần cứng). Làm sạch: không IP/mật khẩu/đường dẫn riêng.
3. PR nhỏ, chia theo từng thay đổi; đo cả trên một cấu hình **tiêu chuẩn** (ví dụ loopback 2 tiến trình hoặc 2 GPU cùng máy) để chứng tỏ không hồi quy.
4. **Việc push lên fork `x3pi/sglang` và mở PR/issue là hành động ra bên ngoài: phải được bạn đồng ý rõ ràng từng lần.**

---

## PHẦN 4: AN TOÀN VÀ RÀNG BUỘC

- Phép đo chiếm GPU của hai máy và cần mạng `wg0` dựng sẵn (xem `KE_HOACH_CAI_THIEN_WINDOWS_GOP_2_GPU_SGLANG_VLLM.md`): xin phép dừng dịch vụ đang giữ GPU, khôi phục đúng trạng thái sau đo; không chạy chồng với P9/P10/P11 hay dịch vụ Coder.
- Không đặt gì vào `/` (chỉ ~9–12 GiB trống); tất cả trên ổ dữ liệu (~31 GB trống: venv ~10 GB, model đã có sẵn).
- Không cài/cập nhật gói hệ thống; không đổi cấu hình Windows ngoài phạm vi đã duyệt.
- Không push, không tạo PR/issue, không đăng lên dịch vụ ngoài khi chưa được duyệt. Không commit vào repo `AIComputeFabric` mã của SGLang; kết quả đo và báo cáo ghi ở `ai-compute-fabric/benchmarks/reports/` (`P12_SGLANG_PP_NET.md`, `p12_*.jsonl`, `logs/p12_*.log`), runner ở `benchmarks/hardware/p12_*`.
- Giấy phép: SGLang Apache-2.0; giữ nguyên header và tuân theo quy định đóng góp (DCO/CLA nếu có).

---

## PHẦN 5: SẢN PHẨM VÀ ƯỚC LƯỢNG

| Pha | Thời gian | Cần phép |
|---|---|---|
| S0 | ~2 giờ | cài venv, chiếm GPU cho tái lập |
| S1 | ~3 giờ | chiếm GPU, mạng `wg0` |
| S2 | ~1 giờ | — |
| S3 | 4–8 giờ | — (chỉ mã) |
| S4 | ~3 giờ | chiếm GPU |
| S5 | ~1 ngày | **mọi hành động push/PR/issue** |

**Báo cáo cuối:** bảng phân rã thời gian/token (trước/sau), giả thuyết H1–H6 nào đúng/sai kèm số, diff đã làm, kết quả ABAB, chất lượng 15/15, mutation, những gì chưa đo (Windows 11, nhiều hơn 2 nút, mô hình khác, `main` so với `v0.5.19`), và khuyến nghị: có nên PR không và PR gì.

---

# PHỤ LỤC P12 (2026-11-01): kết quả đã đo và khung đánh giá cho thay đổi lớn

## A. Điều đã biết (chi tiết: `ai-compute-fabric/benchmarks/reports/P12_S0_RECON_SGLANG_PP.md`)
- `[ĐO]` PP=2 (13,23), 1 người dùng, eager: SGLang chậm hơn vLLM khoảng 16–22%. Một card thì SGLang nhanh hơn 5% ⇒ khoảng cách nằm ở đường phân tán.
- `[ĐO]` Mạng không phải nút thắt: sàn độ trễ gloo/NCCL qua cầu `nccl0` < 1 ms/token; WSL2 không chậm hơn theo từng tầng (0,32 so với 0,35 ms/tầng) nhưng có dao động giữa các lần chạy; gói nguồn Windows không phải nguyên nhân.
- `[ĐO]` `--pp-async-batch-depth 1` chỉ +5% ở C=1 và −8% ở C=4 ⇒ loại.
- `[ĐO]` Prototype `SGLANG_PP_EARLY_PROXY` (rank nhận post `irecv` trước khi launch, bỏ metadata gloo cho decode): eager +10,4% (C=1), +6,1% (C=4), đầu ra greedy giống hệt 14/14.
- `[SUY LUẬN]` Phần còn lại: CPU launch của stage sau và vòng phản hồi token qua CPU (output → rank 0 → xử lý → lập lịch → gửi reqs). vLLM dùng async scheduling nên rút ngắn vòng này.

## B. Hướng thay đổi lớn (xếp theo rủi ro tăng dần)
| Mã | Nội dung | Kỳ vọng | Rủi ro chính |
|---|---|---|---|
| D1 | Tổng quát hoá early-proxy: cả prefill/extend, pp>2, tp>1, bỏ giả định "tin nhắn legacy đầu tiên" | giữ lợi ích, bỏ giới hạn | sai thứ tự NCCL ⇒ treo/sai số; kích thước động của extend |
| D2 | Đường relay output: rank cuối gửi thẳng tới mọi rank thay vì qua rank 0 | giảm 1 chặng trên đường phản hồi | thay đổi giao thức PP, ảnh hưởng PD-disaggregation |
| D3 | Pre-schedule (async scheduling): lập lịch bước k+1 trước khi biết token k, token lấy mẫu giữ trên GPU | lớn nhất (đúng điểm khác biệt với vLLM) | chạm lõi scheduler, abort/stop-string/spec-decode/grammar; khó kiểm |

## C. Quy tắc "không được cái này mất cái khác"
Mỗi thay đổi (cờ tắt mặc định) phải qua **toàn bộ** ma trận dưới đây, so với baseline cùng phiên, xoay thứ tự, ≥ 7 vòng, báo median và số vòng thắng; **không có ô nào được xấu hơn ngưỡng nhiễu đo được (±3 tok/s hoặc ±5%) mà không có giải thích bằng văn bản**:

1. **Tải:** decode C=1, C=4, C=16 (tổng và p50 theo người dùng); prefill dài (4k, 16k) đo TTFT; hỗn hợp prefill+decode (chunked prefill); request đến lệch pha.
2. **Cấu hình:** eager và CUDA graph; `pp-async-batch-depth` 0 và mặc định; tp=1 (có thể thêm tp×pp khi có máy); radix cache bật/tắt; phân vùng tầng khác (13,23) và (18,18).
3. **Độ đúng:** greedy giống hệt baseline (≥ 15 prompt, đủ độ dài); chạy một bộ accuracy (GSM8K nhỏ) so với baseline trong ngưỡng; abort giữa chừng; stop string/EOS; hai request cùng lúc với độ dài khác nhau.
4. **Độ bền:** chạy liên tục ≥ 30 phút với tải ngẫu nhiên, không treo, không rò bộ nhớ (VRAM/RAM đầu cuối ≈ đầu); khởi động lại sạch.
5. **Tài nguyên:** VRAM thêm, số luồng/nhóm NCCL thêm, thời gian khởi động.
6. **Cổng quyết định:** (a) không ô hồi quy; (b) lợi ích có ý nghĩa ở ít nhất một ô chính (C=1 hoặc C=4) lặp lại được; (c) test đơn vị pass; (d) chủ dự án xem diff trước khi bất kỳ thứ gì rời máy.

## D. Thứ tự thực hiện
1. Hoàn tất ma trận cho prototype hiện tại (D0): thêm CUDA graph (đang chạy S7), C=16, prefill dài, abort, soak.
2. D1 → đo lại ma trận. 3. D2 → đo lại. 4. D3 chỉ khởi động nếu D1+D2 vẫn còn khoảng cách đáng kể và có thiết kế đã được duyệt.
Mọi thay đổi nằm trên nhánh cục bộ, bật bằng cờ; viết bằng tiếng Anh; **không push/PR/issue nếu chưa được chủ dự án duyệt**.
