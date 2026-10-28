# BÁO CÁO CUỐI — CỤM 2 GPU (RTX 3060 Linux + RTX 5070 Windows), 3 chế độ — 2026-10-28

Nhãn: **[ĐO]** đã chạy và có file dữ liệu; **[SUY LUẬN]** hợp lý nhưng chưa kiểm chứng; **[CHƯA LÀM]**. Chi tiết, log gốc và các lần đo trung gian: `P3_BAO_CAO_3_CHE_DO.md` (mục 7), thư mục `logs/`.

## 0. Kết luận trong 1 phút

1. **Cả 3 chế độ chạy được qua gateway trên 2 máy thật** (split, smart_1gpu, merged), có rollback tự động khi lỗi và tự phục hồi khi sập engine/mất mạng.
2. **Merged (gộp 2 card) chỉ đáng dùng khi có mạng nhanh.** Qua Wi-Fi (~20–25 Mbps) Qwen3-4B chậm hơn một card 4–7 lần. Qua dây trực tiếp 2,5 GbE (đo 2,37 Gbps, RTT 0,15 ms) merged chạy Qwen3-30B-A3B **~124–128 tok/s**, so với **~36 tok/s (3060)** và **~49 tok/s (5070)** khi một card phải đẩy chuyên gia sang RAM (`-ncmoe`) ở ngữ cảnh 32k.
3. **Chia 1:1 "không được" là do chính sách chừa VRAM + phần Windows chiếm sẵn, không phải phần cứng**: bỏ worker-guard thì 1:1 ở 32k chạy tốt (122–128 tok/s). Xem mục 4.
4. Model 32B dày và model ~200B **chưa đo** (mục 7).

## 1. Môi trường

| | Linux `pc-001` | Windows `pc-win-01` |
|---|---|---|
| GPU | RTX 3060, 12 288 MiB (trống ~11 783 MiB khi rảnh) | RTX 5070, 12 227 MiB; `ggml-rpc-server` báo 12 199 MiB, **trống 11 034 MiB khi rảnh** |
| RAM | 31 GB (Chrome chiếm nhiều) | 61,6 GB |
| headroom của worker-guard | 512 MiB | **1 536 MiB** (dịch vụ Windows) |
| llama.cpp | b11913 (`7aca0d2ea`) CUDA sm_86 + RPC | cùng commit, CUDA sm_120 + RPC |

Mạng [ĐO]: Wi-Fi ~20–25 Mbps, RTT 2,5 ms. Dây trực tiếp (`10.42.0.1` ↔ `10.42.0.225`): 2,376 Gbps Linux→Win và 2,372 Gbps Win→Linux (283 MB/s mỗi chiều), RTT 0,15 ms.
Model chính: Qwen3-30B-A3B Q4_K_M, 18,56 GB, 30,53 tỉ tham số, 128 chuyên gia, 8 chuyên gia/token (đọc từ log llama.cpp).

## 2. Chế độ 1 — Tách (split): mỗi máy một worker độc lập [ĐO]

`llama-server -np 4 -c 16384`, 1 hoặc 4 luồng đồng thời, 0 yêu cầu lỗi:

| Model | 3060 (1 / 4 luồng) | 5070 (1 / 4 luồng) | Cả hai máy cùng lúc (tổng) |
|---|---|---|---|
| Qwen3-4B Q4_K_M | 100,4 / 273,6 | 167,2 / 499,1 | 202,0 / 554,9 |
| Qwen3-8B Q4_K_M | 63,2 / 178,0 | 110,9 / 349,7 | 126,6 / 358,6 |

Mô tả "335 tok/s" trong tài liệu cũ không khớp số đo nào ở trên. Qua gateway, chat 4B trả 101 (3060) và 173 (5070) tok/s.

## 3. Chế độ 2 — Smart 1-GPU (`-ncmoe N`, chuyên gia sang RAM) với Qwen3-30B-A3B [ĐO]

Mỗi ô là trung vị của 5 lần chạy 128 token (`p3_final_3060.jsonl`, `p3_final_5070.jsonl`), 1 luồng, `llama-server -fa on`:

| Card | Ngữ cảnh | `-ncmoe` | Tok/s (min–max) | VRAM đỉnh (MiB) |
|---|---|---|---|---|
| 3060 | 8k | 20 | **44,0** (42,7–45,0) | 11 707 |
| 3060 | 32k | 30 | **35,9** (35,2–36,2) | 10 597 |
| 5070 | 8k | 20 | **65,6** (63,5–66,1) | 11 691 |
| 5070 | 8k | 26 | 54,2 (52,6–54,7) | 9 825 |
| 5070 | 32k | 30 | **49,2** (47,1–49,4) | 10 757 |

