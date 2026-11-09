# Qwen3.8-27B ghép 2 card: context, kiểu KV cache, tốc độ và chất lượng

Ngày đo 09/11/2026. Cụm: Linux RTX 3060 12 GB + Windows RTX 5070 12 GB, cáp 2,5 GbE, llama.cpp GGML RPC chế độ `merged`, model `Qwen3.8-27B-UD-Q4_K_M.gguf` (16,5 GB), tỉ lệ chia 48:52 (3060 đứng trước), 1 slot. Mọi số là **số đo thực, một lượt** (trừ chỗ ghi khác). Không có số ước tính nào trong báo cáo này.

## 1. Kết luận

1. **Mức context cao nhất chạy được là 131072 token (KV q8_0).** 163840, 204800 và 262144 không chạy được: card 3060 hết bộ nhớ khi cấp phát bộ đệm tính toán (mục 4).
2. **Chất lượng không giảm ở các mức đã đo.** Tìm mã giấu trong văn bản dài đúng 100% ở mọi cấu hình (tổng 78/78 lần, đến 127k token). HumanEval-164 (thinking tắt) ở 65536 q8_0 đạt 158/164, bằng bản f16 (158/164).
3. **Tốc độ giải mã lúc ngắn gần như không đổi (23,4–24,0 tok/s) ở mọi context đặt.** Tốc độ giảm theo độ đầy của ngữ cảnh, không theo giá trị `--ctx` đã đặt: từ khoảng 22 tok/s ở 4k còn 12,6 tok/s ở 127k.
4. **KV q8_0 chậm hơn f16 khoảng 3% (ngữ cảnh ngắn) đến 12% (62k token)** nhưng nhỏ hơn: tiết kiệm khoảng 0,8 GB mỗi card ở 65536.
5. **Embed (cổng 18091) và rerank (cổng 18092) chiếm khoảng 1,75 GB của card 3060.** Khi chúng ở GPU, chỉ lên được 32768 f16. Chuyển sang CPU thì lên được 131072 q8_0, đổi lại tìm kiếm của Studio chậm hơn (mục 5).

## 2. Cách đo

- Engine gọi trực tiếp (`127.0.0.1:18090`), thinking tắt (`enable_thinking=false`), `temperature=0`, `cache_prompt=false`.
- **Tìm mã giấu:** văn bản nền gồm các dòng bản ghi giả, một mã 6 chữ số cho "vault Vk-xx" đặt ở độ sâu 10%, 50%, 90%, kèm một mã nhiễu của vault khác tên. Hỏi đúng mã của vault đó. Đây chỉ kiểm tra khả năng tìm một thông tin cụ thể, chưa kiểm suy luận trên ngữ cảnh dài.
- **HumanEval-164:** cùng prompt, trần 1024 token, seed và bộ chấm với lần chạy cơ sở (`BAO_CAO_FULL164_PHAN_TAN_VS_1_CARD`, 158/164 ở 20480 f16).
- Giải mã và prefill lấy từ `timings` của engine; VRAM từ `nvidia-smi` hai máy.
- Mã: `benchmarks/hardware/qwen27b_ctx_probe.py`, `embed_rerank_probe.py`. Dữ liệu thô: `benchmarks/reports/ctx27b/*.json`.

## 3. Kết quả

Giải mã (tok/s) theo độ đầy ngữ cảnh, trung bình 3 độ sâu:

| Cấu hình | Embed/rerank | Ngắn | 4k | 16k | 32k | 62–64k | 100k | 127k | Tìm mã | VRAM Linux / Win (GB) |
|---|---|---|---|---|---|---|---|---|---|---|
| 20480 f16 | GPU | 24,0 | 22,8 | 21,4 (18k) | | | | | 9/9 | 10,3 / 9,2 |
| 32768 f16 | GPU | 23,9 | 22,2 | 21,8 | 20,7 (30k) | | | | 9/9 | 10,8 / 9,6 |
| 65536 f16 | tắt | 24,0 | 22,7 | 21,8 | 20,5 | 18,6 | | | 12/12 | 10,2 / 10,8 |
| 65536 f16 | CPU | 24,0 | 22,8 | | 20,5 | 18,6 | | | 9/9 | 10,6 / 10,6 |
| 65536 q8_0 | tắt | 23,4 | 21,5 | 20,8 | 18,7 | 16,3 | | | 12/12 | 9,4 / 10,0 |
| 131072 q8_0 | tắt | 23,4 | 21,6 | 20,5 | 19,0 | 16,4 | 13,9 | 12,6 | 18/18 | 11,0 / 11,6 |
| 131072 q8_0 | CPU | 23,6 | 21,9 | | | 16,2 | | 12,5 | 9/9 | 11,5 / 11,4 |

Prefill (tok/s): 910–950 ở 4k, 1120–1185 ở 16k–32k, khoảng 950–970 ở 62k, 800 ở 100k, 713–717 ở 127k.

HumanEval-164, thinking tắt: **65536 q8_0 đạt 158/164** (2 009 s, giải mã trung bình 23,2 tok/s) so với **20480 f16 158/164** (1 980 s, 23,65 tok/s). Hai bên đổi kết quả 1 bài mỗi phía; 4 bài ở mỗi bên chạm trần 1024 token. Hai lần chạy khác cấu hình (ctx và kiểu KV), nên đây là so sánh giữa hai cấu hình, và chênh lệch nằm trong mức dao động bình thường giữa các lần chạy.

## 4. Giới hạn bộ nhớ

