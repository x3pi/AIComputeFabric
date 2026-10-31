# BÁO CÁO KỸ THUẬT P9: TỐI ƯU SỐ LUỒNG, GHIM NHÂN CPU P/E-CORE, `--load-mode` VÀ KV-CACHE (`q8_0`) CHO `llama.cpp -ncmoe` TRÊN `pc-001`

- **Ngày thực hiện:** `2026-10-30` (`14:30 – 17:22 +0700`) `[ĐO]`
- **Kế hoạch tham chiếu:** [`KE_HOACH_TOI_UU_CPU_KV_NCMOE.md`](../../../KE_HOACH_TOI_UU_CPU_KV_NCMOE.md) và bối cảnh [`P8_KTRANSFORMERS.md`](P8_KTRANSFORMERS.md) (mục 8–10)
- **Máy thực thi:** `pc-001` (`Intel Core Ultra 7 265K` — 8 nhân P Lion Cove `CPU 0..7` + 12 nhân E Skymont `CPU 8..19`, không SMT, tổng 20 luồng phần cứng; GPU `NVIDIA GeForce RTX 3060 12 GB`; RAM `31.489 MiB`) `[ĐO]`
- **Binary cố định:** `.aicf-work/bin/llama-server` (`version: 0.7.0-dev`, build `11913`, commit `7aca0d2ea`, SHA256: `bd499b266971c66f4db0a835ac404027e7fc1d55a4d816c92054c1158ea480d6`) và `.aicf-work/bin/llama-perplexity` (SHA256: `92594986686a4bbe23a2834695792ce32f8b18b9d57bf3c50e0ac87aba715fdd`) `[ĐO]`
- **Mô hình cố định:** `.aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf` (`18.556.685.824 byte`, SHA256: `0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5`) `[ĐO]`
- **Kịch bản đo & dữ liệu thô:**
  - Runner: [`p9_cpu_kv_runner.py`](../hardware/p9_cpu_kv_runner.py), [`p9_t4_t5_runner.py`](../hardware/p9_t4_t5_runner.py)
  - Dữ liệu thô: [`p9_t0_baseline.jsonl`](p9_t0_baseline.jsonl), [`p9_t1_threads_unpinned.jsonl`](p9_t1_threads_unpinned.jsonl), [`p9_t2_cpu_affinity.jsonl`](p9_t2_cpu_affinity.jsonl), [`p9_t3_prefill_batch.jsonl`](p9_t3_prefill_batch.jsonl), [`p9_t4_kv_q8.jsonl`](p9_t4_kv_q8.jsonl), [`p9_t4_quality.json`](p9_t4_quality.json), [`p9_t5_confirmation.jsonl`](p9_t5_confirmation.jsonl), [`p9_t5_thermal_timeseries.jsonl`](p9_t5_thermal_timeseries.jsonl) và thư mục log `benchmarks/reports/logs/p9_*.log`

---

## 1. Tóm tắt Kết luận & Bảng Tổng hợp Trước / Sau (Baseline vs Tối ưu)

### 1.1. Phạm vi áp dụng
- Các kết quả trong báo cáo này áp dụng cho **chế độ B (`smart_1gpu`, dùng `-ncmoe N`) và phần CPU của chế độ A trên máy Linux `pc-001`** (`Core Ultra 7 265K` + `RTX 3060 12 GB`) `[ĐỌC MÃ]`.
- **Chế độ C** (gộp 2 card `RTX 3060 12 GB` + `RTX 5070 12 GB` chạy 100% mô hình trong VRAM ở tốc độ `~127–132 tok/s` `[ĐO]`) **không thay đổi** `[ĐỌC MÃ]`.
- **Không áp dụng dải nhân Linux (`0-7` / `8-15`) cho máy Windows `pc-win-01`** (`AMD Ryzen 7 9700X` 8 nhân / 16 luồng SMT đồng nhất) `[ĐỌC MÃ]`.

### 1.2. Kết luận theo tiêu chí chấp nhận (Phần 4 của Kế hoạch)

| Hạng mục thử nghiệm | Kết quả kiểm chứng trên `llama-server` | Đạt cổng chấp nhận? | Khuyến nghị cho `pc-001` |
|---|---|---|---|
| **1. Mở 8 luồng & ghim nhân P (`-t 8 -Cr 0-7 --cpu-strict 1` hoặc `-t 8 -Cr 0-7`)** | - **Ở `-c 32768 -ncmoe 30` (`f16`):** Decode tăng từ **`34,11–35,45 tok/s`** lên **`38,18–38,56 tok/s`** (**`+3,36` đến `+4,28 tok/s`**, nằm hoàn toàn ngoài khoảng `min–max` của baseline ở mọi lượt xen kẽ `ABAB`); decode nối tiếp sau prefill 8k tăng từ `29,83–31,08` lên **`34,60–34,70 tok/s`** (`+3,62` đến `+4,77 tok/s`) `[ĐO]`.<br>- **Ở `-c 8192 -ncmoe 48` (100% MoE trên CPU):** Decode tăng từ **`25,34–26,38 tok/s`** lên **`29,98–31,55 tok/s`** (`taskset -c 0-7`: **`32,08 tok/s`**) — tăng **`+4,64` đến `+6,21 tok/s` (`+18%` đến `+24,5%`)**, nằm hoàn toàn ngoài `min–max` của baseline `[ĐO]`.<br>- **Ở `-c 8192 -ncmoe 20` (`f16`):** Baseline không ghim nhân dao động mạnh **`41,32–46,99 tok/s`** (`min = 39,56`) tùy thuộc bộ lập lịch Linux đặt bao nhiêu trong 4 luồng mặc định vào nhân E (`8..19`). Khi ghim `-t 8 -Cr 0-7 --cpu-strict 1`, decode đạt **`45,52–48,09 tok/s`** (`min = 44,67` ở Pha T5) và decode nối tiếp sau prefill 8k tăng từ `37,92–41,35` lên **`40,11–43,20 tok/s`** `[ĐO]`. (Lưu ý: ở 1 trong 2 cặp `ABAB` tại Pha T5 khi baseline ngẫu nhiên rơi vào nhân P đạt `46,99 tok/s`, chênh lệch cặp đó chỉ là `+1,10 tok/s`; còn ở 5/6 lần đo khác và cặp `ABAB` thứ hai chênh lệch đạt `+3,38` đến `+5,31 tok/s` `[ĐO]`). | **ĐẠT** (Xác nhận rõ rệt ở `N=48`, `N=30` và loại bỏ hiện tượng tụt tốc độ xuống `41–42 tok/s` ở `N=20`) | **Khuyến nghị áp dụng** `-t 8 -Cr 0-7 --cpu-strict 1` (hoặc `-t 8 -Cr 0-7`) cho `pc-001`. |
| **2. Tách luồng prefill (`-tb`, `-Crb`, `--cpu-strict-batch`)** | Không làm tăng tốc độ prefill (`881,2–931,0 tok/s` so với `897,4–914,5 tok/s` ở 8k; `686,0–692,3 tok/s` ở 24k — chênh lệch `< 3%`, nằm trong nhiễu) và **không làm thay đổi `active_psrs` trong phép thử đột biến `-tb 4 -Crb 8-11`** (`pf_psrs` vẫn là `[0..7]`) do `.aicf-work/bin/libggml-cpu-alderlake.so` biên dịch với `GGML_USE_OPENMP` (`libgomp`) dùng chung 1 team luồng OpenMP và phần lớn khối lượng prefill (`Attention` 48 tầng + `MoE` 28 tầng) chạy trên GPU `[ĐO]` `[ĐỌC MÃ]`. | **KHÔNG ĐẠT CỔNG T3** | **Bác bỏ / Không thêm `-tb`, `-Crb`, `--cpu-strict-batch`** để tránh phức tạp không có tác dụng thực tế. |
| **3. Cờ `--load-mode none`, `--poll`, `--prio`** | - `--load-mode none`: cải thiện tốc độ prefill của **lượt đầu tiên (cold burn-in)** từ `165,03 tok/s` lên `235,22 tok/s` (`+42,5%`), nhưng từ lượt thứ 2 trở đi tốc độ decode ấm (`46,18 tok/s` vs `46,63 tok/s`) không khác biệt vì toàn bộ tệp GGUF `18,55 GB` đã nằm trong OS page cache (`buff/cache ~20 GiB`) `[ĐO]`.<br>- `--poll 0` (`45,06 tok/s`) và `--poll 100` (`45,19 tok/s`): kém nhẹ so với mặc định `--poll 50` (`46,63 tok/s`) `[ĐO]`.<br>- `--prio 2`: làm **giảm** tốc độ decode xuống `40,20 tok/s` (`-6,43 tok/s`) do tranh chấp lập lịch với luồng điều phối CUDA `[ĐO]`. | **KHÔNG ĐẠT** (cho trạng thái ấm) | Giữ mặc định (`--poll 50`, `--prio 0`). Chỉ cần `--load-mode none` nếu muốn giảm độ trễ request đầu tiên sau khi khởi động lạnh. |
| **4. Lượng tử hóa KV-cache `-ctk q8_0 -ctv q8_0` ở ngữ cảnh `32k` (`-c 32768`)** | - **Tiết kiệm VRAM:** Giảm VRAM đỉnh ở `-ncmoe 30` từ `10.629 MiB` xuống **`9.314 MiB`** (**tiết kiệm `1.315 MiB` VRAM** `[ĐO]`), cho phép hạ `-ncmoe` từ `30` xuống **`26` (`10.587 MiB`)** hoặc **`24` (`11.233 MiB`, dưới ngưỡng an toàn `11.750 MiB`)** mà không bị OOM (`f16` bị OOM ngay cả ở `N=26` và `N=24`) `[ĐO]`.<br>- **Hiệu năng ở `-ncmoe 24` (`q8_0`) + `-t 8 -Cr 0-7 --cpu-strict 1`:** Decode đạt **`39,75–41,05 tok/s`** (**`+5,06` đến `+6,82 tok/s`**, tức **`+14,5%` đến `+19,9%`** so với baseline `34,11–34,82 tok/s`, vượt xa ngoài khoảng `min–max`); prefill 8k tăng lên **`814,3–845,7 tok/s`** (`+11,8%` đến `+18,9%`); prefill 24k tăng lên **`729,1–734,8 tok/s`** (`TTFT` giảm từ `33,69–34,82 s` xuống **`31,60–31,89 s`**, tiết kiệm `~2,1–3,0 giây`) `[ĐO]`.<br>- **Đánh đổi cần lưu ý (`[ĐO]`):** Khi KV-cache đã điền đầy `~23,2k` token, tốc độ decode ngay sau prefill 24k của `q8_0` (`N=24`) giảm còn **`20,56–20,67 tok/s`** (so với `26,44–27,96 tok/s` của `f16` `N=30`) do chi phí giải lượng tử 24k token KV `q8_0` trong nhân FlashAttention trên RTX 3060 `[ĐO]`.<br>- **Chất lượng (`[ĐO]`):** $\text{PPL}_{f16} = 4,0418 \pm 0,08188$, $\text{PPL}_{q8\_0} = 4,0411 \pm 0,08186$ ($\Delta\text{PPL} = -0,0007$, $|\Delta\text{PPL}| \ll 0,05$); Unit test 15 bài code Python: **`14/15` (`93,33%`)** trên cả hai (`13/15` bài trùng khớp từng ký tự SHA256); Truy xuất chính xác 24k token: **`2/2` (`100%`, đúng 6/6 khóa)** `[ĐO]`. | **ĐẠT CỔNG T4 & T5** | **Tại `-c 32768`:**<br>- Nếu ưu tiên tốc độ decode thông thường (`~40–41 tok/s`) và tốc độ prefill 8k/24k (`TTFT` nhanh hơn `2–3 s`): dùng **`-ctk q8_0 -ctv q8_0 -ncmoe 24 -t 8 -Cr 0-7 --cpu-strict 1`**.<br>- Nếu ưu tiên tốc độ decode khi hội thoại đã dài trên 24k token (`27,96 tok/s` vs `20,67 tok/s`): dùng **`-ctk f16 -ctv f16 -ncmoe 30 -t 8 -Cr 0-7 --cpu-strict 1`** (`38,39 tok/s` decode, `+4,2 tok/s` so với baseline).<br>**Tại `-c 8192`:** Giữ **`f16`** (`-ncmoe 20 -t 8 -Cr 0-7 --cpu-strict 1`). |

