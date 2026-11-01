# KẾ HOẠCH: THỬ NGHIỆM vLLM (QUA WSL2 TRÊN WINDOWS) VÀ SO SÁNH VỚI GIẢI PHÁP llama.cpp HIỆN TẠI
*(Bàn giao cho Agent thực thi. Bản 2026-10-31. Mục tiêu: **đo và so sánh**, cả chạy riêng từng card lẫn gộp 2 card. "vLLM không đáng" hoặc "không khả thi trên Win10 + WSL2" là kết quả hợp lệ.)*

**Quy ước nhãn:** `[ĐO]` đã đo · `[ĐỌC MÃ]` đã đọc trong repo · `[TÀI LIỆU]` theo tài liệu, chưa kiểm · `[SUY LUẬN]` tính toán · `[CHƯA XÁC MINH]` phải thử. Con số không có `[ĐO]` là chưa đo. Báo cáo bằng tiếng Việt.

---

## PHẦN 0: HIỆN TRẠNG ĐÃ KIỂM TRA (2026-10-31)

| Hạng mục | Giá trị | Nhãn |
|---|---|---|
| Windows `pc-win-01` (SSH `fios02@10.42.0.225`, qua cáp) | **Windows 10 Pro, build 19045**; RTX 5070 12 GiB (`sm_120`), driver 617.42; RAM 64 GB (~55 GiB trống); `C:` trống **~88 GB** | `[ĐO]` |
| WSL trên Windows | `wsl -l -v` chỉ in trợ giúp lệnh ⇒ **WSL2 chưa được cài** (chưa có bản phân phối) | `[ĐO]`, kết luận "chưa cài" là `[SUY LUẬN]` |
| Dịch vụ đang chiếm GPU Windows | `AICFWorkerGuard` (RUNNING, cổng 8099) và `ggml-rpc-server.exe` (cổng 50052, ~544 MiB VRAM) | `[ĐO]` |
| Linux `pc-001` | RTX 3060 12 GiB; từng chạy adapter vLLM 0.32.0 + torch 2.13.0+cu130 với Qwen3-0.6B: 14/14 test vòng đời pass | `[ĐO]` (`benchmarks/reports/p3_vllm_adapter_contract.json`) |
| Adapter vLLM trong repo | `inference/adapters/vllm/adapter.py`: không offload từng layer, `gpu_layers==0` bị từ chối, `kv_type q8_0` ánh xạ sang fp8 (khác định dạng ggml) | `[ĐỌC MÃ]` |
| Đối chứng llama.cpp (phải đo lại, không dùng số cũ làm mẫu số) | gộp 2 card Qwen3-30B-A3B ~135 tok/s (8k) / ~131,7 (32k); Coder gộp 132,4 / 126,7; một card `-ncmoe 20`: 3060 ~42–44 tok/s, 5070 ~65,6 tok/s | `[ĐO]` quá khứ (P3/P8) |

**Rủi ro lớn nhất đã thấy ngay từ đầu:**
1. **Windows 10 không có chế độ mạng WSL2 `mirrored` hay `bridged`** (chỉ có từ Windows 11 22H2 trở lên) `[TÀI LIỆU, CHƯA XÁC MINH trên máy này]`. WSL2 trên Win10 chạy NAT: máy Linux không tới trực tiếp được tiến trình trong WSL2, phải chuyển cổng bằng `netsh interface portproxy`. vLLM đa nút (Ray / `torch.distributed` + NCCL) dùng nhiều cổng, một số cổng động ⇒ **việc gộp 2 card giữa Linux và WSL2 có thể không khả thi hoặc rất mong manh** `[SUY LUẬN]`. Vì vậy Pha V4 là một **spike có giới hạn thời gian**, không phải cam kết.
2. **Blackwell (`sm_120`) trong WSL2 + vLLM** `[CHƯA XÁC MINH]`: cần torch/vLLM đủ mới (bản Linux hiện có là torch 2.13.0+cu130 chạy được trên 3060, chưa thử trên 5070).
3. **WSL2 dùng GPU qua WDDM:** đã đo trước đây rằng 5070 trên Windows tụt hiệu năng khi vượt ~52–60% VRAM do tràn vào RAM chia sẻ `[ĐO]`; vLLM đặt trước phần lớn VRAM (`gpu_memory_utilization`), cần theo dõi hiện tượng tương tự.
4. **Cài WSL2 cần bật tính năng Windows và có thể phải khởi động lại máy Windows**, trong khi máy này đang giữ vai trò RPC trong cụm.

