# P12 / Pha S0 (bước 1–2): RÀ SỐ LIỆU P11 VÀ ĐỌC MÃ ĐƯỜNG PP CỦA SGLang v0.5.19
*(2026-11-01. Chỉ đọc: không chạy GPU, không đổi dịch vụ, không đụng GitHub. Nhãn: `[ĐỌC MÃ]` đã đọc; `[SUY LUẬN]` chưa đo.)*

## 1. Cấu hình hai engine trong phép đo PP=2 của P11 (`p11_2gpu_runner.py`)  `[ĐỌC MÃ]`
| Mục | SGLang 0.5.19 | vLLM 0.32.0 | Hệ quả |
|---|---|---|---|
| CUDA graph | `--disable-prefill-cuda-graph --disable-decode-cuda-graph` | `--enforce-eager` | Cả hai chạy eager ⇒ đối xứng, **không** giải thích khoảng cách |
| Attention / sampling | `--attention-backend triton --sampling-backend pytorch` | mặc định của vLLM (+ `VLLM_USE_FLASHINFER_SAMPLER=0`) | **Bất đối xứng**: kernel attention/sampling khác nhau, có thể ảnh hưởng độ trễ token, không liên quan mạng. Cần cân bằng hoặc đo riêng |
| Số yêu cầu tối đa | `--max-running-requests 16` | `--max-num-seqs 16` | tương đương |
| Bộ nhớ | `--mem-fraction-static 0.82` | `--gpu-memory-utilization 0.82` | tương đương |
| Ngữ cảnh | `--context-length 16384` | `--max-model-len 16384` | tương đương |
| Điều phối 2 nút | `--dist-init-addr 10.42.0.1:29500 --nccl-port 29505` | `--master-addr/--master-port` + `VLLM_HOST_IP` | khác cơ chế |
| NCCL | `NCCL_SOCKET_IFNAME` (`enp133s0` / `nccl0`), `NCCL_IB_DISABLE=1`, `NCCL_P2P_DISABLE=1` | giống | tương đương |
- Đáng chú ý: P10 (một card) chạy SGLang **không** đặt `--attention-backend triton` (grep `p10_sglang_lmdeploy_runner.py` không thấy). Nên số một card của P10 và PP của P11 **không dùng cùng backend**; không so thẳng hai bảng.
- **Đường mạng P11 KHÔNG phải WireGuard** như kế hoạch `KE_HOACH_CAI_THIEN_WINDOWS_GOP_2_GPU_SGLANG_VLLM.md` giả định: là cầu `nccl0` (giao diện dummy mang IP `10.42.0.225/32` trong WSL2) + `iptables` DNAT/SNAT + dải cổng `40000–40060` + `netsh portproxy` trên Windows `[ĐỌC MÃ: p11_2gpu_runner.py:142–153, P11.md §1.2]`. Mỗi gói đi thêm hai bước chuyển (portproxy Windows, NAT trong WSL2) ⇒ độ trễ nền cao hơn cáp trần và **áp dụng cho cả hai engine**, nhưng nhân với số vòng đi–về mỗi token.

## 2. Đường truyền dữ liệu PP mỗi bước decode (SGLang v0.5.19)  `[ĐỌC MÃ]`
Nguồn: `python/sglang/srt/managers/scheduler_pp_mixin.py` (`event_loop_pp`, dòng 75–183) và `distributed/parallel_state.py` (`send_tensor_dict`/`send_object`).
- Mỗi vòng lặp (mỗi micro-batch, kể cả khi decode `C=1`), mỗi ranh giới stage:
  1. **Yêu cầu** (`recv_reqs`, kể cả danh sách rỗng) gửi sang stage kế qua `point_to_point_pyobj` trên **`world_group.cpu_group` (gloo)**, bất đồng bộ; stage nhận **chờ đồng bộ**.
  2. **Proxy tensors (hidden states)**: `send_tensor_dict` = **metadata trước (`send_object`, gloo, 2 thông điệp: kích thước rồi nội dung)**, rồi từng tensor qua **NCCL** (`device_group`, `isend`). Phía nhận phải nhận metadata xong mới biết hình dạng để đăng ký `recv` NCCL.
  3. **Kết quả (`next_token_ids`)** từ stage cuối quay về stage đầu: lại metadata gloo + tensor NCCL, nhận đồng bộ.
