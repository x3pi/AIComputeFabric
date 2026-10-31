# KẾ HOẠCH: TỐI ƯU LUỒNG CPU (P/E-core) VÀ KV-CACHE CHO CHẾ ĐỘ B (`-ncmoe`) TRÊN `pc-001`
*(Bàn giao cho Agent thực thi. Bản 2026-10-30. Mục tiêu: **đo đầy đủ rồi quyết định**; "không cải thiện" là kết quả hợp lệ. Nối tiếp P8 KTransformers: đây là hướng rẻ nhất, không tải thêm, không cài thêm.)*

**Đường dẫn:** `.aicf-work/` nằm ở **thư mục gốc repo** (`AIComputeFabric/.aicf-work/`), không nằm trong `ai-compute-fabric/`; các đường dẫn `.aicf-work/...` trong tài liệu này tính từ gốc repo. Đã xác nhận `[ĐO]` có `llama-server`, `llama-perplexity`, `llama-bench` (hỗ trợ `-ncmoe`, `--load-mode`).

**Quy ước nhãn:** `[ĐO]` đã đo · `[ĐỌC MÃ]` đã đọc trong repo · `[SUY LUẬN]` tính toán · `[CHƯA XÁC MINH]` phải thử. Con số không có `[ĐO]` là chưa đo. Báo cáo phải bằng tiếng Việt, có nhãn, có median/min/max.

---

## PHẦN 0: CÁC GIẢ THUYẾT CẦN KIỂM CHỨNG (KHÔNG PHẢI SỰ THẬT)

Nguồn: đề xuất của agent khác (Hướng 1). Đã tự kiểm tra ngay:

| # | Nhận định | Trạng thái |
|---|---|---|
| H0 | `engine.go` của worker-guard không truyền `-t/-tb/-Cr/-Crb` | **Đã xác nhận `[ĐỌC MÃ]`**: `apps/worker-guard/engine.go` dòng 253–258 chỉ truyền `-m -c -np -ngl --port --host -fa -ctk -ctv --no-warmup --fit off -lv 4`, rồi `-ncmoe`. Số luồng hoàn toàn để mặc định của `llama.cpp`. |
| H0b | `llama-server` và bộ công cụ trong `.aicf-work/bin/` hỗ trợ đủ cờ CPU và kiểm chất lượng | **Đã xác nhận `[ĐO]`** (`.aicf-work/bin/llama-server --help` và `ls .aicf-work/bin/`): có đủ `-t`, `-tb`, `-C`, `-Cr`, `--cpu-strict`, `-Cb`, `-Crb`, `--cpu-strict-batch`, `--prio`, `--prio-batch`, `--poll`, `--poll-batch`, `--load-mode`; có sẵn binary `.aicf-work/bin/llama-perplexity`. |
| H0c | CPU: 8 nhân P (CPU 0..7, 5.5–5.6 GHz) + 12 nhân E (CPU 8..19, 4.6 GHz), 1 luồng/nhân | **Đã xác nhận `[ĐO]`** (`lscpu -e`). |
| H0d | Mặc định `llama-server` khi không truyền `-t` chỉ khởi tạo **4 luồng** trên `pc-001` | **Đã xác nhận `[ĐO]`**: nhật ký `benchmarks/reports/logs/p8_p1_c8k_N20.log` dòng 8 ghi rõ `init: llama threadpool init, n_threads = 4` (tức mặc định bỏ không 4/8 nhân P). |
| H1 | Ghim decode `-t 8` vào 8 nhân P (`0..7`) tăng decode ở `-ncmoe 48` từ 26,48 lên 34,53 tok/s (+30,4%) | **Mới xác nhận trên `llama-bench` bằng `taskset -c 0-7` `[ĐO]`, CHƯA xác nhận trên `llama-server` bằng `-Cr 0-7`.** Trong `p8_ktransformers_p2a4_llamabench_t8_pcores.json` (dòng 14–16), con số `34,53 tok/s` đạt được khi bọc tiến trình bằng `taskset -c 0-7` (`cpu_mask: "0x0", cpu_strict: false`). Phải đo lại trên `llama-server` và phân biệt rõ: (a) `-t 8 -Cr 0-7` (`--cpu-strict 0`), (b) `-t 8 -Cr 0-7 --cpu-strict 1`, và (c) `taskset -c 0-7`. |
| H2 | Với `-ncmoe 20` decode tăng từ 42,16 lên ~48–50 tok/s | **[SUY LUẬN], chưa đo.** Ở `N=20`, 20 tầng MoE CPU chiếm `~12,2 ms` trên tổng `23,72 ms/tok` `[SUY LUẬN]`; nếu giảm ~25% thời gian phần CPU thì tổng thời gian giảm `~3 ms/tok` ($\rightarrow \sim 48\text{ tok/s}$), nhưng phải đo thực tế trên `llama-server`. |
| H3 | Prefill `-tb 20 -Crb 0-19` đạt ~458 tok/s | **Mới đo trên `llama-bench` (`458,2 tok/s` median ở `-t 20`) `[ĐO]`, chưa đo trên `llama-server` khi tách `-t 8 -Cr 0-7` và `-tb 20 -Crb 0-19`.** Phải đo lại bằng `llama-server` ở prompt 8k và 24k. |
| H4 | KV `q8_0` ở 32k tiết kiệm ~1,4–1,5 GiB, cho hạ `-ncmoe` từ 30 xuống 22–24 | **[SUY LUẬN]**: Qwen3-30B-A3B có 48 lớp, 4 đầu KV, `head_dim = 128` $\Rightarrow 98.304\text{ B/token} \Rightarrow 32\text{k} \approx 3,00\text{ GiB}$ (`f16`), `q8_0` $\approx 1,59\text{ GiB}$, tiết kiệm $\approx 1,41\text{ GiB}$ `[SUY LUẬN]`. Mỗi tầng MoE của model nặng `~342 MiB` nên `1,41 GiB` tương đương ~4 tầng MoE (hạ `-ncmoe` từ `30` xuống `26`, hoặc `24` tùy biên độ trống). Bắt buộc phải đo VRAM đỉnh thật và kiểm tra chất lượng (`q8_0` KV có thể làm giảm độ chính xác ở ngữ cảnh dài). |
| H5 | Cảnh báo `mmap` khi dùng `-ncmoe`: `--load-mode none` có thể loại bỏ sụt tốc độ lượt đầu và/hoặc tăng tốc | **Mới phát hiện từ log `[ĐO]`, chưa kiểm chứng hiệu năng:** `p8_p1_c8k_N20.log` dòng 7 in cảnh báo `W llama_model_loader: tensor overrides to CPU are used with mmap enabled - consider using --load-mode none for better performance`, và lượt gọi đầu tiên (`task 0`) bị tụt xuống `32,23 tok/s` (prefill `11,61 tok/s`) trước khi lên `42,16 tok/s` ở các lượt sau `[ĐO]`. Cần kiểm tra `--load-mode none` ở Pha T2. |