---

## PHẦN 1: ĐIỀU KIỆN AN TOÀN (BẮT BUỘC HỎI CHỦ DỰ ÁN TRƯỚC)

- Cài WSL2 / bật tính năng Windows / khởi động lại Windows / dừng `AICFWorkerGuard` và `ggml-rpc-server` trên Windows: **mỗi việc phải xin phép riêng**, nêu rõ tác động. Không tự ý.
- Phép đo trên `pc-001` chiếm GPU RTX 3060: **không chạy chồng với P9** (`KE_HOACH_TOI_UU_CPU_KV_NCMOE.md`) hay dịch vụ Coder. Kiểm tra `nvidia-smi` và `ps` trước; hiện có một `llama-server` đang giữ ~11,7 GiB VRAM trên `pc-001`.
- Giới hạn tài nguyên: Windows `C:` trống ~88 GB ⇒ tổng (VHDX của WSL + venv + model) **< 50 GB**; Linux ổ dữ liệu trống ~40 GiB và `/` chỉ ~12 GiB ⇒ **mọi thứ cài trên ổ dữ liệu, không đặt vào `/`**.
- `.wslconfig`: giới hạn bộ nhớ WSL2 (đề xuất `memory=40GB`, `swap=0`) để không làm Windows hết RAM.
- Không đụng chế độ C đang cấu hình sẵn: phải khôi phục đúng trạng thái dịch vụ (worker-guard, rpc-server, gateway) sau mỗi phiên đo, và ghi lại trạng thái trước/sau.
- Không `trust_remote_code`, chỉ tải model từ Hugging Face chính thức, ghi SHA256/commit.
- Không commit/push trừ khi được yêu cầu.

---

## PHẦN 2: GIAO THỨC ĐO (ÁP DỤNG CHO MỌI PHA)

1. **Cùng một máy khách đo cho cả hai engine.** vLLM không trả `timings` như llama.cpp, nên **không dùng số tự báo của engine để so**. Viết một client `benchmarks/hardware/p10_client.py` gọi API `/v1/chat/completions` dạng streaming, tự đo: TTFT, thời gian giữa các token, tok/s decode (số token / thời gian sinh sau token đầu), tok/s prefill (số token prompt / TTFT, kiểm tra bằng `usage.prompt_tokens`). Dùng **cùng prompt, `temperature 0`, `seed` cố định, `max_tokens 256`** cho cả hai.
2. **Chặn bộ nhớ đệm tiền tố:** vLLM bật prefix caching mặc định; llama.cpp có slot/LCP cache. Gắn tiền tố nonce duy nhất ở đầu mỗi prompt (`[RunID=<cfg>_rep<r>_<uuid>]`) và kiểm tra token prompt thực sự được tính (vLLM: tắt bằng `--no-enable-prefix-caching` khi đo prefill; llama.cpp: kiểm `timings.prompt_n`).
3. **Số lần lặp:** mỗi cấu hình sau 1 lượt khởi động bỏ đi: **≥ 7 lần** decode, **≥ 3 lần** prefill (prompt ≈ 2k, 8k, 24k token). Báo `median/min/max`. Nhiễu coi là ±3 tok/s (đã đo ở P3); chênh nhỏ hơn ghi "không phân biệt được".
4. **Xen kẽ ABAB giữa hai engine** (llama.cpp → vLLM → llama.cpp → vLLM) trong cùng phiên, cùng nhiệt độ; baseline đo lại trong phiên.
5. **Đa người dùng:** đồng thời `1, 4, 8, 16` yêu cầu (client dùng `asyncio`), mỗi yêu cầu `max_tokens 256` và prompt ≈ 1k. Ghi **thông lượng tổng (tok/s tất cả luồng)**, tok/s/người dùng thấp nhất (P5 hoặc min), TTFT P50/P95, số yêu cầu lỗi. llama.cpp chạy `-np N` với `-c = ctx×N` (đúng quy ước guard) để công bằng.
6. **Ghi mỗi lần:** VRAM đỉnh từng card (`nvidia-smi` mỗi 0,5 s), `MemAvailable`/swap, nhiệt GPU/CPU, lệnh khởi động đầy đủ, phiên bản vLLM/torch/CUDA/driver, SHA model.
7. **Kiểm chứng đột biến (mutation):** với kết luận "vLLM nhanh hơn X", chạy một cấu hình cố ý sai (ví dụ giảm `--max-num-seqs` xuống 1 khi đo đa người dùng) và xác nhận thông lượng giảm đúng hướng; kiểm tra token đầu ra thực sự khác nhau giữa các yêu cầu (không bị trả cache).
8. **Công bằng về lượng tử:** vLLM dùng AWQ/GPTQ-Int4, llama.cpp dùng GGUF Q4_K_M; cùng ~4 bit nhưng **không cùng định dạng**. Ghi rõ trong báo cáo, và chạy mẫu chất lượng nhỏ (Pha V2.4) để chắc không so sánh một bên sinh rác.
9. Lưu: `ai-compute-fabric/benchmarks/reports/p10_*.jsonl` + `reports/logs/p10_*.log`; báo cáo `ai-compute-fabric/benchmarks/reports/P10_VLLM_WSL2.md`; runner `benchmarks/hardware/p10_*_runner.py`.

