# ZeRO-3 / FSDP trên cụm Linux + Windows: không chạy được, không có số đo so sánh

Ngày 08/11/2026 (sửa 09/11/2026). Cụm: Linux RTX 3060 12 GB + Windows RTX 5070 12 GB, LAN 2,5 GbE (đo thực 291 MB/s).

**Báo cáo này không có số liệu tốc độ của FSDP.** ZeRO-3 và FSDP là kỹ thuật huấn luyện, không phải engine suy luận; phép thử là dùng cách chia trọng số của chúng để chạy suy luận trên hai máy. Phép thử không chạy được, nên không có số đo so sánh với chế độ ghép GPU hiện tại (llama.cpp GGML RPC `merged`).

## 1. Kết luận

1. **FSDP/ZeRO-3 không chạy được trên cụm Linux + Windows này.** Thư viện `gloo` của PyTorch không ghép nối được giữa hai hệ điều hành (lỗi `address.cc:27 ... 136 vs 190`: cấu trúc địa chỉ nội bộ của bản Windows và bản Linux khác nhau). Windows không có NCCL, và WSL2 bị NAT chặn.
2. **Không có số tok/s của FSDP để so sánh,** vì thế báo cáo không đưa ra nhận định "nhanh hơn" hay "chậm hơn".
3. **Về cơ chế, FSDP không phù hợp với mạng LAN của cụm này** (mục 3); đây là nhận xét về thiết kế, không phải kết quả đo.

## 2. Đã thử gì

| Bước | Kết quả |
|---|---|
| Cài PyTorch trên Linux và Windows (bản CUDA), tải trọng số Qwen3-Coder-30B | Thực hiện được |
| Khởi tạo nhóm tiến trình `gloo` giữa Linux và Windows (`benchmarks/hardware/fsdp_gloo_probe.py`) | Lỗi: kích thước cấu trúc địa chỉ khác nhau giữa hai hệ điều hành (136 so với 190) |
| NCCL trên Windows | Không có bản NCCL cho Windows |
| Chạy trong WSL2 để cùng hệ Linux | Mạng NAT của WSL2 chặn kết nối từ máy kia |
| DeepSpeed ZeRO-3 | Không chạy được liên hệ điều hành |

Chỉ Qwen3-Coder-30B được thử khởi tạo; ba model còn lại (Qwen3.8-27B, GLM-4-32B, Qwen3-30B-A3B) chưa thử.

## 3. Khác biệt cơ chế (không phải số đo FSDP)

| | `merged` (GGML RPC, hiện tại) | ZeRO-3 / FSDP |
|---|---|---|
| Cách chia | Chia model theo tầng; trọng số đứng yên trên từng card | Mỗi card giữ một mảnh của mọi tầng, gom đủ trọng số từng tầng trước khi tính |
| Thứ chuyển qua mạng mỗi token | Activation: **đo được** khoảng 0,3 MB/token (Coder-30B) và 1,2 MB/token (Qwen3.8-27B) | Trọng số của các tầng (theo thiết kế của thuật toán) |
| Chạy trên Linux + Windows | Có (đang chạy production) | Không (mục 2) |
| Định dạng | GGUF Q4_K_M đã dùng sẵn (Coder-30B 18,6 GB; Qwen3.8-27B 16,5 GB) | Cần trọng số HF; bản bf16 (55–65 GB) không vừa 2×12 GB |
| Phù hợp khi | Mạng chậm, model vừa tổng VRAM | Huấn luyện, mạng NVLink/InfiniBand |

Số `merged` đo thật trên cụm này (Ethernet, 2 card): Qwen3-Coder-30B decode 125,8 tok/s; Qwen3.8-27B decode 23,8 tok/s (báo cáo `BAO_CAO_GOP_GPU_2_CARD_VS_3_CARD`).

Cơ chế khác nhau ở thứ phải đi qua mạng: `merged` chuyển activation (cỡ vài trăm KB mỗi token), còn FSDP phải gom trọng số các tầng từ card kia (cỡ GB) trước khi tính mỗi tầng. Với băng thông LAN 291 MB/s, thiết kế này không phù hợp cho suy luận; tuy nhiên báo cáo không đo hay tính ra tốc độ cụ thể.

## 4. Giới hạn

- Không có bất kỳ số đo tốc độ, thông lượng hay độ trễ nào của FSDP.
- Chưa thử trên cụm cùng hệ điều hành (Linux-Linux) hoặc có NCCL, nơi FSDP có thể khởi tạo được; kết luận ở mục 1 chỉ áp dụng cho cụm Linux + Windows hiện tại.
- ZeRO-3 và FSDP FULL_SHARD cùng một thuật toán nên báo cáo gộp chung.
