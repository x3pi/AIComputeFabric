# BÁO CÁO ĐO ĐỦ 164 BÀI HUMANEVAL: NÊN CHẠY PHÂN TÁN (2 CARD) HAY 1 CARD?

- **Ngày đo:** 2026-11-06 → 2026-11-07
- **Cụm phần cứng:** `pc-001` (Linux, RTX 3060 12 GB, i5-12400F) + `pc-win-01` (Windows 11, RTX 5070 12 GB), nối Ethernet 2.5 GbE trực tiếp, RTT ~0.35 ms.
- **Động cơ:** `llama.cpp` build 11913 qua `site-gateway` + `worker-guard` (ngưỡng headroom 512 MiB), ngữ cảnh 8192, 2 slot, KV `f16`.
- **Mã đo:** `benchmarks/hardware/full164_distributed_benchmark.py` · **bảng tự sinh:** `benchmarks/hardware/build_full164_tables.py` · **dữ liệu thô:** `benchmarks/reports/full164/*.json` (kèm `*.answers.jsonl` là câu trả lời gốc của model).
- Nhãn: `[ĐO]` = đo trực tiếp lần này; `[SUY RA]` = suy luận từ số đo, chưa đo riêng; `[ĐO CŨ]` = lấy từ báo cáo trước, không đo lại.

---

## 1. Kết luận trong một trang

**Câu hỏi:** với mỗi model, nên chạy `merged` (gộp 2 card qua mạng) hay chạy trên 1 card?

**Trả lời ngắn, theo kích thước model** `[ĐO]`:

| Tình huống | Chọn | Bằng chứng đo được |
|:--|:--|:--|
| Model **vừa thoải mái 1 card** (động cơ ≲ 9,000 MiB VRAM): Qwen3-4B/8B, GLM-4-9B, deepseek-coder-6.7B, Kimi-9B | **1 card (`split`)** | Gộp 2 card chỉ nhanh hơn **+12% đến +19%** khi dùng 1 người, nhưng TTFT chậm hơn **1.4–3.1 lần**, tốn điện hơn **5–11%**/token (chỉ còn **−1% đến +10%** nếu cộng điện của card thứ hai đang rảnh vào cấu hình 1 card, xem mục 7), nạp lâu hơn **1.8–2.5 lần**, và chiếm cả 2 máy. Chất lượng không đổi. |
| Model **MoE 30B** (17–18 GB, không vừa 1 card): Coder-30B, Qwen3-30B-A3B, GLM-4.7-Flash | **`merged` (2 card)** | So với 1 card + đẩy expert sang RAM (`smart_1gpu`): decode nhanh **2.4–3.1 lần**, prefill nhanh **4.5–6.0 lần**, TTFT thấp hơn **~3 lần**, điện/token **thấp hơn 12–14%**. Chất lượng không đổi (154 = 154 bài với Coder-30B). |
| Model **dense 27–32B** (Qwen3.8-27B, GLM-4-32B) | **`merged` là lựa chọn duy nhất** | Chạy được (20–24 tok/s) nhưng tốn **~10–11 J/token**, gấp ~6–7 lần Coder-30B. Chỉ dùng khi cần độ chính xác cao nhất và chấp nhận chậm. |
| Model **sát giới hạn 1 card** (DeepSeek-R1-14B, động cơ 9,809 MiB) | **`merged`** | Trên 1 card chỉ còn dư **53 MiB** trên ngưỡng headroom; `worker-guard` đã thu hồi VRAM làm engine dừng **2 lần** trong 164 bài. `merged` chạy trọn vẹn, 0 lần dừng, và nhanh hơn 18%. |

**Chọn model theo bài toán** (xem bảng đầy đủ ở mục 3):

- **Cân bằng tốt nhất:** `Qwen3-Coder-30B-A3B` ở `merged`: **154/164 (93.9%)**, 124.8 tok/s, 1.61 J/token, **2.2 giây và 382 J cho mỗi bài đúng**.
- **Chính xác nhất:** `Qwen3.8-27B` (158/164, 96.3%) nhưng **12.5 giây và 2,455 J mỗi bài đúng**, tức đắt gấp ~6 lần Coder-30B. Do khoảng tin cậy 95% của hai model chồng lên nhau (89–97% so với 92–98%), **chưa thể kết luận Qwen3.8-27B giỏi hơn thật**.
- **Tránh dùng cho code tương tác:** `DeepSeek-R1-14B` (72–74%, 43–47 bài bị cắt do suy luận quá dài, **~13.9–15.6 kJ mỗi bài đúng**, gấp ~36–41 lần Coder-30B) `DeepSeek-R1-32B IQ4_XS` (75%, 41 bài bị cắt, **~25.8 kJ mỗi bài đúng**, tệ nhất bảng) và `DeepSeek-V2-Lite` (Q4: 52%, Q3: 40%).

---

## 2. Những thông số cần có để đánh giá khả năng chạy phân tán

Đây là bộ chỉ số tôi đề xuất dùng cho **mọi model mới** trước khi quyết định `merged` hay 1 card. Cột "Ngưỡng" rút ra từ dữ liệu của chính cụm này, không phải ngưỡng chung của ngành.