- ⇒ Mỗi token cần **tối thiểu hai lần băng qua mạng** (đi và về). Cách làm hiện tại thêm **một chuỗi phụ thuộc** "metadata gloo → rồi mới NCCL" cho mỗi từ điển tensor, tức có thể là **hơn hai lần độ trễ nối tiếp** trên đường găng `[SUY LUẬN, chưa đo]`. Thông điệp yêu cầu (gloo) gửi sớm nên có thể nằm ngoài đường găng.
- Với dữ liệu nhỏ và cố định (decode: `hidden_size × batch`, `next_token_ids`), metadata gần như không đổi giữa các bước ⇒ **ứng viên tối ưu**: lưu đệm metadata (hoặc gộp metadata và tensor) để bỏ bước gloo; cần đo trước khi kết luận.
- Lưu ý bảo mật theo quy tắc SGLang: `send_object`/`recv_object` dùng pickle; mọi thay đổi không được mở rộng việc giải pickle dữ liệu từ mạng.
- **vLLM** dùng cùng khuôn `send_tensor_dict` (metadata qua CPU group, tensor qua device group) `[SUY LUẬN từ kiến thức về mã vLLM, chưa đọc trong phiên này]` ⇒ khác biệt có thể nằm ở **phần còn lại** của vòng lặp, không nhất thiết ở giao thức trên.

## 3. Điều rút ra cho Pha S1 (đo)
1. **Cân bằng cấu hình trước khi so:** chạy SGLang PP=2 với backend tương đương vLLM (nếu `sm_120` cho phép), hoặc ít nhất đo riêng ảnh hưởng của `--attention-backend triton` và `--sampling-backend pytorch` ở một card, `C=1`.
2. **Đo từng thành phần của một token PP=2 `C=1`:** thời gian tính toán từng stage, thời gian chờ gloo (metadata) và NCCL, số thông điệp mỗi bước. Cần công cụ đo `SGLANG_PP_NET_TRACE`.
3. **Sàn mạng thực tế của cầu `nccl0`** (ping-pong gloo/NCCL nhỏ) để biết phần độ trễ nền bao nhiêu ms, vì kế hoạch ban đầu giả định `wg0`.
4. Chưa có nguyên nhân nào được xác nhận. Chưa nên viết mã SGLang.

## 4. Số liệu P11 cần ghi chú (không dùng làm mốc cho đến khi rà xong)
- `306,86 / 304,24 / 553,03 / 539,77` (một card, `C=16`): không có trong `P11.md` và `p11_2gpu_sglang_vllm.jsonl`; mâu thuẫn với P10 (SGLang 206,96 < vLLM 256,96 trên 3060, `C=16`).
- `P11.md` ghi TTFT P95 của llama.cpp gộp = `12.175 ms` (3 chỗ); P10 đo `3.693 ms`.
- `jsonl` có 6 dòng `FAILED` (ví dụ `module 'p10_client' has no attribute 'run_decode_benchmark'`) cho SGLang PP=2 (18,18) ở lần chạy đầu: cần xác nhận số (18,18) đến từ lần chạy lại thành công.

---

## 5. Kết quả đo S0/S1 (2026-11-01, chạy trên cả hai máy)  `[ĐO]`
Dữ liệu thô: `p12_s0_repro.jsonl`, `p12_s1_backend.jsonl`, `p12_s1_single.jsonl`, `p12_s1_trace.jsonl`, `logs/p12/`. Runner: `benchmarks/hardware/p12_*.py`.

**5.1. Tái lập khoảng cách PP=2 (13,23), decode `C=1`, 7 vòng ABAB (đổi thứ tự ở vòng chẵn từ vòng 4):** SGLang median-of-medians **55,76 tok/s** (dải 43,4–62,0), vLLM **66,70** (dải 62,8–78,1), SGLang chậm hơn **16,4%**, thấp hơn ở 6/7 vòng. `C=4` tổng: 130,7 so với 145,6 (−10%). Độ lệch chuẩn trong vòng 4–7 tok/s (lớn hơn ngưỡng ±3). Vòng 1 bị nhiễu RAM (3,5 GiB khả dụng). **Cổng S0: đạt.**

**5.2. Không thể so backend mặc định:** bỏ `--attention-backend triton --sampling-backend pytorch` thì SGLang dùng flashinfer, JIT lỗi `CUDA compiler and CUDA toolkit headers are incompatible` (máy không có CUDA toolkit khớp) ⇒ cấu hình triton là bắt buộc ở đây; các dòng `default` trong `p12_s1_backend.jsonl` (0,0 tok/s) là lỗi, không phải số đo.

