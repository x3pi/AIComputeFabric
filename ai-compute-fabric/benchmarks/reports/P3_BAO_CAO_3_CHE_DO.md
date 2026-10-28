# BÁO CÁO ĐO 3 CHẾ ĐỘ CỤM 2 GPU (RTX 3060 + RTX 5070) — 2026-10-27

Số liệu thô: `benchmarks/reports/logs/p3_mode*_*.json`, `p3_server_cfg.jsonl`, `p3_gpu_modes_sweep.json`; tổng hợp: `p3_three_modes_report.json` (sinh bằng `benchmarks/hardware/build_three_modes_report.py`, không gõ tay). Công cụ đo: `modes_load.py`, `probe_server_cfg.py`, `sweep_ncmoe.py`.

Nhãn: `[ĐO]` đo ở đây · `[SUY LUẬN]` tính toán · `[CHƯA XÁC MINH]` chưa thử.

## 0. Tóm tắt (đọc phần này nếu chỉ có 1 phút)

| Chế độ | Khẳng định trong mô tả | Kết quả đo | Kết luận |
|---|---|---|---|
| 1 Tách 2 worker | Model ≤ 10 GB, song song, `~335 tok/s` | Qwen3-8B Q4: **359 tok/s** tổng 2 máy (4 luồng/máy); Qwen3-4B Q4: **555 tok/s**. Một luồng: 4B 101 (3060) / 175 (5070); 8B 63 / 114 tok/s | **Đúng về cơ chế, chạy tốt.** `335` không phải con số cố định: phụ thuộc model và số luồng |
| 2 Smart 1-GPU | `-ncmoe 18 … -ctk q8_0`, 30B mỗi máy `50–70 tok/s`, 2 worker song song | RTX 3060: **`-ncmoe 18` hết bộ nhớ** ở `-c 4096`; chạy được từ N=19 (`-c 4096`) / N=20 (`-c 8192`), **~41 tok/s** 1 luồng, **50,8 tok/s tổng** với 2 luồng. RTX 5070: N=20 đạt **~65 tok/s** (N=26 với headroom 1536 MiB đạt **~54–56 tok/s**). Cả 2 máy song song (N=26): **87,0 tok/s tổng** (xem 7.1–7.2) | **Sai ở N=18 trên 3060.** Chạy tốt khi chọn N theo từng card (~41 tok/s trên 3060, ~55–65 tok/s trên 5070) |
| 3 Gộp RPC | `24 GB VRAM + 94 GB RAM`, model 70B–235B, bấm 1 nút | Qua Wi-Fi (~20–25 Mbps): Qwen3-4B nạp 879 s / 197 s, sinh 22–27 tok/s. **Sau khi cắm cáp 2,5 GbE (2,372 Gbps, 282,8 MB/s — mục 8):** chuyển sang `merged` qua gateway mất **32,3 s (4B)** và **86,7 s (30B-A3B)**; **Qwen3-30B-A3B (`--tensor-split 1,1`, 100% VRAM) đạt 126,3 tok/s (1 luồng) và 176,2 tok/s tổng (2 luồng, 92,4 tok/s/luồng)** — nhanh gấp ~2–3× so với chạy 1 card `-ncmoe`! | **Trên cáp 2,5 GbE: Chế độ 3 vượt trội hoàn toàn cho 30B-A3B (126 tok/s vs 41–65 tok/s).** Khẳng định "94 GB RAM" và "70B–235B" vẫn không có bằng chứng (xem 4.3) |

Giới hạn chung: một cụm 2 máy, một lần đo mỗi điểm (3 vòng lặp trong mỗi lần), prompt tiếng Việt ngắn, 128 token, temperature 0; không đo chất lượng đầu ra; không soak; mạng giữa hai máy là **Wi-Fi** nên chế độ 3 bị bất lợi nặng.

## 1. Môi trường `[ĐO]`

