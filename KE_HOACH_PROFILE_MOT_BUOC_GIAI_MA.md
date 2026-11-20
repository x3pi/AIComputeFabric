# Kế hoạch profile một bước giải mã (Qwen3.8-27B, 3060 + 5070 qua RPC)

Ngày lập: 2026-11-18. Gắn nhãn: **[ĐO]** đã đo trước đây (ghi chú dự án); **[SUY RA]** tính từ số đo; **[CHƯA]** chưa làm/chưa kiểm.
Nguyên tắc: không đưa số dự đoán như thể đã đo; mỗi kết luận phải có phép đo kèm theo; lặp n=5, báo trung vị, nhiễu nền ±3 tok/s (theo ghi chú cũ).

## 1. Câu hỏi cần trả lời
Một bước giải mã của engine production tốn thời gian ở đâu: kernel trên 3060, kernel trên 5070, truyền RPC giữa hai máy, chờ nhau (pipeline), hay phía CPU (dựng đồ thị, đồng bộ, lấy mẫu, MTP)? Kết quả quyết định có đáng viết/đổi kernel (CUDA hay Triton) hay nên sửa RPC/cấu hình/engine.

## 2. Cấu hình cần đo (đúng production, đọc từ tiến trình đang chạy)
`llama-server` build 11913 (commit 7aca0d2ea, 2026-10-26), mô hình `Qwen3.8-27B-UD-Q4_K_M.gguf`, `-c 98304 -np 2 -kvu -ngl 99 -fa on -ctk q8_0 -ctv q5_0 -ub 256 -t 8 -Cr 0-7 --cpu-strict 1 --rpc 10.42.0.225:50052 --split-mode layer --tensor-split 56,44 --spec-type draft-mtp --no-mmproj-offload`. (Cờ `--tensor-split` và `-ctv` xuất hiện hai lần, giá trị cuối thắng.) 3060 (CUDA0, sm_86, Linux, driver 580.178.04) giữ khoảng lớp 0–37; 5070 (sm_120, Windows, driver 617.42) qua `ggml-rpc-server`, giữ phần còn lại, đầu ra và lớp MTP. Cáp trực tiếp 2,5 GbE, MTU 1500. Embed và rerank chạy trên CPU (core 8–19).

## 3. Số gốc đã biết
- Giải mã MTP n=3: khoảng 48 tok/s đo bằng bench, 53,5 tok/s qua gateway; cơ sở không MTP 23,2 tok/s **[ĐO]**. Tương ứng khoảng 21 ms/token (MTP) và 43 ms/token (không MTP) **[SUY RA]**.
- Chấp nhận MTP khoảng 73% ở n=3 **[ĐO]**.
- Cả hai card đạt khoảng 85–88% mức trần băng thông **[ĐO]**; 3060 giữ khoảng 67% thời gian roofline **[ĐO, ghi chú ablation]**.
- Chi phí cố định mỗi thiết bị RPC từ phép đo 3 card: RTT cộng khoảng 1,2 MB/token cho 27B **[ĐO]**; RTT bridge 0,2–0,4 ms **[ĐO]**.
- Điều chưa biết **[CHƯA]**: tỉ lệ thời gian một bước thuộc về từng thành phần; GPU nào rảnh bao lâu.