**5.3. Đối chứng một card (3060, cùng cờ, eager, 4 vòng ABAB):** SGLang **61,62** tok/s, vLLM **58,45** (SGLang nhanh hơn 5,4%, độ lệch < 0,1). ⇒ Khoảng cách PP=2 **không** đến từ kernel attention/sampling mà đến từ đường phân tán (truyền, đồng bộ, lập lịch PP, hoặc độ trễ của stage trên WSL2).

**5.4. Trace đường PP của SGLang (`SGLANG_PP_NET_TRACE=1`, 2.056 bước decode, 55,3 tok/s khi bật trace so với ~55,8 khi tắt ⇒ chi phí đo không đáng kể):**
| Đại lượng (mỗi bước) | Rank 0 (3060, 13 tầng) | Rank 1 (5070 WSL2, 23 tầng) |
|---|---|---|
| `launch_batch` (chạy batch, eager) | trung bình 4,7 ms (p50 4,2; p90 5,2) | trung bình 8,6 ms (**p50 8,0; p90 14,1**) |
| chờ kết quả `recv_output` | 9,25 ms (đang chờ stage 1) | 8,3 ms (chờ relay từ rank 0) |
| chờ `recv_proxy` (hidden states) | ~0 | **1,05 ms** |
| `commit_comm` (chờ xong send) | **~2,3 ms/bước** (4,75 s / 2.056) | 0,08 ms |
| gửi (`send_tensor_dict`, `send_object`) | 0,15 + 0,05 ms | 0,19 + 0,06 ms |
- Ở một đoạn giữa phép đo, chu kỳ mỗi token khoảng 15,6–16 ms; trong đó `launch_batch` hai stage ≈ 10,3 ms, còn lại ≈ 5,6 ms là truyền/đồng bộ/xử lý kết quả.
- Các quan sát chưa kết luận: (a) `launch_batch` của rank 1 có **đuôi dài** (p90 14 ms so với ~5,7 ms ở đoạn nhanh) ⇒ có thể là **dao động tốc độ ở phía WSL2/WDDM**, không phải mạng; (b) rank 0 mất ~2,3 ms/bước ở `commit_comm`; (c) rank 1 chờ `recv_proxy` ~1,05 ms mỗi bước ngay cả khi dữ liệu có thể đã tới (khớp nối NCCL chỉ hoàn tất khi bên nhận đăng ký `recv`). Đồng hồ hai máy không đồng bộ nên việc xếp chuỗi đường găng giữa hai rank còn là `[SUY LUẬN]`.
- **Chưa có nút thắt mạng đơn lẻ nào được xác nhận.** Bước tiếp theo: (1) đo sàn độ trễ gloo/NCCL của cầu `nccl0`; (2) tìm xem đuôi dài của rank 1 do đâu (so với chạy một card trên 5070); (3) so với phân rã tương đương của vLLM để biết chênh ≈ 3 ms/token nằm ở đâu.

## 6. Sự cố vận hành trong lúc đo (đã khắc phục)
`p11_2gpu_runner.stop_all_engines()` chạy `pkill -9 -f "[b]in/llama-server"`, mẫu này khớp cả dòng lệnh của `worker-guard` (có `--llama-server <path>/bin/llama-server`) nên **đã giết worker-guard trên pc-001 ngay lần chạy đầu**. Phát hiện lúc khôi phục (API 8099 không phản hồi). Đã khởi động lại guard với cờ như tiến trình cũ (`--parallel 2 --ctx 4096`, trạng thái merged Coder được khôi phục từ `worker-state-pc-001.json`); đã kiểm chứng decode 126–132 tok/s qua gateway. Các script `p12_*` nay ghi đè `stop_all_engines` bằng bản không dùng mẫu `bin/llama-server`. **`p11_2gpu_runner.py` vẫn còn lỗi này.**

## 7. S2: network floor and single 5070 (WSL2)  `[ĐO]` (2026-11-01)
**7.1. Sàn mạng qua cầu `nccl0`** (`p12_s2_netfloor.py`, 500 mẫu/bài, torch gloo+NCCL, Linux ↔ WSL2): ping-pong (RTT) 8 B: gloo p50 0,21 ms, NCCL p50 0,21 ms; 8 KiB: gloo 0,28 ms, NCCL 0,31 ms; 64 KiB: gloo 0,78 ms, NCCL 0,85 ms. Gửi hidden states 1 token (8 KiB) + ack: 0,41 ms (p99 0,56). Đuôi p99 thỉnh thoảng 9–41 ms ở một số bài (gloo 8 B, NCCL 8 KiB/64 KiB). ⇒ mỗi token cần ~2 lượt truyền hidden/output ≈ **< 1 ms**, trong khi khoảng cách SGLang–vLLM ≈ 3 ms/token. **Độ trễ mạng tối thiểu không giải thích khoảng cách.**