---

## PHẦN 3: CÁC PHA

### Pha V0: Khảo sát chỉ-đọc và xin phép (≈ 30 phút, không thay đổi hệ thống)
- Kiểm tra (chỉ đọc) trên Windows: `wsl --version`, trạng thái tính năng `VirtualMachinePlatform` và `Microsoft-Windows-Subsystem-Linux` (dùng `dism /online /get-featureinfo` hoặc `Get-WindowsOptionalFeature -FeatureName <từng tên, gọi riêng>`), ảo hóa trong BIOS (`systeminfo`), phiên bản driver NVIDIA có hỗ trợ WSL2 CUDA.
- Kiểm tra Linux: venv vLLM sẵn có (đường dẫn, phiên bản), dung lượng ổ dữ liệu.
- **Nộp danh sách việc cần xin phép** (bật tính năng, khởi động lại, dừng dịch vụ, tải bao nhiêu GB) cho chủ dự án và chờ duyệt.
- **Cổng V0:** chỉ sang V1 khi được duyệt từng việc.

### Pha V1: Dựng môi trường vLLM trong WSL2 (≈ 2–4 giờ + chờ tải)
- Cài WSL2 + Ubuntu 22.04/24.04 (đặt VHDX trên `C:` với đủ chỗ), `.wslconfig` giới hạn bộ nhớ, **không cài driver NVIDIA bên trong WSL** (dùng driver Windows).
- Tạo venv, cài `vllm` (cùng phiên bản đã dùng trên Linux, ghi phiên bản + torch + CUDA), **không cài CUDA toolkit nếu không cần**; nhớ `VLLM_USE_FLASHINFER_SAMPLER=0` như adapter đã ghi nhận nếu thiếu `nvcc` `[ĐỌC MÃ]`.
- Kiểm tra: `torch.cuda.is_available()`, tên GPU = RTX 5070, chạy Qwen3-0.6B với vLLM, sinh được văn bản. **Đây là cổng Blackwell** `[CHƯA XÁC MINH]`: nếu `sm_120` không chạy được, ghi lại lỗi chính xác, thử một bản vLLM/torch mới hơn một lần, rồi dừng và báo.
- **Cổng V1:** vLLM phục vụ được Qwen3-0.6B trên RTX 5070 trong WSL2. Không đạt ⇒ chỉ còn đo trên RTX 3060 (Linux), ghi rõ.

### Pha V2: Chạy riêng từng card (≈ 1 ngày)
Mô hình vừa 12 GiB để so công bằng với llama.cpp cùng model:
- **V2.1 Model:** `Qwen3-8B` (đối chiếu GGUF `Q4_K_M` trên llama.cpp với `Qwen/Qwen3-8B-AWQ` hoặc GPTQ-Int4 trên vLLM). Có thể thêm một model 14B nếu vừa 12 GiB. Ghi dung lượng tải.
- **V2.2 Ma trận:** (a) RTX 3060 Linux: vLLM (native) vs llama.cpp (native); (b) RTX 5070: vLLM (WSL2) vs llama.cpp (Windows native, đã có sẵn trong dịch vụ). Mỗi (engine, card): decode 1 người dùng, prefill/TTFT ở 2k/8k/24k, đa người dùng 1/4/8/16, VRAM đỉnh, thời gian khởi động.
- **V2.3 Chi phí WSL2:** nếu có thể, đo thêm llama.cpp **trong WSL2** (CUDA) so với llama.cpp native Windows để tách riêng "vLLM vs llama.cpp" khỏi "WSL2 vs native".
- **V2.4 Chất lượng mẫu:** 10–15 bài mã Python có unit test (`temperature 0`) trên cả hai engine; xác nhận pass rate không chênh bất thường (nếu một bên sinh sai, so sánh tốc độ vô nghĩa).
- **V2.5 (tùy chọn, hạn 2 giờ):** model 30B-A3B trên **một** card với `--cpu-offload-gb` của vLLM so với `-ncmoe 20` `[TÀI LIỆU, CHƯA XÁC MINH]`. Nhiều khả năng chậm hơn rõ rệt hoặc không chạy; ghi kết quả thật.