## 4. Giả thuyết và cách bác bỏ
| # | Giả thuyết | Dấu hiệu ủng hộ | Dấu hiệu bác bỏ |
|---|---|---|---|
| H1 | Kernel 3060 chiếm phần lớn bước và đã sát roofline | GPU0 bận gần liên tục trong bước, băng thông bộ nhớ đo được gần trần | GPU0 có khoảng trống đáng kể |
| H2 | Kernel 5070 chiếm phần lớn | tương tự cho GPU 5070 | khoảng trống lớn |
| H3 | Truyền RPC (tuần tự hóa, RTT, băng thông) | luồng chính bị chặn ở `recv`; byte/bước × giá trị RTT giải thích được khoảng trống | thời gian chờ nhỏ |
| H4 | Bong bóng pipeline (3060 chờ 5070 và ngược lại) | hai GPU luân phiên bận, tổng bận ≈ 1 GPU | cả hai gối nhau |
| H5 | Phía CPU (đồ thị, đồng bộ, sampling, bước nháp MTP) | khoảng trống GPU lặp lại cùng pha với việc dựng đồ thị/launch | CPU rảnh trong các khoảng trống |
| H6 | Chi phí launch/WDDM bên Windows | rpc-server tốn CPU, kernel 5070 thưa | launch dày, không trống |
| H7 | MTP: kernel xác minh batch nhỏ (1+n token) kém hiệu quả, bước nháp đắt | thời gian bước xác minh theo batch không tuyến tính; chi phí nháp lớn | tuyến tính, nháp rẻ |

## 5. Công cụ và độ sẵn sàng
| Công cụ | Trạng thái |
|---|---|
| Linux: Nsight Systems **2022.4.2**, Nsight Compute 2022.4.1, `perf`, `bpftrace`, `strace` | có (**[CHƯA]** nsys quá cũ so với driver 580/CUDA 13: có thể thiếu hỗ trợ hoặc cho ra dữ liệu thiếu; cần thử trên một lệnh ngắn trước, nếu lỗi cài bản mới của NVIDIA) |
| Windows 5070 (SSH khóa, `fios02@10.42.0.225`): Nsight Systems | **không có** (`where nsys` không thấy) ⇒ cần cài (xin bạn duyệt) hoặc dùng thay thế: `nvidia-smi dmon`, bộ đếm hiệu năng, CPU của `ggml-rpc-server` |
| `llama-server --perf`, `--log-timestamps`, `-lv 4` | có; cho thời gian thô của libllama |
| `llama-bench`, `llama-batched-bench` | có, để đo hiệu ứng theo kích thước batch (H7) |
| Bench giải mã: `benchmarks/hardware/openai_stream_bench.py`, `long_prompt_bench.py`, `llamacpp_slot_pair_probe.py` | có (**[CHƯA]** chọn script khớp; kiểm tra trước) |
| Gói tin RPC: `ss -ti`, `tcpdump`/`bpftrace` trên `enp133s0` | có |

## 6. Quy trình theo tầng
**Tầng 0: số thô, không cần profiler (rẻ, làm trước).**
Một luồng, nhiệt độ 0, prompt ngắn cố định, `max_tokens` cố định, n=5. Ghi tok/s, thời gian prefill/decode từ `--perf`, tỉ lệ chấp nhận MTP. Biến thể để tách thành phần: (E0) cấu hình production; (E1) `--spec-type none` (loại MTP, tách H7); (E2) mô hình nhỏ 4B một card so với 4B ghép hai card (hiệu chuẩn chi phí cố định của RPC, H3); (E3) quét tỉ lệ chia quanh 56,44 (đã biết 58,42 xấu, nguyên nhân chưa rõ **[ĐO]**); (E4) dừng embed/rerank để thấy tranh chấp CPU (H5).

**Tầng 1: dòng thời gian bên Linux (nsys).**
Chạy engine **dưới** nsys (nsys không gắn vào tiến trình đang chạy) với đúng cờ production, cùng workload Tầng 0, cửa sổ ổn định khoảng 10–20 giây sau khởi động nóng. Thu: CUDA API, kernel, memcpy, OS runtime (để thấy chờ ở `recv`/`poll`), mẫu CPU. Dẫn xuất mỗi bước: tổng thời gian kernel GPU0, khoảng trống GPU0, thời gian luồng chính bị chặn đợi RPC, thời gian dựng/launch đồ thị.
[CHƯA] Lệnh mẫu cần kiểm cờ với bản nsys đang cài: `nsys profile -o <out> --trace=cuda,osrt --sample=cpu --duration=20 <llama-server ...>`.