### 1.3. Bảng Tổng hợp Trước / Sau (Xác nhận tại Pha T0, T4 và Pha T5 Xen kẽ ABAB)

| Ngữ cảnh | Cấu hình | VRAM đỉnh (`MiB`) | Decode 256 tok (`median [min–max] tok/s`) | Prefill ~8k tok (`median tok/s` / `TTFT ms`) | Decode sau Prefill 8k (`tok/s`) | Prefill ~24k tok (`median tok/s` / `TTFT ms`) | Decode sau Prefill 24k (`tok/s`) |
|---|---|---:|---:|---:|---:|---:|---:|
| **`-c 8192`** | **Baseline hiện tại** (`-ncmoe 20 -ctk f16 -ctv f16`, mặc định 4 luồng không ghim) | `11.717–11.735` `[ĐO]` | `T0`: **`42,46`** `[41,42–43,61]`<br>`T2_A`: **`41,32`** `[40,76–43,11]`<br>`T3_A`: **`41,63`** `[41,23–41,85]`<br>`T5_1`: **`46,99`** `[42,36–48,09]`<br>`T5_3`: **`42,55`** `[39,56–44,69]` `[ĐO]` | `T0`: `866,1` / `8.965`<br>`T3_A`: `897,4` / `8.652`<br>`T5_1`: `933,1` / `8.318`<br>`T5_3`: `904,8` / `8.580` `[ĐO]` | `T0`: `37,92`<br>`T3_A`: `38,28`<br>`T5_1`: `41,35`<br>`T5_3`: `38,32` `[ĐO]` | — | — |
| **`-c 8192`** | **Tối ưu CPU** (`-ncmoe 20 -ctk f16 -ctv f16 -t 8 -Cr 0-7 --cpu-strict 1`) | `11.735` `[ĐO]` | `T2`: **`46,63`** `[45,75–47,70]`<br>`T3`: **`45,52`** `[43,83–47,12]`<br>`T4`: **`46,11`** `[39,18–47,20]`<br>`T5_2`: **`48,09`** `[45,90–49,74]`<br>`T5_4`: **`45,93`** `[44,67–46,38]` `[ĐO]` | `T3`: `914,5` / `8.497`<br>`T4`: `900,5` / `8.627`<br>`T5_2`: `952,0` / `8.157`<br>`T5_4`: `898,3` / `8.643` `[ĐO]` | `T3`: **`43,20`**<br>`T4`: **`40,11`**<br>`T5_2`: **`41,88`**<br>`T5_4`: **`42,49`** `[ĐO]` | — | — |
| **`-c 32768`** | **Baseline hiện tại** (`-ncmoe 30 -ctk f16 -ctv f16`, mặc định 4 luồng không ghim) | `10.607–10.629` `[ĐO]` | `T0`: **`35,45`** `[33,56–36,56]`<br>`T3_A`: **`34,43`** `[32,87–35,48]`<br>`T4`: **`34,82`** `[33,65–35,27]`<br>`T5_1`: **`34,11`** `[33,69–34,72]`<br>`T5_3`: **`34,23`** `[33,27–34,90]` `[ĐO]` | `T0`: `783,8` / `9.903`<br>`T3_A`: `741,4` / `10.471`<br>`T4`: `711,0` / `10.926`<br>`T5_1`: `750,6` / `10.346`<br>`T5_3`: `774,4` / `10.052` `[ĐO]` | `T0`: `28,39`<br>`T3_A`: `28,84`<br>`T4`: `29,83`<br>`T5_1`: `30,45`<br>`T5_3`: `31,08` `[ĐO]` | `T0`: `704,9` / `32.969`<br>`T3_A`: `686,4` / `33.809`<br>`T4`: `666,5` / `34.820`<br>`T5_1`: `689,1` / `33.689`<br>`T5_3`: `689,9` / `33.688` `[ĐO]` | `T0`: `25,38`<br>`T3_A`: `26,81`<br>`T4`: `26,53`<br>`T5_1`: `26,44`<br>`T5_3`: `27,31` `[ĐO]` |
| **`-c 32768`** | **Phương án Tối ưu 32k-A (Bảo toàn `f16`):** `-ncmoe 30 -ctk f16 -ctv f16 -t 8 -Cr 0-7 --cpu-strict 1` | `10.629` `[ĐO]` | `T3`: **`38,39`** `[37,40–39,14]`<br>`T4`: **`38,18`** `[34,92–39,87]`<br>`T5_5`: **`38,39`** `[36,40–38,85]`<br>(**`+3,96` đến `+4,28 tok/s`**) `[ĐO]` | `T3`: `762,3` / `10.184`<br>`T4`: `765,3` / `10.176`<br>`T5_5`: `767,0` / `10.156` `[ĐO]` | `T4`: **`34,60`**<br>`T5_5`: **`34,70`**<br>(**`+3,6` đến `+4,8 tok/s`**) `[ĐO]` | `T3`: `686,0` / `33.829`<br>`T4`: `675,8` / `34.393`<br>`T5_5`: `687,2` / `33.788` `[ĐO]` | `T3`: **`28,16`**<br>`T4`: **`27,84`**<br>`T5_5`: **`27,96`** `[ĐO]` |
| **`-c 32768`** | **Phương án Tối ưu 32k-B (KV `q8_0` + hạ `N=24`):** `-ncmoe 24 -ctk q8_0 -ctv q8_0 -t 8 -Cr 0-7 --cpu-strict 1` | `11.233` `[ĐO]` | `T4`: **`39,88`** `[38,71–41,95]`<br>`T5_2`: **`39,75`** `[32,12–40,87]`<br>`T5_4`: **`41,05`** `[39,24–41,72]`<br>`T5_30m`: **`40,22`** `[28,35–42,60]`<br>(**`+5,06` đến `+6,82 tok/s`**) `[ĐO]` | `T4`: **`845,7`** / **`9.184`**<br>`T5_2`: **`839,3`** / **`9.271`**<br>`T5_4`: **`814,3`** / **`9.539`**<br>(**`+11,8%` đến `+18,9%`**) `[ĐO]` | `T4`: `31,14`<br>`T5_2`: `30,95`<br>`T5_4`: `31,54` `[ĐO]` | `T4`: **`729,8`** / **`31.800`**<br>`T5_2`: **`729,1`** / **`31.885`**<br>`T5_4`: **`734,8`** / **`31.599`**<br>(Rút ngắn `2,1–3,0 s` TTFT) `[ĐO]` | `T4`: `20,58`<br>`T5_2`: `20,67`<br>`T5_4`: `20,56` `[ĐO]` |

---

## 2. Toàn bộ Ma trận Số liệu Pha T0 → T5 (Bao gồm cả Cấu hình Thua & Đột biến)