Các điểm đo trước (3 lần): 3060 `-ncmoe 18` hết VRAM; 3060 32k `-ncmoe 24` hết VRAM; 5070 `-ncmoe 16/18` không báo lỗi nhưng chỉ 13–22 tok/s với VRAM gần kín (`[SUY LUẬN]`: tràn bộ nhớ chia sẻ của WDDM). **Trên Windows phải chọn N theo tok/s, không theo "không OOM".**

Hai worker cùng chạy 30B (`-ncmoe 26`, 2 luồng mỗi máy, mẫu ngắn 11,8 s): tổng 87,0 tok/s; 5070 54,6, 3060 22,4 tok/s mỗi luồng; 0 lỗi.
Lưu ý: `-ncmoe 20` trên 5070 chiếm ~11,2 GB nên bị worker-guard (headroom 1 536 MiB) tự gỡ; qua worker-guard phải dùng N ≥ 26.

## 4. Chế độ 3 — Merged (GGML RPC, 3060 làm master, 5070 làm thành viên RPC)

### 4.1. Kết quả [ĐO]

| Điều kiện | Model | Nạp | Tok/s 1 luồng | Ghi chú |
|---|---|---|---|---|
| Wi-Fi | Qwen3-4B | 879 s (lạnh), 197 s (cache) | 22–27 | chậm hơn 1 card 4–7×; 1 yêu cầu chỉ 1 tok/s |
| Wi-Fi | Qwen3-0.6B qua gateway | master không lên sau 5 phút | — | rollback tự động; nguyên nhân chưa xác minh |
| **Dây** | Qwen3-4B | 32,3 s | 117,7 (4 luồng tổng 200,5) | |
| **Dây** | Qwen3-30B-A3B | 86,7 s | **126,3** (2 luồng tổng 176,2; 4 luồng 172,8) | VRAM 9 191 / 9 879 MiB, không cần `-ncmoe`, 0 lỗi |
| **Dây, ngữ cảnh 32k** | Qwen3-30B-A3B | — | **121–127** (tỷ lệ 44:56, 3 lần); **122,5–128,2** (1:1 không worker-guard, 5 lần, trung vị 127,6) | |

### 4.2. So sánh trực tiếp Qwen3-30B-A3B, 1 luồng [ĐO]

| | 8k | 32k |
|---|---|---|
| 3060 một card (tốt nhất) | 44,0 | 35,9 |
| 5070 một card (tốt nhất; 8k dùng N=20, vượt headroom dịch vụ) | 65,6 | 49,2 |
| **Merged 2 card qua dây** | ~126 | **~124–128** |

Ở 32k merged nhanh ~2,5× card đơn nhanh nhất (5070) và ~3,5× 3060. `[SUY LUẬN]` lý do: merged giữ toàn bộ trọng số trên VRAM, còn card đơn phải chạy chuyên gia trên CPU.

### 4.3. Vì sao chia đều (1:1) "không được" ở 32k — giải thích có số đo

Hai card có VRAM gần bằng nhau, nhưng dùng được thì không bằng nhau:

- **5070 bị Windows/driver chiếm sẵn ~1,1 GB**: `ggml-rpc-server` báo 12 199 MiB tổng, 11 034 MiB trống khi rảnh (3060: ~11 783 MiB trống).
- **Việc chia theo lớp không đều tuyệt đối**: ở tỷ lệ 1:1 3060 nhận 8 549 (trọng số) + 1 472 (KV) + 224 (compute) ≈ 10,2 GB, còn 5070 nhận 8 976 + 1 600 + 264 ≈ 10,8 GB.
- **Dịch vụ Windows chừa 1 536 MiB** cho người dùng (3060 chỉ chừa 512). Khi 5070 còn trống 1 330 MiB (1:1) hoặc 1 462 MiB (47:53), worker-guard **kill `ggml-rpc-server`** và master sập.
- **Kiểm chứng**: chạy rpc-server thủ công, không worker-guard, chia 1:1: **chạy được**, 122,5–128,2 tok/s; VRAM Windows đỉnh 11 163 MiB (còn trống ~1 064 MiB, thấp hơn 1 536) — tức đúng là chính sách chừa trống, không phải phần cứng, chặn cấu hình này.
- Chia nghiêng về 3060 (4:6) thì 3060 hết VRAM (10,3 GB trọng số). Chỉ một khoảng hẹp (44:56) thỏa cả hai giới hạn khi vẫn giữ headroom.

Hệ quả: **`tensor_split` phải được tính từ VRAM trống thật của từng card, không nên mặc định chia đều.** Hiện gateway mặc định `1,1,…` (chia đều); đây là việc cần cải thiện.

## 5. So sánh model (RTX 3060, 20 câu cố định, greedy, tối đa 3 000 token) [ĐO]

20 câu **không phải benchmark chuẩn**; chỉ phát hiện chênh lệch lớn. Bộ chấm lấy số cuối cùng trong câu trả lời nên có thể chấm sai câu đúng.

