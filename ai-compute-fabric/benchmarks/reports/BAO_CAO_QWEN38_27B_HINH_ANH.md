# Qwen3.8-27B nhận hình ảnh: cấu hình và đo thử

Ngày 09/11/2026. Cụm RTX 3060 (Linux) + RTX 5070 (Windows), chế độ `merged` 48:52, ngữ cảnh 65536, 1 slot. Số đo thực, một lượt. Mã: `benchmarks/hardware/vision_probe.py`; dữ liệu `benchmarks/reports/vision27b/`.

## 1. Kết luận

1. **Model nhìn được ảnh** khi nạp thêm bộ chiếu `mmproj-F16.gguf` (927 607 488 byte, sha256 `cbb841a9…0b4e43e` khớp với Hugging Face). File `Qwen3.8-27B-UD-Q4_K_M.gguf` đang dùng không có phần nhìn ảnh.
2. **Không giới hạn số token mỗi ảnh thì không an toàn:** ảnh 1920×1080 làm VRAM card 3060 xuống 461 MiB trống, dưới ngưỡng 512 MiB của worker-guard nên guard dừng engine (engine tự hồi phục khoảng 1 phút; guard còn dừng một container serverless của khách).
3. **Cấu hình triển khai: C** (bộ chiếu ở GPU, `--image-max-tokens 2048`, KV q8_0): 16/16 câu đúng, mỗi ảnh lớn khoảng 5 giây, VRAM 3060 đỉnh 11 135 MiB (còn khoảng 1 150 MiB trống).

## 2. Ba cấu hình đã so sánh (16 câu: 4 loại câu hỏi × 4 cỡ ảnh)

| Cấu hình | Đúng | Giây/ảnh 512 / 1024 / 1080p / 4K | VRAM đỉnh 3060 | Ghi chú |
|---|---|---|---|---|
| A: GPU, tối đa 1024 token/ảnh, KV f16 | 16/16 | 1,4 / 2,4 / 2,8 / 2,9 | 11 717 MiB (còn khoảng 570, sát ngưỡng 512) | nhanh nhưng biên an toàn hẹp |
| **C: GPU, tối đa 2048 token/ảnh, KV q8_0** | **16/16** | 1,3 / 2,3 / 5,2 / 5,3 | **11 135 MiB** (còn khoảng 1 150) | **triển khai** |
| B: bộ chiếu chạy CPU, tối đa 2048, KV f16 | 15/16 | 2,3 / 7,5 / 26,4 / 25,3 | 10 645 MiB | chậm gấp 5 lần; 1 câu đếm sai (6 thay vì 7) ở 1080p |

Bộ câu hỏi: đọc số hóa đơn, đếm 7 hình tròn đỏ, đếm 4 hình vuông xanh, nhận màu hình ở góc trên trái (ảnh tạo bằng PIL, đáp án biết trước). Lưu ý: mỗi câu một lượt, chưa đo ảnh thực tế (ảnh chụp, biểu đồ, tài liệu quét).

## 3. Kiểm tra đầu cuối qua cổng LAN (cấu hình C)

| Phép thử | Kết quả |
|---|---|
| Ảnh 1080p hóa đơn | đúng (`48213`), 2 060 token, 5,5 giây |
| Ảnh nhiễu 4K, PNG 24,9 MB | xử lý được (trả lời mô tả), 2 002 token, 6,4 giây |
| 4 ảnh 1080p trong một yêu cầu | đúng (`4`), 8 190 token, 18 giây; VRAM 3060 giữ 11,1 GB |
| `:think` kèm ảnh | đúng (`Green`), 6,3 giây |
| Chỉ chữ | bình thường |
| Body 33 MiB | bị từ chối (kết nối đóng) |

Giới hạn body của gateway nâng từ 1 MiB lên 32 MiB và của proxy LAN từ 4 MiB lên 32 MiB.

## 4. Cách triển khai

- `llama-server-vision` (bộ bọc tại `.aicf-work/bin/`) thêm `--mmproj` cho Qwen3.8-27B, đọc các cờ phụ từ `/mnt/Data/aicf-3node/vision/vision.flags` (hiện là `--image-max-tokens 2048`). Worker-guard không phải sửa mã.
- Guard khởi động với `--llama-server .../llama-server-vision`, KV q8_0, ngữ cảnh 65536.

## 5. Giới hạn

- KV q8_0 chậm hơn f16 khoảng 3% (ngắn) đến 12% (62k token) như đo ở báo cáo `BAO_CAO_QWEN38_27B_CONTEXT_TOI_UU`.
- Chưa đo chất lượng nhận dạng trên ảnh thực tế và với thinking bật; chưa thử video; mỗi cấu hình một lượt.
- Chưa bền qua khởi động lại máy.
