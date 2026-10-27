# Chạy & Nghiệm thu trên Windows (Đã nghiệm thu trên Windows 10 Pro + RTX 5070)

> **Kế hoạch triển khai & nghiệm thu chi tiết qua SSH (`fios02@192.168.1.49`):** Xem chi tiết đầy đủ tại [`../../KE_HOACH_TRIEN_KHAI_WINDOWS_192_168_1_49.md`](file:///mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/KE_HOACH_TRIEN_KHAI_WINDOWS_192_168_1_49.md).

**Trạng thái nghiệm thu thực tế (2026-10-27):**
Toàn bộ chuỗi thành phần Windows (`worker-guard.exe`, `site-gateway.exe`, `pki.exe`, `install-service.ps1`, `probe_windows.ps1`, `probe_node.py`, `reclaim_poc.py`) đã được triển khai, kiểm thử thực nghiệm và đạt **PASS 100%** trên máy thật `DESKTOP-2MJNHTH` (Windows 10 Pro, AMD Ryzen 7 9700X, NVIDIA GeForce RTX 5070 12GB, Driver 617.42 WDDM).
- **Khảo sát phần cứng & WDDM:** `p0_probe_windows_rtx5070.json`, `p0_probe_node_windows_rtx5070.json`. Xác nhận `nvidia-smi` per-process trả `[N/A]`, nhưng PDH `\GPU Process Memory(*)\Dedicated Usage` đọc VRAM theo tiến trình chính xác (chốt **D06**).
- **Cổng CPU ISA & Hạng GPU:** Ryzen 7 9700X phát hiện đủ `avx2`, `avx512f/bw/vl`, chọn `llama_cpp_avx512`. RTX 5070 xếp hạng `"group"`.
- **Baseline B01:** Qwen3-4B đạt `179.7 tok/s` (decode), Qwen3-8B đạt `115.65 tok/s` (decode).
- **Thu hồi GPU (G-RECLAIM):** 10/10 lần đạt, trung vị `174.1 ms`, max `638.2 ms` (< 2.0 s).
- **Dịch vụ Windows AICFWorkerGuard:** Cài đặt SCM thành công, `Stop-Service` tắt sạch tiến trình con trong `262 ms` và trả VRAM về nền `203 MiB`.
- **Cụm 2 máy thật mTLS:** `site-gateway` điều phối song song Linux RTX 3060 (`180.4 tok/s`) và Windows RTX 5070 (`166.9 tok/s`), đạt thông lượng tổng hợp **334.9 tok/s** (hiệu suất scale 96.4%).

## 0. Chuẩn bị (máy Windows có GPU NVIDIA)
- Driver NVIDIA mới, `nvidia-smi` chạy được trong PowerShell.
- Python 3.11+ (để chạy `reclaim_poc.py`), PowerShell mở **quyền Administrator** cho bước 3.
- `llama-server.exe` bản CUDA của llama.cpp (cùng bản đã dùng trên Linux nếu có) và một file GGUF nhỏ, ví dụ Qwen3-8B-Q4_K_M.
- Copy `dist/windows/worker-guard.exe` (sinh bằng `GOOS=windows go build`, thư mục `dist/` không nằm trong git) và thư mục repo sang máy Windows.

## 1. Thăm dò phần cứng — việc quan trọng nhất (rủi ro R-WDDM)
```powershell
cd benchmarks\hardware
.\probe_windows.ps1 -Out probe_windows.json
python probe_node.py > probe_node.json
```
Câu hỏi cần trả lời: `nvidia-smi --query-compute-apps` có in `[N/A]` dưới WDDM không, và bộ đếm
`\GPU Process Memory(*)\Dedicated Usage` có cho thấy VRAM theo từng tiến trình không. Kết quả quyết định
cách worker-guard biết "động cơ đã nhả GPU" trên Windows.

## 2. Cổng ISA CPU
```powershell
python -c "from core.hardware_profiler import cpu_isa as c; f=c.detect_flags(); print(sorted(f)); print(c.allowed_builds(f))"
```
(chạy từ thư mục gốc `ai-compute-fabric`). Đối chiếu với CPU thật (`probe_windows.json` → `cpu`). Lưu ý: `avx512bw/vl` được
*suy ra* từ `avx512f`, chưa kiểm chứng; AMX luôn báo không có.

## 3. Thử thu hồi GPU (cổng < 2 giây)
```powershell
python benchmarks\hardware\reclaim_poc.py --guard C:\aicf\worker-guard.exe --server C:\aicf\llama\llama-server.exe `
  --engine-model C:\aicf\models\Qwen3-8B-Q4_K_M.gguf --user-model C:\aicf\models\<model_lớn_hơn_để_chiếm_VRAM>.gguf `
  --trials 10 --out reclaim_windows.json --workdir C:\aicf\work
```
Trên WDDM, VRAM theo tiến trình là `[N/A]` nên harness dùng mức giảm của tổng VRAM đã dùng để quyết định "đã nhả" — nhánh này chưa chạy.

## 4. Cài dịch vụ (PowerShell Administrator)
```powershell
.\deploy\windows\install-service.ps1 -Bin C:\aicf\worker-guard.exe -LlamaServer C:\aicf\llama\llama-server.exe `
   -Model C:\aicf\models\Qwen3-8B-Q4_K_M.gguf
Get-Service AICFWorkerGuard
curl http://127.0.0.1:8099/api/state
Stop-Service AICFWorkerGuard    # kiểm tra llama-server cũng tắt theo
```
Đã sửa trước khi giao: biến `$args` (biến tự động của PowerShell) đổi thành `$svcArgs`.

## 5. Gửi lại
`probe_windows.json`, `probe_node.json`, `reclaim_windows.json`, đầu ra của bước 2 và 4, kèm mọi lỗi. Khi có các file này
mới đánh dấu `[x]` cho P1.1 (Windows), P2.1 (Windows) và R-WDDM trong kế hoạch; chưa có thì giữ `[ ]`/`[~]`.