**7.2. Một card 5070 trong WSL2** (`p12_s2_wsl_single.py`, ABAB, eager; vòng 1 SGLang và vLLM-r2 lỗi khởi động `exit 9`, chưa rõ nguyên nhân): SGLang 85,97 / 96,55 tok/s, vLLM 58,22 / 69,31 tok/s, **độ lệch chuẩn trong mỗi chuỗi rất lớn (SGLang 6,7–12,4; vLLM 13–22; min–max 29–93)** ⇒ tốc độ trên WSL2 dao động mạnh từ lần gọi này sang lần gọi khác. Phù hợp với giả thuyết đuôi dài của `launch_batch` rank 1 là do phía WSL2 chứ không do mạng; số liệu còn ít, chưa kết luận.

## 8. S2c/S3: power plan, critical-path accounting, `--pp-async-batch-depth`  `[ĐO]` (2026-11-01)
**8.1. Gói nguồn Windows không phải nguyên nhân dao động WSL2:** SGLang một card 5070, ABAB Balanced vs High performance (3 vòng mỗi loại): Balanced median 86,5 / 84,3 / 74,5, High 74,9 / 95,6 / 91,5 (dải chồng nhau, độ lệch chuẩn 9–17 ở cả hai). Gói nguồn đã trả về Balanced. Một lượt đầu chạy nhầm GUID nên tất cả vẫn là Balanced: `p12_s2_power_invalid_wrong_guid.jsonl` (không dùng; nhưng cho thấy dao động giữa các lần chạy cùng gói Balanced: median 79,7–91,6).

**8.2. Cộng thời gian theo chu kỳ (từ trace, 861 token liên tiếp, 18,1 ms/token = 55,2 tok/s):**
rank 0: launch 4,68 + chờ output 9,02 + commit_comm 2,09 + send 0,24 + process 0,16 + còn lại 1,85; rank 1: launch 8,26 + chờ output 8,52 + recv_proxy 1,05 + send 0,21 + còn lại 1,36 ms.
Chi phí launch theo tầng gần như bằng nhau (rank 0: 0,32 ms/tầng; rank 1: 0,35 ms/tầng) ⇒ WSL2 không chậm hơn theo trung bình. Với 1 request, chuỗi nối tiếp = tổng CPU launch hai stage (~13 ms) + ~5 ms overhead lập lịch/đồng bộ. Phần truyền thuần < 1 ms (mục 7). `[SUY LUẬN]` khoảng cách ~3 ms/token so với vLLM nằm ở overhead lập lịch/đồng bộ chứ không ở mạng.

**8.3. Cờ có sẵn `--pp-async-batch-depth` (mặc định 0), PP=2 (13,23), C=1, ABAB/xoay vòng, median-of-medians:**
depth 0: **52,04** tok/s (n=4: 57,7 / 52,5 / 50,8 / 51,5); depth 1: **58,25** (n=7); depth 2: 56,33 (n=3); depth 4: 59,61 (n=3). depth 1 nhanh hơn depth 0 ở 4/4 cặp (+12%); depth 2/4 không tốt hơn depth 1. vLLM (mục 5.1) vẫn 66,7 ⇒ depth 1 thu hẹp khoảng cách từ −16% xuống khoảng −13% so với số S0 (55,8) nhưng dùng cùng cấu hình thì depth 0 hiện 52,0 (nền máy hôm nay thấp hơn S0), nên so sánh phải làm lại cùng phiên với vLLM.
**Chưa kiểm:** C=4, TTFT/prefill, đầu ra giống hệt (greedy), PP với nhiều request, RAM/VRAM thêm, vì sao mặc định là 0 (cần đọc lịch sử PR).