Phạm vi: **chỉ chế độ B (smart_1gpu)** và phần CPU của chế độ A. Chế độ C (gộp 2 card, ~127–132 tok/s `[ĐO]`) nằm trọn trong VRAM nên **không hưởng lợi**; không đổi gì ở đó.

---

## PHẦN 1: ĐIỀU KIỆN TIÊN QUYẾT VÀ AN TOÀN

- Phép đo chiếm GPU RTX 3060 và RAM của `pc-001`. **Phải dừng dịch vụ Qwen3-Coder trên pc-001 trước khi đo và khôi phục sau** (hiện dịch vụ đã chết sau OOM-killer 2026-10-30 02:04 nên có thể đang trống; phải kiểm tra bằng `nvidia-smi`, không giả định). Phải được chủ dự án đồng ý trước khi dừng/khôi phục bất kỳ dịch vụ nào.
- RAM khả dụng phải ≥ 20 GiB suốt phép đo (ghi `MemAvailable` đầu/cuối mỗi cấu hình; loại mẫu nếu swap tăng). Nếu không đủ, dừng và báo, không đo trong điều kiện thiếu RAM.
- Không sửa `/`, mọi file tạm trong `.aicf-work/` hoặc thư mục scratch. Không cài gói mới.
- Không để quá nhiệt/ngắt do điện: ghi `scaling_governor` và nhiệt CPU đầu/cuối; chạy cấu hình theo thứ tự xen kẽ (ABAB) để loại trừ trôi nhiệt.
- Không đổi mặc định của worker-guard cho đến khi có kết quả Pha T5 và được duyệt.

---

## PHẦN 2: GIAO THỨC ĐO (ÁP DỤNG CHO MỌI PHA)

1. **Binary và model cố định:**
   - Binary: `.aicf-work/bin/llama-server` (build `11913`, commit `7aca0d2ea` `[ĐO]`).
   - Model: `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` (`18.550.716.416 bytes`, SHA256 `0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5` `[ĐO]`).
   - Ghi SHA256 của cả hai và chuỗi build vào báo cáo.