| Cấu hình | Kết quả |
|---|---|
| 65536 f16, embed/rerank ở GPU | Engine nạp xong rồi chết ngay ở request đầu với `CUDA error: out of memory` (không xác định được card nào, log gốc bị ghi đè) |
| 65536 q8_0, embed/rerank ở GPU | Log engine ghi card 3060 còn trống 5 MiB sau khi nạp; worker-guard dừng engine (ngưỡng headroom 512 MiB) |
| 163840 q8_0 (48:52) | Chết ở request đầu. Log engine bị ghi đè nên tôi không lưu được nguyên văn lần này |
| 163840 q8_0 (52:48) | Không nạp được: `cudaMalloc failed ... allocating 1401 MiB on device 0` (CUDA0 = RTX 3060). Lưu ý: `52,48` dồn **thêm** tải sang 3060 (tỉ lệ viết theo thứ tự card chính trước), nên đây là hướng sai; thử đúng hướng (`44,56`) chưa làm |
| 204800 q8_0 | Không nạp được: cấp phát bộ đệm tính toán 1721 MiB trên CUDA0 thất bại |
| 262144 q8_0 | Không nạp được, cùng loại lỗi `cudaMalloc failed` trên CUDA0 |

Hai cấu hình sau cùng nạp thất bại rồi gateway treo khoảng 15 phút trước khi khôi phục.

## 5. Embed và rerank chuyển sang CPU

Đo bằng `embed_rerank_probe.py` (trung vị 5 lần). CPU: `-ngl 0`, ghim nhân 8–19 (engine ghim nhân 0–7).

| | GPU | CPU, rảnh | CPU, lúc engine đang giải mã |
|---|---|---|---|
| Embed ~60 token | 12,7 ms | 74,6 ms | 176–205 ms |
| Embed ~500 token | 35,2 ms | 114 ms | 251–274 ms |
| Embed ~2000 token | 110 ms | 253 ms | 485–487 ms |
| Rerank 20 tài liệu | 244 ms | 635 ms | 1 416–1 439 ms |

Khi embed/rerank chạy đồng thời, tốc độ giải mã của engine giảm: 23,7 → 21,9 tok/s (65536 f16) và 23,1 → 21,5 tok/s (131072 q8_0), tức khoảng 7–8%. Mỗi con số là một lượt đo với một yêu cầu tại một thời điểm; chưa đo khi nhiều người gọi tìm kiếm cùng lúc.

## 6. Thinking bật so với tắt

Mọi số ở mục 3 đo với thinking **tắt**. Model mặc định là thinking **bật** (trả thêm `reasoning_content`). Đo trên engine 65536 f16, HumanEval, trần 8192 token, seed và bộ chấm như trên (`benchmarks/hardware/thinking_probe.py`, dữ liệu `reports/ctx27b/thinking_he_{pilot,failed}.json`):

| Mẫu | Thinking tắt | Thinking bật | Ghi chú |
|---|---|---|---|
| 12 bài rải đều (cách 14 bài) | 12/12 | 12/12 | 6 150 token so với 2 139 token (gấp 2,9 lần); khoảng 27 giây mỗi bài; không bài nào chạm trần |
| 6 bài thinking tắt từng sai (116, 127, 129, 132, 145, 147) | 0/6 | **3/6** | sửa được 127, 132, 147; 116, 129, 145 chạm trần 8192 token (khoảng 354 giây mỗi bài) |

- Tốc độ giải mã không đổi (23,3–23,9 tok/s); chi phí của thinking nằm ở số token sinh thêm.
- **Chưa đo** thinking bật cho khoảng 146 bài còn lại, nên chưa biết nó có làm hỏng bài đang đúng không, và không đưa ra tỉ lệ cho toàn bộ 164 bài. Chưa thử trần 16384.
- Một bài khó có thể giữ engine (1 slot) khoảng 6 phút.

## 7. Đề xuất

| Mục tiêu | Cấu hình | Điều kiện |
|---|---|---|
| Giữ nguyên dịch vụ, tăng nhẹ | 32768 f16 | Không phải đổi gì khác. Giải mã ngắn không đổi |
| Cân bằng context, tốc độ, chất lượng | **65536 f16** | Embed/rerank chuyển sang CPU. Nhanh hơn q8_0, còn dư VRAM |
| Context tối đa | **131072 q8_0** | Embed/rerank trên CPU. Dư khoảng 0,8 GB mỗi card, decode ở 127k còn 12,6 tok/s |

Cả ba chỉ có một slot, người dùng thứ hai phải chờ khi có prompt dài.

## 8. Chưa đo / hạn chế

- Thinking bật ở ngữ cảnh dài và trên toàn bộ 164 bài; HumanEval ở 131072; bài suy luận nhiều bước trên ngữ cảnh dài (bài tìm mã chỉ kiểm tra truy xuất).
- 163840 với tỉ lệ `44,56`; KV q4_0; `--kv-unified`.
- Mỗi cấu hình chỉ một lượt đo; hai lần đo độc lập cùng cấu hình chênh rất ít (65536 f16 ở 62k: 18,6 và 18,6; 131072 q8_0 ở 127k: 12,6 và 12,5), nhưng đó chỉ là hai mẫu.
- Cấu hình 20480 và 32768 đo khi embed/rerank còn ở GPU, nên VRAM không so trực tiếp được với các cấu hình còn lại.
- Production đã được trả về 20480 f16 với embed/rerank ở GPU sau khi đo; chưa triển khai cấu hình nào ở trên.