### Pha V3: Tổng hợp chạy riêng và quyết định có đi tiếp V4 hay không
- Lập bảng: tok/s decode, TTFT, thông lượng ở 4/8/16 người dùng, VRAM cho từng (engine, card).
- **Cổng V3:** đi tiếp V4 chỉ khi V1 đạt (có vLLM trên Windows) hoặc chủ dự án muốn thử một số cấu hình khác. Nếu vLLM thua rõ ở mọi mức người dùng, vẫn thực hiện V4 một cách rút gọn để có đủ dữ liệu so sánh gộp card, nhưng ghi nhận là **kết quả phụ**.

### Pha V4: Spike gộp 2 card giữa Linux và WSL2 (hạn **4 giờ**, kết quả "không khả thi" hợp lệ)
- Thiết lập mạng: tìm cách để nút Linux (`10.42.0.1`) và tiến trình trong WSL2 (NAT) kết nối được hai chiều: `netsh interface portproxy` cho dải cổng cố định, đặt cổng cố định cho `torch.distributed`/Ray, `NCCL_SOCKET_IFNAME`, `NCCL_IB_DISABLE=1`, `GLOO_SOCKET_IFNAME`. Ghi nhận **mỗi cổng** cần dùng.
- Đo trước: băng thông và độ trễ thực giữa Linux và trong WSL2 (`iperf3`), so với cáp thẳng (kỳ vọng ~2,3–2,5 Gbit/s `[SUY LUẬN]`).
- Thử khởi tạo 2 nút (Ray cluster hoặc `--nnodes 2` với `torch.distributed`) và chạy Qwen3-0.6B với pipeline parallel 2. Nếu thành công, chuyển sang V5. Nếu không thành công sau 4 giờ, ghi rõ chặn ở bước nào, các cách đã thử, **đề xuất** (ví dụ cần Windows 11 `mirrored` hoặc chạy vLLM cả hai nút trên Linux) và dừng. **Không** nâng cấp Windows hay đổi mạng khi chưa hỏi chủ dự án.

### Pha V5: Đo gộp 2 card (chỉ khi V4 đạt)
- **Model:** Qwen3-30B-A3B bản lượng tử 4 bit (GPTQ-Int4 chính thức `Qwen/Qwen3-30B-A3B-GPTQ-Int4`, ~16,93 GB `[TÀI LIỆU]`; ghi rõ nếu không chạy được trên `sm_120` hoặc không hỗ trợ MoE trong bản vLLM này). Đối chiếu llama.cpp gộp (`-ts` 58:42 ở 8k, 52:48 ở 32k, đã đo).
- **Cấu hình vLLM:** `pipeline_parallel_size=2` (kỳ vọng tốt nhất qua Ethernet) và, để đối chứng, `tensor_parallel_size=2` (kỳ vọng rất chậm vì đồng bộ mỗi lớp qua 2,5 GbE) `[SUY LUẬN]`. Ghi tỷ lệ VRAM thực tế từng card.
- **Đo:** decode 1 người dùng, TTFT ở 2k/8k/24k, đa người dùng 1/4/8/16, ctx 8k và 32k, so llama.cpp gộp cùng điều kiện ABAB. Ghi băng thông mạng giữa hai nút trong lúc đo.
- **Tùy chọn:** Qwen3-Coder-30B-A3B (nếu có bản GPTQ/AWQ tương ứng và còn thời gian).

### Pha V6: Báo cáo và khuyến nghị (≈ 2 giờ)
- `P10_VLLM_WSL2.md`: bảng so sánh (chạy riêng 3060 / 5070, gộp), kết quả mutation, chất lượng mẫu, các điều đã thử mà không chạy được, chi phí vận hành (cài đặt, dung lượng, độ phức tạp, độ ổn định), và danh sách **điều chưa đo**.
- Kết luận theo quy tắc "ngang llama.cpp là dùng vLLM" ở Phần 4, theo từng kịch bản. Khôi phục dịch vụ về đúng trạng thái ban đầu, báo cáo trạng thái.

---

## PHẦN 4: TIÊU CHÍ KẾT LUẬN (CHỦ DỰ ÁN CHỐT 2026-10-31: **NGANG llama.cpp LÀ DÙNG vLLM**)