| # | Chỉ số | Đo bằng gì | Ngưỡng/diễn giải từ dữ liệu này |
|:-:|:--|:--|:--|
| 1 | **Vừa 1 card không?** | `trọng số + KV + compute` so với **~9,860 MiB** khả dụng (12,288 − ~1,914 MiB hệ điều hành/desktop − 512 MiB headroom) | Vượt ngưỡng → phải phân tán hoặc đẩy sang RAM. Sát ngưỡng (R1-14B dư 53 MiB) → không bền vững. R1-32B bản Q4_K_M (19.9 GB) **không vừa cả hai card ở KV f16**: engine hết VRAM khi cấp bộ đệm tính toán trên card 3060 (đo lại bằng log), nên phải dùng bản IQ4_XS (17.7 GB). |
| 2 | **Tăng tốc giải mã** = decode(`merged`) ÷ decode(1 card) | `decode tok/s` p50 | **≥ ~2×** mới đáng gộp (MoE 30B: 2.4–3.1×). Model nhỏ chỉ 1.12–1.19× → không đáng. |
| 3 | **Tỷ lệ TTFT** và **tốc độ prefill prompt dài** | TTFT p50; prefill với prompt ~1,400 token | Gộp thêm độ trễ mạng vào mỗi prompt: TTFT nhân **1.4–3.1×** với model nhỏ. Với MoE 30B thì ngược lại, `merged` nhanh hơn vì 1 card phải để CPU xử lý expert. |
| 4 | **Năng lượng/token** (cả 2 GPU) | công suất `nvidia-smi` của **cả hai** máy, chỉ trong cửa sổ sinh | `merged` thắng khi 1 card phải dùng CPU (MoE 30B: −12–14%); thua khi 1 card đủ sức (model nhỏ: +5–11%; còn −1% đến +10% nếu tính cả card rảnh vào cấu hình 1 card). |
| 5 | **Nhu cầu mạng**: Mbit/s khi sinh và KB/token | bộ đếm NIC của `pc-001` | **44–493 Mbit/s**, **72–770 KB/token**. 2.5 GbE dùng tối đa ~20%; 1 GbE sẽ dùng tới ~49% ở deepseek-coder. |
| 6 | **Thời gian nạp lạnh** | giây từ lệnh đổi cấu hình đến khi `health=ok` | `merged` lâu hơn 1 card **1.8–2.5×** (model nhỏ: 29–42 s so với 13–25 s) vì phải truyền trọng số qua mạng. |
| 7 | **Chất lượng có giữ nguyên không** | Pass@1 hai chế độ + số bài đổi kết quả | Chênh ≤ 5 bài (nằm trong nhiễu của khoảng tin cậy ±6–7 điểm). Số bài đổi kết quả: 4–9/164 với model thường, 24–27/164 với model suy luận. |
| 8 | **Độ ổn định** | số lần engine dừng, bucket VRAM còn dư của từng card | `merged` chia đôi VRAM nên dư ≥ 3 GB mỗi card; 1 card có thể chỉ dư vài chục MiB. |
| 9 | **Mở rộng theo số người dùng** | thông lượng gộp với C=1 → C=2 | MoE 30B: `merged` 193 tok/s (C=2) so với 50 tok/s của 1 card (3.9×). Model nhỏ/vừa: chỉ 0.93–1.15× so với 1 card. |
| 10 | **Cấu hình có nạp được không** | ghi lỗi nạp/hết hạn | `DeepSeek-V2-Lite` ở `merged` không nạp được ở cả Q3 và Q4 (xem mục 5); R1-32B Q4_K_M không vừa VRAM (xem mục 6). |

> **Quy tắc rút gọn:** nếu `trọng số + KV + compute ≲ 9,000 MiB` thì chạy 1 card (hoặc 2 máy độc lập để tăng thông lượng). Nếu không vừa mà là MoE thì `merged`. Nếu dense lớn thì `merged` và chấp nhận chậm.

---

## 3. Bảng số liệu đầy đủ (164 bài, 25 cấu hình: 23 đo được và 2 không nạp được; mọi số đều từ file JSON)

**Cách đọc:** *Pass@1* là số bài đúng trên 164; *CI 95%* là khoảng tin cậy Wilson (khác biệt nhỏ hơn ~6–7 điểm phần trăm giữa hai dòng **chưa có ý nghĩa thống kê**). *Bài bị cắt* là số bài chạm trần token (1024 với model thường, 2048 với GLM-4.7-Flash, 3072 với model suy luận), kể cả bài vẫn đúng.

### 3.1. Chất lượng và hiệu năng

