# BẢNG SỐ LIỆU CUỐI — CỤM 2 GPU (RTX 3060 + RTX 5070) — 2026-10-28

Mọi số dưới đây là **[ĐO]** (có file dữ liệu trong `benchmarks/reports/`), trừ khi ghi **[SUY LUẬN]**. Đơn vị tốc độ: **tok/s** = token sinh ra mỗi giây cho **một yêu cầu** (một luồng). Sai số giữa các lần chạy: khoảng ±3 tok/s.

## 1. Thông tin model dùng để đo chính

| Mục | Giá trị |
|---|---|
| Tên | **Qwen3-30B-A3B**, lượng tử hóa **Q4_K_M** (GGUF) |
| Kiến trúc | `qwen3moe` — **MoE (chuyên gia)**: 128 chuyên gia, mỗi token chỉ dùng 8 |
| Tham số | 30,53 tỉ (tổng); ~3,3 tỉ hoạt động mỗi token (theo tên A3B) |
| Kích thước file | 18,56 GB → **lớn hơn VRAM 12 GB của mỗi card**, vừa trong tổng 24 GB |
| Cấu hình đo | `llama-server -fa on -ngl 99`, 1 luồng, 128 token sinh, 5 lần mỗi ô, trung vị |
| Máy | Linux `pc-001`: **RTX 3060 12 GB** · Windows `pc-win-01`: **RTX 5070 12 GB** · nối bằng **dây trực tiếp 2,5 GbE** (đo 2,37 Gbps, RTT 0,15 ms) |

## 2. BẢNG CHÍNH — Qwen3-30B-A3B: chạy riêng từng card so với gộp 2 card

Mỗi cột "mức tăng" so với **card đơn nhanh nhất ở cùng ngữ cảnh**.

### Ngữ cảnh 8k (`-c 8192`)

| Cách chạy | Cấu hình | Tok/s | VRAM dùng | So với card đơn nhanh nhất |
|---|---|---|---|---|
| ① 3060 chạy riêng | `-ncmoe 20` (chuyên gia sang RAM) | **44,0** | 11 707 MiB | 0,67× |
| ② 5070 chạy riêng (nhanh nhất) | `-ncmoe 20` | **65,6** | 11 691 MiB | 1,00× (mốc) |
| ②' 5070 chạy riêng (chừa VRAM an toàn) | `-ncmoe 26` | 54,2 | 9 825 MiB | 0,83× |
| ③ Hai card, mỗi máy một bản chạy độc lập* | `-ncmoe 26` mỗi máy | 5070: 54,6 · 3060: 22,4 (mỗi luồng); **tổng 87,0** | — | tổng 1,3× |
| ④ **Gộp 2 card (merged)** | chia 5070:3060 = **58:42** | **≈135** (136,7 và 133,9 ở hai lần đo) | 3060: 7 751 · 5070: ~11 400 MiB | **2,1×** |

\* Ô ③ đo với 2 luồng đồng thời mỗi máy, tổng 4 yêu cầu; không so sánh trực tiếp từng luồng với ô ①②④.

### Ngữ cảnh 32k (`-c 32768`) — "khó" hơn: KV cache lớn gấp 4

| Cách chạy | Cấu hình | Tok/s | VRAM dùng | So với card đơn nhanh nhất |
|---|---|---|---|---|
| ① 3060 chạy riêng | `-ncmoe 30` | **35,9** | 10 597 MiB | 0,73× (`-ncmoe 24` hết VRAM) |
| ② 5070 chạy riêng | `-ncmoe 30` | **49,2** | 10 757 MiB | 1,00× (mốc) |
| ④ **Gộp 2 card (merged)** | chia **50:50** | **≈132** (131,7; 127,6 khi đo không qua worker-guard) | 3060: 10 407 MiB | **2,7×** |

**Cách đọc:** card đơn phải đẩy một phần chuyên gia sang RAM/CPU nên chậm; gộp hai card giữ **toàn bộ trọng số trên VRAM** (không `-ncmoe`) nên nhanh gấp ~2–3,7 lần. Mức tăng càng lớn khi ngữ cảnh càng dài.