## 9. S4: same-session comparison, SGLang depth 0 / depth 1 / vLLM  `[ĐO]` (2026-11-01)
`p12_s4_session.py`, `p12_s4_session.jsonl`; PP=2 (13,23), 7 vòng xoay thứ tự, 0 lỗi. Decode C=1 median-of-medians: **SGLang depth 0: 54,68**, **SGLang depth 1: 57,27 (+4,7%, nhanh hơn ở 4/7 vòng)**, **vLLM: 67,92 (nhanh hơn depth 1 ở 7/7 vòng)**. C=4 tổng: depth 0 **130,2**, depth 1 **119,8** (−8%), vLLM **138,1**; TTFT p50 937 / 922 / 790 ms.
Kết luận: mức +12% ở mục 8.3 chủ yếu là nhiễu (n nhỏ, không cùng phiên); lợi ích của depth 1 ở C=1 chỉ ~+5% và **làm xấu C=4**. Cờ này **không** thu hẹp đáng kể khoảng cách với vLLM (−19% ở C=1 sau khi đo cùng phiên; trước đó −16%) ⇒ không đáng để đổi mặc định hay làm PR. Nguồn gốc cờ: PR upstream #11852 "[PP] Refactor PP to async mode" (nhắm PP lớn + tải cao, không nêu lý do mặc định 0).

## 10. S5: prototype `SGLANG_PP_EARLY_PROXY` (hypothesis H8)  `[ĐO]` (2026-11-01)
**H8 `[ĐỌC MÃ]`:** vLLM's receiving stage posts `irecv_tensor_dict` *before* running its forward pass and only waits at stream level, so both stages launch their (eager, CPU-bound) kernels concurrently. SGLang's receiving stage blocks the CPU in the gloo `recv_object` (proxy metadata) until the previous stage has finished launching, so launch times add up (rank 0 4,7 ms + rank 1 8,3 ms).
**Prototype (worktree `sglang-v0519`, uncommitted, flag off by default):** after the first decode proxy message, decode batches (pp=2, tp=1) send hidden states over a dedicated NCCL group with no gloo metadata; the receiver derives shape/dtype from the batch and the first message and posts the receive before launching. Files: `scheduler_pp_mixin.py`, `environ.py`. Runner: `p12_s5_early.py`, data `p12_s5_early.jsonl` (+ `p12_s5_early_smoke.jsonl`).
**Result (same session, 7 rotating rounds, 0 failures, PP=2 (13,23)):** decode C=1 median-of-medians: flag off **54,49**, flag on **60,14 (+10,4%, faster in 5/7 rounds)**, vLLM **69,70** (faster than "on" in 6/7). C=4 aggregate: off 131,7 vs on 139,8 (+6,1%); TTFT p50 940 vs 935 ms. Greedy output identical to flag off in all 14 runs (4 prompts × 96 tokens each).
**Reading:** closes roughly 37% of the C=1 gap to vLLM (−21,8% → −13,7%); the remaining gap is still unexplained `[SUY LUẬN]`: output relay (rank 0 → rank 1) and scheduler work still in the critical path. Not yet checked: CUDA graphs on, prefill/extend batches (fall back to legacy path), pp_size > 2 / tp > 1 (flag disables itself), long-running stability, behaviour under load/aborts.

## 11. S7: CUDA graphs enabled  `[ĐO]` (2026-11-01)
`p12_s7_graph.py`, `p12_s7_graph.jsonl` (rounds 2–8; `p12_s7_graph_smoke.jsonl` is a 1-round smoke test with different numbers: off 65,4 / on 69,9 / vLLM 86,1, not pooled). SGLang decode graph on (max bs 16), vLLM without `--enforce-eager`, PP=2 (13,23), rotating order.
- Decode C=1 median-of-medians: **SGLang flag off 81,58** (7 rounds), **SGLang flag on 80,16** (6 rounds), **vLLM 77,69** (7 rounds, per-round range 69,3–86,2). With CUDA graphs the SGLang–vLLM gap seen in eager mode **disappears within noise**, and the early-proxy flag gives **no gain**.
- **Regression found:** with graphs + flag on, C=4 completes only 4–5 tokens in total (aggregate 0,7 tok/s, `errors=0` in all 6 rounds) vs 153,5 tok/s with the flag off; one round crashed (`Process exited early with code -11`, segfault). Greedy C=1 output stayed identical (13/13). So the prototype is **unsafe with CUDA graphs and batch size > 1** (cause not yet found: the failing path is graph-replayed decode with bs > 1 over the dedicated NCCL group; eager mode passed C=4 with equal output).
- Consequence for the plan: the eager-mode gap (−16% to −22%) is not representative of production-style settings. The gain of D1–D3 for 1-user PP=2 on this hardware is likely small once graphs are on.