| Model | Chế độ | Pass@1 /164 | CI 95% | Bài bị cắt (chạm trần) | Decode p50 (tok/s) | TPOT p50 (ms) | TTFT p50 (ms) | Prefill prompt dài (tok/s) | J/token | J/bài đúng | Giây/bài đúng |
|:--|:--|--:|:--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Qwen3-Coder-30B-A3B | `merged` | **154** (93.9%) | 89–97% | 2 | 124.76 | 7.97 | 263.1 | 2745.53 | 1.6087 | 381.7 | 2.18 |
| Qwen3-Coder-30B-A3B | `smart_1gpu` | **154** (93.9%) | 89–97% | 2 | 40.08 | 24.83 | 743.7 | 460.47 | 1.8642 | 440.1 | 6.81 |
| Qwen3-30B-A3B | `merged` | **147** (89.63%) | 84–93% | 0 | 126.27 | 7.87 | 251.3 | 2810.65 | 1.6209 | 336.4 | 1.93 |
| Qwen3-30B-A3B | `smart_1gpu` | **146** (89.02%) | 83–93% | 0 | 41.49 | 23.95 | 727.0 | 570.33 | 1.8929 | 397.0 | 5.99 |
| GLM-4.7-Flash | `merged` | **130** (79.27%) | 72–85% | 0 | 94.77 | 10.49 | 231.9 | 2234.91 | 1.9803 | 506.4 | 2.99 |
| GLM-4.7-Flash | `smart_1gpu` | **135** (82.32%) | 76–87% | 0 | 39.3 | 25.33 | 766.2 | 499.3 | 2.2422 | 536.4 | 7.01 |
| Qwen3.8-27B-UD | `merged` | **158** (96.34%) | 92–98% | 4 | 23.65 | 42.06 | 1717.2 | 606.99 | 9.6992 | 2454.8 | 12.51 |
| GLM-4-32B-0414 | `merged` | **150** (91.46%) | 86–95% | 0 | 20.69 | 48.06 | 392.6 | 871.56 | 11.1815 | 2425.6 | 10.89 |
| DeepSeek-R1-Distill-Qwen-14B | `merged` | **118** (71.95%) | 65–78% | 47 | 40.58 | 24.6 | 1450.5 | 1546.34 | 5.3439 | 15565.6 | 74.33 |
| DeepSeek-R1-Distill-Qwen-32B-IQ4_XS | `merged` | **123** (75.0%) | 68–81% | 41 | 22.35 | 44.66 | 1724.4 | 982.44 | 9.8764 | 25791.4 | 119.24 |
| DeepSeek-R1-Distill-Qwen-14B | `split` | **121** (73.78%) | 67–80% | 43 | 34.3 | 29.11 | 483.1 | 1144.12 | 4.9774 | 13881.5 | 82.09 |
| Qwen3.5-9B-Kimi-k3-Distilled | `merged` | **133** (81.1%) | 74–86% | 22 | 63.55 | 15.72 | 715.9 | 1440.24 | 3.2685 | 4927.3 | 25.36 |
| Qwen3.5-9B-Kimi-k3-Distilled | `split` | **129** (78.66%) | 72–84% | 26 | 56.5 | 17.68 | 258.4 | 1936.27 | 3.0314 | 4980.2 | 29.47 |
| GLM-4-9B-0414 | `merged` | **136** (82.93%) | 76–88% | 0 | 59.64 | 16.58 | 161.3 | 2160.09 | 3.6387 | 460.7 | 2.31 |
| GLM-4-9B-0414 | `split` | **132** (80.49%) | 74–86% | 0 | 51.43 | 19.19 | 118.4 | 1844.76 | 3.4341 | 444.1 | 2.64 |
| Qwen3-8B | `merged` | **135** (82.32%) | 76–87% | 0 | 72.23 | 13.72 | 258.3 | 2334.16 | 3.0064 | 538.6 | 2.78 |
| Qwen3-8B | `split` | **134** (81.71%) | 75–87% | 0 | 62.99 | 15.75 | 129.2 | 2116.94 | 2.7903 | 499.8 | 2.99 |
| deepseek-coder-6.7b-instruct | `merged` | **129** (78.66%) | 72–84% | 0 | 84.01 | 11.83 | 843.3 | 2500.78 | 2.8255 | 705.0 | 4.06 |
| deepseek-coder-6.7b-instruct | `split` | **128** (78.05%) | 71–84% | 0 | 70.79 | 14.03 | 269.3 | 2196.87 | 2.5363 | 641.4 | 3.93 |
| Qwen3-4B | `merged` | **125** (76.22%) | 69–82% | 1 | 111.97 | 8.87 | 216.0 | 3686.29 | 1.8206 | 407.8 | 2.3 |
| Qwen3-4B | `split` | **128** (78.05%) | 71–84% | 0 | 100.26 | 9.91 | 88.9 | 3666.94 | 1.736 | 382.0 | 2.31 |
| DeepSeek-V2-Lite-Chat Q3 | merged | **KHÔNG NẠP ĐƯỢC** | — | — | — | — | — | — | — | — | — |
| DeepSeek-V2-Lite-Chat | merged | **KHÔNG NẠP ĐƯỢC** | — | — | — | — | — | — | — | — | — |
| DeepSeek-V2-Lite-Chat Q3 | `smart_1gpu` | **66** (40.24%) | 33–48% | 3 | 24.98 | 39.77 | 460.3 | 659.35 | 2.4474 | 1133.5 | 20.91 |
| DeepSeek-V2-Lite-Chat | `smart_1gpu` | **86** (52.44%) | 45–60% | 1 | 29.27 | 33.8 | 578.4 | 503.3 | 2.4906 | 503.4 | 8.8 |

### 3.2. Tài nguyên, điện, mạng, nạp, đồng thời