```
Qwen3-30B-A3B, ngữ cảnh 32k (tok/s)
3060 riêng    ███████████▌                     35,9
5070 riêng    ███████████████▊                 49,2
GỘP 2 CARD    ██████████████████████████████████████████▍   131,7
```


## 2b. PROMPT DÀI — ngữ cảnh đầy thật (Qwen3-30B-A3B, `-c 32768`, 1 luồng, 64 token sinh) [ĐO]

Prompt khác nhau mỗi lần (tắt bộ nhớ đệm prompt), số token đếm bằng chính server. **Prefill** = tốc độ đọc prompt (tok/s); **Sinh** = tốc độ sinh khi ngữ cảnh đã đầy (tok/s). Mỗi ô là **1 lần chạy** (không lặp), nên sai số lớn hơn các bảng trước.

| Độ dài prompt (token) | 3060 riêng (`-ncmoe 30`) | 5070 riêng (`-ncmoe 30`) | **Gộp 2 card (50:50)** |
|---|---|---|---|
| ~1 000 | prefill 251 · sinh 33,6 | prefill 367 · sinh 47,2 | prefill 1 981 · sinh **116,3** |
| ~4 000 | 667 · 33,0 | 802 · 47,4 | 3 293 · **110,2** |
| ~8 000 | 697 · 32,5 | 905 · 47,3 | 3 223 · **95,0** |
| ~16 000 | 666 · 28,2 | 853 · 45,0 | 2 830 · **84,3** |
| ~30 000 | 603 · 26,2 | 832 · 40,1 | 2 290 · **62,2** |
| Thời gian xử lý prompt 30k + 64 token | 53,0 s | 37,9 s | **17,9 s** |

```
Tốc độ SINH khi prompt dài (tok/s)            1k     8k     30k
3060 riêng   ███████████▎ 33,6  ██████████▉ 32,5  ████████▊ 26,2
5070 riêng   ███████████████▊ 47,2  ███████████████▊ 47,3  █████████████▍ 40,1
GỘP 2 CARD   ██████████████████████████████████████▊ 116,3  ███████████████████████████████▋ 95,0  ████████████████████▋ 62,2
```

Nhận xét:
- **Tốc độ sinh giảm khi prompt dài, và gộp giảm nhanh hơn card đơn:** gộp từ 116 (1k) xuống 62 (30k), giảm ~47%; card đơn giảm ~22% (3060: 33,6→26,2) và ~15% (5070: 47,2→40,1). `[SUY LUẬN]`: attention trên ngữ cảnh dài tốn thêm thời gian trên cả hai card và phần KV cache nằm trên hai card phải đồng bộ qua mạng; chưa phân tích riêng từng nguyên nhân.
- **Dù giảm, gộp vẫn nhanh nhất ở mọi độ dài:** ở 30k, sinh 62,2 so với 40,1 (5070) và 26,2 (3060), tức 1,6× và 2,4×.
- **Prefill gộp nhanh gấp 3–4,5 lần card đơn** (ví dụ 4k: 3 293 so với 802 và 667 tok/s), nên với prompt dài thì **tổng thời gian chờ** giảm nhiều nhất: xử lý prompt 30k trong 17,9 s thay vì 37,9 s (5070) hoặc 53,0 s (3060). `[SUY LUẬN]`: prefill ở card đơn bị chậm do phần chuyên gia chạy trên CPU.
- Số prefill ở prompt ~1k thấp hơn các độ dài khác ở mọi cấu hình vì prompt ngắn chưa đủ để khởi động GPU hết tốc độ (một lần đo, không lặp).
- Đây là prompt dạng danh sách bản ghi lặp có cấu trúc; chưa thử văn bản tự nhiên hay kiểm tra chất lượng câu trả lời với ngữ cảnh dài.

## 3. Tìm tỷ lệ chia tối ưu khi gộp (5070 : 3060) — vì sao 1:1 không phải tốt nhất

Phần trăm = phần trọng số giao cho **RTX 5070** (card có băng thông bộ nhớ cao hơn).

