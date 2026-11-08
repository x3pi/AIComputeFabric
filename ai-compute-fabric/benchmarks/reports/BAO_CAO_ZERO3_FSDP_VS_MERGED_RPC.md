# ZeRO-3 / FSDP so với chế độ ghép GPU hiện tại (llama.cpp GGML RPC `merged`)

Ngày 08/11/2026. Cụm: Linux RTX 3060 12 GB + Windows RTX 5070 12 GB, LAN 2,5 GbE (đo thực 291 MB/s). Số liệu về tốc độ FSDP là ước tính từ băng thông đo được, được gắn [ƯỚC TÍNH]; các số còn lại là số đo thực.

## 1. Kết luận

1. **FSDP/ZeRO-3 không chạy được trên cụm Linux + Windows này.** Thư viện `gloo` của PyTorch không ghép nối được giữa hai hệ điều hành (lỗi `address.cc:27 ... 136 vs 190`: cấu trúc địa chỉ nội bộ của bản Windows và bản Linux khác nhau). Windows không có NCCL, và WSL2 bị NAT chặn.
2. **Dù chạy được, FSDP sẽ chậm hơn `merged` khoảng 19–220 lần** [ƯỚC TÍNH], vì mỗi bước giải mã phải chuyển khoảng một nửa model qua LAN, trong khi `merged` chỉ chuyển khoảng 0,3 MB mỗi token.
3. **FSDP không đem lại cải thiện nào so với cách ghép hiện tại;** `merged` RPC vẫn là phương án phù hợp.

## 2. So sánh nguyên nhân

| | `merged` (GGML RPC, hiện tại) | ZeRO-3 / FSDP |
|---|---|---|
| Cách chia | Chia model theo tầng; trọng số đứng yên trên từng card | Mỗi card giữ một mảnh của mọi tầng, gom đủ trọng số từng tầng trước khi tính |
| Dữ liệu qua mạng mỗi token | ~0,3 MB (Coder-30B) đến ~1,2 MB (Qwen3.8-27B) | ~8–9 GB (một nửa model 4-bit) |
| Chạy trên Linux + Windows | Có (đang chạy production) | Không |
| Định dạng | GGUF Q4_K_M đã dùng sẵn | Cần trọng số HF; bf16 (55–65 GB) không vừa 2×12 GB nên phải hạ xuống 4-bit |
| Phù hợp khi | Mạng chậm, model vừa tổng VRAM | Huấn luyện, mạng NVLink/InfiniBand |

Cơ chế khác biệt là thứ được chuyển qua mạng: `merged` chuyển activation (nhỏ), FSDP chuyển trọng số (lớn gấp khoảng 27.000 lần ở Coder-30B).

## 3. Ước tính tốc độ nếu FSDP chạy được [ƯỚC TÍNH]

Giả định nhẹ nhất cho FSDP: 4-bit, truyền chồng lấn hoàn hảo với tính toán, chỉ tính thời gian truyền trọng số.

| Model | `merged` (đo thật) | FSDP: thời gian mỗi bước | FSDP: tok/s (8 chuỗi mỗi card) | Chậm hơn `merged` | FSDP: tok/s (16 chuỗi mỗi card) | Chậm hơn `merged` |
|---|---|---|---|---|---|---|
| Qwen3.8-27B (dense) | 23,65 tok/s | 25,4 s | 0,63 | ~38× | 1,26 | ~19× |
| Qwen3-Coder-30B-A3B (MoE) | 124,67 tok/s | 27,9 s | 0,57 | ~218× | 1,15 | ~109× |
| GLM-4-32B (dense) | 20,69 tok/s | 29,8 s | 0,54 | ~38× | 1,08 | ~19× |
| Qwen3-30B-A3B (MoE) | 126,27 tok/s | 27,9 s | 0,57 | ~220× | 1,15 | ~110× |

- Một chuỗi đơn trên Coder-30B: khoảng 28 giây mỗi token với FSDP, so với 8 ms với `merged`.
- Chạy 164 bài HumanEval trên Coder-30B: `merged` mất 342 giây; FSDP tối thiểu 9–18 giờ.
- Model MoE chịu thiệt nhiều nhất vì `merged` chỉ kích hoạt khoảng 3 tỷ tham số mỗi token, còn FSDP vẫn phải gom toàn bộ trọng số.
- Để FSDP ngang `merged` ở Coder-30B cần khoảng 31 GB/s mỗi chiều, gấp khoảng 108 lần LAN hiện tại (cỡ PCIe 4.0 x16 hoặc NVLink). Nâng lên 10 GbE vẫn chậm hơn khoảng 26 lần.

## 4. Giới hạn

- Chưa có số tok/s FSDP đo thật vì FSDP không khởi tạo được; các con số ở mục 3 là cận dưới tính từ băng thông, thực tế có thể còn chậm hơn.
- Chất lượng HumanEval của bản 4-bit dưới FSDP chưa được đo (khác Q4_K_M). Chỉ Coder-30B được thử khởi tạo; ba model còn lại chỉ ngoại suy theo kích thước.
- ZeRO-3 và FSDP FULL_SHARD cùng một thuật toán, nên báo cáo gộp chung; DeepSpeed không chạy được liên hệ điều hành.
