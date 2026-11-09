# llama.cpp: thông lượng 2 luồng thấp hơn một luồng đơn, nguyên nhân và cách xử lý

Ngày đo 09/11/2026. Máy: Linux RTX 3060 12 GB; llama-server build 11913; Qwen3-4B và Qwen3-8B Q4_K_M; `-c 16384 -np 4 -fa on`, KV f16. Chỉ đo hiệu suất. Mọi số liệu là số đo thực.

## 1. Kết luận

1. **llama.cpp chỉ gom hai luồng vào cùng một bước giải mã khi chúng nằm ở hai slot kề nhau** (0-1, 1-2, 2-3). Khi hai luồng rơi vào slot không kề nhau (0-2, 0-3, 1-3), chúng được giải mã lần lượt và thông lượng tổng bằng một luồng đơn.
2. **Hệ quả đo được (Qwen3-4B):** hai luồng đạt 178 tok/s ở slot kề nhau và 90 tok/s ở slot không kề nhau, chênh gấp 2. Qwen3-8B: 113 và 58 tok/s.
3. **Máy chủ chọn slot theo độ giống với prompt đã lưu trong bộ nhớ đệm**, nên cặp slot phụ thuộc lịch sử yêu cầu: trên server mới hai luồng rơi vào slot 0-1 (nhanh); sau một thời gian chạy, chúng có thể rơi vào cặp không kề nhau (chậm). Đây là lý do bài đo trước ra 93 tok/s và các lần thử trên server mới ra 186 tok/s.
4. **Cờ `--kv-unified` loại bỏ điểm bất thường:** mọi cặp slot đều đạt 167–168 tok/s (4B) và 109 tok/s (8B). Đổi lại, thông lượng 4 luồng giảm 10% (240 so với 268 tok/s) và độ trễ token đầu tệ nhất khi 4 luồng tăng từ 128 lên 414 ms. Một luồng đơn không đổi (101 tok/s).
5. **Production không bị ảnh hưởng ở cấu hình hiện tại:** hồ sơ lập trình dùng một slot (`PARALLEL=1`). Điểm bất thường chỉ xảy ra khi chạy `-np` lớn hơn 1 với 2–3 người dùng đồng thời.

## 2. Kết quả

**Thông lượng tổng, 2 luồng đồng thời, theo cặp slot (tok/s)**

| Cấu hình | (0,1) | (1,2) | (2,3) | (0,2) | (0,3) | (1,3) |
|---|---|---|---|---|---|---|
| Qwen3-4B, mặc định | 177,7 | 177,8 | 178,2 | **89,9** | **90,0** | **89,7** |
| Qwen3-4B, `--kv-unified` | 167,7 | 167,1 | 168,0 | 168,2 | 167,3 | 167,6 |
| Qwen3-8B, mặc định | 112,9 | không đo | 113,2 | **58,1** | không đo | không đo |
| Qwen3-8B, `--kv-unified` | 108,6 | không đo | 108,8 | 108,9 | không đo | không đo |

**Qwen3-4B, kết quả bài đo đầy đủ (một luồng, 2, 4 luồng)**

| Chỉ số | Mặc định | `--kv-unified` |
|---|---|---|
| Decode một luồng | 100,8 tok/s | 101,2 tok/s |
| 2 luồng (cặp slot không kề nhau / kề nhau) | 92,3 / 178–186 | 168,7 |
| 3 luồng | không đo ở chế độ mặc định | 196–207 |
| 4 luồng | 268,4 tok/s | 240,3 tok/s |
| TTFT tệ nhất khi 4 luồng | 128 ms | 414 ms |

Các cờ khác đã thử trên server mới (Qwen3-4B, `-np 4`): `-np 2`, tắt CUDA graph, `--no-kv-unified`, `--no-cont-batching`, `-b/-ub 512` đều cho 2 luồng 174–186 tok/s ở cặp slot kề nhau; tắt flash attention làm xấu đi mọi mức (một luồng 82 tok/s, 4 luồng 169 tok/s); `-np 1` chỉ xử lý một luồng mỗi lúc (2 luồng 99 tok/s, luồng còn lại xếp hàng) và `-np 2` chỉ xử lý hai luồng mỗi lúc.

## 3. So sánh nguyên nhân

| Hiện tượng | Nguyên nhân |
|---|---|
| Hai luồng ở slot không kề nhau chậm gấp đôi | Chế độ mặc định của `-np 4` tách KV theo từng slot; bộ chia batch chỉ gom các chuỗi có số slot liên tiếp vào cùng một bước tính (hiện tượng đo được; cơ chế bên trong là suy luận, chưa đọc mã nguồn) |
| Bất thường chỉ xuất hiện sau một thời gian chạy | Slot được chọn theo độ giống với prompt đã lưu, nên cặp slot phụ thuộc lịch sử |
| `--kv-unified` hết bất thường | KV dùng chung một vùng cho mọi slot nên không còn ràng buộc slot liên tiếp; giá cả là tổng thông lượng 4 luồng thấp hơn 10% |

## 4. Giới hạn

- Chỉ đo Qwen3-4B (đủ 6 cặp slot) và Qwen3-8B (3 cặp) trên RTX 3060; mỗi cặp một lần sau một lượt khởi động. Chưa đo trên Windows (RTX 5070), nơi bài Foundry Local cũng gặp 2 luồng thấp, nên nhiều khả năng cùng nguyên nhân nhưng chưa kiểm tra theo cặp slot.
- Chưa đo ảnh hưởng của `--kv-unified` ở 8 và 16 luồng, hay khi ngữ cảnh dài.
- Chưa đọc mã nguồn llama.cpp để xác nhận cơ chế ở mục 3.
- Chưa kiểm tra có cách nào ghim yêu cầu vào slot kề nhau từ phía gateway (tham số `id_slot` có hỗ trợ ở máy chủ).