| Phần của 5070 | Ngữ cảnh 8k (tok/s) | Ngữ cảnh 32k (tok/s) |
|---|---|---|
| 35% | 121 | — |
| 40% | 121 | — |
| 45% | 126 | — |
| 50% | 128 | **131,7** |
| 52% | — | 128,8 |
| 54% | — | **55,3 (sụp)** |
| 55–56% | 130 (55%), 130,7 (56%) | 84,7 (56%, không ổn định) |
| 57% | 126,8 | — |
| **58%** | **134–137 (tốt nhất)** | 44,1 (sụp) |
| 59% | 134,7 | — |
| 60% | 133,5 | — |
| 62% | **45,5 (sụp)** | — |
| 65% | **~35 (sụp)** | — |

- Chia nghiêng về 5070 thì nhanh dần (băng thông cao hơn), nhưng **có "vách" về dung lượng**: vượt phần VRAM dùng được của 5070 thì tốc độ rớt mạnh (không báo lỗi). `[SUY LUẬN]` cơ chế: Windows đẩy phần thừa sang bộ nhớ chia sẻ trong RAM, đọc qua PCIe chậm hơn nhiều.
- **Vách đến sớm hơn khi ngữ cảnh dài**: ở 8k vách nằm giữa 60% và 62%, ở 32k giữa 52% và 54% (KV cache chiếm thêm chỗ).
- **Khuyến nghị chừa lề an toàn:** 8k → **58:42**; 32k → **50:50**. Không nên chọn sát vách.
- 5070 chỉ còn ~11,0 GB trống khi rảnh (Windows và driver chiếm sẵn ~1,1 GB), còn 3060 ~11,8 GB; cộng thêm việc chia theo lớp không đều tuyệt đối nên 1:1 không "công bằng" về VRAM thực tế.

## 4. Các tham số khác đã thử (ngữ cảnh 8k, tại 60:40)

| Tham số | Tok/s | Kết luận |
|---|---|---|
| Mặc định | 133,5 | mốc |
| KV cache `q8_0` | 120,9 | **chậm hơn ~9%**, không dùng |
| `-t 4` / `-t 12` (luồng CPU) | 130,3 / 130,0 | không khác biệt (trong sai số) |
| `-ub 1024` (batch) | 130,1 | không khác biệt |

## 5. Còn bao nhiêu dư địa? (ước lượng, không phải số đo)

`[SUY LUẬN]` Tốc độ giải mã bị giới hạn bởi băng thông bộ nhớ. Với ~2,0 GB dữ liệu đọc mỗi token (3,3 tỉ tham số × ~4,8 bit) và băng thông theo thông số nhà sản xuất (5070 ≈ 672 GB/s, 3060 ≈ 360 GB/s, **chưa tự đo**): chia lý tưởng (~65:35) cho trần khoảng **250 tok/s**. Đo được **~135 tok/s ≈ 54% trần**. Phần còn thiếu đến từ đồng bộ giữa hai card qua mạng, chi phí tính attention/định tuyến chuyên gia và cấp phát. Và **chia lý tưởng 65:35 không thực hiện được vì chạm vách VRAM của 5070**, nên giới hạn thật sự là dung lượng, không phải thuật toán chia. Ước lượng này chưa kiểm chứng.

## 6. Chạy riêng nhiều model nhỏ hơn (chế độ split — mỗi card một việc)

Một luồng / bốn luồng đồng thời (`-np 4`), **không lỗi nào**:

| Model | 3060 riêng | 5070 riêng | Hai máy cùng lúc (tổng) |
|---|---|---|---|
| Qwen3-4B Q4_K_M | 100,4 / 273,6 | 167,2 / 499,1 | 202,0 / 554,9 |
| Qwen3-8B Q4_K_M | 63,2 / 178,0 | 110,9 / 349,7 | 126,6 / 358,6 |

Với model nhỏ vừa 12 GB, chạy riêng từng card (split) là lựa chọn tốt nhất: không tốn mạng, không cần gộp.

## 7. Gộp 2 card qua Wi-Fi so với qua dây (điều kiện mạng quyết định)

| Điều kiện | Model | Nạp model | Tok/s |
|---|---|---|---|
| Wi-Fi (~20–25 Mbps) | Qwen3-4B | 879 s | 22–27 (chậm hơn 1 card 4–7×) |
| **Dây 2,5 GbE** | Qwen3-4B | 32 s | 117,7 |
| **Dây 2,5 GbE** | Qwen3-30B-A3B | 87 s | 126 (ngữ cảnh mặc định) |