*VRAM 3060/5070* là bộ nhớ do engine báo (trọng số + KV + compute), chưa tính ~1.9 GB hệ điều hành/desktop trên mỗi card. *Công suất* là trung bình trong cửa sổ sinh. *Byte mạng/token* chia theo cả token prompt và token sinh. *Với `split`/`smart_1gpu` đây là nhiễu nền* (≤ 3 KB/token), dùng làm mốc đối chứng.

| Model | Chế độ | Nạp (s) | VRAM 3060 (MiB) | VRAM 5070 (MiB) | RAM host mmap (MiB) | Công suất 3060 / 5070 (W) | Mạng khi đang sinh (Mbit/s) | Byte mạng / token | CPU host (%) | C=1 → C=2 (tok/s gộp) |
|:--|:--|--:|--:|--:|--:|:--:|--:|--:|--:|:--:|
| Qwen3-Coder-30B-A3B | `merged` | 53.871 | 8,678 | 9,879 | 167 | 107.6 / 67.1 | 155 | 96,706 | 10.2 | 110.58 → 193.06 (1.75×) |
| Qwen3-Coder-30B-A3B | `smart_1gpu` | 13.164 | 9,496 | 0 | 108 | 64.6 / 2.4 | 0 | 36 | 29.8 | 32.25 → 49.56 (1.54×) |
| Qwen3-30B-A3B | `merged` | 71.89 | 8,678 | 9,879 | 167 | 107.3 / 67.0 | 169 | 96,951 | 16.5 | 111.59 → 195.48 (1.75×) |
| Qwen3-30B-A3B | `smart_1gpu` | 22.713 | 9,496 | 0 | 108 | 66.3 / 2.1 | 0 | 25 | 27.5 | 38.26 → 61.69 (1.61×) |
| GLM-4.7-Flash | `merged` | 78.244 | 8,741 | 9,208 | 170 | 105.3 / 64.2 | 93 | 72,191 | 11.2 | 89.41 → 139.76 (1.56×) |
| GLM-4.7-Flash | `smart_1gpu` | 19.724 | 9,283 | 0 | 108 | 76.5 / 11.2 | 1 | 1,798 | 24.0 | 36.27 → 61.2 (1.69×) |
| Qwen3.8-27B-UD | `merged` | 58.977 | 8,036 | 7,896 | 682 | 121.7 / 74.6 | 227 | 769,732 | 13.4 | 20.52 → 36.02 (1.76×) |
| GLM-4-32B-0414 | `merged` | 80.311 | 9,278 | 10,048 | 500 | 132.0 / 90.8 | 44 | 145,249 | 11.4 | 20.36 → 39.31 (1.93×) |
| DeepSeek-R1-Distill-Qwen-14B | `merged` | 44.385 | 4,954 | 5,154 | 418 | 127.4 / 82.0 | 70 | 204,930 | 12.3 | 39.12 → 77.81 (1.99×) |
| DeepSeek-R1-Distill-Qwen-32B-IQ4_XS | `merged` | 87.402 | 9,254 | 9,854 | 394 | 129.7 / 86.6 | 46 | 238,366 | 11.2 | 21.91 → 43.26 (1.97×) |
| DeepSeek-R1-Distill-Qwen-14B | `split` | 24.719 | 9,809 | 0 | 418 | 169.1 / 10.8 | 1 | 2,923 | 14.3 | 34.7 → 67.58 (1.95×) |
| Qwen3.5-9B-Kimi-k3-Distilled | `merged` | 42.189 | 2,875 | 2,638 | 546 | 122.7 / 71.6 | 90 | 162,179 | 11.0 | 54.61 → 91.61 (1.68×) |
| Qwen3.5-9B-Kimi-k3-Distilled | `split` | 17.73 | 5,277 | 0 | 546 | 169.0 / 10.8 | 1 | 2,169 | 13.4 | 54.66 → 98.04 (1.79×) |
| GLM-4-9B-0414 | `merged` | 39.483 | 3,076 | 3,159 | 333 | 122.0 / 77.8 | 115 | 97,017 | 10.7 | 58.25 → 111.24 (1.91×) |
| GLM-4-9B-0414 | `split` | 15.943 | 5,975 | 0 | 333 | 168.1 / 11.5 | 1 | 771 | 13.0 | 51.02 → 97.69 (1.91×) |
| Qwen3-8B | `merged` | 37.798 | 2,920 | 3,047 | 334 | 119.4 / 74.3 | 188 | 158,468 | 8.9 | 66.59 → 131.17 (1.97×) |
| Qwen3-8B | `split` | 20.67 | 5,715 | 0 | 334 | 167.1 / 2.1 | 0 | 13 | 10.3 | 61.94 → 117.7 (1.90×) |
| deepseek-coder-6.7b-instruct | `merged` | 29.154 | 3,674 | 4,589 | 71 | 101.7 / 71.8 | 493 | 408,742 | 12.4 | 64.38 → 139.16 (2.16×) |
| deepseek-coder-6.7b-instruct | `split` | 12.927 | 8,018 | 0 | 71 | 163.3 / 2.0 | 0 | 10 | 15.3 | 66.42 → 127.07 (1.91×) |
| Qwen3-4B | `merged` | 28.697 | 1,855 | 1,940 | 304 | 109.1 / 68.5 | 219 | 132,656 | 8.5 | 98.7 → 202.57 (2.05×) |
| Qwen3-4B | `split` | 15.121 | 3,609 | 0 | 304 | 165.5 / 2.0 | 0 | 10 | 13.3 | 97.83 → 187.21 (1.91×) |
| DeepSeek-V2-Lite-Chat Q3 | `smart_1gpu` | 21.694 | 7,548 | 0 | 272 | 54.2 / 2.9 | 9 | 24,550 | 17.2 | 26.95 → 16.65 (0.62×) |
| DeepSeek-V2-Lite-Chat | `smart_1gpu` | 18.177 | 7,694 | 0 | 319 | 57.2 / 5.9 | 0 | 67 | 24.8 | 29.1 → 15.82 (0.54×) |