2. **Số lần lặp và chống nhiễu LCP Slot Cache:**
   - Mỗi cấu hình: khởi động tiến trình mới, `--no-warmup --fit off` như production, chạy **1 lượt khởi động bỏ đi (burn-in)** để loại trừ hiện tượng sụt tốc độ do lỗi trang `mmap` ở lượt đầu (`task 0` trong `p8_p1_c8k_N20.log` tụt xuống `32,23 tok/s` trước khi ổn định ở `42,16 tok/s` `[ĐO]`), đồng thời vẫn ghi riêng số đo lượt cold đầu tiên để đối chiếu tác dụng của `--load-mode none`.
   - Sau lượt burn-in: chạy **$\ge 7$ lần đo** decode (prompt ngắn, `n_predict: 256`, `temperature: 0`, `seed` cố định) và **$\ge 3$ lần** prefill prompt dài ($\approx 8\text{k}$ và $\approx 24\text{k}$ token, `n_predict: 64`).
   - **Chống bẫy LCP Slot Similarity Cache `[ĐO]`:** Nhật ký `p8_p1_c8k_N20.log` (dòng 21, 29, 37) cho thấy dù gửi `"cache_prompt": false`, `llama-server` vẫn chọn slot theo `LCP similarity`. Để đảm bảo 100% token của prompt được đánh giá lại ở mỗi lần lặp, mỗi lượt gọi phải gắn tiền tố không trùng lặp ở đầu prompt (ví dụ `[RunID=<cfg>_rep<r>_nonce<uuid>]`) **và** kiểm tra trường `timings.prompt_n` trong JSON trả về phải khớp tổng số token của prompt.
   - Lấy `median / min / max`; ngưỡng nhiễu coi là **$\pm 3\text{ tok/s}$** (đã đo ở P3), khác biệt dưới ngưỡng này ghi `"không phân biệt được"`.
3. **Thứ tự xen kẽ (ABAB):**
   - Chạy theo thứ tự `baseline → cấu hình thử → baseline → cấu hình thử` để loại trừ trôi nhiệt CPU/GPU. Baseline (`-t` mặc định = 4 luồng `[ĐO]`) phải đo lại trong cùng phiên; không lấy số cũ làm mẫu số.
4. **Thông số bắt buộc thu thập mỗi lần chạy:**
   - `predicted_per_second`, `predicted_ms`, `prompt_per_second`, `prompt_ms`, `prompt_n` lấy trực tiếp từ trường `timings` của JSON phản hồi `llama-server` (không tự tính từ thời gian tường).
   - VRAM đỉnh (`nvidia-smi --query-gpu=memory.used,temperature.gpu,power.draw` lấy mẫu mỗi `0,5 s` bằng luồng nền).
   - `MemAvailable` và `SwapFree` từ `/proc/meminfo` trước và sau mỗi cấu hình; nhiệt độ gói CPU (`cat /sys/class/thermal/thermal_zone*/temp` hoặc `sensors`) và tần số thực tế của nhân P (`CPU 0..7`) vs nhân E (`CPU 8..19`) từ `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` ngay trong lúc đang decode.
   - **Bằng chứng ghim nhân thực tế (`[ĐO]`, không tin cờ dòng lệnh):** Trong lúc `llama-server` đang bận giải mã (và đang prefill), runner tự động đọc `/proc/<PID>/task/<TID>/status` (`Name`, `Cpus_allowed_list`) và `ps -L -p <PID> -o tid,comm,psr,pcpu,stat` để xác nhận các luồng worker thực sự chạy trên đúng tập nhân (`psr` $\in \{0..7\}$ khi decode, $\in \{0..19\}$ khi prefill).
5. **Kiểm chứng đột biến (Mutation Check):**
   - Với mỗi kết luận `"cờ X có tác dụng"`, chạy thêm một cấu hình **cố ý sai** (ví dụ ghim decode vào 8 nhân E: `-t 8 -Cr 8-15 --cpu-strict 1` hoặc `taskset -c 8-15`) và xác nhận: (a) `psr` thực sự nằm ở `8..15`, và (b) tốc độ decode giảm rõ rệt theo hướng dự đoán. Nếu cấu hình sai không khác cấu hình đúng thì cờ không có tác dụng thật.
6. **Quy ước lưu trữ:**
   - Dữ liệu thô: `ai-compute-fabric/benchmarks/reports/p9_*.jsonl` + log thô `ai-compute-fabric/benchmarks/reports/logs/p9_*.log`.
   - Báo cáo tổng hợp: `ai-compute-fabric/benchmarks/reports/P9_CPU_AFFINITY_KV.md`.
   - Script thực thi: `ai-compute-fabric/benchmarks/hardware/p9_*_runner.py` (đặt seed cố định, in đầy đủ lệnh và bảng kiểm tra `Cpus_allowed_list` vào log).

