# Qwen3-Coder-30B-A3B-Instruct Q4_K_M, gộp RTX 3060 + RTX 5070 (RPC, layer split)

Đo 2026-10-28: llama-server build 11913, `-fa on -np 1 -ngl 99`, cáp 2,5 GbE, rpc-server trên Windows (guard headroom 300 MiB),
5 lần x 128 token, trung vị [ĐO]. Tỷ lệ = phần của RTX 5070 / RTX 3060 (`--tensor-split` thứ tự llama: RPC trước). Dữ liệu:
`p3_coder_8k.jsonl`, `p3_coder_32k.jsonl`, log `logs/p3_server_c*.log`.

| Ngữ cảnh | 5070 : 3060 | Tốc độ giải mã (tok/s) | Ghi chú |
|---|---|---|---|
| 8k | 50 : 50 | 126,6 | |
| 8k | 54 : 46 | 126,9 | |
| 8k | **58 : 42** | **132,4** | tối ưu đo được |
| 8k | 60 : 40 | 131,6 | ngang 58:42 trong sai số, sát vách |
| 8k | 62 : 38 | 47,3 | **sụp** (card Windows tràn VRAM sang RAM dùng chung) |
| 32k | 46 : 54 | 125,3 | |
| 32k | 50 : 50 | 123,8 | |
| 32k | **52 : 48** | **126,7** | tối ưu đo được |
| 32k | 54 : 46 | 99,3 | **bắt đầu sụp** (-22%) |

Kết luận: giống Qwen3-30B-A3B thường (58:42 ở 8k, ~50:50 ở 32k, vách ở 62% / 54%). Dùng 58:42 (8k) hoặc 52:48 (32k); không đi sát vách.
Chưa đo: chất lượng code có hệ thống (chỉ 3 bài mẫu: SQL, sửa lỗi đúng; hàm sàng nguyên tố thừa), 16k, tỷ lệ giữa các mốc đã đo.
Điều kiện: cần hạ `--headroom-mib` của worker-guard Windows (1536 -> 300), nếu không guard tắt rpc-server khi cấp phát bộ đệm tính toán.