### 3.3. So sánh trực tiếp `merged` với 1 card (cùng model)

Tỷ lệ > 1 nghĩa là `merged` lớn hơn. Với *Tốc độ giải mã*, *Prefill dài* và *C=2*, lớn hơn là tốt. Với *TTFT* và *J/token*, lớn hơn là tệ.

| Model | So sánh | Tốc độ giải mã | TTFT p50 | J/token | Prefill dài | Pass@1 | Số bài khác kết quả | Tăng tốc theo C=2 |
|:--|:--|--:|--:|--:|--:|--:|--:|--:|
| Qwen3-Coder-30B-A3B | merged vs `smart_1gpu` | **3.11×** (40.08 → 124.76) | 0.35× (743.7 → 263.1) | 0.86× (1.8642 → 1.6087) | 5.96× | 154 → 154 | 4/164 | 3.90× |
| Qwen3-30B-A3B | merged vs `smart_1gpu` | **3.04×** (41.49 → 126.27) | 0.35× (727.0 → 251.3) | 0.86× (1.8929 → 1.6209) | 4.93× | 146 → 147 | 5/164 | 3.17× |
| GLM-4.7-Flash | merged vs `smart_1gpu` | **2.41×** (39.3 → 94.77) | 0.30× (766.2 → 231.9) | 0.88× (2.2422 → 1.9803) | 4.48× | 135 → 130 | 7/164 | 2.28× |
| DeepSeek-R1-Distill-Qwen-14B | merged vs `split` | **1.18×** (34.3 → 40.58) | 3.00× (483.1 → 1450.5) | 1.07× (4.9774 → 5.3439) | 1.35× | 121 → 118 | 27/164 | 1.15× |
| Qwen3.5-9B-Kimi-k3-Distilled | merged vs `split` | **1.12×** (56.5 → 63.55) | 2.77× (258.4 → 715.9) | 1.08× (3.0314 → 3.2685) | 0.74× | 129 → 133 | 24/164 | 0.93× |
| GLM-4-9B-0414 | merged vs `split` | **1.16×** (51.43 → 59.64) | 1.36× (118.4 → 161.3) | 1.06× (3.4341 → 3.6387) | 1.17× | 132 → 136 | 4/164 | 1.14× |
| Qwen3-8B | merged vs `split` | **1.15×** (62.99 → 72.23) | 2.00× (129.2 → 258.3) | 1.08× (2.7903 → 3.0064) | 1.10× | 134 → 135 | 5/164 | 1.11× |
| deepseek-coder-6.7b-instruct | merged vs `split` | **1.19×** (70.79 → 84.01) | 3.13× (269.3 → 843.3) | 1.11× (2.5363 → 2.8255) | 1.14× | 128 → 129 | 5/164 | 1.10× |
| Qwen3-4B | merged vs `split` | **1.12×** (100.26 → 111.97) | 2.43× (88.9 → 216.0) | 1.05× (1.736 → 1.8206) | 1.01× | 128 → 125 | 9/164 | 1.08× |

### 3.4. Nguyên nhân các bài không đạt