- Linux `192.168.1.40`: RTX 3060 12 GB (11 909 MiB dùng được), 30 GB RAM, llama.cpp commit `7aca0d2ea` (CUDA sm_86 + RPC). Windows `192.168.1.49`: RTX 5070 12 GB, Windows 10, cùng commit (CUDA sm_120 + RPC).
- Mạng: Linux dùng **Wi-Fi** (`wlp132s0`, cổng Ethernet chưa cắm cáp). TCP 1 luồng ~25 Mbps (Linux→Windows) và ~20 Mbps (chiều ngược lại), RTT trung bình 2,5 ms. Trong lúc nạp RPC thực tế đo ~2,2 MB/s.
- Trước mỗi lần đo: dừng dịch vụ `AICFWorkerGuard` trên Windows (để GPU chỉ chạy job đo), kiểm không còn `llama-server` khác. Dịch vụ đã được bật lại sau khi đo.
- Chế độ 1 và 3 chạy trực tiếp `llama-server`/`ggml-rpc-server`, **chưa qua gateway/`worker-guard` mới** (Windows vẫn dùng bản `worker-guard.exe` cũ). Phần điều phối 1-click chỉ kiểm bằng test đơn vị với worker giả (xem 5).

## 2. Chế độ 1 — Tách 2 worker độc lập

`llama-server -ngl 99 -fa on -c 16384 -np 4`, mỗi máy một model giống nhau, 3 vòng × 128 token. "Tổng" = số token của mọi yêu cầu / thời gian của cả lần chạy (cả hai máy cùng lúc).

| Model | Cấu hình | RTX 3060 (tok/s) | RTX 5070 (tok/s) | Tổng 2 máy (tok/s) |
|---|---|---:|---:|---:|
| Qwen3-4B-Q4_K_M | 1 luồng/máy | 100,4 | 167,2 | 202,0 |
| Qwen3-4B-Q4_K_M | 4 luồng/máy | 273,6 | 499,1 | **554,9** |
| Qwen3-8B-Q4_K_M | 1 luồng/máy | 63,2 | 110,9 | 126,6 |
| Qwen3-8B-Q4_K_M | 4 luồng/máy | 178,0 | 349,7 | **358,6** |

- 0 yêu cầu lỗi ở mọi cấu hình. Tốc độ một luồng (decode): 4B 101 / 175, 8B 63,5 / 114 tok/s (3060 / 5070).
- VRAM sau tải: 4B 4909 MiB (3060) / 5001 MiB (5070); 8B 7017 / 7111 MiB (đã gồm KV cho `-c 16384`, và trên Windows gồm cả desktop).
- **Hai máy thật sự độc lập**: khi chạy chung, mỗi máy giữ nguyên tốc độ riêng (4B, 4 luồng: 3060 277,5 và 5070 509,9 tok/s, so với 273,6 và 499,1 khi chạy một mình), nên thông lượng khi cả hai cùng bận ≈ 787 tok/s. Cột "Tổng 2 máy" (555) thấp hơn vì được tính theo thời gian của cả lần chạy: máy 5070 làm xong phần việc cố định sớm hơn rồi chờ máy 3060. Với tải liên tục từ gateway thì con số sát 787 hơn; con số 555 chỉ là cách tính của bài đo này.
- Một lần đo đầu của Windows 4B 4 luồng cho 167 tok/s (ngoại lai, nghi nhiễu Wi-Fi/khởi động); chạy lại cho 499 tok/s, báo cáo dùng lần chạy lại; file lần đầu đã bị ghi đè.
- Con số mô tả `~335 tok/s`: **khớp thứ tự độ lớn với 8B 4 luồng (359)** nhưng không được kiểm chứng như một hằng số.
- **Kết luận chế độ 1: đạt.** Hai worker thực sự độc lập và cộng dồn thông lượng.

## 3. Chế độ 2 — Smart 1-GPU Hybrid (`-ncmoe`) với Qwen3-30B-A3B-Q4_K_M (18,56 GB)

Đo trên **RTX 3060** (Linux), `-ngl 99 -fa on`, KV f16 trừ khi ghi, `llama-server` thật, 3 lần lặp mỗi điểm.