### 2.1. Pha T0 — Xác nhận Đường cơ sở Nội phiên & Chẩn đoán Mặc định (`p9_t0_baseline.jsonl`)
- Kiểm tra tiền điều kiện trước khi chạy: `MemAvailable = 23.854 MiB` ($\ge 20\text{ GiB}$), `SwapUsed = 12.660 MiB`, `VRAM used = 15 MiB`, governor CPU `powersave` (`intel_pstate`), không có tiến trình `worker-guard` hay `llama-server` chạy nền `[ĐO]`.
- Khi không truyền `-t`, `llama-server` tự khởi tạo **`n_threads = 4`** `[ĐO]` (xác nhận từ log khởi động `n_threads = 4 / 20`) và `Cpus_allowed_list = 0-19` trên toàn bộ 30 luồng OS của tiến trình, dẫn đến việc bộ lập lịch Linux đẩy ngẫu nhiên các luồng tính toán sang nhân E (`CPU 8..19`) `[ĐO]`:

| Tag (`p9_t0_baseline.jsonl`) | `-c` | `-ncmoe` | `n_threads` (log) | VRAM đỉnh (`MiB`) | Burn-in Decode / Prefill (`tok/s`) | Decode 7 lượt (`median [min–max] tok/s`) | `active_psrs` khi Decode | Prefill 8k (`median [min–max] tok/s`, `TTFT ms`, post-dec) | Prefill 24k (`median [min–max] tok/s`, `TTFT ms`, post-dec) |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| `T0_c8k_N20_base` | `8192` | `20` | `4` `[ĐO]` | `11.735` `[ĐO]` | `17,84` / `201,6` `[ĐO]` | **`42,46`** `[41,42–43,61]` `[ĐO]` | `[0, 4, 6, 9]` `[ĐO]` | **`866,1`** `[850,0–866,9]`, `8.965,3 ms`, post `37,92` (`psrs=[4,8,10,16]`) `[ĐO]` | — |
| `T0_c8k_N48_base` | `8192` | `48` | `4` `[ĐO]` | `3.155` `[ĐO]` | `24,99` / `146,1` `[ĐO]` | **`26,38`** `[25,34–27,86]` `[ĐO]` | `[0, 1, 4, 6]` `[ĐO]` | **`556,3`** `[528,1–557,3]`, `13.952,0 ms`, post `23,36` (`psrs=[0,1,4,5,19]`) `[ĐO]` | — |
| `T0_c32k_N30_base` | `32768` | `30` | `4` `[ĐO]` | `10.629` `[ĐO]` | `31,11` / `154,2` `[ĐO]` | **`35,45`** `[33,56–36,56]` `[ĐO]` | `[5, 10, 11, 14]` `[ĐO]` | **`783,8`** `[773,3–795,2]`, `9.903,3 ms`, post `28,39` (`psrs=[0,7,14,19]`) `[ĐO]` | **`704,9`** `[686,2–708,5]`, `32.968,8 ms`, post `25,38` (`psrs=[4,6,8,11]`) `[ĐO]` |

- **Đối chiếu Cổng T0 với Pha 1 của P8 (`p8_ktransformers_p1_llamacpp.jsonl`):**
  - Tại `-c 8192 -ncmoe 20`: `42,46 tok/s` vs `42,16 tok/s` ($\Delta = +0,30\text{ tok/s} \le 3,0\text{ tok/s}$) `[ĐO]`.
  - Tại `-c 8192 -ncmoe 48`: `26,38 tok/s` vs `25,08 tok/s` ($\Delta = +1,30\text{ tok/s} \le 3,0\text{ tok/s}$) `[ĐO]`.
  - Tại `-c 32768 -ncmoe 30`: `35,45 tok/s` vs `34,99 tok/s` ($\Delta = +0,46\text{ tok/s} \le 3,0\text{ tok/s}$) `[ĐO]`.
  - **Kết luận Cổng T0:** **ĐẠT** `[ĐO]`.

---

### 2.2. Pha T1 — Quét Số Luồng Decode Chưa Ghim Nhân (`-t` đơn thuần, `p9_t1_threads_unpinned.jsonl`)
Quét `-t ∈ {4, 6, 8, 12, 16, 20}` không kèm `-Cr` trên cả `-ncmoe 48` (toàn bộ 48 tầng MoE trên CPU) và `-ncmoe 20` (20 tầng MoE trên CPU), có kẹp hai lượt baseline đầu/cuối (`_A` và `_B`) `[ĐO]`:

| Tag (`p9_t1_threads_unpinned.jsonl`) | `-ncmoe` | Cờ `extra_args` | Decode Median (`tok/s`) | Min–Max (`tok/s`) | `ms/tok` | `Cpus_allowed_list` | `active_psrs` thực tế (`ps -L`) | Xung P / E (`MHz`) |
|---|---:|---|---:|---:|---:|---|---|---:|
| `T1_N48_base_t_default_A` | `48` | `[]` (mặc định `-t 4`) | **`25,75`** `[ĐO]` | `24,52–27,63` `[ĐO]` | `38,67` | `['0-19']` | `[4, 11, 13, 15]` (1P + 3E) `[ĐO]` | `1804 / 3826` |
| `T1_N48_t4_unpinned` | `48` | `['-t', '4']` | **`27,24`** `[ĐO]` | `25,65–29,21` `[ĐO]` | `36,55` | `['0-19']` | `[4, 7, 13, 18]` (2P + 2E) `[ĐO]` | `2788 / 3827` |
| `T1_N48_t6_unpinned` | `48` | `['-t', '6']` | **`28,23`** `[ĐO]` | `26,43–29,03` `[ĐO]` | `35,28` | `['0-19']` | `[1, 2, 4, 8, 12, 17]` (3P + 3E) `[ĐO]` | `2651 / 3832` |
| `T1_N48_t8_unpinned` | `48` | `['-t', '8']` | **`28,71`** `[ĐO]` | `27,82–29,48` `[ĐO]` | `34,69` | `['0-19']` | `[0, 1, 2, 5, 7, 12, 14, 16]` (5P + 3E) `[ĐO]` | `2973 / 3611` |
| `T1_N48_t12_unpinned` | `48` | `['-t', '12']` | **`25,33`** `[ĐO]` | `23,77–25,53` `[ĐO]` | `39,30` | `['0-19']` | `[1, 3, 6, 8..13, 15, 17, 18]` (3P + 9E) `[ĐO]` | `3381 / 4600` |
| `T1_N48_t16_unpinned` | `48` | `['-t', '16']` | **`26,14`** `[ĐO]` | `25,08–26,64` `[ĐO]` | `38,10` | `['0-19']` | `[0, 2, 3, 5, 6, 8..16, 18, 19]` (5P + 11E) `[ĐO]` | `3409 / 4600` |
| `T1_N48_t20_unpinned` | `48` | `['-t', '20']` | **`25,87`** `[ĐO]` | `25,21–26,57` `[ĐO]` | `38,49` | `['0-19']` | `[0..19]` (8P + 12E) `[ĐO]` | `4756 / 4600` |
| `T1_N48_base_t_default_B` | `48` | `[]` (mặc định `-t 4`) | **`27,39`** `[ĐO]` | `26,67–28,45` `[ĐO]` | `36,36` | `['0-19']` | `[0, 3, 5, 18]` (3P + 1E) `[ĐO]` | `2225 / 3520` |
| `T1_N20_base_t_default_A` | `20` | `[]` (mặc định `-t 4`) | **`42,01`** `[ĐO]` | `40,78–43,66` `[ĐO]` | `23,71` | `['0-19']` | `[1, 4, 16, 18]` (2P + 2E) `[ĐO]` | `1896 / 3901` |
| `T1_N20_t4_unpinned` | `20` | `['-t', '4']` | **`44,64`** `[ĐO]` | `43,53–46,79` `[ĐO]` | `22,31` | `['0-19']` | `[1, 4, 5, 8]` (3P + 1E) `[ĐO]` | `3373 / 3600` |
| `T1_N20_t6_unpinned` | `20` | `['-t', '6']` | **`44,41`** `[ĐO]` | `43,54–46,03` `[ĐO]` | `22,43` | `['0-19']` | `[0, 1, 2, 4, 6, 11]` (5P + 1E) `[ĐO]` | `2514 / 3719` |
| `T1_N20_t8_unpinned` | `20` | `['-t', '8']` | **`44,50`** `[ĐO]` | `43,62–46,74` `[ĐO]` | `22,38` | `['0-19']` | `[2, 4, 6, 7, 8, 9, 18, 19]` (4P + 4E) `[ĐO]` | `2547 / 3981` |
| `T1_N20_t12_unpinned` | `20` | `['-t', '12']` | **`42,85`** `[ĐO]` | `41,72–43,92` `[ĐO]` | `23,24` | `['0-19']` | `[0, 1, 2, 4, 8..13, 16, 19]` (4P + 8E) `[ĐO]` | `4303 / 4449` |
| `T1_N20_t16_unpinned` | `20` | `['-t', '16']` | **`42,81`** `[ĐO]` | `41,11–44,23` `[ĐO]` | `23,26` | `['0-19']` | `[0, 1, 3, 5, 7, 8..18]` (5P + 11E) `[ĐO]` | `3874 / 4519` |
| `T1_N20_t20_unpinned` | `20` | `['-t', '20']` | **`41,13`** `[ĐO]` | `39,46–42,35` `[ĐO]` | `24,22` | `['0-19']` | `[0..19]` (8P + 12E) `[ĐO]` | `5129 / 4600` |
| `T1_N20_base_t_default_B` | `20` | `[]` (mặc định `-t 4`) | **`43,70`** `[ĐO]` | `43,07–44,98` `[ĐO]` | `22,79` | `['0-19']` | `[2, 5, 7, 15]` (3P + 1E) `[ĐO]` | `2321 / 3811` |