| Model | Chế độ | Pass | Sai logic (AssertionError) | Lỗi cú pháp/chạy | Bị cắt khi sinh | Token sinh/bài | Prompt token/bài |
|:--|:--|--:|--:|--:|--:|--:|--:|
| Qwen3-Coder-30B-A3B | `merged` | 154 | 8 | 0 | 2 | 222.8 | 187.2 |
| Qwen3-Coder-30B-A3B | `smart_1gpu` | 154 | 8 | 0 | 2 | 221.7 | 187.2 |
| Qwen3-30B-A3B | `merged` | 147 | 15 | 2 | 0 | 186.0 | 191.2 |
| Qwen3-30B-A3B | `smart_1gpu` | 146 | 16 | 2 | 0 | 186.7 | 191.2 |
| GLM-4.7-Flash | `merged` | 130 | 32 | 2 | 0 | 202.7 | 177.5 |
| GLM-4.7-Flash | `smart_1gpu` | 135 | 28 | 1 | 0 | 196.9 | 177.5 |
| Qwen3.8-27B-UD | `merged` | 158 | 2 | 0 | 4 | 243.8 | 200.0 |
| GLM-4-32B-0414 | `merged` | 150 | 13 | 1 | 0 | 198.4 | 178.5 |
| DeepSeek-R1-Distill-Qwen-14B | `merged` | 118 | 2 | 0 | 44 | 2095.8 | 177.2 |
| DeepSeek-R1-Distill-Qwen-32B-IQ4_XS | `merged` | 123 | 1 | 0 | 40 | 1958.6 | 177.2 |
| DeepSeek-R1-Distill-Qwen-14B | `split` | 121 | 0 | 2 | 41 | 2057.7 | 174.8 |
| Qwen3.5-9B-Kimi-k3-Distilled | `merged` | 133 | 7 | 2 | 22 | 1222.6 | 198.0 |
| Qwen3.5-9B-Kimi-k3-Distilled | `split` | 129 | 6 | 3 | 26 | 1292.3 | 198.0 |
| GLM-4-9B-0414 | `merged` | 136 | 25 | 3 | 0 | 105.0 | 178.5 |
| GLM-4-9B-0414 | `split` | 132 | 29 | 3 | 0 | 104.1 | 178.5 |
| Qwen3-8B | `merged` | 135 | 25 | 4 | 0 | 147.5 | 191.2 |
| Qwen3-8B | `split` | 134 | 26 | 4 | 0 | 146.4 | 191.2 |
| deepseek-coder-6.7b-instruct | `merged` | 129 | 31 | 4 | 0 | 196.3 | 285.9 |
| deepseek-coder-6.7b-instruct | `split` | 128 | 32 | 4 | 0 | 197.4 | 285.9 |
| Qwen3-4B | `merged` | 125 | 30 | 8 | 1 | 170.7 | 191.2 |
| Qwen3-4B | `split` | 128 | 29 | 7 | 0 | 171.7 | 191.2 |
| DeepSeek-V2-Lite-Chat Q3 | `smart_1gpu` | 66 | 70 | 25 | 3 | 186.4 | 203.2 |
| DeepSeek-V2-Lite-Chat | `smart_1gpu` | 86 | 65 | 12 | 1 | 106.0 | 203.2 |

---

## 4. Những điều số liệu cho thấy

1. **Phân tán không làm model giỏi hơn hay dở hơn.** 9 cặp so sánh, chênh Pass@1 ≤ 5 bài, và đều nằm trong khoảng tin cậy. Lợi ích của `merged` chỉ là *chạy được model lớn* và *chạy nhanh hơn khi 1 card phải dùng CPU*.
2. **Với MoE 30B, `merged` thắng toàn diện so với "1 card + CPU".** Coder-30B: decode 124.8 so với 40.1 tok/s (3.1×), prefill 2,746 so với 460 tok/s (6.0×), TTFT 263 so với 744 ms, 1.61 so với 1.86 J/token. Nút thắt của 1 card là CPU và RAM xử lý expert (CPU host 24–30% so với ~9–17% ở `merged`).
3. **Với model nhỏ, gộp card là lãng phí.** Ví dụ Qwen3-8B: 72.2 so với 63.0 tok/s (+15%) nhưng TTFT 258 so với 129 ms, và chiếm cả 2 máy trong khi 1 máy đủ chạy. `[SUY RA]` Chạy 2 bản độc lập trên 2 máy cho thông lượng cụm xấp xỉ gấp đôi 1 card, chưa đo lại lần này (báo cáo cũ có số đo).
4. **Tải mạng khá lớn nhưng 2.5 GbE vẫn dư.** `merged` dùng 44–493 Mbit/s khi sinh. Model nhỏ nhưng nhanh (deepseek-coder 84 tok/s) dùng nhiều nhất (493 Mbit/s) vì nhiều token/giây, mỗi token đều phải qua mạng. `[ĐO CŨ]` Với mạng 100 Mbps, Coder-30B tụt ~27% tốc độ, nhất quán với việc nhu cầu thực tế vượt xa 100 Mbps.
5. **Độ ổn định là chỉ số thật sự khác biệt ở ngưỡng 12 GB.** DeepSeek-R1-14B `split`: bucket VRAM của `pc-001` sau khi khởi động lại gồm hệ điều hành 1,914 + trọng số 8,148 + KV 1,536 + compute 125 + headroom 512 MiB, chỉ còn **~53 MiB** dư. `worker-guard` (cơ chế bảo vệ người dùng D07) thu hồi VRAM và engine dừng **2 lần** trong lần chạy cuối (bài 68 và 122; lần chạy trước đó cũng dừng ở bài 23 nên đã bị loại). Chính mã nguồn xác nhận điều kiện kích hoạt: free VRAM < `--headroom-mib`. Nguyên nhân chính xác của từng lần dừng không còn trong log (bị ghi đè), nên đây là kết luận *có bằng chứng gián tiếp mạnh*, không phải chứng minh trực tiếp.
6. **Model suy luận dài ("thuế suy luận") làm hỏng bài đo bằng ngân sách token.** R1-14B sinh trung bình ~2,060–2,100 token/bài (so với ~200 của model thường), 43–47 bài chạm trần 3072; R1-32B ~1,960 token/bài, 41 bài chạm trần, và **đúng 122/123 bài nó tự kết thúc** (99.2%), nên điểm 75% của nó gần như hoàn toàn bị giới hạn bởi ngân sách token. Kimi-9B: ~1,220–1,290 token/bài, 22–26 bài bị cắt. Điểm của hai model này **là cận dưới**: tăng ngân sách có thể cải thiện, nhưng slot ngữ cảnh chỉ 4096 nên tôi không thử được.
7. **Số đo cũ bằng 8–12 bài đã sai lệch rõ:**