| Cấu hình | Kết quả | VRAM đỉnh | Sinh 1 luồng (tok/s) |
|---|---|---:|---:|
| không `-ncmoe`, `-c 4096` | **Hết bộ nhớ** (`cudaMalloc failed`, cần 17 524 MiB) | — | — |
| `-ncmoe 18`, `-c 4096` | **Hết bộ nhớ** khi cấp compute buffer | 11 325 MiB | — |
| `-ncmoe 19`, `-c 4096` | chạy | 11 645 MiB | 42,1 / 42,0 / 40,9 |
| `-ncmoe 20`, `-c 4096` | chạy | 11 319 MiB | 41,1 / 40,8 / 40,0 |
| `-ncmoe 20`, `-c 4096`, KV q8_0 | chạy | 11 143 MiB | 40,3 / 40,1 / 40,0 |
| `-ncmoe 22`, `-c 4096` | chạy | 10 623 MiB | 38,4 / 39,0 / 38,7 |
| `-ncmoe 19`, `-c 8192` | **Hết bộ nhớ** | — | — |
| `-ncmoe 20`, `-c 8192` | chạy | 11 707 MiB | 41,1 / 40,4 / 41,3 |
| `-ncmoe 24`, `-c 8192` | chạy | 10 315 MiB | 37,3 / 36,4 / 36,2 |

Nhiều luồng (`-ncmoe 20`, `-c 8192 -np 2`): 1 luồng 40,4 tok/s tổng (41,2/luồng); **2 luồng đồng thời 50,8 tok/s tổng (25,8/luồng)**, 0 lỗi, VRAM 11 705 MiB, RAM khả dụng còn ~23,4 GB.

Mối quan hệ N ↔ tốc độ (`llama-bench`, ngữ cảnh nhỏ, chỉ để xem xu hướng): tg128 N=18 44,7 → N=48 (mọi expert ở CPU) 25,8 tok/s.

- **`-ncmoe 18` như mô tả: không chạy được** ở ngữ cảnh 4096 (nạp được ở `llama-bench` vì ngữ cảnh nhỏ — đừng chọn N theo `llama-bench`).
- **`50–70 tok/s mỗi máy`: không đạt**; đo 40–42 tok/s một luồng; 50,8 tok/s chỉ là *tổng* của 2 luồng trên cùng một card.
- **`-ctk q8_0` tự động bật: không cần** — không nhanh hơn f16 (40,3 vs 41,1) và chỉ tiết kiệm ~175 MiB. Mã đã bỏ việc tự bật; KV là lựa chọn của người dùng.
- **"2 worker chạy song song, mỗi máy 30B": chưa kiểm.** `pc-win-01` chưa có file `Qwen3-30B-A3B` (chỉ 0.6B/4B/8B); chép 18,56 GB qua Wi-Fi 2–3 MB/s ≈ 1,7–2,5 giờ `[SUY LUẬN]`. Nên chép sau khi cắm Ethernet. Giá trị N cho RTX 5070 phải đo riêng (cùng 12 GB nhưng Windows chiếm VRAM nền khác).
- **Kết luận chế độ 2: đạt một phần** — hoạt động trên RTX 3060 với N ≥ 19–20; chưa kiểm trên RTX 5070 và chưa kiểm hai worker song song.

## 4. Chế độ 3 — Gộp qua GGML RPC

### 4.1. Đã thử được (model nhỏ, vì mạng Wi-Fi không cho phép thử 30B trong thời gian hợp lý)

Qwen3-4B-Q4_K_M, `--rpc 192.168.1.49:50052 --split-mode layer -ngl 99 -c 4096 -np 1`, `ggml-rpc-server -c` (cache) trên Windows.

| Cấu hình | Thời gian nạp | Sinh 1 luồng (tok/s, trung vị; min–max) | Sinh 2 luồng (tok/s/luồng) |
|---|---:|---|---:|
| `--tensor-split 1,1`, nạp lần đầu (nguội) | **879 s** | 24,4 (23,5–27,4) | 26,2 |
| `--tensor-split 1,1`, nạp lại (cache RPC) | **197 s** | 22,1 (21,3–30,2) | 27,2 |
| `--tensor-split 4,6` | 181 s | 19,0 (**1,0**–20,9) | 13,8 |