---

## PHẦN 3: CÁC PHA THỰC NGHIỆM CHI TIẾT

### Pha T0: Chuẩn bị, Kiểm tra môi trường và Baseline (≈ 45–60 phút)
- Kiểm tra `free -m` (`MemAvailable >= 20 GiB`), `nvidia-smi` (GPU rảnh `~15 MiB`), `scaling_governor`, nhiệt độ nền, SHA256 của binary và model.
- Chạy baseline đúng y hệt câu lệnh của `worker-guard` hiện tại (`engine.go` dòng 253–258 `[ĐỌC MÃ]`, không truyền cờ luồng):
  1. `c8k_N20_base`: `-c 8192 -np 1 -ngl 99 -fa on -ctk f16 -ctv f16 --no-warmup --fit off -lv 4 -ncmoe 20`
  2. `c8k_N48_base`: `-c 8192 -np 1 -ngl 99 -fa on -ctk f16 -ctv f16 --no-warmup --fit off -lv 4 -ncmoe 48`
  3. `c32k_N30_base`: `-c 32768 -np 1 -ngl 99 -fa on -ctk f16 -ctv f16 --no-warmup --fit off -lv 4 -ncmoe 30`
- Ghi nhận dòng log `llama threadpool init, n_threads = ...` (xác nhận mặc định `n_threads = 4` `[ĐO]`) và bảng `psr` / `Cpus_allowed_list` của các luồng.
- **Cổng T0:** Baseline `c8k_N20_base` nằm trong khoảng `42,16 ± 3,0 tok/s` và `c32k_N30_base` nằm trong `34,99 ± 3,0 tok/s` (khớp P8 Pha 1 `[ĐO]`). Nếu lệch lớn, điều tra ngay (RAM, swap, tần số CPU, tiến trình nền) trước khi qua T1.

### Pha T1: Quét số luồng Decode chưa ghim nhân (≈ 1–1,5 giờ)
- **Mục đích:** Tách biệt tác dụng của "tăng số luồng" (`-t`) khỏi "ghim nhân" (`-Cr` / `taskset`).
- **Ma trận đo:** Tại `-c 8192` với `-ncmoe 20` (điểm vận hành thực tế) và `-ncmoe 48` (điểm thuần MoE CPU nhạy nhất với luồng), quét `-t ∈ {4, 6, 8, 12, 16, 20}` (không truyền `-Cr`, không dùng `taskset`).
- **Giả thuyết cần kiểm:** Khi không ghim nhân, ở `-t 8`, bộ lập lịch Linux có tự đặt luồng vào 8 nhân P hay làm rơi một số luồng sang nhân E (`CPU 8..19`) gây trễ rào chắn đồng bộ (barrier stall)? Ở `-t 12, 16, 20`, việc trộn nhân P (5,5 GHz) và nhân E (4,6 GHz) trong decode `qlen=1` có làm giảm tốc độ như đã thấy ở P8 không?

### Pha T2: Ghim nhân Decode & Kiểm tra cơ chế ghim nội tại vs `taskset` + `--load-mode` (≈ 2 giờ)
- **Mục đích:** Xác định cơ chế ghim nhân nào thực sự có hiệu lực trên `llama-server` và đo tác dụng của `--poll`, `--prio`, `--load-mode none`.
- **Bước T2.1 — So sánh cơ chế ghim ở `-t 8` (trên `-ncmoe 20` và `-ncmoe 48`, `-c 8192`):**
  1. `-t 8` (không ghim — mốc từ T1).
  2. `-t 8 -Cr 0-7` (`--cpu-strict 0` mặc định): kiểm tra `Cpus_allowed_list` xem `ggml-cpu` có thực sự thu hẹp mặt nạ nhân về `0-7` khi không bật `--cpu-strict 1` hay không.
  3. `-t 8 -Cr 0-7 --cpu-strict 1`: kiểm tra mặt nạ nhân của từng luồng worker và của luồng chính/CUDA.
  4. `taskset -c 0-7` bọc ngoài `llama-server -t 8`: đối chiếu với (3) để xem việc nhốt cả luồng HTTP/CUDA vào `0-7` có gây tranh chấp nhân như ở `kt-kernel` không, hay `ggml-cpu` (`--poll 50`) nhường nhân tốt hơn.
  5. **Đối chứng đột biến (Mutation Check — bắt buộc):** `-t 8 -Cr 8-15 --cpu-strict 1` (và/hoặc `taskset -c 8-15`, ép chạy trên 8 nhân E `4,6 GHz`) và `-t 8 -Cr 4-11 --cpu-strict 1` (4 nhân P + 4 nhân E). Xác nhận tốc độ sụt giảm đúng theo xung nhịp / kiến trúc nhân.
  6. Kiểm tra mở rộng: `-t 12 -Cr 0-11 --cpu-strict 1` và `-t 20 -Cr 0-19 --cpu-strict 1`.