**Quy tắc chốt:** với mỗi kịch bản đã đo (một card 3060, một card 5070, gộp 2 card), nếu vLLM **không kém** llama.cpp thì **chọn vLLM** cho kịch bản đó. Định nghĩa "không kém" (áp dụng cho từng kịch bản, theo giao thức Phần 2):
1. **Tốc độ:** decode 1 người dùng và thông lượng tổng ở 4/8/16 người dùng của vLLM không thấp hơn llama.cpp quá nhiễu đo (±3 tok/s, hoặc ≤ 3% nếu lớn hơn), TTFT prefill (2k/8k/24k) không tệ hơn quá 10%. "Ngang" gồm cả "không phân biệt được".
2. **Chất lượng mẫu** (V2.4) không kém: pass rate không thấp hơn, đầu ra không bất thường.
3. **Vận hành được:** chạy ổn định ≥ 30 phút tải liên tục, không lỗi OOM/crash, khởi động lại được qua worker-guard/adapter, tự khôi phục khi guard dừng engine.
4. Kết quả lặp lại ở lượt ABAB thứ hai và mutation check nhất quán.

**Hệ quả khi đạt:** vLLM trở thành động cơ mặc định **cho kịch bản đó**, qua adapter `inference/adapters/vllm` (việc tích hợp vào worker-guard/topology là pha riêng, làm sau khi chủ dự án duyệt báo cáo, không nằm trong kế hoạch đo này).

**Những ràng buộc kỹ thuật vẫn giữ llama.cpp ở lại (không phải "vLLM thua", mà là vLLM không chạy được ở đó) `[ĐỌC MÃ/TÀI LIỆU]`:**
- Model không vừa VRAM cần đẩy chuyên gia sang CPU (`-ncmoe`, chế độ B): vLLM không có offload từng tầng. Giữ llama.cpp.
- Kịch bản nào vLLM không chạy được (ví dụ gộp card nếu V4 không khả thi trên Win10 + WSL2, hoặc `sm_120` không chạy): kết luận là "giữ llama.cpp vì vLLM không chạy được", **không** phải "vLLM chậm hơn".
- Hệ thống sẽ chạy **hỗn hợp** nếu kết quả khác nhau theo kịch bản; gateway/topology đã định tuyến theo tên model nên không cần thống nhất một động cơ cho toàn cụm.

**Cảnh báo bắt buộc ghi trong báo cáo (để quyết định "dùng luôn" có cơ sở):**
- "Ngang" mới chỉ là tốc độ và chất lượng mẫu. Phải nêu rõ **chi phí vận hành thêm** nếu chọn vLLM: bắt buộc WSL2 trên Windows, thêm dung lượng đĩa/RAM, phụ thuộc phiên bản torch/CUDA/driver, định dạng model khác (safetensors AWQ/GPTQ thay GGUF, phải tải lại), `kv_type q8_0` thành fp8 (khác định dạng, chưa đo chất lượng).
- Không ghi "nhanh hơn X%" cho phép đo chưa lặp lại; không suy ra số cho model/cấu hình khác khi chưa đo (gắn `[SUY LUẬN]`).
- Nêu giới hạn: khác định dạng lượng tử, WSL2 có thể thêm chi phí, đa người dùng chỉ mô phỏng bằng client tổng hợp, chưa đo tải tương tác thực (VS Code/Cline).

---

## PHẦN 5: ƯỚC LƯỢNG VÀ THỨ TỰ

| Pha | Thời gian | Cần phép chủ dự án |
|---|---|---|
| V0 | ~30 phút | Có (danh sách việc) |
| V1 | 2–4 giờ + tải | Cài WSL2, có thể khởi động lại Windows, dừng guard/rpc |
| V2 | ~1 ngày | Chiếm GPU từng máy |
| V3 | ~1 giờ | Không |
| V4 | ≤ 4 giờ | Mạng/portproxy, dừng dịch vụ gộp |
| V5 | ~0,5–1 ngày | Chiếm cả 2 GPU |
| V6 | ~2 giờ | Không |

**Thứ tự đề nghị:** chờ P9 (tối ưu CPU/KV) kết thúc rồi mới chạy V2 trên `pc-001`; V0 và V1 trên Windows có thể làm song song nếu được duyệt. Pha V4 chỉ bắt đầu sau khi V2 cho thấy vLLM đủ hứa hẹn hoặc chủ dự án muốn có số liệu gộp để đóng vấn đề.