- So với chạy trên 1 card cùng model: 4B đơn: 101 (3060) / 175 (5070) tok/s → **qua RPC chậm hơn ~4× so với 3060 và ~7× so với 5070**. Nguyên nhân khả dĩ `[SUY LUẬN]`: mỗi token cần nhiều lượt đi–về RPC; RTT Wi-Fi 2–4 ms nhân lên thành ~40 ms/token. Chưa tách riêng băng thông và độ trễ.
- Có một yêu cầu chỉ đạt 1,0 tok/s trong lần `4,6` (Wi-Fi giật); độ biến thiên lớn.
- Nạp 4B: 1,09 GiB sang Windows mất 14,6 phút lần đầu. Cache `-c` của rpc-server giảm còn 3,3 phút nhưng không về 0.
- VRAM trên Windows (cho công thức `own` của worker RPC, Pha 1.5): log của master ghi buffer RPC0 = 1 090 (trọng số) + 304 (KV) + 156 (compute) = 1 550 MiB. Lần nạp đầu `memory.used` tăng 1 715 MiB (71 → 1 786), tức hơn buffer **165 MiB**; **sau khi dừng master, `memory.used` vẫn ở 236 MiB** (rpc-server giữ ~165 MiB, nhiều khả năng ngữ cảnh CUDA, không nhả). Các lần nạp sau tính từ mức nền mới 236 MiB: lần `1,1` nạp lại +1 552 MiB (so với 1 550 theo log), lần `4,6` +1 258 MiB (so với 1 256 theo log): chênh ~2 MiB. Vậy `own ≈ tổng buffer RPC trong log của master` khi so với mức nền ngay trước khi nạp; ~165 MiB là chi phí cố định một lần của tiến trình rpc-server.

### 4.2. Phát hiện quan trọng: thứ tự `--tensor-split`

Log `llama-server` ghi `using device RPC0 … then CUDA0`. Với `--tensor-split 4,6`: RPC0 (RTX 5070) nhận **40%** (859,8 MiB), CUDA0 (RTX 3060) nhận **60%** (1 516 MiB). **Tức là thiết bị RPC đứng TRƯỚC thiết bị local trong tensor-split** — ngược với giả định trước đó. Đã sửa gateway (`llamaTensorSplit`): API vẫn nhận "primary rồi các member", gateway đảo thứ tự khi gửi cho master; có test và đột biến. Giới hạn: kiểm với 1 RPC server; thứ tự khi có ≥ 2 RPC server chưa kiểm.

### 4.3. Những khẳng định KHÔNG có bằng chứng

- **"24 GB VRAM + 94 GB RAM"**: RPC chỉ cho master dùng VRAM của GPU từ xa. `-ncmoe`/`--override-tensor` đưa trọng số sang RAM *của máy chạy `llama-server`*. Chưa có phép đo nào cho thấy RAM của Windows được dùng làm bộ nhớ expert `[CHƯA XÁC MINH]` (kế hoạch 1.7). Dung lượng thực: ≈ 22 GB VRAM (11 783 + 11 034 MiB trống `[ĐO]`) + RAM master.
- **"Model 70B–235B"**: không có model nào trong `.aicf-work/`, và 70B ~43 GB không vừa 22 GB VRAM cộng RAM master còn trống ~23 GB một cách thoải mái; 235B (85–140 GB) không vừa. Chưa thử, không nên hứa.
- **30B qua RPC**: chưa chạy. `[SUY LUẬN]`: ~9,3 GB sang Windows ở ~2,2 MB/s ≈ 70 phút cho lần nạp đầu. Chạy lại sau khi cắm Ethernet.
- **"Bấm 1 nút … ngay lập tức"**: lần nạp RPC đã thấy tốn 3–15 phút cho 4B. "Tách" về chế độ 1/2 thì nhanh hơn (model local nạp ~2 s khi file đã nằm trong cache hệ thống), nhưng thời gian chuyển end-to-end qua gateway chưa đo.
- **Kết luận chế độ 3: cơ chế hoạt động (đã chứng minh với 4B), nhưng chưa dùng được cho mục tiêu đã nêu** trên mạng Wi-Fi; cần Ethernet và phép đo lại với 30B.