- **Nhận xét Pha T1 (`[ĐO]`):**
  - Ngay cả khi chưa ghim nhân, `-t 8` đạt đỉnh ở cả `N=48` (`28,71 tok/s`) và `N=20` (`44,50 tok/s`), trong khi tăng lên `-t 12, 16, 20` làm tốc độ **sụt giảm** về `25,33–26,14 tok/s` (`N=48`) và `41,13–42,85 tok/s` (`N=20`) do rào chắn đồng bộ OpenMP (`barrier`) phải đợi các nhân E chậm hơn và tăng tranh chấp bộ nhớ đệm L3 `[ĐO]` `[SUY LUẬN]`.
  - Bằng chứng `active_psrs` cho thấy rõ lý do `-t 8` chưa ghim nhân chưa phát huy hết hiệu năng: ở `T1_N48_t8_unpinned`, OS đặt 3/8 luồng vào nhân E (`12, 14, 16`); ở `T1_N20_t8_unpinned`, OS đặt 4/8 luồng vào nhân E (`8, 9, 18, 19`) `[ĐO]`.

---

### 2.3. Pha T2 — Ghim Nhân P/E (`-Cr`, `--cpu-strict 1`, `taskset`), Kiểm chứng Đột biến & Cờ Đơn lẻ (`p9_t2_cpu_affinity.jsonl`)