## 8. So sánh các model (RTX 3060, 20 câu cố định, greedy)

20 câu **chưa phải benchmark chuẩn**; chỉ cho thấy khác biệt lớn.

| Model | Loại | Kích thước | Đúng | Tok/s | VRAM đỉnh |
|---|---|---|---|---|---|
| Qwen3-4B Q4_K_M | dày | ~2,5 GB | 18/20 | 91,0 | 3,7 GiB |
| Qwen3-8B Q4_K_M | dày | ~5 GB | **20/20** | 58,7 | 5,7 GiB |
| DeepSeek-R1-Distill-Qwen-14B Q4_K_M | dày | 8,4 GiB | 18/20 | 33,8 | 9,7 GiB |
| Qwen3-30B-A3B Q4_K_M (`-ncmoe 20`) | MoE | 18,56 GB | **20/20** | 25,5 (2 luồng) | 11,4 GiB |
| DeepSeek-V2-Lite-Chat Q3_K_M | MoE | 7,6 GiB | 11/20* | 9,8 | 9,8 GiB |

\* Bản Q3 (đĩa đầy), bộ chấm có chấm sai ít nhất một câu đúng, tốc độ bất thường chưa giải thích: **không đủ cơ sở kết luận về DeepSeek-V2-Lite**. DeepSeek-R1-14B không nhanh hơn hay đúng hơn Qwen3-8B (chậm gần gấp đôi, nặng gần gấp đôi VRAM).

## 9. Độ tin cậy (tự phục hồi)

| Sự cố | Kết quả |
|---|---|
| Đổi chế độ qua gateway | 13–37 s (merged 4B: 32 s; merged 30B: 87 s) |
| Master không lên đúng hạn | rollback tự động |
| Sập `llama-server` | tự khởi động lại sau ~13 s |
| Kill `ggml-rpc-server` khi gộp | 1 yêu cầu lỗi, rồi tự phục hồi (~12 s + 30 s nạp lại), 0 lỗi sau đó |
| Mất mạng 25 s khi gộp | yêu cầu treo/lỗi trong lúc đứt, master tự sập rồi tự dựng lại, 0 lỗi sau đó |

## 10. Giới hạn — cần biết khi dùng bảng này làm báo cáo

- **Chưa đo**: Qwen3-32B dày (đang tải trên Windows, 10,2/19,76 GB, rất chậm do internet); model ~200B (không vừa 24 GB VRAM + RAM master).
- Các bài đo ở mục 2 và 3 dùng prompt ngắn (~36 token); tốc độ khi ngữ cảnh đầy thật xem mục 2b (mỗi ô chỉ 1 lần chạy, chưa lặp; chưa kiểm tra chất lượng trả lời với ngữ cảnh dài).
- Số "gộp" chỉ đo **1 luồng**; nhiều luồng chỉ đo ở cấu hình mặc định (30B: 176 tok/s tổng với 2 luồng).
- Tỷ lệ tối ưu **phụ thuộc phần VRAM Windows đang dùng lúc đó** (cửa sổ, trình duyệt...), và phụ thuộc ngữ cảnh. Nên đo lại khi đổi môi trường.
- Điểm đo "5070 riêng `-ncmoe 20`" vượt mức chừa VRAM (1 536 MiB) của dịch vụ worker-guard Windows; qua dịch vụ phải dùng `-ncmoe` ≥ 26.
- Khi đo tỷ lệ chia, rpc-server chạy thủ công (không qua worker-guard) để không bị chặn bởi mức chừa VRAM; chạy qua worker-guard thì chia nghiêng về 5070 sẽ bị gỡ sớm hơn.
- Chưa commit gì; chi tiết, log, lỗi đã sửa: `P3_BAO_CAO_3_CHE_DO.md` (mục 7), `P3_BAO_CAO_CUOI_CUNG.md`. Dữ liệu thô: `p3_final_3060.jsonl`, `p3_final_5070.jsonl`, `p3_merged_opt_split.jsonl`, `p3_merged_opt_knobs.jsonl`, `p3_merged_opt_fine8k.jsonl`, `p3_merged_opt_fine32k.jsonl`, `p3_long_prompt.jsonl` (prompt dài).
