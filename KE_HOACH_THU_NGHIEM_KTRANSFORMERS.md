# KẾ HOẠCH: THỬ NGHIỆM KTRANSFORMERS (kvcache-ai) ĐỂ TĂNG TỐC SUY LUẬN MoE TRÊN CỤM RTX 3060 + RTX 5070
*(Tài liệu bàn giao cho Agent thực thi. **BẢN CHỐT 2026-10-30.** Mục tiêu là **đo và báo cáo có cải thiện hay không**, không phải "chứng minh KTransformers tốt". Kết luận "không đáng" là kết quả hợp lệ.)*

**Trạng thái:** Pha 0 **đã xong** (kết quả: `benchmarks/reports/P8_KTRANSFORMERS.md`). Pha 1, 2A, 2B **chờ chủ dự án duyệt** (cần chiếm GPU/RAM và có thể tải 17 GB). Pha 4 **tạm dừng** (không hỗ trợ). Pha 3, 5 chạy sau khi có số đo.

**Quy ước nhãn bằng chứng:** `[ĐO]` đã đo trên máy thật · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` theo README/tài liệu KTransformers, chưa kiểm · `[SUY LUẬN]` tính toán/ước lượng · `[CHƯA XÁC MINH]` phải thử. Con số không có nhãn `[ĐO]` là **chưa đo**.

---

## PHẦN 0: BRIEF BÀN GIAO (ĐỌC TRƯỚC)

**KTransformers là gì `[TÀI LIỆU]`** (github.com/kvcache-ai/ktransformers, đọc 2026-10-30): khung suy luận/tinh chỉnh **lai CPU–GPU cho model MoE**: phần attention/không-chuyên-gia chạy GPU, các **chuyên gia (experts) chạy trên CPU** bằng nhân (kernel) tối ưu (`kt-kernel`), cho phép chạy model lớn hơn VRAM. Phần suy luận nay đi qua **SGLang** (`sglang-kt`); dòng lệnh dạng `python -m sglang.launch_server --kt-method … --kt-weight-path … --kt-cpuinfer … --kt-threadpool-count … --kt-num-gpu-experts … --kt-max-deferred-experts-per-token …`. Công bố tăng tốc lớn chủ yếu trên **máy chủ** (Xeon AMX, DeepSeek-R1, 8×L20; "27,79× so với llama.cpp" trên dual-socket 32 nhân AMX); **không có** so sánh trực tiếp với llama.cpp trên card phổ thông cho Qwen3-30B-A3B `[TÀI LIỆU]`. Số trên máy chủ **không** suy ra được cho máy này.

**Nó tương ứng với cái gì trong hệ thống của chúng ta:** chế độ B `smart_1gpu` (llama.cpp `-ncmoe N`: đẩy chuyên gia của N tầng sang RAM/CPU) `[ĐỌC MÃ]`. **Không** thay được chế độ C (gộp 2 card, toàn bộ trọng số nằm trong VRAM): model nằm trọn trong VRAM thì không có chuyên gia nào phải chạy trên CPU. Số đã đo để so (Qwen3-30B-A3B Q4_K_M, `benchmarks/reports/P3_BANG_SO_LIEU_CUOI.md`, `p3_gpu_modes_sweep.json`): RTX 3060 `-c 8192` `N=20` **44,0 tok/s**, `-c 32768` `N=30` **35,9**; RTX 5070 `-c 8192` `N=20` **65,6**, `N=26` **54,2**, `-c 32768` `N=30` **49,2**; gộp 2 card **~135** (8k) / **~131,7** (32k); Qwen3-Coder gộp **132,4** (8k) / **126,7** (32k). Số cũ chỉ để tham khảo: **mọi so sánh phải có đường cơ sở llama.cpp đo lại trong cùng phiên, cùng prompt, cùng máy** (Pha 1).

**Điều đã biết về phần cứng và gói phần mềm (`kt-kernel==0.7.2`, `sglang-kt==0.7.2`) — đã xác minh ở Pha 0:**
| Hạng mục | Giá trị đã xác minh (`[ĐO]` / `[ĐỌC MÃ]`) | Hệ quả kỹ thuật |
|---|---|---|
| CPU Linux `pc-001` | `Intel Core Ultra 7 265K`: 1 socket, 1 NUMA node, **20 nhân vật lý / 20 luồng** (`Thread(s) per core: 1`), gồm **8 nhân P** (`CPU 0..7`, xung tối đa `5500–5600 MHz`) + **12 nhân E** (`CPU 8..19`, xung tối đa `4600 MHz`); có `avx2`, `fma`, `f16c`, `avx_vnni`; **không có `avx512*`, không có `amx*`** `[ĐO]` (`lscpu`, `lscpu -e`) | Chạy được 2 nhóm backend CPU trong `kt-kernel==0.7.2` (`kt-kernel/python/utils/amx.py`, `kt-kernel/operators/avx2/`, `kt-kernel/operators/llamafile/moe.hpp`) `[ĐỌC MÃ]`: (1) `LLAMAFILE` (AVX2, đọc thẳng GGUF `Q4_K_M`, nhánh `tp_count==1` dùng trực tiếp `mmap` alias không nhân đôi 18 GB sang heap) và (2) nhóm AVX2 / **AVX-VNNI** (`AVXVNNI256GPTQInt4Packed_MOE`, `AVXVNNI256RawInt4_MOE`, `AVX2BF16_MOE`, `AVX2FP8_MOE`). Các backend `AMXINT4/8`, `AVX512*` **không chạy được** |
| CPU Windows `pc-win-01` | `AMD Ryzen 7 9700X` (Zen 5): **8 nhân / 16 luồng**, `avx2=true`, **`avx512f=true`**, `avx512_vnni=false`, `amx=false` `[ĐO]` (`benchmarks/reports/p0_probe_node_windows_rtx5070.json`) | Có `avx512f`, **nhưng** wheel `kt-kernel==0.7.2` và `sglang-kt==0.7.2` chỉ phát hành cho **Linux x86-64** (`manylinux_2_35_x86_64`, `Operating System :: POSIX :: Linux`) `[ĐỌC MÃ]` → không chạy được trên Windows gốc |
| GPU | `pc-001`: RTX 3060 (`12288 MiB`, `sm_86`, Driver `580.178.04`, `nvcc` `12.0.140`) `[ĐO]`; `pc-win-01`: RTX 5070 (`12227 MiB`, `sm_120`, Driver `617.42`) `[ĐO]` | `kt-kernel/CMakeLists.txt:341` đặt mặc định `CMAKE_CUDA_ARCHITECTURES "80;86;89;90"` `[ĐỌC MÃ]`: **RTX 3060 (`sm_86`) được hỗ trợ sẵn** trong wheel; **RTX 5070 (`sm_120` Blackwell) không có** trong wheel dựng sẵn |
| RAM | Linux `pc-001`: tổng `30 GiB`, nhưng hiện tiến trình màn hình nền (Chrome + IDE) đang dùng **`24 GiB` RAM + `10 GiB` Swap**, chỉ còn **`6,0 GiB` RAM khả dụng (`available`)** dù GPU đang rảnh (`15 MiB / 12288 MiB`) `[ĐO]` (`free -h`, `nvidia-smi`). Windows `pc-win-01`: `64 GB` (trống `~56 GB`) `[ĐO]` | Nạp chuyên gia của model 30B (~11–18 GiB trên CPU/mmap + ~4 GiB RSS PyTorch/SGLang) khi RAM khả dụng chỉ còn `6,0 GiB` sẽ gây **đảo trang (page thrashing) / tràn Swap trên NVMe** làm sai lệch hoàn toàn tốc độ giải mã `[SUY LUẬN]`. Muốn chạy toàn trình 30B trên `pc-001` bắt buộc phải giải phóng RAM (trống ≥ 18–20 GiB) |
| Đĩa Linux `pc-001` | Ổ dữ liệu `/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d` còn **`40 GiB` trống** (đã dùng 88%); ổ `/` còn **`12 GiB` trống** (đã dùng 97%) `[ĐO]` (`df -h`). Đã có sẵn `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` (`18 GB`) và `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` (`18 GB`) `[ĐO]` | Mọi thứ cài/tải phải nằm trên ổ dữ liệu; **tuyệt đối không** đặt vào `/`. Tổng dung lượng (venv + trọng số tải thêm) phải **< 30 GiB** để giữ an toàn tối thiểu `10 GiB` trống trên ổ dữ liệu |
| Mạng tải | Wi-Fi `~0,3–1,2 MB/s`, có lúc `~40 MB/s` `[ĐO]`, không ổn định | Tải trọng số lớn (`17–32 GB`) có thể mất từ ~7 phút đến **4–15 giờ** |

**Kết quả kiểm tra 3 rủi ro khả thi ở Pha 0 (`[ĐỌC MÃ]` + `[ĐO]`, chi tiết tại `benchmarks/reports/P8_KTRANSFORMERS.md`):**
1. **Định dạng và dung lượng thật của trọng số GPU khi dùng SGLang-KT (`[ĐỌC MÃ]` `sglang/srt/models/qwen3_moe.py:1081-1160`, `sglang/srt/layers/moe/kt_ep_wrapper.py:5208-5447` & `[ĐO]` qua Hugging Face API):**
   - SGLang-KT bắt buộc tham số `--model <THƯ_MỤC_HF>` là một thư mục Hugging Face đầy đủ để GPU nạp phần không-chuyên-gia (`embed_tokens`, `self_attn.*`, `mlp.gate`, `lm_head` ≈ 1,75 tỷ tham số) và các chuyên gia đặt trên GPU (`num_gpu_experts` mỗi tầng). Vì các file `.safetensors` trên Hugging Face trộn chung `self_attn` và `mlp.experts.{0..127}` trong từng shard, không thể chỉ tải 1–2 shard lẻ.
   - Dung lượng thật của các kho trọng số `Qwen3-30B-A3B` trên Hugging Face `[ĐO]`:
     - **Đường A (`--kt-method LLAMAFILE` + `--model Qwen/Qwen3-30B-A3B` BF16 theo `kt-kernel/README.md`):** Kho BF16 gồm 16 shard `.safetensors`, tổng cộng **`61,07 GB` (`56,87 GiB`)** `[ĐO]` → **KHÔNG KHẢ THI (VƯỢT QUÁ `40 GiB` ĐĨA TRỐNG)**.
     - **Đường A' (`--model Qwen/Qwen3-30B-A3B-FP8`):** Gồm 7 shard, tổng **`32,45 GB` (`30,22 GiB`)** `[ĐO]` → Cộng thêm venv (`~6 GiB`) sẽ tốn `~36,5 GiB / 40 GiB` (quá sát ngưỡng đầy đĩa) và RTX 3060 (`sm_86`) không hỗ trợ phần cứng FP8 (`sm_89+`).
     - **Đường B (`--kt-method GPTQ_INT4` + `--model Qwen/Qwen3-30B-A3B-GPTQ-Int4` theo `doc/en/kt-kernel/AVX2-Tutorial.md`):** Chỉ gồm **1 file `.safetensors` duy nhất nặng `16,93 GB` (`15,77 GiB`)** `[ĐO]`, dùng chung cho cả `--model` và `--kt-weight-path`, chạy nhân **`AVXVNNI256GPTQInt4Packed_MOE`** (tận dụng tập lệnh `avx_vnni` của Core Ultra 7 265K `[ĐỌC MÃ]`). Vừa ổ đĩa `40 GiB`, nhưng (i) phải tải mới `16,93 GB` và (ii) khác định dạng lượng tử (`GPTQ-Int4` so với `GGUF Q4_K_M`).
     - **Đường C (Bổ sung để kiểm chứng nhanh với `0 GB` tải trọng số mới — Micro-benchmark trực tiếp `KTMoEWrapper` `LLAMAFILE` của `kt-kernel==0.7.2`):** Nạp trực tiếp các tầng MoE từ file `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` có sẵn (`18 GB`, `tp_count==1` alias `mmap`) bằng lớp Python `KTMoEWrapper` (`kt-kernel/python/experts.py`) mà không cần tải thêm 61 GB BF16 của SGLang. Đo trực tiếp tốc độ nhân CPU `LLAMAFILE` (AVX2) trên 48 tầng MoE với cùng tỷ lệ chia chuyên gia GPU/CPU như `llama.cpp -ncmoe` để trả lời ngay câu hỏi: *Nhân CPU `LLAMAFILE` của `kt-kernel` trên `Core Ultra 7 265K` có thực sự nhanh hơn nhân GGML MoE CPU của `llama.cpp` hay không?*
2. **Yêu cầu gói Python / PyTorch / CUDA (`[ĐỌC MÃ]` metadata wheel PyPI `kt-kernel==0.7.2` & `sglang-kt==0.7.2`):**
   - Có sẵn wheel `manylinux_2_35_x86_64` cho `cp312` (khớp `Python 3.12.3` và `glibc 2.39` trên `pc-001` `[ĐO]`).
   - Đòi hỏi cố định **`torch==2.9.1`**, `flashinfer_python==0.6.3`, `flashinfer_cubin==0.6.3`, `cuda-python<13.3,>=13.1`, `apache-tvm-ffi==0.1.11`, `accelerate-kt==1.14.0.post4`, `gguf>=0.17.0` `[ĐỌC MÃ]`. (Các venv hiện có `.aicf-work/venv-vllm` dùng `torch==2.13.0` nên bắt buộc tạo venv riêng nếu cài `sglang-kt`).
3. **Ánh xạ công bằng giữa `llama.cpp -ncmoe N` và `--kt-num-gpu-experts K` (`[ĐỌC MÃ]` `sglang/srt/layers/moe/kt_ep_wrapper.py:4606-4613`):**
   - `Qwen3-30B-A3B` có `48` tầng MoE × `128` chuyên gia/tầng = `6.144` chuyên gia (`num_experts_per_tok = 8`, `hidden_size = 2048`, `moe_intermediate_size = 768`) `[ĐỌC MÃ]` (`.aicf-work/qwen3-tok/config.json`).
   - `llama.cpp -ncmoe N` đẩy toàn bộ 128 chuyên gia của `N` tầng đầu lên CPU và giữ `48 - N` tầng trên GPU. Trong khi đó `--kt-num-gpu-experts K` của KTransformers tính **số chuyên gia giữ trên GPU của mỗi tầng MoE** (`K / 128` trên GPU, `128 - K` trên CPU cho cả 48 tầng) `[ĐỌC MÃ]`. Để có cùng tổng tỷ lệ chuyên gia trên GPU/CPU (và tinh chỉnh thêm để khớp đúng đỉnh VRAM `nvidia-smi`):
     - `-ncmoe 20` (`28/48 = 58,33%` chuyên gia trên GPU) ⇔ `--kt-num-gpu-experts 75` (`75/128 = 58,59%` trên GPU, `53/128` trên CPU).
     - `-ncmoe 26` (`22/48 = 45,83%` chuyên gia trên GPU) ⇔ `--kt-num-gpu-experts 59` (`59/128 = 46,09%` trên GPU, `69/128` trên CPU).
     - `-ncmoe 30` (`18/48 = 37,50%` chuyên gia trên GPU) ⇔ `--kt-num-gpu-experts 48` (`48/128 = 37,50%` trên GPU, `80/128` trên CPU).
     - `-ncmoe 36` (`12/48 = 25,00%` chuyên gia trên GPU) ⇔ `--kt-num-gpu-experts 32` (`32/128 = 25,00%` trên GPU, `96/128` trên CPU).

**Câu hỏi cần trả lời bằng số:**
- Q1. Trên **RTX 3060 + AVX2 (đường duy nhất khả thi trên `pc-001`)**, KTransformers `LLAMAFILE` có nhanh hơn llama.cpp `-ncmoe` ở **cùng model, cùng độ lượng tử, cùng mức VRAM**? (decode, prefill, độ trễ token đầu).
- Q2. Cải thiện (nếu có) có đủ lớn để thay chế độ B, hoặc để chạy **1 card đơn lẻ** thay vì gộp 2 card (thông lượng trên mỗi card)?
- Q3. Chất lượng đầu ra có giữ nguyên không (cùng lượng tử → kỳ vọng giống; backend INT4/INT8 tự lượng tử lại có thể giảm độ chính xác `[TÀI LIỆU]`).
- Q4. Có tích hợp được vào hệ thống (worker-guard, gateway, estimator VRAM) với chi phí hợp lý không?

**Quy tắc bắt buộc (từ chủ dự án):**
1. Báo cáo và trả lời bằng **tiếng Việt**; mọi số có nhãn; nói rõ cái gì CHƯA đo.
2. Không tick mục nào trong `KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md` khi chưa có bằng chứng. **Kiểm đột biến** mọi mã mới. Tránh thí nghiệm nhiễm: hai tác vụ chung một GPU, tiến trình `llama-server`/`sglang` mồ côi, KV/cache còn sót, đĩa đầy, RAM hết.
3. **Không `git push`**; chỉ commit khi được yêu cầu (kèm dòng đồng tác giả theo hướng dẫn phiên).
4. Dùng `pkill -x <tên>` hoặc `kill <pid>`; **không** `pkill -f`/`pgrep -f` với mẫu khớp chính lệnh của mình.
5. Pha nào Check Pass không đạt: **dừng và báo cáo**, không hạ tiêu chí.
   **RAM là nguy cơ thật:** Linux 30 GB, Chrome/IDE của chủ dự án đang chiếm ~24 GB + 10 GB swap `[ĐO]`; OOM-killer đã giết dịch vụ một lần. **Không tự đóng ứng dụng của chủ dự án.** Trước mỗi lần chạy đo `free -m` và dừng nếu `available` < 8 GiB (nạp mmap 18 GB + PyTorch ~4 GB sẽ đẩy hệ thống vào swap, làm sai số đo và có thể gây OOM). Xin chủ dự án đóng bớt ứng dụng nặng hoặc hẹn khung giờ.
6. **Cụm phục vụ người dùng thật** (Qwen3-Coder gộp 2 card cho VS Code). **Trạng thái lúc chốt `[ĐO]` 2026-10-30 10:03:** `worker-guard` trên `pc-001` đã bị **kernel OOM-killer giết lúc 02:04:49** (`journalctl -k`: `teamviewerd invoked oom-killer`), RTX 3060 đang rảnh (15 MiB), gateway báo `pc-001` offline → **dịch vụ Coder đang ngừng**. Trước khi làm gì: hỏi chủ dự án muốn **khôi phục dịch vụ trước** (`deploy/demo/apply_coder_merged.sh` sau khi bật lại guard với `coding_profile.env`) hay dùng GPU để đo ngay. **Không** dừng/đổi chế độ/khởi động lại dịch vụ, gateway, worker-guard, dịch vụ Windows `AICFWorkerGuard` khi chưa hỏi chủ dự án, nêu rõ thời gian gián đoạn dự kiến và cách khôi phục (`deploy/demo/apply_coder_merged.sh`, `coding_profile.env`). Mức dự trữ (headroom) Windows đang hạ 1536→300 MiB: **đừng đổi**.
7. Windows: chỉ ghi vào `C:\aicf\`; không đụng `C:\Users\Fios02\`. Không tắt card mạng/không làm mất kết nối. **Không cài WSL2/đổi hệ điều hành** khi chưa hỏi (đây là thay đổi hệ thống).
8. Cài đặt: môi trường ảo mới nằm trên **ổ dữ liệu** (vd `.aicf-work/ktransformers/venv`); **không** cài vào venv dự án (`.venv`) và không làm hỏng các gói của AI Studio/agents. Mã/gói tải từ ngoài là **không tin cậy**: để trong thư mục riêng, chạy Python bằng `-I`, không chạy từ trong thư mục chứa mã tải về.
9. Không đưa khoá API, mật khẩu, file `.whl`, trọng số hay dữ liệu rác vào repo.

---

## PHẦN 1: PHƯƠNG PHÁP ĐO (ÁP DỤNG CHO MỌI PHA)

- **So sánh công bằng:** cùng model (`Qwen3-30B-A3B-Q4_K_M.gguf`, file có sẵn `.aicf-work/`, SHA256 ghi lại), cùng prompt (dùng `benchmarks/hardware/probe_server_cfg.py` và `long_prompt_bench.py` đã có để mọi phía nhận **cùng** văn bản), cùng `max_tokens`, `temperature=0`, cùng ngữ cảnh (8k và 32k).
- **Cùng ngân sách VRAM:** đưa mỗi cấu hình về cùng mức VRAM dùng (đo bằng `nvidia-smi`, đỉnh trong lượt chạy) rồi so tốc độ; hoặc vẽ đường cong tốc độ theo VRAM. Không so một bên chiếm 11 GB với bên kia 6 GB.
- **Chỉ số:** tốc độ giải mã (tok/s), tốc độ xử lý prompt/prefill (tok/s), độ trễ token đầu (TTFT) với prompt 1k/8k/24k token, VRAM đỉnh, RAM đỉnh, tải CPU, thời gian nạp model.
- **Thống kê:** ≥5 lần mỗi điểm, báo **trung vị, nhỏ nhất, lớn nhất**; nhiễu đã biết ±3 tok/s `[ĐO]`; chênh lệch dưới nhiễu = "không phân biệt được".
- **Không nhiễm:** một việc một lúc trên GPU; ghi `nvidia-smi` trước/sau; xác nhận không còn tiến trình cũ; CPU ổn định (ghi governor/xung nếu đổi được mà không cần quyền hệ thống — nếu không, ghi rõ).
- **Chất lượng:** cùng GGUF → so đầu ra từng token ở `temperature=0` (độ lệch), cộng thêm tập nhỏ bài toán có đáp án (nếu `benchmarks/hardware/code_eval.py` của kế hoạch Best-of-N đã có thì dùng lại; chưa có thì dùng bộ câu hỏi `quiz_models.py`).
- Mọi dữ liệu thô vào `benchmarks/reports/p8_ktransformers_*.jsonl`, log vào `benchmarks/reports/logs/`, lệnh chạy chính xác ghi trong báo cáo.

---

## PHẦN 2: CÁC PHA

Mỗi pha có **Check Pass**; không đạt thì **dừng và báo cáo**.

### Pha 0 — Khảo sát khả thi (không đụng cụm, không tải lớn) — **ĐÃ HOÀN THÀNH (2026-10-30, chi tiết tại `benchmarks/reports/P8_KTRANSFORMERS.md`)**
- 0.1 Đã đọc nguyên văn tài liệu + mã nguồn `kt-kernel==0.7.2` và `sglang-kt==0.7.2` (`kt-kernel/README.md`, `doc/en/kt-kernel/AVX2-Tutorial.md`, `doc/en/operators/llamafile.md`, `kt-kernel/CMakeLists.txt`, `kt-kernel/python/experts.py`, `kt-kernel/python/utils/llamafile.py`, `kt-kernel/python/utils/amx.py`, `kt-kernel/operators/llamafile/moe.hpp`, `sglang/srt/models/qwen3_moe.py`, `sglang/srt/layers/moe/kt_ep_wrapper.py`) `[ĐỌC MÃ]`:
  - **(a) Trọng số GPU & dung lượng thật:** SGLang-KT cần thư mục HF đầy đủ ở `--model`; bản `Qwen/Qwen3-30B-A3B` BF16 nặng **`61,07 GB` (`56,87 GiB`)** `[ĐO]` (**không vừa `40 GiB` đĩa trống**); bản `Qwen/Qwen3-30B-A3B-GPTQ-Int4` nặng **`16,93 GB` (`15,77 GiB`)** `[ĐO]` dùng chung cho `--model` và `--kt-weight-path` với `--kt-method GPTQ_INT4` (`AVXVNNI256GPTQInt4Packed_MOE` `[ĐỌC MÃ]`).
  - **(b) `--kt-num-gpu-experts`:** tính **trên mỗi tầng MoE** (`/ 128` chuyên gia/tầng, nhân với 48 tầng trong `kt_ep_wrapper.py:4606-4613`) `[ĐỌC MÃ]`.
  - **(c) Yêu cầu Python/PyTorch/CUDA/SGLang:** Python 3.10–3.12 (`cp312` khớp `Python 3.12.3` + `glibc 2.39` trên `pc-001` `[ĐO]`), `torch==2.9.1`, `flashinfer_python==0.6.3`, `flashinfer_cubin==0.6.3`, `cuda-python<13.3,>=13.1`, `apache-tvm-ffi==0.1.11`, `accelerate-kt==1.14.0.post4` `[ĐỌC MÃ]`.
  - **(d) Kiến trúc GPU:** Hỗ trợ sẵn `sm_80;sm_86;sm_89;sm_90` (`kt-kernel/CMakeLists.txt:341`) → **RTX 3060 (`sm_86`) đạt**, **RTX 5070 (`sm_120`) không có** trong wheel dựng sẵn và gói chỉ hỗ trợ Linux `[ĐỌC MÃ]`.
  - **(e) API chuẩn OpenAI:** `sglang.launch_server` cung cấp đầy đủ `/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/health` `[ĐỌC MÃ]`.
- 0.2 Đã kiểm tra thực tế cả 2 máy (`lscpu`, `lscpu -e`, `free -h`, `df -h`, `nvidia-smi`, `nvcc --version`, `p0_probe_node_windows_rtx5070.json`) `[ĐO]`.
- 0.3 **Kết luận Check Pass Pha 0 (Bắt buộc hỏi chủ dự án trước khi qua Pha 1/2):**
  1. **Đường A (`sglang-kt` + `--kt-method LLAMAFILE` + `--model Qwen/Qwen3-30B-A3B` BF16):** **DỪNG** vì trọng số BF16 nặng `56,87 GiB` > `40 GiB` đĩa trống `[ĐO]`.
  2. **Đường C (Pha 2A — Khuyến nghị chạy trước, `0 GB` tải trọng số mới):** Chỉ cài `kt-kernel==0.7.2` (`101,5 MB` wheel) vào venv riêng trên ổ dữ liệu, dùng trực tiếp lớp `KTMoEWrapper` (`backend="LLAMAFILE"`) nạp thẳng file `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` có sẵn (`18 GB`, nhánh `tp_count==1` alias `mmap` `[ĐỌC MÃ]`), đo tốc độ thực thi CPU MoE của 48 tầng ở `num_gpu_experts = 75, 59, 48, 32` (tương ứng `-ncmoe 20, 26, 30, 36`) với `20` luồng (`8P+12E`) và `8` luồng (`8P`). Phép đo này kiểm chứng trực tiếp xem nhân `LLAMAFILE` AVX2 của `kt-kernel` trên `Core Ultra 7 265K` có thắng được nhân GGML MoE CPU của `llama.cpp` hay không **mà không tốn 1 byte tải trọng số mới**.
  3. **Đường B (Pha 2B — Toàn trình `sglang-kt` + `--kt-method GPTQ_INT4` AVX-VNNI):** Khả thi về đĩa (`15,77 GiB` model + `~6 GiB` venv ≈ `22 GiB / 40 GiB`), nhưng cần **hỏi chủ dự án** vì: (i) phải tải mới `16,93 GB` từ `Qwen/Qwen3-30B-A3B-GPTQ-Int4`, (ii) khác phương pháp lượng tử (`GPTQ-Int4` so với `GGUF Q4_K_M`), và (iii) cần giải phóng RAM trên `pc-001` từ `6,0 GiB` khả dụng hiện tại lên `≥ 18–20 GiB`.

### Pha 1 — Đường cơ sở llama.cpp, đo lại trong cùng phiên (cần chiếm GPU)
- **Cần hỏi chủ dự án trước:** Ghi chú hiện trạng `[ĐO]`: tiến trình `worker-guard`/`llama-server` trên `pc-001` đã tự dừng lúc `2026-10-30 02:04:50` (`.demo/logs/worker.log`, GPU RTX 3060 hiện chỉ dùng `15 MiB / 12288 MiB`), nhưng `site-gateway` (`PID 1105295`) vẫn đang chạy với `.demo/topology.json` ở chế độ `merged`. Cần chủ dự án xác nhận cho phép chiếm GPU RTX 3060 để đo Pha 1–2 và xác nhận có khôi phục lại `worker-guard` (`deploy/demo/apply_coder_merged.sh`) ngay sau khi đo xong hay không.
- 1.1 RTX 3060, `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf`, `-c 8192` và `-c 32768`, quét `-ncmoe N` quanh các giá trị đã đo (`N=20, 26, 30, 36`, cộng thêm `N=0 / -ngl 99` trên phần không-chuyên-gia hoặc `N=48` để tách riêng thời gian GPU attention và thời gian CPU MoE mỗi token), `-fa on`, ghi VRAM đỉnh và RAM khả dụng. Dùng `benchmarks/hardware/sweep_ncmoe.py` / `probe_server_cfg.py`.
- 1.2 Đo prefill và TTFT với prompt dài (1k/8k/24k token) bằng `long_prompt_bench.py`.
- **Check Pass:** bảng "N × (VRAM đỉnh, giải mã, prefill, TTFT)" lặp lại được (chạy ≥5 lần/điểm, báo trung vị/min/max, chênh trong nhiễu); khớp thứ tự độ lớn với số cũ ở Phần 0, hoặc có giải thích nếu lệch (ví dụ do áp lực RAM/Swap nếu RAM trống thấp).

### Pha 2 — Cài và đo KTransformers trên RTX 3060 (Chia 2 bước có cổng lọc sớm)
- **Pha 2A (Cổng lọc sớm — 0 GB tải trọng số mới, cùng file `Qwen3-30B-A3B-Q4_K_M.gguf`):**
  - 2A.1 Tạo môi trường ảo `.aicf-work/ktransformers/venv` trên ổ dữ liệu; cài `kt-kernel==0.7.2` + `gguf` (tận dụng PyTorch có sẵn hoặc venv tối giản, ghi rõ dung lượng đĩa thực tế). Chạy `kt version` / kiểm tra tính năng CPU được nhận diện (`CPUInfer`, `LLAMA_MOE_TP`).
  - 2A.2 Viết script chuẩn đo trực tiếp `KTMoEWrapper(backend="LLAMAFILE")` nạp thật các tensor `blk.{i}.ffn_{gate,up,down}_exps.weight` từ `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` (`hidden_size=2048`, `moe_intermediate_size=768`, `num_experts=128`, `num_experts_per_tok=8`).
  - 2A.3 Đo thời gian thực thi CPU MoE của 48 tầng ở `num_gpu_experts = 75, 59, 48, 32` (tương ứng `-ncmoe 20, 26, 30, 36`), quét `cpuinfer_threads = 20` (`8P + 12E`) và `cpuinfer_threads = 8` (chỉ `8 P-core`, ghim `taskset -c 0-7`), `max_deferred_experts_per_token ∈ {0, 1, 2, 4}`, cho cả decode (`qlen=1`) và prefill (`qlen=512, 2048`).
  - 2A.4 **Mốc so sánh phía llama.cpp (phải có, nếu không 2A vô nghĩa):** thời gian phép nhân chuyên gia (`MUL_MAT_ID`) trên CPU của llama.cpp với **cùng hình dạng** (hidden 2048, ffn 768, 128 chuyên gia, top-8, Q4_K) bằng `test-backend-ops perf -o MUL_MAT_ID` `[CHƯA XÁC MINH có sẵn trong build 11913]`; nếu không có, dùng đo đầu-cuối gián tiếp `llama-bench` ở `-ngl 99` với `-ncmoe 0` và `-ncmoe 48` rồi suy ra phần CPU (ghi rõ đây là ước lượng `[SUY LUẬN]`).
  - **Check Pass Pha 2A:** Có số đo ms/token của 48 tầng MoE trên `kt-kernel LLAMAFILE` và mốc llama.cpp cùng điều kiện (RAM `available` ≥ 8 GiB, không swap). **Giới hạn phải ghi trong báo cáo:** 2A chỉ đo **nhân CPU riêng lẻ**; lợi thế hệ thống của KTransformers (đặt chuyên gia lên GPU theo từng tầng, hoãn chuyên gia `--kt-max-deferred-experts-per-token`, chồng lấp CPU/GPU) **không** lộ ra ở đây. Vì vậy: nếu nhân KT **chậm hơn rõ rệt** (≥ 15%) ở decode VÀ không nhanh hơn ở prefill → **dừng tại 2A, kết luận "không đáng"** (tiết kiệm 17 GB tải); nếu **ngang hoặc nhanh hơn** → phải qua Pha 2B (có chủ dự án duyệt) mới kết luận được cho cả hệ thống; chênh trong nhiễu → "không phân biệt được", chưa kết luận.
- **Pha 2B (Toàn trình `sglang-kt==0.7.2` — chỉ chạy khi chủ dự án duyệt tải thêm `16,93 GB` và giải phóng RAM ≥ 18 GiB):**
  - 2B.1 Cài đầy đủ `sglang-kt==0.7.2` + `torch==2.9.1` vào `.aicf-work/ktransformers/venv`; tải `Qwen/Qwen3-30B-A3B-GPTQ-Int4` (`15,77 GiB`) vào `.aicf-work/Qwen3-30B-A3B-GPTQ-Int4` và kiểm SHA256.
  - 2B.2 Chạy `python -I -m sglang.launch_server --model-path .aicf-work/Qwen3-30B-A3B-GPTQ-Int4 --kt-weight-path .aicf-work/Qwen3-30B-A3B-GPTQ-Int4 --kt-method GPTQ_INT4 --kt-cpuinfer <8|20> --kt-threadpool-count 1 --kt-num-gpu-experts <K> --kt-max-deferred-experts-per-token <0|1|2|4> --attention-backend flashinfer`.
  - 2B.3 Thu cùng các chỉ số Phần 1; đưa về **cùng VRAM** với từng điểm của Pha 1; ghi mọi cấu hình lỗi.
  - **Check Pass Pha 2B:** Có ít nhất một cấu hình chạy ổn định và đo được; nếu không chạy được (thiếu kernel, hết RAM/Swap, lỗi cài), ghi nguyên nhân thật và **dừng**.

### Pha 3 — So sánh có kiểm soát và chất lượng
- 3.1 Bảng song song: llama.cpp `-ncmoe` (Pha 1) so với KTransformers (Pha 2A / 2B) theo **cùng tỷ lệ chuyên gia / cùng VRAM** và **cùng ngữ cảnh**: giải mã, prefill, TTFT, RAM, tải CPU; kèm hiệu (%).
- 3.2 Kiểm chất lượng: so đầu ra `temperature=0` (độ lệch token đầu tiên, độ dài) + tập bài có đáp án (`quiz_models.py` / `code_eval.py`); ghi rõ nếu dùng `GPTQ_INT4` thì khác phương pháp lượng tử so với `GGUF Q4_K_M`.
- 3.3 **Kiểm đột biến phép đo:** cố ý đổi một tham số có tác dụng đã biết (vd giảm `num_gpu_experts` từ `75` xuống `32` hoặc đổi `qlen` từ `1` lên `64`) → số đo phải đổi theo hướng dự đoán; nếu không đổi, công cụ đo hỏng.
- **Check Pass:** mỗi ô bảng có ≥5 lần đo, trung vị/min/max; chênh trong nhiễu được gọi đúng tên "không phân biệt được".

### Pha 4 — RTX 5070 / Windows và máy có AVX-512 — **ĐÃ XÁC MINH Ở PHA 0: TẠM DỪNG (KHÔNG HỖ TRỢ TRÊN WINDOWS GỐC / SM_120)**
- 4.1 Đã xác minh ở Pha 0 `[ĐỌC MÃ]` + `[ĐO]`:
  - `pc-win-01` có CPU `AMD Ryzen 7 9700X` hỗ trợ `avx512f=true` (`avx512_vnni=false`, `amx=false`) và `64 GB RAM` `[ĐO]`.
  - Tuy nhiên `kt-kernel==0.7.2` và `sglang-kt==0.7.2` chỉ phát hành wheel cho **Linux x86-64** (`manylinux_2_35_x86_64`), và `kt-kernel/CMakeLists.txt:341` chỉ biên dịch sẵn cho `CMAKE_CUDA_ARCHITECTURES "80;86;89;90"` — **không có `sm_120` (Blackwell RTX 5070)** `[ĐỌC MÃ]`.
  - Theo Quy tắc 7 (**Không cài WSL2/đổi hệ thống khi chưa hỏi chủ dự án**), ghi nhận lựa chọn **(a) không hỗ trợ trên môi trường hiện tại của `pc-win-01`**; chỉ chuyển sang (b) nếu chủ dự án chủ động yêu cầu dựng môi trường Linux/WSL2 + biên dịch mã nguồn CUDA 12.8+ cho `sm_120`.

### Pha 5 — Đánh giá khả năng tích hợp (không viết mã sản xuất nếu kết quả không đáng)
Chỉ làm khi Pha 3 cho thấy cải thiện đáng kể (tiêu chí ở Phần 3).
- 5.1 Đối chiếu với `worker-guard`: engine hiện chỉ là `llama-server` (`apps/worker-guard/engine.go`, `Args()`); cần gì để chạy `sglang`/KT như một engine (khởi động, kiểm sức khoẻ, đo VRAM thật cho cơ chế headroom, dừng êm)?
- 5.2 Đối chiếu với gateway (`/v1` proxy, `ClusterTopology` mode `smart_1gpu`), bộ ước lượng VRAM (`estimate-vram`, hiện dựa trên GGUF/llama.cpp), và khái niệm agent tính toán (Phần 2B của `KE_HOACH_BEST_OF_N_CODE.md`).
- 5.3 Ước chi phí và rủi ro; **chỉ đề xuất**, không tích hợp khi chưa được duyệt.
- **Check Pass:** tài liệu đề xuất tích hợp có danh sách thay đổi, rủi ro, phương án quay lui; hoặc ghi "không đáng tích hợp" kèm số liệu.

---

## PHẦN 3: TIÊU CHÍ KẾT LUẬN (đặt TRƯỚC khi đo, không đổi sau)

- **Đáng cân nhắc tích hợp** nếu, ở cùng VRAM, cùng chất lượng: giải mã tăng **≥ 15%** HOẶC prefill/TTFT cải thiện **≥ 30%** trên ít nhất một ngữ cảnh mà hệ thống thật sự dùng (8k hoặc 32k), và chênh lệch **lớn hơn nhiễu** (≥5 lần đo, khoảng min–max không chồng nhau).
- **Không đáng** nếu cải thiện nằm trong nhiễu, hoặc chất lượng giảm, hoặc không chạy ổn định, hoặc chi phí cài/đĩa/RAM/bảo trì lớn hơn lợi ích. Báo cáo kết luận này như một kết quả bình thường.
- **Không thay được chế độ C:** nếu model nằm trọn trong VRAM 2 card (30B Q4, 18,6 GB) thì chế độ gộp đã đạt ~126–135 tok/s; KTransformers chỉ có ý nghĩa cho **model không vừa VRAM** hoặc cho việc **1 card đơn lẻ**. Nêu rõ trong báo cáo: thông lượng trên mỗi card (tok/s ÷ số card) của KTransformers 1 card so với gộp 2 card.
- **Model lớn hơn (80B, 235B…):** chỉ thử nếu Pha 0 chứng minh RAM/đĩa đủ (Linux 30 GB, Windows 64 GB `[ĐO]`); nếu không thì ghi "ngoài khả năng phần cứng", không tải về để thử.

## PHẦN 4: KẾT QUẢ BÀN GIAO

1. `benchmarks/reports/P8_KTRANSFORMERS.md`: Phần Pha 0 (bảng yêu cầu/máy), bảng đường cơ sở, bảng so sánh Pha 3 kèm hiệu %, kết luận theo tiêu chí Phần 3, những gì **không** đo được hoặc không chạy được và vì sao.
2. Dữ liệu thô `p8_ktransformers_*.jsonl`, log, lệnh chạy chính xác, phiên bản gói (KTransformers/SGLang/PyTorch/CUDA).
3. Nếu có cải thiện: tài liệu đề xuất tích hợp (Pha 5). Nếu không: một đoạn giải thích rõ vì sao và khi nào nên thử lại (phần cứng AVX-512/AMX, model lớn hơn, bản mới).
4. Khôi phục hoàn toàn: cụm trở về Qwen3-Coder gộp 2 card đang phục vụ (`apply_coder_merged.sh`), môi trường thử nghiệm dọn sạch hoặc ghi rõ nằm ở đâu; không để tiến trình/GPU bị chiếm.

## PHẦN 5: RỦI RO

| Rủi ro | Hậu quả | Cách giảm |
|---|---|---|
| Dừng cụm để đo làm gián đoạn người dùng | Mất dịch vụ | Hỏi trước; khung giờ ngắn; khôi phục bằng script và kiểm sau khi xong |
| Trọng số GPU quá lớn so với ổ ~40 GB | Không chạy được / đĩa đầy làm hỏng hệ thống | Pha 0 tính trước; dừng nếu không đủ; dùng ổ dữ liệu |
| RAM hết khi nạp chuyên gia | **Đã xảy ra:** OOM-killer giết `worker-guard` lúc 02:04:49 ngày 30/10 `[ĐO]` → dịch vụ Coder ngừng ~8 giờ | Đo `free -m` trước mỗi lần chạy, dừng nếu `available` < 8 GiB; không đóng ứng dụng của chủ dự án; xin khung giờ; theo dõi `journalctl -k` sau mỗi lần |
| Cài gói làm hỏng môi trường dự án | Hỏng AI Studio/agents | Môi trường ảo riêng ở ổ dữ liệu, không đụng `.venv` |
| So sánh không công bằng (khác VRAM, khác prompt, khác lượng tử) | Kết luận sai | Phần 1; kiểm đột biến phép đo |
| Số máy chủ trong README bị hiểu là áp dụng cho máy này | Kỳ vọng sai | Phần 0 nói rõ; chỉ tin số đo trên máy này |
| RTX 5070 (sm_120) không được hỗ trợ | Không đo được card nhanh nhất | Ghi nhận như kết luận; không ép cài WSL2/đổi hệ thống |