## 5. Kiểm thử phần mềm điều phối

- `make verify-release-v1` exit 0 ở lần chạy trước khi sửa thứ tự `tensor_split`; sau khi sửa chỉ chạy lại `make test-go` (xanh, gofmt sạch), **chưa chạy lại toàn bộ `verify-release-v1`**. Test điều phối dùng worker giả (không phải 2 máy thật): thứ tự member→master, chờ `rpc-ready`, rollback, 409/400, cụm 3 node, router loại worker RPC, đảo `tensor_split`.
- End-to-end qua gateway trên 2 máy thật: đã chạy ở ngày 28/10, xem mục 7 (trong đó có 4 lỗi thật tìm ra và đã sửa).

## 6. Việc cần làm tiếp (theo mức ưu tiên)

1. Cắm Ethernet cho máy Linux, đo lại băng thông; chạy lại mục 4 với **Qwen3-30B-A3B** (nạp nguội/ấm, `tensor_split` quét `1,1`, `4,6`, `6,4`) và Qwen3-8B.
2. Chép `Qwen3-30B-A3B` sang Windows (sau khi có Ethernet), quét N cho RTX 5070, rồi đo "2 worker chạy 30B song song".
3. Cập nhật `worker-guard.exe` lên Windows, chạy E2E qua gateway cho cả 3 chế độ và kịch bản lỗi (rút mạng, kill rpc-server, thu hồi GPU).
4. Quyết định D-TOPO (kênh điều khiển) và làm `llama_commit`/directive trong `gateway.proto`.
5. Sửa mô tả sản phẩm/UI: bỏ "94 GB RAM", "70B–235B", "`50–70 tok/s`", "`-ncmoe 18`" cho tới khi có số đo ủng hộ.

## 7. Bổ sung 28/10: model 30B trên RTX 5070, so sánh model (có DeepSeek), E2E qua gateway

Mọi số dưới đây `[ĐO]` trừ khi ghi khác. Mạng vẫn là Wi-Fi (cáp Ethernet chưa cắm), nên **chế độ 3 (merged) với 30B chưa chạy**.

### 7.1. Qwen3-30B-A3B trên RTX 5070 (Windows, `llama-server`, `-c 8192`, 1 slot, `-fa on`)

Dữ liệu: `p3_ncmoe_sweep_rtx5070.jsonl`, log: `logs/rtx5070/`. File 30B chép sang Windows qua Wi-Fi, SHA256 hai đầu giống nhau (`0d003f66…`).

| `-ncmoe` | Tok/s (3 lần) | VRAM đỉnh (MiB, tổng 12 227) |
|---|---|---|
| 16 | 21,6 – 21,8 | 11 898 |
| 18 | 13,5 – 13,6 | 11 902 |
| 20 | 64,1 – 66,2 | 11 850 |
| 22 | 60,0 – 61,9 | 11 152 |
| 26 | 53,6 – 55,8 | 9 808 |

- Không có OOM ở N=16 và 18 nhưng tốc độ **sụp** (13–22 tok/s) trong khi VRAM đỉnh gần kín (~11,9 GB). `[SUY LUẬN]`: Windows/WDDM không báo hết VRAM mà tràn sang bộ nhớ chia sẻ; chưa kiểm chứng bằng cách khác. Trên Linux cùng tình huống báo OOM (mục 3). **Hệ quả: không chọn N chỉ bằng "không OOM" trên Windows, phải nhìn tok/s.**
- N=20 là điểm nhanh nhất đo được trên 5070 (~65 tok/s), so với RTX 3060 ở N=20 là ~41 tok/s (mục 3) → 5070 nhanh hơn ~60%.
- Worker-guard Windows dùng `--headroom-mib 1536`. Engine ở N=20 chiếm 11 154 MiB nên còn trống 867 MiB < 1536, và worker-guard **tự gỡ engine của chính nó** (sự kiện `reclaim` trong log). Muốn chạy qua worker-guard với headroom đó phải dùng N đủ lớn: N=26 (đỉnh 9 808 MiB) chạy ổn. Đây là sự đánh đổi giữa tốc độ và việc chừa VRAM cho người dùng, không phải lỗi.