**Tầng 2: bên Windows (5070).**
Nếu cài được Nsight Systems: chạy `ggml-rpc-server` dưới nó với cùng thời điểm đo. Nếu không: `nvidia-smi dmon -s pucm` mỗi 100 ms, CPU và số luồng của `ggml-rpc-server`, và đối chiếu thời điểm với Linux (hai đồng hồ lệch nhau; cần đồng bộ NTP hoặc đo độ lệch trước).

**Tầng 3: mạng.**
Đếm byte và gói mỗi bước trên cáp trực tiếp (`ss -ti`/`tcpdump`), nhân với RTT để kiểm H3 và so với số đo 1,2 MB/token.

**Tầng 4 (tùy kết quả): kernel cụ thể.**
Chỉ khi Tầng 1 cho thấy một kernel chiếm phần lớn: dùng Nsight Compute trên đúng kernel đó để xem có sát roofline không. Nếu sát, dừng ở đây (đã đúng với kết luận cũ).

## 7. Quy tắc quyết định (ngưỡng đề xuất, không phải số đo)
- Nếu GPU nào bận ≥ ~85% bước và băng thông sát trần: kernel không còn nhiều dư địa ⇒ ưu tiên giảm số byte phải đọc (lượng tử, KV), không viết kernel.
- Nếu thời gian chờ RPC/pipeline ≥ ~15% bước: ưu tiên RPC (gộp thông điệp, tránh đồng bộ thừa, MTU, tỉ lệ chia) hoặc cấu trúc ghép.
- Nếu khoảng trống CPU/launch ≥ ~15%: ưu tiên CUDA graph, giảm đồng bộ, affinity, tranh chấp CPU.
- Nếu H7 (kernel xác minh batch nhỏ) nổi bật: thử llama.cpp `v1.0.0` (có PR MMQ cho batch nhỏ #31533/#32065).
Ngưỡng 85% và 15% là lựa chọn thiết kế để có tiêu chí rõ ràng; có thể chỉnh trước khi đo, không chỉnh sau khi thấy kết quả.

## 8. An toàn production
- Production dùng chung VRAM; guard giết engine nếu bộ nhớ trống dưới ngưỡng headroom (512 MiB), và nsys tốn thêm bộ nhớ. Không đo song song với production.
- Cần **cửa sổ bảo trì** (lần trước khoảng 10 phút). Trước khi dừng: đặt bộ hẹn giờ khôi phục an toàn (tiền lệ: các timer `aicf-failsafe-restore*`), và đọc lại ghi chú `reference-aicf-engine-hang-rpc` + `reference-aicf-model-swap-gotchas` (bộ điều phối của gateway dừng `ggml-rpc-server` bên Windows nếu chỉ dừng guard; dùng pause/resume worker của gateway thay vì dừng tay).
- Khôi phục production sau đo và xác nhận bằng một yêu cầu chat qua gateway.
- Đo hạng mục tiêu hao (nsys cộng overhead) tách khỏi số liệu tok/s: tốc độ đo lúc có profiler không dùng làm số hiệu năng.

## 9. Sản phẩm
Báo cáo `benchmarks/reports/P14_PROFILE_BUOC_GIAI_MA.md` (bảng thời gian mỗi bước chia theo thành phần, kết luận từng giả thuyết, dữ liệu thô nsys `.nsys-rep` và JSON, hạn chế). Chỉ kết luận điều có phép đo; điều chưa đo ghi là chưa đo.

## 10. Quyết định cần bạn
1. Cửa sổ bảo trì (ngày/giờ) và cho phép dừng production tạm thời.
2. Cho phép cài Nsight Systems bản mới trên Linux (nếu bản 2022.4 không chạy được) và trên máy Windows 5070.
3. Có cho chạy thêm bản `llama.cpp v1.0.0` ở bước sau không (sau khi có profile).