| Model | Đúng | Tok/s/luồng | VRAM đỉnh (MiB) | Thời gian cả bộ |
|---|---|---|---|---|
| Qwen3-4B Q4_K_M | 18/20 | 91,0 | 3 759 | 91 s |
| Qwen3-8B Q4_K_M | 20/20 | 58,7 | 5 867 | 139 s |
| DeepSeek-R1-Distill-Qwen-14B Q4_K_M | 18/20 | 33,8 | 9 965 | 105 s |
| Qwen3-30B-A3B Q4_K_M (`-ncmoe 20`) | 20/20 | 25,5 (2 luồng) | 11 705 | 339 s |
| DeepSeek-V2-Lite-Chat Q3_K_M | 11/20 | 9,8 | 10 069 | 178 s |

- DeepSeek-R1-14B không nhanh hơn hay đúng hơn Qwen3-8B (chậm gần gấp đôi, nặng gần gấp đôi VRAM), nhưng viết ít token hơn.
- V2-Lite chỉ có bản Q3 (đĩa đầy); có ít nhất một câu bị chấm sai oan; tốc độ bất thường (`llama-bench`: 127,5 tok/s với `-fa 0`, 63,0 với `-fa 1`; bộ câu hỏi chỉ 9,8) chưa điều tra. **Không kết luận được về DeepSeek-V2-Lite nói chung.**
- Chưa chạy bộ câu hỏi trên cấu hình merged.

## 6. Độ tin cậy của cơ chế điều phối [ĐO]

| Kịch bản | Kết quả |
|---|---|
| Đổi chế độ qua gateway | 13–37 s (merged 4B 32 s, 30B 87 s) |
| Master không lên đúng hạn | rollback tự động về cấu hình cũ |
| `kill -9 llama-server` (Linux) | tự khởi động lại sau 13,2 s |
| Kill `ggml-rpc-server` khi merged | yêu cầu kế tiếp lỗi (master chỉ phát hiện khi có yêu cầu); sau đó Windows khởi động lại rpc-server ~12 s, master tự nạp lại 30,5 s, 123 tok/s, 0 lỗi |
| Mất mạng 25 s (tắt card dây Windows) | worker offline ~20 s rồi tự online; chat treo/lỗi trong lúc đứt; master tự sập rồi tự khởi động lại; sau đó 0 lỗi, 124,5 tok/s |

**Lỗi thật tìm ra khi chạy máy thật, đã sửa, có test và mutation-test:** (1) gateway gọi sai cổng điều khiển (mọi bước 500); (2) worker-guard quên footprint model cũ nên rollback không khởi động lại engine nhỏ; (3) gateway báo thành công giả khi chỉ đổi `-ncmoe`; (4) giới hạn khởi động engine cứng 180 s (nay có `--engine-start-timeout`). `make test-go` xanh.

## 7. Chưa làm / hạn chế — cần biết khi đọc số liệu

- **Qwen3-32B dày**: đang tải trên Windows (6,22 / 19,76 GB), tốc độ ~1 MB/s do internet của máy Linux. **Chưa có số đo.**
- **Model ~200B**: không vừa 24 GB VRAM + RAM khả dụng của master (RPC không gộp RAM hai máy); không thử.
- Các bài đo 32k dùng prompt ngắn (~36 token): cấp phát KV 32k nhưng **chưa đo tốc độ khi ngữ cảnh thật sự đầy** (prefill và giải mã với ngữ cảnh dài).
- Bài merged 8k/32k dùng 1 luồng; merged nhiều luồng chỉ đo ở ngữ cảnh mặc định.
- Số của 3060 `-ncmoe 20` lúc đầu là ~41 tok/s (3 lần) và lần này 44,0 (5 lần): biến thiên giữa các lần chạy vài tok/s.
- Số đo Wi-Fi bị ảnh hưởng bởi việc chép/tải model chạy cùng lúc.
- Kênh điều khiển gateway→worker vẫn là HTTP không xác thực (quyết định D-TOPO chưa chốt); công thức `own`/`engine_mib` của worker RPC sai (báo 256 MiB khi thực tế ~10 GB). Giao diện web đã kiểm bằng Chrome thật (`tests/e2e/topology_e2e.mjs`, 11/11 PASS → `p3_adaptive_topology_and_large_model_e2e.json`); `make verify-release-v1` đã chạy lại đạt `exit 0`. Quét tỷ lệ cho `Qwen3-Coder-30B-A3B-Instruct` xem thêm ở `P3_CODER_30B_GOP_2_CARD.md` (8k 58:42 đạt 132,4 tok/s; 32k 52:48 đạt 126,7 tok/s).
- Mạng: Windows hiện không có Wi-Fi, nối gateway qua dây; IP `10.42.0.225` do DHCP cấp nên có thể đổi.