### 7.2. Hai worker cùng chạy 30B (smart_1gpu `-ncmoe 26` trên cả hai card), qua gateway

2 luồng mỗi máy × 2 vòng × 128 token, 0 yêu cầu lỗi. Tổng 87,0 tok/s (thời gian chạy chỉ 11,8 s nên mẫu ngắn):

| Worker | Tok/s mỗi luồng (trung vị) |
|---|---|
| pc-win-01 (RTX 5070) | 54,6 |
| pc-001 (RTX 3060) | 22,4 |

Dữ liệu: `logs/p3_e2e_smart_both_load.json`. Một yêu cầu chat đơn lẻ đo được 49,1 tok/s (5070, N=26) và 46,6 (N=28).

### 7.3. So sánh model trên RTX 3060 (bộ 20 câu cố định, greedy, ngân sách 3 000 token)

Dữ liệu: `p3_models_quiz.jsonl`, script `benchmarks/hardware/quiz_models.py`. **20 câu không phải benchmark**: chỉ phát hiện chênh lệch lớn.

| Model | Đúng | Tok/s mỗi luồng | VRAM đỉnh (MiB) | Token sinh | Thời gian cả bộ |
|---|---|---|---|---|---|
| Qwen3-4B Q4_K_M | 18/20 | 91,0 | 3 759 | 16 260 | 91 s |
| Qwen3-8B Q4_K_M | 20/20 | 58,7 | 5 867 | 15 714 | 139 s |
| DeepSeek-R1-Distill-Qwen-14B Q4_K_M | 18/20 | 33,8 | 9 965 | 6 789 | 105 s |
| Qwen3-30B-A3B Q4_K_M (`-ncmoe 20`) | 20/20 | 25,5 | 11 705 | 16 959 | 339 s |
| DeepSeek-V2-Lite-Chat Q3_K_M | 11/20 | 9,8 | 10 069 | 3 394 | 178 s |

Giới hạn cần biết:
- Bộ chấm lấy **số cuối cùng** trong câu trả lời, nên có thể chấm sai câu trả lời đúng nhưng có thêm số phía sau. Với V2-Lite thấy ít nhất 1 trường hợp như vậy ("năm nhuận có 366 ngày" bị tính sai). Tôi chỉ lưu 120 ký tự đầu của mỗi câu trả lời nên không kiểm lại được toàn bộ.
- DeepSeek-R1-14B sai 2 câu: một câu trả lời "Hanoi" bằng tiếng Anh cho câu hỏi tiếng Việt (bộ chấm tìm "hà nội"), một câu đếm ký tự chưa xác minh được.
- V2-Lite chỉ có bản Q3 (đĩa Linux chỉ còn ~10 GB trống), nên **không so sánh công bằng về lượng tử hóa** với các model Q4. Có một chuỗi ký tự lỗi trong đầu ra ("nh�aim"). Số 11/20 không nên dùng để kết luận về DeepSeek-V2-Lite nói chung.
- V2-Lite có tốc độ bất thường: `llama-bench` trên cùng card cho 127,5 tok/s với `-fa 0` và 63,0 tok/s với `-fa 1` (2 lần lặp), trong khi bộ câu hỏi (server, `-fa on`, 2 slot) chỉ 9,8 tok/s. Lý do chưa điều tra.
- DeepSeek-R1-14B không nhanh hơn hay đúng nhiều hơn Qwen3-8B trong bài này, mà chậm gần gấp đôi và nặng gần gấp đôi VRAM. Nó viết ít token hơn (6 789 so với 15 714) nên thời gian cả bộ (105 s) ngắn hơn 8B (139 s).
- Model DeepSeek lớn hơn (V3, R1 đầy đủ) không chạy được trên 2 card 12 GB, nên không thử.