- **Bước T2.2 — Tinh chỉnh từng tham số đơn lẻ trên cấu hình thắng T2.1 (`-ncmoe 20`):**
  - Thử `--poll ∈ {0, 50, 100}` (mặc định là `50`).
  - Thử `--prio ∈ {0, 2}` (mặc định là `0`).
  - Thử `--load-mode none` (theo cảnh báo dòng 7 của `p8_p1_c8k_N20.log` `[ĐO]`): so sánh thời gian nạp model, tốc độ lượt đầu tiên (`task 0` cold) và trung vị 7 lượt warm, cũng như mức tiêu thụ RAM.
- **Cổng T2:** Chỉ kết luận ghim nhân/tăng luồng có lợi cho decode nếu cấu hình tốt nhất vượt baseline (`-t 4`) **$\ge 3,0\text{ tok/s}$** (hoặc rút ngắn thời gian phần MoE CPU $\ge 15\%$ một cách nhất quán ở cả `N=48` và `N=20`) và vượt qua kiểm chứng đột biến (ghim vào nhân E `8-15` chậm hơn rõ rệt).

### Pha T3: Tách luồng Prefill (`-tb`, `-Crb`, `--cpu-strict-batch`) (≈ 1–1,5 giờ)
- **Mục đích:** Tối ưu tốc độ nạp ngữ cảnh dài (prefill / TTFT) mà không làm suy giảm tốc độ decode đã đạt ở Pha T2.
- **Ma trận đo:** Giữ cố định cấu hình decode tốt nhất từ Pha T2 (ví dụ `-t 8 -Cr 0-7 --cpu-strict 1`), quét cấu hình batch trên prompt **$\approx 8\text{k}$** (`-c 8192`, `-ncmoe 20`) và **$\approx 24\text{k}$** (`-c 32768`, `-ncmoe 30`):
  1. `-tb 8 -Crb 0-7` (chỉ dùng 8 nhân P cho cả prefill và decode).
  2. `-tb 12 -Crb 0-11` (8 nhân P + 4 nhân E).
  3. `-tb 16 -Crb 0-15` (8 nhân P + 8 nhân E).
  4. `-tb 20 -Crb 0-19` (toàn bộ 8 nhân P + 12 nhân E).
  5. **Đối chứng đột biến prefill:** `-tb 4 -Crb 8-11` (chỉ 4 nhân E cho prefill) — xác nhận `prompt_per_second` giảm mạnh và TTFT tăng vọt.
- **Điểm kiểm tra bắt buộc:** Đo đồng thời `prompt_per_second`, `TTFT` và `predicted_per_second` (64 token sinh ngay sau prefill 8k/24k) để xác nhận việc mở rộng `-tb` sang nhân E không làm giảm tốc độ decode nối tiếp ngay sau đó (với `-np 1` như production; ghi rõ ca `-np > 1` đa người dùng chưa đo).

### Pha T4: Lượng tử hóa KV-cache (`q8_0` vs `f16`) & Kiểm định chất lượng nghiêm ngặt (≈ 2–2,5 giờ)
- **Mục đích:** Kiểm tra xem `-ctk q8_0 -ctv q8_0` ở ngữ cảnh 32k có tiết kiệm đúng `~1,4 GiB` VRAM để hạ `-ncmoe` từ `30` xuống `24–26` (tăng tốc độ) mà không làm suy giảm chất lượng sinh mã hay không.
- **Bước T4.1 — Hiệu năng & Biên độ VRAM (dùng cấu hình luồng tốt nhất từ T2–T3):**
  - Tại `-c 32768`:
    - Đối chứng `f16`: `-ncmoe 30` (`VRAM 10.629 MiB` `[ĐO]`), `-ncmoe 26`, `-ncmoe 24` (đã biết OOM ở `f16` `[ĐO]`).
    - Thử nghiệm `q8_0` (`-ctk q8_0 -ctv q8_0`): quét `-ncmoe ∈ {30, 26, 24, 22}`. Ghi nhận chính xác VRAM đỉnh (`MiB`), điểm nào bắt đầu OOM (hoặc vượt ngưỡng an toàn `11.750 MiB`), tốc độ decode (`tok/s`) và prefill 8k / 24k (`tok/s`, `TTFT`).
  - Tại `-c 8192` (`-ncmoe 20`): đo đối chiếu `f16` vs `q8_0` để biết chi phí giải lượng tử (`dequant`) KV `q8_0` ở ngữ cảnh ngắn làm tăng hay giảm tốc độ decode.