| Model | Báo cáo cũ (8–12 bài) | Lần này (164 bài) |
|:--|:--|:--|
| deepseek-coder-6.7B | 100% (8/8) | 78.05% / 78.66% |
| Kimi-k3-9B | 87.5% / 100% (7–8/8) | 78.66% / 81.1% |
| DeepSeek-R1-14B | 37.5% (ngân sách 896) | 73.78% / 71.95% (ngân sách 3072) |
| DeepSeek-V2-Lite Q3 `smart_1gpu` | 62.5% (5/8) | 40.24% |
| DeepSeek-V2-Lite Q4 `smart_1gpu` (đo bổ sung) | không có | 52.44% |
| Coder-30B | 91.67% (11/12) | 93.9% |

   Chênh lệch này đến từ cỡ mẫu nhỏ, ngân sách token cũ quá thấp, và lỗi chấm bài (xem mục 6).
8. **Điều chỉnh một nhận định của báo cáo cũ.** Báo cáo cũ viết họ kiến trúc `deepseek2` "không tương thích với GGML RPC". Lần này **GLM-4.7-Flash** (tệp GGUF khai báo kiến trúc `deepseek2`) chạy `merged` qua RPC bình thường (130/164, 94.8 tok/s). Vậy lỗi `merged` của `DeepSeek-V2-Lite` là của **riêng checkpoint/cấu hình đó**, không phải của cả họ kiến trúc. Tôi đo bổ sung bản **Q4_K_M** (10.4 GB) để loại trừ nguyên nhân do lượng tử hóa: nó cũng **không nạp được ở `merged`** (cùng lỗi hết hạn 5 phút ở gateway: `master pc-001 did not start ... timed out after 5m0s`). Quan sát được: engine nạp xong tensor trên card 3060 và cấp bộ nhớ RPC cho card 5070 rồi `llama-server` thoát ngay, không in thông báo lỗi; rpc-server bên Windows chỉ ghi `recv returned 0 (peer closed?)`. Tức tiến trình phía Linux tự kết thúc. **Nguyên nhân gốc vẫn chưa tìm ra**; lần chạy thủ công để bắt lỗi bị nhiễu bởi engine của gateway nên không dùng làm bằng chứng. Riêng `smart_1gpu` thì chạy bình thường: Q4 đạt 86/164 (52.4%) so với Q3 66/164 (40.2%), nên lượng tử hóa Q3 làm mất khoảng 20 bài.

---

## 5. Hai phát hiện về cách vận hành cụm (nên sửa trong sản phẩm)

1. **GGUF `deepseek-coder-6.7b-instruct` không có chat template.** `llama-server` rơi về ChatML, nên khi gọi qua `/v1/chat/completions` model **không bao giờ dừng**: 148/164 bài chạm trần 1024 token, tốc độ và năng lượng mỗi bài bị phóng đại, Pass@1 tụt còn 59.8%. Khi dùng đúng khuôn của DeepSeek-Coder qua `/completion`, model dừng đúng chỗ (hàm `add` thử nghiệm: 27 token) và đạt ~78%. Gợi ý: gắn chat template riêng cho từng model khi nạp, hoặc phát hiện model thiếu template và cảnh báo.
2. **Chuyển cấu hình hỏng có thể kẹt gateway.** Khi `smart_1gpu` của Coder-30B với `-ncmoe 20` hết VRAM, lệnh chuyển quá hạn 240 giây, gateway kẹt ở `switching` ~5 phút rồi sang `error`, trong lúc mọi lệnh khác bị từ chối 409. Cần có cơ chế tự phục hồi hoặc hủy chuyển đổi. `[ĐO]` `-ncmoe 26` chạy ổn (đúng với ghi chú tối ưu đã lưu từ trước; `-ncmoe 20` không vừa).

---

## 6. Phương pháp và những gì đã sửa so với bài đo cũ

| Điểm yếu của bài đo cũ | Cách khắc phục lần này |
|:--|:--|
| 8–12 bài/cấu hình | **Đủ 164 bài** cho mọi cấu hình đo được |
| Ngân sách token 384/896 cắt cụt đáp án | 1024 (thường), 2048 (GLM-4.7-Flash), 3072 (suy luận); ghi lại số bài chạm trần |
| Chấm thiếu hàm phụ trong đề (ví dụ `is_palindrome`) | Chương trình chấm = prompt gốc + đáp án của model |
| Công suất máy Windows ước lượng cứng 85 W | Đo thật `nvidia-smi` của RTX 5070 qua SSH, **chỉ trong các cửa sổ sinh token** |
| Chỉ đo 1 người dùng | Thêm thông lượng C=1 và C=2, prefill prompt dài ~1,400 token |
| Không đo mạng/CPU | Đếm byte NIC và CPU host trong lúc chạy |
| Không kiểm tra bộ chấm | **Kiểm tra đột biến:** đáp án chuẩn đạt 164/164, hàm rỗng (`pass`) đạt 0/164 |
| Không lưu đáp án | Lưu đáp án gốc `*.answers.jsonl` (từ lần chạy deepseek-coder trở đi) để chấm lại |