### 7.4. E2E qua gateway trên 2 máy thật (`POST /api/cluster/topology`, `benchmarks/hardware/e2e_topology.py`)

| Bước | Thời gian đổi | Kết quả |
|---|---|---|
| split, 8B (pc-001) | 36,5 s | 63,0 tok/s |
| smart_1gpu 30B N=20 (pc-001) | 24,5 s | 33,6 tok/s |
| split 4B (pc-001) | 14,0 s | 100,2 tok/s |
| smart_1gpu 30B N=26 (cả hai) | 27,4 s | pc-001 23,4 / pc-win-01 48,8 tok/s |
| smart_1gpu cùng model N 26→28 (pc-win-01, sau sửa) | 15,0 s | 46,6 tok/s |
| split 4B (cả hai) | 13,1 s | 102,1 / 173,6 tok/s |
| merged Qwen3-0.6B (Wi-Fi bận) | 312 s, **thất bại**, rollback về split | tự phục hồi, split 12,5 s sau đó |

Kịch bản lỗi:
- **Master không lên trong hạn → rollback tự động**: hoạt động (bước merged 0.6B). Nguyên nhân master không lên chưa xác minh; `[SUY LUẬN]` là băng thông Wi-Fi đang bị lần chép 30B chiếm, vì worker-guard chỉ chờ 180 s cho engine khởi động.
- **Kill -9 `llama-server` trên Linux**: worker-guard ghi `crash` và tự khởi động lại sau **13,2 s** (gồm cửa sổ ổn định 10 s).
- **Chưa làm**: kill `ggml-rpc-server`, rút mạng khi đang merged, chiếm VRAM khi đang merged, merged với 30B.

### 7.5. Lỗi thật tìm ra khi chạy E2E (đã sửa, có test, mutation-test đã bắt)

1. **Gateway gọi sai cổng điều khiển**: `engine_url` là cổng suy luận (llama-server) nên `/api/engine` trả 404 và cả 3 bước đầu lỗi 500. Test giả không bắt được vì worker giả phục vụ cả hai việc ở một cổng. Sửa: cờ `-worker-control-port` (mặc định 8099).
2. **Rollback không thực sự khôi phục**: sau khi worker-guard Windows gỡ engine 30B (xem 7.1), nó nhớ footprint cũ (11 154 MiB) và đòi 11 154 + 1 536 MiB trống, lớn hơn cả tổng VRAM, nên engine 4B không bao giờ khởi động lại. Worker kẹt ở `stopped`, `switching=true`, trong khi gateway báo "previous configuration restored". Sửa: reset footprint khi đổi cấu hình (`TestSwitchForgetsPreviousFootprintSoSmallerModelCanStart`; bỏ phần sửa thì test fail).
3. **Báo thành công từ heartbeat cũ**: đổi chỉ `-ncmoe` (cùng tên model) làm gateway trả 200 sau 0,6 s trong khi engine còn đang nạp lại (chat nhận 503). Sửa: chỉ chấp nhận heartbeat nhận sau khi worker nhận lệnh (`TestSmartSameModelNewNWaitsForAFreshHeartbeat`; đã mutation-test; chạy lại thật: 15 s và chat thành công).
4. **Giới hạn khởi động engine cố định 180 s** thấp hơn giới hạn chờ 300 s của gateway. Sửa: cờ `--engine-start-timeout` (mặc định vẫn 180 s).

`make test-go` xanh sau các sửa này. `make verify-release-v1` chưa chạy lại sau lượt sửa này.

### 7.6. Còn lại sau mục 7