- **Bước T4.2 — Kiểm định chất lượng bắt buộc (`f16` vs `q8_0`):**
  1. **Đo Perplexity bằng `.aicf-work/bin/llama-perplexity`:**
     - Tạo tệp văn bản/mã nguồn cố định `.aicf-work/bench_corpus_p9.txt` (ghép từ mã nguồn Go/Python trong repo, độ dài $\ge 32.768$ token, ghi SHA256 của tệp).
     - Chạy `.aicf-work/bin/llama-perplexity -m .aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf -f .aicf-work/bench_corpus_p9.txt -c 8192 -ngl 99 -ncmoe 30 -fa on` với `-ctk f16 -ctv f16` và `-ctk q8_0 -ctv q8_0`. Ghi nhận $\text{PPL}_{f16}$, $\text{PPL}_{q8\_0}$ và $\Delta\text{PPL} = \text{PPL}_{q8\_0} - \text{PPL}_{f16}$.
  2. **Đánh giá 15 bài toán sinh code Python có Unit Test tự động + 2 bài truy xuất ngữ cảnh dài (24k token):**
     - Chạy cùng 15 prompt thuật toán/cấu trúc dữ liệu Python (`temperature: 0`, `seed: 42`, `n_predict: 512`) trên cả `f16` và `q8_0`, chấm tự động bằng `assert` trong tiến trình cô lập; đồng thời chạy 2 prompt truy xuất chính xác hằng số/chữ ký hàm đặt ở vị trí token thứ `2.000`, `12.000` và `22.000` trong ngữ cảnh 24k token.
     - Ghi nhận tỷ lệ qua test (`pass@1`), số lượng đầu ra trùng khớp từng ký tự (`exact match`), và bất kỳ sai lệch nào ở ngữ cảnh 24k.
- **Cổng T4:** Chỉ khuyến nghị dùng `q8_0` cho ngữ cảnh 32k khi: (a) tăng tốc độ decode $\ge 3\text{ tok/s}$ (nhờ hạ `-ncmoe` từ `30` xuống `24–26` trong cùng ngân sách VRAM) hoặc tiết kiệm $\ge 1.200\text{ MiB}$ VRAM mà không giảm tốc độ, **VÀ** (b) $\Delta\text{PPL} \le 0,05$, tỷ lệ qua unit test code và bài truy xuất 24k không suy giảm so với `f16`.

### Pha T5: Xác nhận Xen kẽ Cuối cùng (ABAB × 7) & Đo Bền Nhiệt 30 Phút (≈ 1–1,5 giờ)
- Chọn tổ hợp thắng cuộc từ T2–T4, chạy **7 lượt xen kẽ trực tiếp** (`Baseline` $\leftrightarrow$ `Optimized`) ở cả `-c 8192` (`-ncmoe 20`) và `-c 32768` (`-ncmoe 30` `f16` vs `-ncmoe N_opt` `q8_0`).
- Chạy bài kiểm tra tải liên tục **30 phút** trên cấu hình thắng cuộc: ghi đồ thị theo thời gian của `tok/s`, nhiệt độ CPU/GPU, xung nhịp nhân P/E, `MemAvailable` và `SwapUsed` để chứng minh không bị giảm xung nhiệt (`thermal throttling`) và không rò rỉ bộ nhớ/tăng swap.

### Pha T6: Thiết kế Triển khai vào `worker-guard` (Chỉ thực hiện sau khi Chủ dự án duyệt kết quả Pha T5)
Dựa trên kết quả đọc mã nguồn `apps/worker-guard/` (`main.go`, `engine.go`, `server.go`, `guard_test.go`, `topology_modes_test.go`) `[ĐỌC MÃ]`, thiết kế thay đổi tối giản, bảo toàn 100% tương thích ngược như sau:

1. **Nguyên tắc kiến trúc `[ĐỌC MÃ]`:**
   - Số luồng và dải nhân (`-t`, `-tb`, `-Cr`, `-Crb`, `--cpu-strict`, `--poll`, `--load-mode`) là **đặc trưng vật lý của từng máy worker** (`pc-001` là Core Ultra 7 265K 8P+12E trên Linux; `pc-win-01` là Ryzen 7 9700X 8C/16T trên Windows), nên được cấu hình qua **cờ khởi động của `worker-guard`** trên từng máy (tương tự `--ctx`, `--parallel`, `--ngl`, `--flash-attn` trong `main.go` dòng 47–56 `[ĐỌC MÃ]`), không đẩy qua giao thức mạng của `site-gateway` để tránh làm bẩn API cụm.
   - Riêng `kv_type` (`f16` / `q8_0`) **đã được hỗ trợ đầy đủ** trong `EngineOptions` (`engine.go:62`), `POST /api/engine` (`server.go:81`), `TopologySwitchRequest` (`cluster_topology.go:88`) và giao diện Web (`index.html:1253`) `[ĐỌC MÃ]` — không cần sửa giao thức cho `kv_type`.
2. **Thay đổi cụ thể trong `apps/worker-guard/`:**
   - **`apps/worker-guard/main.go`:** Thêm các cờ CLI tùy chọn (mặc định toàn bộ là `0` hoặc `""` = giữ nguyên hành vi cũ):
     - `--threads` (`int`, mặc định `0` $\rightarrow$ không truyền `-t`)
     - `--threads-batch` (`int`, mặc định `0` $\rightarrow$ không truyền `-tb`)
     - `--cpu-range` (`string`, mặc định `""` $\rightarrow$ không truyền `-Cr`)
     - `--cpu-range-batch` (`string`, mặc định `""` $\rightarrow$ không truyền `-Crb`)
     - `--cpu-strict` (`bool`, mặc định `false` $\rightarrow$ không truyền `--cpu-strict 1`)
     - `--load-mode` (`string`, mặc định `""` $\rightarrow$ không truyền `--load-mode`, chỉ thêm nếu Pha T2 chứng minh `none` có lợi).
   - **`apps/worker-guard/engine.go`:**
     - Bổ sung các trường tương ứng vào struct `LlamaEngine` (`Threads`, `ThreadsBatch`, `CPURange`, `CPURangeBatch`, `CPUStrict`, `LoadMode`).
     - Trong `LlamaEngine.Args()` (`engine.go:204-274`):
       - Nhánh `if e.Mode == "rpc-worker"` (dòng 205) giữ nguyên tuyệt đối (chỉ trả về `-H <host> -p <port> -c` cho `ggml-rpc-server`).
       - Kiểm tra hợp lệ: nếu `Threads < 0` hoặc `ThreadsBatch < 0` $\rightarrow$ trả lỗi; nếu `CPURange` hoặc `CPURangeBatch` khác rỗng $\rightarrow$ kiểm tra định dạng `^\d+(-\d+)?(,\d+(-\d+)?)*$` và giới hạn chỉ số nhân $< \text{số nhân logic của máy}$ (`runtime.NumCPU()`).
       - Chỉ nối thêm `-t`, `-tb`, `-Cr`, `-Crb`, `--cpu-strict 1`, `--load-mode` vào `args` khi trường tương ứng được cấu hình khác mặc định.
   - **Kiểm thử đơn vị (`apps/worker-guard/guard_test.go` & `topology_modes_test.go`):**
     - Test hồi quy: khi không cấu hình cờ mới, `e.Args()` sinh ra mảng đối số **giống hệt từng phần tử** như hiện tại.
     - Test hợp lệ & đột biến: kiểm tra truyền `--threads 8 --cpu-range 0-7 --cpu-strict --threads-batch 20 --cpu-range-batch 0-19`; kiểm tra từ chối dải nhân sai cú pháp hoặc vượt `runtime.NumCPU()` (mutation test); kiểm tra chuyển sang `rpc-worker` không bị rò cờ CPU sang `ggml-rpc-server`.
3. **Cập nhật kịch bản vận hành & Tài liệu:**
   - Cập nhật `deploy/demo/run_demo.sh` và `deploy/demo/worker.sh`: đọc biến môi trường tùy chọn `AICF_CPU_FLAGS` hoặc truyền các cờ tối ưu đã xác nhận cho `pc-001`.
   - Tuyệt đối **không áp dụng dải nhân của Linux (`0-7` / `0-19`) sang máy Windows `pc-win-01`** (Ryzen 7 9700X 8 nhân / 16 luồng SMT).

---

## PHẦN 4: TIÊU CHÍ CHẤP NHẬN VÀ BÁO CÁO