Cấu hình đo: `temperature=0`, `seed=1234`, `cache_prompt=false`. Model Qwen3 và GLM-4.7-Flash chạy với *thinking tắt* (`enable_thinking=false`) để so sánh công bằng ở chế độ "viết code trực tiếp". Tỷ lệ chia tầng `merged` cố định 48:52 (chưa tinh chỉnh riêng từng model). `smart_1gpu` dùng `-ncmoe 26` cho hai model 30B và `-ncmoe 10` cho V2-Lite.

**Đã loại bỏ và lưu lại ở `full164/_v1_discarded/`:** lần chạy deepseek-coder sai khuôn prompt (59.8%) và lần chạy R1-14B `split` bị dừng ở bài 23. Với R1-32B Q4_K_M: bản `merged` KV f16 không nạp được (hết VRAM khi cấp bộ đệm tính toán, hoặc hết hạn 5 phút khi ổ đĩa đang bị lượt tải model khác chiếm), còn bản KV `q8_0` nạp được nhưng **sinh chuỗi rác** (0/20 bài đúng, hỏi trực tiếp engine cho ra "11. . of of..."), nên đã bỏ và không đưa vào bảng; dấu vết lưu ở `full164/_failed_load/`. Chưa chứng minh được nguyên nhân là KV `q8_0` qua RPC. Kết quả R1-32B trong bảng là bản **IQ4_XS** với KV `f16` chuẩn (nhẹ hơn Q4_K_M 2.2 GB, ảnh hưởng tới điểm không tách riêng được).

---

## 7. Hạn chế cần biết trước khi dùng các con số

1. **Mỗi cấu hình chỉ chạy 1 lần.** Khoảng tin cậy Wilson chỉ phản ánh sai số lấy mẫu trên 164 bài, không phản ánh dao động giữa các lần chạy. `merged` và 1 card cho ra 4–9 bài khác kết quả dù cùng trọng số và `temperature=0`; điều này cho thấy tính không tất định số học khi chia tầng giữa hai GPU khác thế hệ. `[SUY RA]` đây là nguồn gây chênh, chưa kiểm chứng.
2. **Điện chỉ tính GPU.** Với `smart_1gpu`, CPU làm phần việc của expert (CPU host 24–30%) nhưng điện CPU/RAM **không được đo**, nên J/token của `smart_1gpu` thực tế **cao hơn** số hiển thị và lợi thế điện của `merged` còn lớn hơn. Ngược lại, `merged` có thêm điện của card thứ hai và bộ nguồn, cũng không đo riêng. Card 5070 nhàn rỗi (2–12 W) không được tính vào các cấu hình 1 card; nếu cộng vào thì chênh lệch J/token giữa `merged` và 1 card của 5 model nhỏ đổi từ +5–11% thành **−1% đến +10%** (GLM-4-9B và Kimi-9B gần như bằng nhau), tức kết luận "gộp tốn điện hơn" yếu hơn con số ghi ở mục 1. Cách tính nào đúng phụ thuộc card thứ hai có đang làm việc khác hay không. **Kiểm chứng độc lập `[ĐO]`:** trong lúc sinh token liên tục với Coder-30B `merged`, so cách lấy mẫu `nvidia-smi` 500 ms (dùng trong bài đo) với bộ đếm năng lượng NVML của chính GPU: card 3060 lệch −1.9% (109.6 W so với 111.7 W, 45 giây), card 5070 lệch +0.1% (70.9 W so với 70.8 W, 40 giây). Chỉ kiểm một mức tải; cảm biến công bố sai số ±5 W mỗi card; chưa có đồng hồ đo công suất ở ổ cắm nên điện tại tường vẫn chưa đo.
3. **Quy tắc trích code đổi giữa chừng.** 8 cấu hình đầu (Coder, Qwen3-30B-A3B, Qwen3-4B/8B) dùng quy tắc "lấy khối code cuối" (v1), phần còn lại dùng "lấy khối đầu tiên định nghĩa hàm" (v2). Tôi chạy lại Coder-30B `merged` bằng v2 và so từng bài: **0 bài khác biệt**, không câu trả lời nào có nhiều khối hàm. Với model dừng đúng, quy tắc không ảnh hưởng, nhưng tôi mới kiểm chứng trên Coder-30B.
4. **Model suy luận chịu giới hạn ngân sách** (mục 4, điểm 6): điểm của R1-14B, R1-32B và Kimi-9B là cận dưới.
5. **GLM-4.7-Flash đo ở chế độ không suy luận.** Model này mạnh hơn khi bật thinking, nên 79–82% có thể đánh giá thấp khả năng thật của nó.
6. **Chưa đo:** RTX 5070 chạy một mình; nhiều hơn 2 người dùng đồng thời (engine chỉ có 2 slot); ngữ cảnh dài; mạng 1 GbE/100 Mbps (số liệu mạng chậm lấy từ báo cáo cũ); tinh chỉnh tỷ lệ chia tầng cho từng model; lặp lại nhiều lần.
7. **Cụm có người dùng khác** (Studio, embedder, reranker chạy trên CPU) nên có nhiễu nền nhỏ.