- Cắm Ethernet (hoặc nối dây trực tiếp Linux–Windows), đo lại băng thông, chạy merged với 4B/8B/30B và các kịch bản lỗi còn lại ở 7.4. Bật `--engine-log` để biết vì sao master không lên.
- Kênh điều khiển gateway→worker vẫn là HTTP không xác thực (D-TOPO chưa quyết định).
- Chưa thử giao diện web trong trình duyệt thật.

## 8. Bổ sung 28/10 (08:00): Cắm cáp Ethernet 2,5 GbE trực tiếp (`10.42.0.1` ↔ `10.42.0.225`) — Đo lại Chế độ 3 (`merged` GGML RPC) `[ĐO]`

Sau khi nối cáp Ethernet trực tiếp giữa cổng `enp133s0` trên Linux (`10.42.0.1/24`) và cổng 2.5GbE trên Windows (`10.42.0.225/24`):
- **Băng thông TCP 1 luồng (`tcp_bench.py`):** **2,372 Gbps (282,77 MB/s)** — tăng **~95 lần** so với Wi-Fi (~25 Mbps).
- **Chuyển đổi E2E qua Gateway (`POST /api/cluster/topology`, `logs/p3_e2e_g_merged4b.json`, `logs/p3_e2e_h_merged30b.json`) và tải đa luồng (`modes_load.py`, `logs/p3_merged*_cable_c*.json`):**

| Model (Chế độ 3 `merged` qua cáp 2,5 GbE) | `--tensor-split` | Thời gian chuyển mode (`switch_s`) | 1 luồng (`c=1`, trung vị; min–max) | 2 luồng (`c=2`, tổng / mỗi luồng) | 4 luồng (`c=4`, tổng / mỗi luồng) | Lỗi |
|---|---|---:|---|---|---|---:|
| `Qwen3-4B-Q4_K_M.gguf` | `4,6` | **32,3 s** | **117,7 tok/s** (116,6–119,6) | — | **200,5 tok/s tổng** (108,2/luồng) | 0 |
| `Qwen3-30B-A3B-Q4_K_M.gguf` (18,56 GB, **100% VRAM**, `-ncmoe 0`) | `1,1` | **86,7 s** | **126,3 tok/s** (125,7–127,6) | **176,2 tok/s tổng** (92,4/luồng) | **172,8 tok/s tổng** (91,4/luồng) | 0 |

- **Phân bổ VRAM thực tế khi gộp 2 card chạy `Qwen3-30B-A3B-Q4_K_M` (`-c 8192 -np 2 -ngl 99 --tensor-split 1,1`):**
  - `pc-001` (RTX 3060 12 GB — Master `CUDA0`): chiếm **9 168 MiB** (`UsedMiB: 9191 / 12288 MiB`, còn trống **2 720 MiB**).
  - `pc-win-01` (RTX 5070 12 GB — Member `RPC0`): giữ nửa số tầng còn lại trên VRAM. Toàn bộ 48 tầng (Attention + toàn bộ Expert MoE) nằm **100% trên VRAM của 2 GPU**, không phải đẩy tầng nào xuống CPU RAM (`-ncmoe 0`).
  - Vì `Qwen3-30B-A3B` chỉ kích hoạt 3B tham số mỗi token và kích thước vector kích hoạt chuyển qua mạng mỗi bước rất nhỏ, đường truyền cáp 2,5 GbE giúp tốc độ sinh 1 luồng đạt **126,3 tok/s** (nhanh gấp **3,1×** so với Smart 1-GPU trên RTX 3060 là ~41 tok/s và nhanh gấp **~2×** so với Smart 1-GPU trên RTX 5070 là ~65 tok/s), đồng thời 2 luồng song song đạt **176,2 tok/s tổng** (gấp đôi tổng thông lượng của hai máy chạy Smart 1-GPU N=26 song song là 87,0 tok/s).
- **Tự phục hồi khi tiến trình `llama-server` (merged master) bị ngắt đột ngột:** sự kiện `crash` trong `worker-guard` lúc `08:05:41` và `08:22:34` đều được `worker-guard` tự động khởi động lại thành công ở chế độ `merged` sau **30,5 s** và **42,4 s**.
