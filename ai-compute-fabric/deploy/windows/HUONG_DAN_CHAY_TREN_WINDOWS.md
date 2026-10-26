# Chạy các mục Windows (chưa từng chạy trên Windows thật)

**Trạng thái trung thực:** `worker-guard.exe` và `site-gateway.exe` mới chỉ được *biên dịch chéo* từ Linux
(`GOOS=windows`). Các script `.ps1` và nhánh `sys.platform == "win32"` trong Python chưa bao giờ chạy trên Windows.
Lần chạy đầu rất có thể lộ lỗi; hãy gửi lại nguyên văn thông báo lỗi, đừng tự sửa lặng lẽ.

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