| Tag (`p9_t2_cpu_affinity.jsonl`) | `-ncmoe` | Cờ `extra_args` / `taskset` | Decode Median (`tok/s`) | Min–Max (`tok/s`) | Burn-in Decode / Prefill (`tok/s`) | `Cpus_allowed_list` (`/proc/<pid>/task/*/status`) | `active_psrs` thực tế (`ps -L`) | Xung P / E (`MHz`) & Nhiệt CPU |
|---|---:|---|---:|---:|---:|---|---|---|
| **Ma trận tại `-ncmoe 48` (100% MoE trên CPU)** | | | | | | | | |
| `T2_N48_base_A` | `48` | `[]` | **`26,08`** `[ĐO]` | `24,38–27,99` | `26,41 / 145,5` | `['0-19']` | `[2, 5, 11, 13]` | `2588 / 3826` (`64°C`) |
| `T2_N48_t8_unpinned` | `48` | `['-t', '8']` | **`28,52`** `[ĐO]` | `26,84–29,51` | `26,95 / 172,7` | `['0-19']` | `[0, 1, 2, 4, 8, 9, 10, 17]` | `2981 / 3868` (`59°C`) |
| `T2_N48_t8_Cr0_7_nostrict` | `48` | `['-t', '8', '-Cr', '0-7']` | **`31,55`** `[ĐO]` | `30,65–31,68` | `31,08 / 159,2` | `['0-19', '0-7']` | `[0, 1, 2, 4, 5, 6, 7]` | `4125 / 1219` (`66°C`) |
| `T2_N48_t8_Cr0_7_strict1` | `48` | `['-t', '8', '-Cr', '0-7', '--cpu-strict', '1']` | **`29,98`** `[ĐO]` | `29,00–31,17` | `28,39 / 171,3` | `['0', '0-19', '1'..'7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `4542 / 2254` (`67°C`) |
| `T2_N48_t8_taskset0_7` | `48` | `taskset -c 0-7` + `['-t', '8']` | **`32,08`** `[ĐO]` | `31,68–32,72` | `32,12 / 177,8` | `['0-7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `4609 / 1559` (`64°C`) |
| `T2_N48_MUTATION_t8_Cr8_15_strict1` | `48` | `['-t', '8', '-Cr', '8-15', '--cpu-strict', '1']` | **`26,49`** `[ĐO]` | `26,18–26,84` | `26,01 / 128,8` | `['0-19', '8'..'15']` | `[8, 9, 10, 11, 12, 13, 14, 15]` | `2230 / 3826` (`61°C`) |
| `T2_N48_MUTATION_t8_taskset8_15` | `48` | `taskset -c 8-15` + `['-t', '8']` | **`29,77`** `[ĐO]` | `29,39–30,00` | `28,60 / 128,9` | `['8-15']` | `[8, 9, 10, 11, 12, 13, 14, 15]` | `1243 / 4470` (`59°C`) |
| `T2_N48_MUTATION_t8_Cr4_11_strict1` | `48` | `['-t', '8', '-Cr', '4-11', '--cpu-strict', '1']` | **`29,32`** `[ĐO]` | `28,90–30,10` | `29,03 / 159,5` | `['0-19', '10', '11', '4'..'9']` | `[4, 5, 6, 7, 8, 9, 10, 11]` | `2998 / 2736` (`66°C`) |
| `T2_N48_t12_Cr0_11_strict1` | `48` | `['-t', '12', '-Cr', '0-11', '--cpu-strict', '1']` | **`18,28`** `[ĐO]` | `17,86–18,75` | `17,91 / 152,0` | `['0', '0-19', '1'..'11']` | `[0..11]` | `4287 / 3972` (`63°C`) |
| `T2_N48_t20_Cr0_19_strict1` | `48` | `['-t', '20', '-Cr', '0-19', '--cpu-strict', '1']` | **`14,40`** `[ĐO]` | `14,04–14,81` | `14,05 / 160,6` | `['0', '0-19', '1'..'19']` | `[0..19]` | `5081 / 4600` (`68°C`) |
| `T2_N48_base_B` | `48` | `[]` | **`25,34`** `[ĐO]` | `24,66–27,71` | `24,52 / 145,7` | `['0-19']` | `[1, 5, 11, 16]` | `2076 / 2368` (`58°C`) |
| **Ma trận tại `-ncmoe 20` (28 tầng GPU + 20 tầng CPU)** | | | | | | | | |
| `T2_N20_base_A` | `20` | `[]` | **`41,32`** `[ĐO]` | `40,76–43,11` | `41,17 / 138,7` | `['0-19']` | `[2, 4, 8, 13]` | `3707 / 3901` (`60°C`) |
| `T2_N20_t8_unpinned` | `20` | `['-t', '8']` | **`44,66`** `[ĐO]` | `42,88–45,93` | `42,27 / 153,0` | `['0-19']` | `[2, 3, 5, 6, 8, 9, 10, 15]` | `3513 / 3971` (`59°C`) |
| `T2_N20_t8_Cr0_7_nostrict` | `20` | `['-t', '8', '-Cr', '0-7']` | **`44,71`** `[ĐO]` | `43,95–46,79` | `44,44 / 153,7` | `['0-19', '0-7']` | `[0, 1, 2, 3, 4, 5, 7]` | `5006 / 1810` (`66°C`) |
| `T2_N20_t8_Cr0_7_strict1` | `20` | `['-t', '8', '-Cr', '0-7', '--cpu-strict', '1']` | **`46,63`** `[ĐO]` | `45,75–47,70` | `44,44 / 165,0` | `['0', '0-19', '1'..'7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `5194 / 1362` (`67°C`) |
| `T2_N20_t8_taskset0_7` | `20` | `taskset -c 0-7` + `['-t', '8']` | **`45,65`** `[ĐO]` | `45,16–46,93` | `44,33 / 156,7` | `['0-7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `4322 / 1456` (`58°C`) |
| `T2_N20_MUTATION_t8_Cr8_15_strict1` | `20` | `['-t', '8', '-Cr', '8-15', '--cpu-strict', '1']` | **`49,29`** `[ĐO]` | `48,68–50,27` | `49,49 / 118,5` | `['0-19', '8'..'15']` | `[8, 9, 10, 11, 12, 13, 14, 15]` | `2239 / 4600` (`59°C`) |
| `T2_N20_MUTATION_t8_taskset8_15` | `20` | `taskset -c 8-15` + `['-t', '8']` | **`51,30`** `[ĐO]` | `50,89–51,77` | `51,32 / 119,6` | `['8-15']` | `[8, 9, 10, 11, 12, 13, 14, 15]` | `1876 / 4600` (`55°C`) |
| `T2_N20_MUTATION_t8_Cr4_11_strict1` | `20` | `['-t', '8', '-Cr', '4-11', '--cpu-strict', '1']` | **`42,40`** `[ĐO]` | `41,10–43,23` | `41,37 / 138,1` | `['0-19', '10', '11', '4'..'9']` | `[4, 5, 6, 7, 8, 9, 10, 11]` | `2481 / 2528` (`65°C`) |
| `T2_N20_t12_Cr0_11_strict1` | `20` | `['-t', '12', '-Cr', '0-11', '--cpu-strict', '1']` | **`30,13`** `[ĐO]` | `29,40–30,45` | `29,51 / 145,5` | `['0', '0-19', '1'..'11']` | `[0..11]` | `3707 / 2394` (`67°C`) |
| `T2_N20_t20_Cr0_19_strict1` | `20` | `['-t', '20', '-Cr', '0-19', '--cpu-strict', '1']` | **`24,10`** `[ĐO]` | `23,37–25,21` | `24,02 / 150,9` | `['0', '0-19', '1'..'19']` | `[0..19]` | `4783 / 4474` (`62°C`) |
| `T2_N20_t8_Cr0_7_strict1_poll0` | `20` | `... --poll 0` | **`45,06`** `[ĐO]` | `44,61–46,96` | `43,72 / 160,9` | `['0', '0-19', '1'..'7']` | `[0, 1, 2, 3, 4, 6, 7]` | `2233 / 2120` (`57°C`) |
| `T2_N20_t8_Cr0_7_strict1_poll100` | `20` | `... --poll 100` | **`45,19`** `[ĐO]` | `44,75–46,79` | `44,63 / 152,1` | `['0', '0-19', '1'..'7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `5200 / 2131` (`67°C`) |
| `T2_N20_t8_Cr0_7_strict1_prio2` | `20` | `... --prio 2` | **`40,20`** `[ĐO]` | `38,44–42,36` | `41,19 / 157,8` | `['0', '0-19', '1'..'7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `5086 / 1791` (`69°C`) |
| `T2_N20_t8_Cr0_7_strict1_loadnone` | `20` | `... --load-mode none` | **`46,18`** `[ĐO]` | `44,39–48,02` | `43,03 / 235,2` | `['0', '0-19', '1'..'7']` | `[0, 1, 2, 3, 4, 5, 6, 7]` | `4376 / 1456` (`59°C`) |
| `T2_N20_base_B` | `20` | `[]` | **`43,58`** `[ĐO]` | `42,12–45,81` | `40,86 / 151,1` | `['0-19']` | `[1, 2, 9, 10]` | `2485 / 3811` (`54°C`) |

---

### 2.4. Pha T3 — Tối ưu Luồng Prefill / Batch (`-tb`, `-Crb`, `--cpu-strict-batch`, `p9_t3_prefill_batch.jsonl`)

| Tag (`p9_t3_prefill_batch.jsonl`) | `-c` / `-ncmoe` | Cờ `extra_args` | Decode 7 lượt (`median [min–max] tok/s`) | Prefill 8k (`median [min–max] tok/s`, `TTFT ms`, post-dec) | Prefill 24k (`median [min–max] tok/s`, `TTFT ms`, post-dec) | `pf_allowed` & `pf_psrs` khi Prefill |
|---|---|---|---:|---:|---:|---|
| `T3_c8k_N20_base_A` | `8192` / `20` | `[]` | **`41,63`** `[41,23–41,85]` `[ĐO]` | **`897,4`** `[892,5–914,6]`, `8.651,9 ms`, post `38,28` `[ĐO]` | — | `allowed=['0-19']`, `psrs=[1,2,10,16]` `[ĐO]` |
| `T3_c8k_N20_tb8_Crb0_7` | `8192` / `20` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 8 -Crb 0-7 --cpu-strict-batch 1` | **`45,52`** `[43,83–47,12]` `[ĐO]` | **`914,5`** `[903,3–916,0]`, `8.497,2 ms`, post **`43,20`** `[ĐO]` | — | `allowed=['0','0-19','1'..'7']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c8k_N20_tb12_Crb0_11` | `8192` / `20` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 12 -Crb 0-11 --cpu-strict-batch 1` | **`45,56`** `[43,88–46,79]` `[ĐO]` | **`895,5`** `[891,2–914,3]`, `8.670,0 ms`, post `42,00` `[ĐO]` | — | `allowed=['0','0-19','1'..'7']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c8k_N20_tb16_Crb0_15` | `8192` / `20` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 16 -Crb 0-15 --cpu-strict-batch 1` | **`45,15`** `[43,92–46,72]` `[ĐO]` | **`907,0`** `[887,3–915,0]`, `8.559,7 ms`, post `41,87` `[ĐO]` | — | `allowed=['0','0-19','1'..'7']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c8k_N20_tb20_Crb0_19` | `8192` / `20` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 20 -Crb 0-19 --cpu-strict-batch 1` | **`44,88`** `[43,45–45,35]` `[ĐO]` | **`881,2`** `[876,6–887,8]`, `8.820,1 ms`, post `41,10` `[ĐO]` | — | `allowed=['0','0-19','1'..'7']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c8k_N20_tb20_Crb0_19_nostrict` | `8192` / `20` | `-t 8 -Cr 0-7 -tb 20 -Crb 0-19` | **`45,41`** `[44,19–46,81]` `[ĐO]` | **`931,0`** `[919,1–932,3]`, `8.351,2 ms`, post `41,07` `[ĐO]` | — | `allowed=['0-19','0-7']`, `psrs=[0,1,2,3,5,6,7]` `[ĐO]` |
| `T3_c8k_N20_MUTATION_tb4_Crb8_11` | `8192` / `20` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 4 -Crb 8-11 --cpu-strict-batch 1` | **`45,56`** `[44,44–46,96]` `[ĐO]` | **`895,9`** `[878,0–903,7]`, `8.665,7 ms`, post `42,27` `[ĐO]` | — | `allowed=['0','0-19','1'..'8']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c32k_N30_base_A` | `32768` / `30` | `[]` | **`34,43`** `[32,87–35,48]` `[ĐO]` | **`741,4`** `[721,6–751,4]`, `10.470,9 ms`, post `28,84` `[ĐO]` | **`686,4`** `[648,3–694,5]`, `33.808,9 ms`, post `26,81` `[ĐO]` | `allowed=['0-19']`, `psrs=[1,2,4,9,10]` `[ĐO]` |
| `T3_c32k_N30_tb8_Crb0_7` | `32768` / `30` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 8 -Crb 0-7 --cpu-strict-batch 1` | **`38,39`** `[37,40–39,14]` `[ĐO]` | **`762,3`** `[750,8–764,6]`, `10.184,2 ms`, post `31,29` `[ĐO]` | **`686,0`** `[673,8–695,8]`, `33.828,6 ms`, post `28,16` `[ĐO]` | `allowed=['0','0-19','1'..'7']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c32k_N30_tb20_Crb0_19` | `32768` / `30` | `-t 8 -Cr 0-7 --cpu-strict 1 -tb 20 -Crb 0-19 --cpu-strict-batch 1` | **`38,56`** `[36,93–39,81]` `[ĐO]` | **`764,0`** `[738,6–765,0]`, `10.161,0 ms`, post `31,33` `[ĐO]` | **`687,7`** `[661,4–699,6]`, `33.744,7 ms`, post `28,72` `[ĐO]` | `allowed=['0','0-19','1'..'7']`, `psrs=[0..7]` `[ĐO]` |
| `T3_c32k_N30_tb20_Crb0_19_nostrict` | `32768` / `30` | `-t 8 -Cr 0-7 -tb 20 -Crb 0-19` | **`37,95`** `[36,36–39,85]` `[ĐO]` | **`764,2`** `[755,0–773,3]`, `10.158,3 ms`, post `30,52` `[ĐO]` | **`692,3`** `[686,2–695,8]`, `33.520,4 ms`, post `28,23` `[ĐO]` | `allowed=['0-19','0-7']`, `psrs=[0..7]` `[ĐO]` |

- **Giải phẫu nguyên nhân kỹ thuật (`[ĐO]` + `[ĐỌC MÃ]` disassembly `.aicf-work/bin/libggml-cpu-alderlake.so` và `libllama.so.0`):**
  1. Thư viện `.aicf-work/bin/libggml-cpu-alderlake.so` được biên dịch với **`GGML_USE_OPENMP` (`libgomp.so.1`, gọi `GOMP_parallel` tại `0x2084b`)** `[ĐỌC MÃ]`. Trong `libgomp`, mỗi luồng gọi (`main` / `sched`) quản lý một team luồng OpenMP cố định (`total_os_threads = 34` khi `-t 8`), và khi `-Cr 0-7 --cpu-strict 1` đã ghim team OpenMP này vào `CPU 0..7`, cờ `-tb` / `-Crb` không tạo ra một team OpenMP thứ hai tách biệt trên `CPU 8..19` `[ĐO]` `[ĐỌC MÃ]`.
  2. Do đó, phép thử đột biến bắt buộc `T3_c8k_N20_MUTATION_tb4_Crb8_11` (`-tb 4 -Crb 8-11`) giữ nguyên `pf_psrs = [0, 1, 2, 3, 4, 5, 6, 7]` và tốc độ prefill `895,9 tok/s` không đổi `[ĐO]`. Tuân thủ nghiêm ngặt Quy tắc 3 & 4 (Cổng T3), chúng tôi **bác bỏ việc thêm `-tb` / `-Crb` / `--cpu-strict-batch`** và chỉ giữ lại bộ cờ decode `-t 8 -Cr 0-7 --cpu-strict 1`.

---

### 2.5. Pha T4.1 — Biên độ VRAM & Hiệu năng KV-Cache `q8_0` vs `f16` (`p9_t4_kv_q8.jsonl`)

| Tag (`p9_t4_kv_q8.jsonl`) | `-c` | `-ncmoe` | KV (`-ctk/-ctv`) | Cờ CPU | Trạng thái | VRAM đỉnh (`MiB`) | Decode 7 lượt (`median [min–max] tok/s`) | Prefill 8k (`median tok/s` / `TTFT ms` / post-dec) | Prefill 24k (`median tok/s` / `TTFT ms` / post-dec) |
|---|---:|---:|---|---|---|---:|---:|---:|---:|
| `T4_c8k_N20_f16_cpuopt` | `8192` | `20` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | `healthy` `[ĐO]` | `11.735` `[ĐO]` | **`46,11`** `[39,18–47,20]` `[ĐO]` | **`900,5`** / `8.626,7` / **`40,11`** `[ĐO]` | — |
| `T4_c8k_N20_q8_cpuopt` | `8192` | `20` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | `healthy` `[ĐO]` | `11.377` `[ĐO]` | **`45,44`** `[33,89–47,25]` `[ĐO]` | **`891,6`** / `8.710,1` / `32,40` `[ĐO]` | — |
| `T4_c32k_N30_f16_base` | `32768` | `30` | `f16` | `[]` (mặc định) | `healthy` `[ĐO]` | `10.607` `[ĐO]` | **`34,82`** `[33,65–35,27]` `[ĐO]` | `711,0` / `10.925,9` / `29,83` `[ĐO]` | `666,5` / `34.819,5` / `26,53` `[ĐO]` |
| `T4_c32k_N30_f16_cpuopt` | `32768` | `30` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | `healthy` `[ĐO]` | `10.629` `[ĐO]` | **`38,18`** `[34,92–39,87]` `[ĐO]` | **`765,3`** / `10.176,2` / **`34,60`** `[ĐO]` | **`675,8`** / `34.392,6` / **`27,84`** `[ĐO]` |
| `T4_c32k_N30_q8_cpuopt` | `32768` | `30` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | `healthy` `[ĐO]` | **`9.314`** (`-1.315 MiB`) `[ĐO]` | **`33,98`** `[32,39–37,41]` `[ĐO]` | `742,0` / `10.470,5` / `26,21` `[ĐO]` | `658,8` / `35.242,4` / `18,67` `[ĐO]` |
| `T4_c32k_N26_f16_cpuopt` | `32768` | `26` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | **OOM khi khởi tạo** (`rc=1`) `[ĐO]` | `8.635` (lúc lỗi `cudaMalloc 249,5 MiB`) `[ĐO]` | — | — | — |
| `T4_c32k_N24_f16_cpuopt` | `32768` | `24` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | **OOM khi khởi tạo** (`rc=1`) `[ĐO]` | `9.283` (lúc lỗi `cudaMalloc 3072 MiB`) `[ĐO]` | — | — | — |
| `T4_c32k_N26_q8_cpuopt` | `32768` | `26` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | `healthy` `[ĐO]` | **`10.587`** `[ĐO]` | **`39,05`** `[36,86–39,61]` `[ĐO]` | **`812,8`** / `9.556,1` / `29,03` `[ĐO]` | **`717,6`** / `32.356,4` / `20,76` `[ĐO]` |
| `T4_c32k_N24_q8_cpuopt` | `32768` | `24` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | `healthy` `[ĐO]` | **`11.233`** (`< 11.750`) `[ĐO]` | **`39,88`** `[38,71–41,95]` `[ĐO]` | **`845,7`** / `9.184,3` / `31,14` `[ĐO]` | **`729,8`** / `31.800,0` / `20,58` `[ĐO]` |
| `T4_c32k_N22_q8_cpuopt` | `32768` | `22` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | **CUDA OOM khi decode** (`cudaGraphInstantiate`) `[ĐO]` | **`11.881`** (`> 11.750`) `[ĐO]` | Burn-in: `41,84` (lỗi ở lượt 1) `[ĐO]` | — | — |

---

### 2.6. Pha T5 — Xác nhận Xen kẽ Cuối cùng (`ABAB`) & Đo Bền Nhiệt 30 Phút (`p9_t5_confirmation.jsonl`, `p9_t5_thermal_timeseries.jsonl`)

#### A. Bảng đối chiếu xen kẽ `ABAB` (`p9_t5_confirmation.jsonl`)

| Thứ tự chạy | Tag | `-c` | `-ncmoe` | KV | Cờ CPU | VRAM đỉnh (`MiB`) | Decode 7 lượt (`median [min–max] tok/s`) | Prefill 8k (`median tok/s` / `TTFT ms` / post-dec) | Prefill 24k (`median tok/s` / `TTFT ms` / post-dec) | `active_psrs` khi Decode |
|---:|---|---:|---:|---|---|---:|---:|---:|---:|---|
| `1` | `T5_c8k_ABAB_1_base` | `8192` | `20` | `f16` | `[]` | `11.717` | **`46,99`** `[42,36–48,09]` `[ĐO]` | `933,1` / `8.318,2` / `41,35` `[ĐO]` | — | `[0, 1, 9, 10]` `[ĐO]` |
| `2` | `T5_c8k_ABAB_2_opt` | `8192` | `20` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | `11.735` | **`48,09`** `[45,90–49,74]` `[ĐO]` | **`952,0`** / `8.157,0` / **`41,88`** `[ĐO]` | — | `[0..7]` `[ĐO]` |
| `3` | `T5_c8k_ABAB_3_base` | `8192` | `20` | `f16` | `[]` | `11.735` | **`42,55`** `[39,56–44,69]` `[ĐO]` | `904,8` / `8.580,2` / `38,32` `[ĐO]` | — | `[4, 8, 10, 13]` `[ĐO]` |
| `4` | `T5_c8k_ABAB_4_opt` | `8192` | `20` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | `11.735` | **`45,93`** `[44,67–46,38]` `[ĐO]` | `898,3` / `8.642,9` / **`42,49`** `[ĐO]` | — | `[0..7]` `[ĐO]` |
| `5` | `T5_c32k_ABAB_1_base` | `32768` | `30` | `f16` | `[]` | `10.607` | **`34,11`** `[33,69–34,72]` `[ĐO]` | `750,6` / `10.345,6` / `30,45` `[ĐO]` | `689,1` / `33.688,5` / `26,44` `[ĐO]` | `[0, 3, 17, 18]` `[ĐO]` |
| `6` | `T5_c32k_ABAB_2_opt_q8_N24` | `32768` | `24` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | `11.233` | **`39,75`** `[32,12–40,87]` `[ĐO]` | **`839,3`** / `9.270,5` / `30,95` `[ĐO]` | **`729,1`** / `31.885,1` / `20,67` `[ĐO]` | `[0..7]` `[ĐO]` |
| `7` | `T5_c32k_ABAB_3_base` | `32768` | `30` | `f16` | `[]` | `10.629` | **`34,23`** `[33,27–34,90]` `[ĐO]` | `774,4` / `10.051,7` / `31,08` `[ĐO]` | `689,9` / `33.688,1` / `27,31` `[ĐO]` | `[0, 2, 3, 5]` `[ĐO]` |
| `8` | `T5_c32k_ABAB_4_opt_q8_N24` | `32768` | `24` | `q8_0` | `-t 8 -Cr 0-7 --cpu-strict 1` | `11.233` | **`41,05`** `[39,24–41,72]` `[ĐO]` | **`814,3`** / `9.539,3` / `31,54` `[ĐO]` | **`734,8`** / `31.599,2` / `20,56` `[ĐO]` | `[0..7]` `[ĐO]` |
| `9` | `T5_c32k_ABAB_5_opt_f16_N30` | `32768` | `30` | `f16` | `-t 8 -Cr 0-7 --cpu-strict 1` | `10.629` | **`38,39`** `[36,40–38,85]` `[ĐO]` | `767,0` / `10.156,3` / **`34,70`** `[ĐO]` | `687,2` / `33.787,9` / **`27,96`** `[ĐO]` | `[0..7]` `[ĐO]` |

#### B. Bài kiểm tra tải liên tục 30 phút (`p9_t5_thermal_timeseries.jsonl`)
- **Cấu hình chạy liên tục `30,09 phút` (`1.805,4 giây`, tổng cộng `239` request liên tiếp = `215` request decode 256 token + `23` request prefill 8k kèm decode 64 token + `1` burn-in):** `-c 32768 -ncmoe 24 -ctk q8_0 -ctv q8_0 -t 8 -Cr 0-7 --cpu-strict 1` `[ĐO]`.
- **Độ ổn định tốc độ theo thời gian (`[ĐO]`):**
  - Nửa đầu (`0 – 15 phút`, `n = 107` lượt decode 256 token): median = **`39,89 tok/s`** `[ĐO]`.
  - Nửa sau (`15 – 30 phút`, `n = 108` lượt decode 256 token): median = **`40,32 tok/s`** ($\Delta = +0,43\text{ tok/s}$ — **không có hiện tượng giảm xung nhiệt / thermal throttling**) `[ĐO]`.
  - Toàn bộ 30 phút (`n = 215` lượt ấm): median = **`40,22 tok/s`** (`min = 28,35`, `max = 42,60`) `[ĐO]`.
  - Các lượt prefill 8k xen kẽ mỗi 10 bước (`n = 23`): prefill median = **`823,38 tok/s`**, decode 64 token ngay sau prefill 8k median = **`30,68 tok/s`** `[ĐO]`.
- **Độ ổn định Nhiệt độ, VRAM, RAM và Swap (`[ĐO]`):**
  - Nhiệt độ GPU RTX 3060: duy trì trong dải **`47,0 °C – 55,0 °C`** (`công suất 58,4 W` khi decode, `97,3 – 104,2 W` khi prefill 8k) `[ĐO]`.
  - Nhiệt độ gói CPU (`x86_pkg_temp`) trong suốt các lượt đo `T0–T5`: **`55,0 °C – 72,0 °C`** (dưới rất xa ngưỡng giảm xung `100 °C` của Arrow Lake); xung nhịp trung bình 8 nhân P khi decode duy trì **`4.184 – 5.200 MHz`** `[ĐO]`.
  - VRAM sử dụng: giữ phẳng tuyệt đối ở **`11.233,0 MiB`** từ phút thứ `6,5` đến phút thứ `30,09` (**rò rỉ VRAM = `0 MiB`**) `[ĐO]`.
  - RAM khả dụng (`MemAvailable`) và Swap (`SwapUsed`): trước bài đo 30 phút `MemAvailable = 23.670 MiB`, `SwapUsed = 12.952 MiB`; sau 30 phút chạy liên tục `MemAvailable = 19.561 MiB`, `SwapUsed = 12.561 MiB` ($\Delta\text{Swap} = -391\text{ MiB}$, **không tăng swap, không có OOM-killer trong `journalctl -k`**) `[ĐO]`.

---

## 3. Bằng chứng Ghim Nhân OS Thực tế (`psr` / `Cpus_allowed_list`) & Kiểm chứng Đột biến (Mutation Check)

### 3.1. Phân biệt 3 cơ chế ghim nhân trên Linux (`-Cr` vs `-Cr --cpu-strict 1` vs `taskset -c`)

| Cơ chế | Bằng chứng `/proc/<pid>/task/*/status` (`Cpus_allowed_list`) | Bằng chứng `ps -L -p <pid> -o psr` khi đang Decode | Decode tại `N=48` (`tok/s`) | Decode tại `N=20` (`tok/s`) | Giải thích cơ chế `[ĐO]` `[ĐỌC MÃ]` |
|---|---|---|---:|---:|---|
| **Mặc định (Không cờ)** | `['0-19']` (mọi luồng chạy tự do trên 20 nhân) `[ĐO]` | `4` luồng, thường xuyên rơi vào nhân E (`[4,11,13,15]`, `[5,10,11,14]`, `[4,8,10,13]`) `[ĐO]` | `25,34–26,38` `[ĐO]` | `41,32–46,99` `[ĐO]` | `n_threads = 4`, bộ lập lịch Linux di trú luồng sang nhân E làm giảm tốc độ và gây phương sai lớn giữa các lần khởi chạy. |
| **`-t 8 -Cr 0-7` (`--cpu-strict 0`)** | `['0-19', '0-7']` (8 luồng worker có mặt nạ `0-7`, luồng phụ giữ `0-19`) `[ĐO]` | `[0, 1, 2, 4, 5, 6, 7]` `[ĐO]` | **`31,55`** `[ĐO]` | **`44,71`** `[ĐO]` | Gán một mặt nạ tập thể `0-7` cho nhóm 8 luồng tính toán; OS tự xếp 8 luồng trong 8 nhân P. |
| **`-t 8 -Cr 0-7 --cpu-strict 1`** | `['0', '0-19', '1', '2', '3', '4', '5', '6', '7']` (từng luồng worker ghim cứng 1-1 vào từng nhân `0..7`) `[ĐO]` | `[0, 1, 2, 3, 4, 5, 6, 7]` `[ĐO]` | **`29,98`** `[ĐO]` | **`45,52–48,09`** `[ĐO]` | Ghim 1-1 từng luồng worker vào đúng 1 nhân P cố định (`pthread_setaffinity_np`), loại bỏ di trú luồng giữa các nhân P, đồng thời để các luồng HTTP/CUDA quản lý ở `0-19`. |
| **`taskset -c 0-7 ... -t 8`** | `['0-7']` (toàn bộ 34 luồng của tiến trình kể cả HTTP/CUDA bị nhốt vào `0-7`) `[ĐO]` | `[0, 1, 2, 3, 4, 5, 6, 7]` `[ĐO]` | **`32,08`** `[ĐO]` | **`45,65`** `[ĐO]` | Nhốt toàn bộ tiến trình vào `0-7`. Nhanh nhất ở `N=48` (thuần CPU), nhưng ở `N=20` (hybrid GPU+CPU) kém nhẹ so với `--cpu-strict 1` (`45,65` vs `46,63 tok/s`) vì luồng điều phối CUDA phải chia sẻ nhân `0-7` với 8 luồng OpenMP đang spin-wait (`--poll 50`). |

### 3.2. Kết quả Kiểm chứng Đột biến (Mutation Check)
1. **Đột biến tại `-ncmoe 48` (100% MoE trên CPU — bài đo cô lập năng lực tính toán CPU):**
   - Ghim đúng 8 nhân P (`-t 8 -Cr 0-7` / `--cpu-strict 1` / `taskset 0-7`): đạt **`29,98 – 32,08 tok/s`**, `active_psrs = [0..7]` `[ĐO]`.
   - **Đột biến 1 — Ghim sai sang 8 nhân E (`-t 8 -Cr 8-15 --cpu-strict 1` và `taskset -c 8-15`):** `Cpus_allowed_list` chuyển sang `8..15`, `active_psrs` chuyển sang **`[8, 9, 10, 11, 12, 13, 14, 15]`**, tốc độ decode tụt xuống **`26,49 tok/s`** (`-Cr 8-15 --cpu-strict 1`, giảm `-3,49` đến `-5,06 tok/s`) và **`29,77 tok/s`** (`taskset -c 8-15`, giảm `-2,31 tok/s` so với `taskset -c 0-7`) `[ĐO]`.
   - **Đột biến 2 — Ghim lệch 4P + 4E (`-t 8 -Cr 4-11 --cpu-strict 1`):** `active_psrs = [4..11]`, tốc độ giảm xuống **`29,32 tok/s`** `[ĐO]`.
   - **Đột biến 3 — Mở tràn sang nhân E có ghim cứng (`-t 12 -Cr 0-11 --cpu-strict 1` và `-t 20 -Cr 0-19 --cpu-strict 1`):** Tốc độ sụp đổ xuống **`18,28 tok/s`** (`-t 12`) và **`14,40 tok/s`** (`-t 20`) — giảm hơn `50%` so với `-t 8 -Cr 0-7` vì các nhân P (`5,2 GHz`) phải dừng đợi các nhân E ở mỗi rào chắn đồng bộ OpenMP qua 48 tầng MoE `[ĐO]`.
2. **Hiện tượng kiến trúc đáng chú ý tại `-ncmoe 20` trên Arrow Lake (`Core Ultra 7 265K`):**
   - Tại `N=20`, đột biến trộn 4P+4E (`-t 8 -Cr 4-11 --cpu-strict 1`) làm tốc độ tụt xuống **`42,40 tok/s`** (từ `46,63 tok/s`), và `-t 12` / `-t 20` tụt xuống **`30,13` / `24,10 tok/s`** `[ĐO]`.
   - Tuy nhiên, khi ghim đồng nhất cả 8 luồng vào 8 nhân E Skymont (`-t 8 -Cr 8-15 --cpu-strict 1` hoặc `taskset -c 8-15`) ở `N=20` (chỉ có 20 tầng MoE trên CPU, còn 28 tầng MoE + 48 tầng Attention trên GPU), tốc độ decode ngắn hạn đạt `49,29 – 51,30 tok/s`, trong khi prefill burn-in giảm từ `165,0 tok/s` xuống `118,5 tok/s` (`-28%`) `[ĐO]`.
   - **Giải thích `[SUY LUẬN]`:** Trên Arrow Lake, 8 nhân E Skymont (`CPU 8..15`, chia sẻ cụm L2 `4 MB` / 4 nhân, xung `4,6 GHz`, IPC AVX-VNNI cao) là đồng nhất về tốc độ; khi đẩy 8 luồng spin-wait (`--poll 50`) của OpenMP sang `8..15` ở tải CPU nhẹ (`N=20`), toàn bộ 8 nhân P (`0..7`) được giải phóng 100% cho luồng chính và trình điều khiển CUDA thực hiện đồng bộ CPU↔GPU qua 20 điểm chuyển tiếp. Dù vậy, vì nhân E làm giảm tốc độ prefill (`118,5` vs `165,0 tok/s`) và chậm hơn rõ rệt khi số tầng CPU lớn (`N=48`: `26,49` vs `31,55 tok/s`), cấu hình chuẩn xác và an toàn cho mọi `N` trên `pc-001` vẫn là **ghim vào 8 nhân P (`-t 8 -Cr 0-7 --cpu-strict 1`)**.

---

## 4. Kết quả Kiểm định Chất lượng `q8_0` vs `f16` (Pha T4.2 — `p9_t4_quality.json`)

### 4.1. Perplexity trên Văn bản Mã nguồn 32.768 Token (`.aicf-work/bench_corpus_p9.txt`)
- **Tệp dữ liệu chuẩn:** `.aicf-work/bench_corpus_p9.txt` (`172.677 byte`, SHA256: `332a2bcddb7f6ac988f5d3c54f1c712b37ccbfeab4061baeb4601664b4e91a54`) `[ĐO]`.
- **Lệnh đo:** `.aicf-work/bin/llama-perplexity -m .aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf -f .aicf-work/bench_corpus_p9.txt -c 8192 --chunks 4 -ngl 99 -ncmoe 30 -fa on -t 8 -Cr 0-7 --cpu-strict 1` (`4 chunks × 8.192 = 32.768 token`) `[ĐO]`:

| Kiểu KV-cache | Perplexity (`PPL`) | Độ lệch chuẩn (`±`) | Thời gian chạy (`s`) | Chênh lệch $\Delta\text{PPL} = \text{PPL}_{q8\_0} - \text{PPL}_{f16}$ | Cổng $|\Delta\text{PPL}| \le 0,05$ |
|---|---:|---:|---:|---:|---|
| `-ctk f16 -ctv f16` | **`4,0418`** `[ĐO]` | `± 0,08188` `[ĐO]` | `75,04 s` `[ĐO]` | — | — |
| `-ctk q8_0 -ctv q8_0` | **`4,0411`** `[ĐO]` | `± 0,08186` `[ĐO]` | `75,40 s` `[ĐO]` | **`-0,0007`** `[ĐO]` | **ĐẠT** (`0,0007 <= 0,05`) `[ĐO]` |

### 4.2. Đánh giá 15 Bài toán Sinh Code Python (`temperature = 0`, `seed = 42`, chấm tự động bằng `assert`)
- Đối chiếu trực tiếp giữa `-c 32768 -ncmoe 30 -ctk f16 -ctv f16` và `-c 32768 -ncmoe 24 -ctk q8_0 -ctv q8_0` (`[ĐO]`):
  - **Tỷ lệ vượt qua Unit Test (`pass@1`):**
    - `f16` (`-ncmoe 30`): **`14 / 15` (`93,33%`)** `[ĐO]`.
    - `q8_0` (`-ncmoe 24`): **`14 / 15` (`93,33%`)** ($\Delta = 0$) `[ĐO]`.
  - **Số bài có đầu ra trùng khớp từng ký tự (`exact SHA256 match`):** **`13 / 15` (`86,67%`)** (`p01`, `p02`, `p03`, `p05`, `p06`, `p07`, `p08`, `p09`, `p10`, `p11`, `p12`, `p14`, `p15`). Hai bài còn lại (`p04_lru_cache`, `p13_subarray_sum_k`) chỉ khác nhau 1 dòng trống định dạng và đều vượt qua 100% các lệnh `assert` `[ĐO]`.

### 4.3. Đánh giá 2 Bài Truy xuất Chính xác ở Ngữ cảnh Dài `~24,8k` Token (`Needle-in-a-Haystack`)
- Chèn 3 bản ghi `[CONFIG_RECORD]` ở vị trí token thứ `~2.100`, `~11.900` và `~21.800` trong tổng ngữ cảnh `24.847 – 24.888 token` (`[ĐO]`):

| Bài kiểm tra | Số token (`prompt_n`) | Tốc độ Prefill `f16 N=30` (`tok/s`) | Tốc độ Prefill `q8_0 N=24` (`tok/s`) | Kết quả truy xuất 3 khóa (`early`, `mid`, `late`) trên `f16` | Kết quả truy xuất 3 khóa (`early`, `mid`, `late`) trên `q8_0` |
|---|---:|---:|---:|---|---|
| **Needle 24k #1** (`AICF_SECRET_KEY_EARLY/MID/LATE`) | `24.888` (`f16`) / `24.886` (`q8_0`) `[ĐO]` | `700,62` `[ĐO]` | **`743,14`** (`+6,1%`) `[ĐO]` | **`3/3 PASS`** (`K9X-7419-ALPHA`, `M4Z-8823-BRAVO`, `Q7W-3105-OMEGA`) `[ĐO]` | **`3/3 PASS`** (`K9X-7419-ALPHA`, `M4Z-8823-BRAVO`, `Q7W-3105-OMEGA`) `[ĐO]` |
| **Needle 24k #2** (`MODULE_CHECKSUM_01/02/03`) | `24.847` (`f16`) / `24.848` (`q8_0`) `[ĐO]` | `697,52` `[ĐO]` | **`793,81`** (`+13,8%`) `[ĐO]` | **`3/3 PASS`** (`SHA-994120`, `SHA-553817`, `SHA-208469`) `[ĐO]` | **`3/3 PASS`** (`SHA-994120`, `SHA-553817`, `SHA-208469`) `[ĐO]` |

---

## 5. Sai lệch so với Kế hoạch & Những Điều Chưa Đo

### 5.1. Sai lệch / Phát hiện khác với Giả thuyết trong Kế hoạch
1. **Con số `34,53 tok/s` (decode) và `458,2 tok/s` (prefill) của `llama-bench` trong tài liệu P8 mục 9.2 chỉ đúng ở cấp micro-bench `N=48`, còn `llama-server` thực tế có đặc tính khác ở phần prefill (`[ĐO]`):**
   - Ở phần **Decode tại `N=48`**: `llama-server` với `-t 8 -Cr 0-7` đạt **`31,55 tok/s`** và với `taskset -c 0-7 -t 8` đạt **`32,08 tok/s`** `[ĐO]` — tiệm cận mức `34,53 tok/s` của `llama-bench` (phần chênh lệch `~2,4 tok/s` là chi phí quản lý KV-cache, sampling và HTTP slot của `llama-server`).
   - Ở phần **Prefill (`-tb`, `-Crb`)**: Kế hoạch giả định rằng truyền `-tb 20 -Crb 0-19` sẽ huy động cả 20 nhân để tăng tốc prefill trên `llama-server`. Thực tế đo và đọc mã máy (disassembly `libggml-cpu-alderlake.so`) cho thấy bản build `llama-server` hiện tại dùng `GGML_USE_OPENMP` (`libgomp`), nên `-tb` / `-Crb` không tách được một threadpool thứ hai cho prefill và không vượt qua phép thử đột biến `-tb 4 -Crb 8-11` `[ĐO]` `[ĐỌC MÃ]`. Vì vậy, tuân thủ Cổng T3, chúng tôi dừng hướng `-tb` / `-Crb` và không đưa các cờ `-tb` / `-Crb` vào cấu hình khuyến nghị.
2. **Chi phí giải lượng tử (`dequant`) của KV-cache `q8_0` khi ngữ cảnh thực tế đã lấp đầy 24k token (`[ĐO]`):**
   - Kế hoạch dự kiến `q8_0` chỉ ảnh hưởng nhẹ đến tốc độ decode. Phép đo thực tế cho thấy: khi ngữ cảnh ngắn (`~90` token), `-ncmoe 24` + `q8_0` tăng tốc decode rất mạnh (`39,75–41,05 tok/s` vs `34,11–34,82 tok/s`, tức **`+5,06` đến `+6,82 tok/s`**) và tăng tốc prefill 8k/24k (`+10%` đến `+19%`) `[ĐO]`. Tuy nhiên, ngay sau khi prefill 24k token (khi nhân FlashAttention phải đọc qua 24.000 token KV `q8_0` ở mỗi bước sinh token), tốc độ decode nối tiếp đạt `20,56–20,67 tok/s` so với `26,44–27,96 tok/s` của `f16` `[ĐO]`. Báo cáo đã tách bạch rõ hai phương án `32k-A` (`f16 N=30` + ghim nhân P) và `32k-B` (`q8_0 N=24` + ghim nhân P) để Chủ dự án lựa chọn theo đặc thù tải.

### 5.2. Danh sách Những Điều Chưa Đo (`[CHƯA XÁC MINH]`)
1. **Tải đa người dùng đồng thời (`-np > 1`):** Toàn bộ phép đo trong Pha T0–T5 được thực hiện với `-np 1` (khớp với cấu hình production hiện tại của `worker-guard` trên `pc-001`). Chưa đo khi `-np = 2` hoặc `-np = 4` `[CHƯA XÁC MINH]`.
2. **Máy Windows `pc-win-01` (`AMD Ryzen 7 9700X` + `RTX 5070 12 GB`):** Chưa đo hiệu ứng của `-t 8` trên kiến trúc Zen 5 đồng nhất (8 nhân / 16 luồng SMT) của `pc-win-01` `[CHƯA XÁC MINH]`.
3. **Tải tương tác trực tiếp từ IDE (VS Code / Cline) qua `site-gateway`:** Các phép đo trong báo cáo gọi trực tiếp HTTP `/completion` của `llama-server` trên `127.0.0.1:18199` để loại bỏ nhiễu mạng; chưa đo qua lớp proxy của `site-gateway` `[CHƯA XÁC MINH]`.
4. **Các giá trị `-ncmoe` trung gian khác (`N=25`, `N=23` ở `32k` `q8_0`):** Đã đo `N ∈ {30, 26, 24, 22}` ở `32k` `q8_0` (trong đó `N=24` ổn định ở `11.233 MiB` và `N=22` bị OOM ở `11.881 MiB`); chưa đo `N=23` `[CHƯA XÁC MINH]`.

---

## 6. Kết quả Triển khai Pha T6 vào `apps/worker-guard` (Đã Duyệt & Thực thi)

Sau khi Chủ dự án duyệt kết quả Pha T5, Pha T6 đã được triển khai vào [`apps/worker-guard`](../../apps/worker-guard) và các kịch bản vận hành [`deploy/demo/`](../../deploy/demo) `[ĐỌC MÃ]` `[ĐO]`:

1. **Thay đổi trong [`apps/worker-guard/main.go`](../../apps/worker-guard/main.go) và [`apps/worker-guard/engine.go`](../../apps/worker-guard/engine.go) `[ĐỌC MÃ]`:**
   - Bổ sung các cờ tùy chọn `--threads` (`-t`), `--threads-batch` (`-tb`), `--cpu-range` (`-Cr`), `--cpu-range-batch` (`-Crb`), `--cpu-strict` (`--cpu-strict 1`), `--load-mode` (`--load-mode`).
   - Toàn bộ các cờ có mặc định `0` / `""` / `false`, bảo toàn 100% hành vi cũ khi không cấu hình.
   - Nhánh `e.Mode == "rpc-worker"` giữ nguyên tuyệt đối (chỉ truyền `-H <host> -p <port> -c` cho `ggml-rpc-server`, không rò rỉ cờ CPU).
   - Hàm `validateCPURange` kiểm tra cú pháp dải nhân `^\d+(-\d+)?(,\d+(-\d+)?)*$`, thứ tự `lo <= hi` và giới hạn chỉ số nhân `< runtime.NumCPU()`.
2. **Kiểm thử Đơn vị (`go test -v ./...` trong `apps/worker-guard`) `[ĐO]`:**
   - `TestLlamaEngineDefaultArgsUnchangedRegression`: xác nhận `e.Args()` khi không bật cờ mới sinh ra mảng đối số giống hệt từng phần tử so với trước khi sửa (`PASS`).
   - `TestLlamaEngineCPUAffinityAndMutationChecks`: xác nhận truyền đúng `-t 8 -tb 20 -Cr ... -Crb ... --cpu-strict 1 --load-mode none`, xác nhận `rpc-worker` không bị rò cờ CPU, và xác nhận từ chối 8 đột biến đầu vào sai (`Threads < 0`, `ThreadsBatch < 0`, dải ngược `7-0`, chuỗi tiêm lệnh, chỉ số nhân `>= runtime.NumCPU()`, `LoadMode` sai) (`PASS`).
   - Toàn bộ **`30/30` unit test** trong `apps/worker-guard` vượt qua (`0,043 s`) `[ĐO]`.
3. **Cập nhật kịch bản vận hành (`deploy/demo/coding_profile.env`, `deploy/demo/run_demo.sh`, `deploy/demo/worker.sh`) `[ĐỌC MÃ]`:**
   - Thiết lập `AICF_CPU_FLAGS="--threads 8 --cpu-range 0-7 --cpu-strict"` cho worker Linux `pc-001` (`Core Ultra 7 265K`), không áp dụng sang Windows `pc-win-01`.