- **Có cải thiện thật** nếu median tốt hơn baseline **$> 3,0\text{ tok/s}$** ở decode (hoặc **$> 10\%$** ở prefill / TTFT) và nằm ngoài khoảng `min–max` của baseline, lặp lại được ở lượt xen kẽ thứ hai (Pha T5), và kiểm chứng đột biến (mutation check) nhất quán.
- Báo cáo [`ai-compute-fabric/benchmarks/reports/P9_CPU_AFFINITY_KV.md`](ai-compute-fabric/benchmarks/reports/P9_CPU_AFFINITY_KV.md) phải có:
  1. Bảng tổng hợp trước/sau (Baseline vs Tối ưu) ở cả `-c 8192` và `-c 32768`.
  2. Toàn bộ ma trận số liệu của Pha T0, T1, T2, T3, T4, T5 (bao gồm cả các cấu hình thua hoặc không phân biệt được).
  3. Bảng bằng chứng `psr` / `Cpus_allowed_list` và kết quả Mutation Check (ghim sai vào nhân E `8-15`).
  4. Kết quả kiểm định chất lượng `q8_0` vs `f16` ($\Delta\text{PPL}$ trên 32k token, tỷ lệ pass 15 bài code Python + 2 bài truy xuất ngữ cảnh 24k).
  5. Điều kiện RAM, Swap, nhiệt độ CPU/GPU, xung nhịp P/E thực tế và danh sách **những điều chưa đo** (đa người dùng `-np > 1`, máy Windows `pc-win-01`, tải tương tác thực tế từ VS Code/Cline).
- **Không** ghi `"tăng X%"` cho cấu hình chưa lặp lại; **không** suy ra số cho `-ncmoe N` khác từ `N` đã đo mà không gắn nhãn `[SUY LUẬN]`.
- Báo cáo nêu rõ giới hạn phạm vi: cấu hình này tối ưu cho **chế độ B (`smart_1gpu`) và phần CPU của chế độ A trên `pc-001`**; chế độ C (gộp 2 card RTX 3060 + RTX 5070 chạy 100% trong VRAM ở `~127–132 tok/s` `[ĐO]`) không đổi.

---

## PHẦN 5: THỨ TỰ ƯU TIÊN CÁC HƯỚNG CÒN LẠI (CHO CHỦ DỰ ÁN QUYẾT)

1. **Hướng này — Tối ưu CPU P/E-core & KV-cache cho `llama.cpp` (`KE_HOACH_TOI_UU_CPU_KV_NCMOE.md`, ≈ 1 ngày công, `0 GB` tải thêm):**
   - **Ưu tiên số 1 (Làm ngay sau khi duyệt kế hoạch).** Rủi ro bằng 0, không cài thêm gói, giải quyết trực tiếp việc `llama-server` mặc định chỉ chạy `n_threads = 4` trên CPU 20 nhân `[ĐO]` và kiểm chứng lợi ích của `q8_0` KV-cache ở ngữ cảnh 32k.
2. **Best-of-N cho sinh code (`KE_HOACH_BEST_OF_N_CODE.md`):**
   - **Ưu tiên số 2.** Mang lại giá trị cao nhất về chất lượng sinh code trên chế độ C (`~127 tok/s` `[ĐO]`); độc lập với hướng này, có thể triển khai ngay sau khi khôi phục dịch vụ Qwen3-Coder gộp 2 card.
3. **KTransformers Pha 2B (`KE_HOACH_THU_NGHIEM_KTRANSFORMERS.md`):**
   - **Tạm hoãn / Xếp ưu tiên thấp nhất.** Runner Pha 2A (`p8_phase2a_runner.py`) **đã được sửa lỗi bốc `topk_ids` và singleton `WorkerPool` và đã đo lại đầy đủ ngày `2026-10-30`** (`P8_KTRANSFORMERS.md` mục 8–10 `[ĐO]`): ở $K=75$ (`def=0`, chuẩn bảo toàn toán học), riêng 48 tầng MoE CPU của `kt-kernel LLAMAFILE` mất **`16,29 ms/tok`** (chậm hơn phần MoE CPU `~12,2 ms/tok` `[SUY LUẬN]` của `llama.cpp -ncmoe 20` ngay cả khi `llama.cpp` mới chạy `-t 4`), và ở $K=0$ mất **`28,43 ms/tok`** (chậm hơn mức `~21,4 ms/tok` của `llama-bench` khi ghim 8 nhân P).
   - Khi Pha T2 của kế hoạch này xác nhận mức tăng tốc của `llama-server` sau khi mở đủ 8 nhân P (`-t 8 -Cr 0-7 --cpu-strict 1`), khoảng cách nghiêng về `llama.cpp` trên định dạng `GGUF Q4_K_M` sẽ còn lớn hơn nữa, nên không cần tốn `16,93 GB` tải model + `~6 GiB` venv cho Pha 2B trừ khi chủ dự án muốn khảo sát riêng định dạng `GPTQ-Int4`.

