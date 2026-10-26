# Đánh giá tính khả thi: card GTX, mạng LAN và suy luận phân tán — nên làm / không nên làm

Ngày lập: 2026-10-26. Phạm vi: đánh giá đoạn "Những khó khăn và điểm nghẽn lớn khi dùng card GTX" mà bạn dán vào, đối chiếu với kế hoạch (`KE_HOACH_THUC_HIEN_AI_COMPUTE_FABRIC_HF.md`) và với những gì đã đo trên máy này.

Quy ước độ tin cậy dùng xuyên suốt:
- **[ĐO]** đã đo trên máy này (1× RTX 3060 12 GB, Linux), có file báo cáo trong `ai-compute-fabric/benchmarks/reports/`.
- **[TÀI LIỆU]** lấy từ nguồn bên ngoài (liệt kê ở cuối). Phần lớn là blog, kho benchmark cá nhân, bài đánh giá phần cứng, trừ bài báo prima.cpp và Sarathi-Serve là bài báo khoa học. Không nguồn nào đo đúng cấu hình của chúng ta.
- **[SUY LUẬN]** lập luận của tôi từ kiến trúc, chưa có phép đo nào kiểm chứng.
- **[CHƯA XÁC MINH]** tôi tìm mà không thấy nguồn xác nhận.

---

## 1. Kết luận ngắn

1. Đoạn bạn dán **đúng về hướng** nhưng **phóng đại ở một chỗ** và **không áp dụng trực tiếp** cho dự án:
   - Đúng: mạng LAN thường làm chậm suy luận phân tán, GTX thiếu Tensor Core và VRAM thấp.
   - Phóng đại: "chậm đến mức không thể sử dụng được". Số liệu tài liệu cho thấy chạy được nhưng giảm khoảng 15% đến 50% tốc độ sinh token, tùy cấu hình. Tensor parallelism qua Ethernet mới là trường hợp thực sự không dùng được.
   - Không áp dụng trực tiếp: **đội máy của dự án là RTX 3060 12 GB (Ampere, có Tensor Core), không phải GTX.** Kế hoạch không hề nhắc GTX. Rủi ro thật là nếu một ngày fleet có lẫn máy GTX thì hiện chưa có cổng nào chặn.
2. Phát hiện quan trọng nhất cho kế hoạch: ngưỡng **"LAN ≥ 2 Gbps"** ở Phụ lục B dòng 3 **không có bằng chứng đỡ**. Thứ quyết định là độ trễ mỗi bước nhảy và tốc độ sinh token đo được, không phải băng thông danh nghĩa. Cần thay bằng cổng đo thực (mục 5).
3. Con đường tốt nhất theo bằng chứng hiện có: **nhân bản độc lập (data parallel)** là mặc định; **offload MoE trên một máy** (đã đo 49,9 tok/s) tốt hơn phân tán cho Qwen MoE; **phân tán layer-split chỉ là phương án cuối** cho model dense không vừa một máy, và chỉ chấp nhận sau khi B03–B05 đo thật.

---

## 2. Từng nhận định trong đoạn bạn dán

### 2.1 "Interconnect bottleneck: NVLink/InfiniBand hàng trăm GB/s, Ethernet 1–10 Gbps"

| Ý | Đánh giá | Mức |
|---|---|---|
| NVLink/IB có băng thông cao hơn Ethernet rất nhiều | Đúng về hướng. Lưu ý chi tiết: "hàng trăm GB/s" là NVLink; InfiniBand thường thấp hơn nhiều so với con số đó. | kiến thức chung, [CHƯA XÁC MINH] bằng nguồn riêng |
| Latency giữa các tầng lớn hơn thời gian tính toán | Đúng ở giai đoạn **decode**: mỗi token phải đi qua mọi bước nhảy mạng nên độ trễ cộng dồn. Với layer-split, thứ đi qua mạng chỉ là vector activation của token, rất nhỏ, nên **độ trễ mới là vấn đề, không phải băng thông**. | [TÀI LIỆU] + [SUY LUẬN] |
| "Chậm đến mức không thể sử dụng được" | **Quá lời.** Xem số liệu dưới. | [TÀI LIỆU] |
| RTX 3060 có NVLink không? | Không. Dòng RTX 3060 không có đầu NVLink, nên ngay cả hai card trong một máy cũng đi qua PCIe. | kiến thức chung, [CHƯA XÁC MINH] bằng nguồn riêng |

Số liệu bên ngoài về llama.cpp RPC / layer split (đều không phải GTX và không phải 3060, chỉ để lấy cỡ độ lớn):
- Kho benchmark hai máy, liên kết 10 GbE trực tiếp: model 7B sinh token giảm từ 91,8 xuống 52,7 tok/s; model 72B giảm từ 11,1 xuống 5,9 tok/s (khoảng −47%). Prompt processing lại nhanh hơn vì GPU mạnh hơn xử lý lô. [kjaiswal]
- Bài đánh giá cụm Ryzen AI Max+: prompt processing chậm 13–15% trên 1 GbE, chỉ 2–5% trên 10 GbE; sinh token giảm khoảng 15% với mọi loại kết nối; máy đơn còn nhanh hơn cụm cho model đó. [LTT Labs]
- prima.cpp (bài báo): 4 thiết bị gia đình, model 70B đạt 674 ms/token, tức khoảng 1,5 tok/s; model 32B có speculative decoding đạt 26 tok/s. [arXiv]

Diễn giải: phân tán cho **model quá lớn** là khả thi nhưng đắt về tốc độ (mất 15–50% decode, và 70B chỉ khoảng 1,5 tok/s, tức không dùng được cho chat tương tác). Phân tán cho model **vừa một máy** gần như luôn tệ hơn chạy một máy. Đây là lý do nên tránh.

Tensor parallelism qua Ethernet: các nguồn đều cho rằng không phù hợp vì mỗi layer cần một all-reduce; ngay cả bài báo Sarathi-Serve với 100 GbE giữa hai node A100 cũng thấy TP liên-node có độ trễ decode trung vị gấp khoảng 2 lần cấu hình lai TP-4/PP-2. Không nguồn nào đo TP trên 10 GbE với card phổ thông. [TÀI LIỆU]

### 2.2 "VRAM thấp (6–11 GB) và thiếu Tensor Core"

| Ý | Đánh giá | Mức |
|---|---|---|
| GTX có 6–11 GB VRAM | Đúng cho GTX 10/16. Nhưng dự án dùng RTX 3060 **12 GB**. | [ĐO] máy này |
| Không có Tensor Core | Đúng với GTX 10 (Pascal) và GTX 16 (Turing). Với suy luận lượng tử hóa, **decode bị giới hạn bởi băng thông bộ nhớ**, nên thiếu Tensor Core ảnh hưởng chủ yếu prompt processing và huấn luyện, ít ảnh hưởng decode. | [TÀI LIỆU] + [SUY LUẬN] |
| FP16 chậm trên Pascal | Có: một nguồn nêu FP16 trên GTX 1080 Ti chỉ bằng khoảng 1/64 FP32. llama.cpp có đường flash attention FP32 để dùng được trên card không có FP16 nhanh; một người dùng 2×1080 Ti báo 20,2 so với 13,4 tok/s khi bật. Đây là một giai thoại, không phải benchmark kiểm soát. | [TÀI LIỆU] yếu |
| CUDA 13 bỏ hỗ trợ Pascal | Chỉ một nguồn nêu. Cần kiểm tra ghi chú phát hành CUDA trước khi dựa vào. Nếu đúng thì máy GTX 10 sẽ bị kẹt ở CUDA 12, ảnh hưởng đóng gói. | [CHƯA XÁC MINH] |
| Kernel int8 (DP4A) cho matmul lượng tử hóa trên Pascal | Tôi không tìm được nguồn. | [CHƯA XÁC MINH] |

Tham chiếu tốc độ một card GTX 1080 Ti: Llama 2 7B Q4_0 khoảng 62 tok/s sinh, 1.084 tok/s prompt (benchmark cộng đồng). Máy này với RTX 3060 đo Qwen3-4B và các cấu hình khác trong `benchmarks/reports/`; so sánh trực tiếp hai con số này không có ý nghĩa vì khác model.

---

## 3. Những gì dự án đã có bằng chứng thật (khác với tài liệu bên ngoài)

Tất cả đều trên 1× RTX 3060, Linux. **Chưa có phép đo đa máy nào.**

- Flash attention bật mặc định: tổng thông lượng tăng khoảng 21% ở 8 luồng song song [ĐO] (`p2_engine_cfg_8b_fa*.json`). Trên card không có Tensor Core thì kết quả có thể khác; chưa đo.
- MoE offload trên một máy (B06): 49,9 tok/s với Qwen3-30B-A3B [ĐO] (`p4_B06_moe_hybrid_rtx3060.json`), nhanh hơn 92% TPOT so với 32B dense trên cùng máy (khác model, chưa so chất lượng). Việc này **chưa** chứng minh phân tán kém hơn, vì chưa đo phân tán.
- Speculative decoding (B07) **không đạt cổng**: tỉ lệ chấp nhận 0,49, nhanh 1,185×, đầu ra khớp greedy chỉ 25–33% [ĐO]. Giữ tắt mặc định.
- Điều phối nhiều tác vụ độc lập: trần khoảng 115 tác vụ/s trên máy này; hiệu suất 0,94 ở 40 worker giả lập với tác vụ 2 giây [ĐO, giả lập, không phải 40 máy thật].
- Affinity (giữ phiên về cùng worker) chưa chứng minh lợi ích qua gateway [ĐO, 3 lần không khác biệt].

---

## 4. NÊN làm / KHÔNG NÊN làm

### 4.1 Nên làm

| # | Việc | Vì sao | Mức |
|---|---|---|---|
| D1 | **Mặc định nhân bản độc lập** (mỗi máy chạy trọn một model, gateway chia request). | Không có truyền thông giữa máy khi decode; mất một máy chỉ mất một phần công suất; phù hợp với việc người dùng văn phòng có thể thu hồi GPU bất kỳ lúc nào. Tính chất khả năng mở rộng tác vụ độc lập đã đo ở mô phỏng. | [ĐO] mô phỏng + [SUY LUẬN] |
| D2 | **Qwen MoE: ưu tiên offload trên một máy** trước khi nghĩ đến phân tán. | B06 đo được 49,9 tok/s cho Qwen3-30B-A3B trên một RTX 3060 (`p4_B06_moe_hybrid_rtx3060.json`). **Chưa đo so sánh trực tiếp với phân tán**; con số 1,5 tok/s của prima.cpp là model 70B trên thiết bị khác nên không so được. Kết luận này là suy luận (model vừa một máy thì phân tán chỉ thêm độ trễ mạng), cần B03–B05 trên máy thật để xác nhận. | [ĐO] cho 49,9 tok/s; so sánh với phân tán là [SUY LUẬN] |
| D3 | Chỉ phân tán layer-split cho model dense **không vừa một máy**, và chỉ sau khi B03/B04/B05 đo được số thật. | Phân tán luôn mất tốc độ; chỉ có lý khi không còn lựa chọn. | [TÀI LIỆU] |
| D4 | Thay ngưỡng "LAN ≥ 2 Gbps" bằng **cổng đo thực**: RTT giữa các máy, tok/s decode của nhóm so với 1 máy + offload RAM, tối thiểu 10 GbE có dây, cùng một switch. | Băng thông danh nghĩa không phải thứ quyết định; 1 GbE làm prompt processing chậm 13–15% trong bài đo duy nhất tìm được. | [TÀI LIỆU] |
| D5 | Thêm **cổng compute capability / VRAM** vào `hardware_profiler`: xếp máy không có Tensor Core hoặc VRAM < 12 GB vào hạng "chỉ tác vụ nhẹ / embedding / model nhỏ", không cho vào nhóm phân tán. | Hiện chưa có cổng nào; lẫn một máy chậm vào nhóm pipeline kéo cả nhóm xuống vì pipeline chạy theo mắt xích chậm nhất. | [SUY LUẬN] |
| D6 | Đo trên card không có Tensor Core (nếu có một máy GTX) **trước** khi hứa gì về hiệu năng; đặc biệt flash attention FP32 vs FP16. | Kết quả bật flash attention của chúng ta chỉ là của Ampere. | [ĐO] cho Ampere, [CHƯA XÁC MINH] cho GTX |
| D7 | Giữ mTLS và không mở RPC trần. | Đã có trong kế hoạch; RPC của llama.cpp không xác thực. | kế hoạch |
| D8 | Với phiên bản chạy phân tán, thiết kế **xử lý khi một thành viên mất GPU**: nhóm phải dừng và dựng lại, không treo. | Máy văn phòng bị thu hồi GPU là tình huống bình thường của dự án; pipeline chết nếu mất một mắt xích. | [SUY LUẬN], chưa kiểm thử |

### 4.2 Không nên làm

| # | Việc | Vì sao | Mức |
|---|---|---|---|
| K1 | **Không dùng tensor parallelism giữa các PC** qua LAN. | Mỗi layer cần all-reduce; ngay cả 100 GbE giữa node A100 cũng chịu phạt độ trễ lớn. | [TÀI LIỆU] |
| K2 | **Không phân tán pipeline qua WAN.** | Kế hoạch đã cấm; độ trễ WAN còn tệ hơn LAN rất nhiều. | kế hoạch + [SUY LUẬN] |
| K3 | **Không phân tán model vừa một máy.** | Chỉ thêm độ trễ mạng mà không có lợi; bài đo LTT cho thấy máy đơn nhanh hơn cụm. | [TÀI LIỆU] |
| K4 | **Không hứa tốc độ tương tác cho 70B phân tán.** | Kết quả tốt nhất của prima.cpp trong bài báo là khoảng 1,5 tok/s cho 70B. Chọn model 32B nếu cần đáp ứng chat. | [TÀI LIỆU] |
| K5 | **Không lấy số của prima.cpp / llama.cpp RPC bên ngoài làm số của dự án.** | Thiết bị khác (có cả máy Apple, Ryzen AI), không phải 3060; chỉ là cỡ độ lớn. Chỉ số B03–B05 tự đo mới có giá trị cho cổng G-DIST. | [TÀI LIỆU] |
| K6 | **Không tin 1 GbE là đủ** cho nhóm pipeline, và không chạy huấn luyện DDP qua 1 GbE. | Với DDP, mỗi bước phải đồng bộ gradient (kích thước LoRA nhỏ hơn full model, nhưng vẫn mỗi bước); kế hoạch đã nghiêng về DiLoCo cho trường hợp băng thông thấp, đó là hướng đúng. | [SUY LUẬN], chưa đo |
| K7 | **Không bật speculative decoding mặc định.** | B07 không đạt cổng. | [ĐO] |
| K8 | **Không đưa máy GTX vào fleet suy luận chung** khi chưa có cổng D5 và chưa đo D6. | Thiếu Tensor Core làm prompt processing chậm hơn; VRAM thấp hạn chế model và ngữ cảnh. | [TÀI LIỆU] + [SUY LUẬN] |
| K9 | **Không ghi "xong" cho P3.2/B03–B05** khi chưa có ít nhất 3 máy thật. | Hiện không có phép đo đa máy nào; kế hoạch đã để các mục này mở. | trạng thái dự án |
| K10 | **Không cho rằng prima.cpp chạy trên Windows.** | Một bài tổng hợp liệt kê macOS, Linux, Android, HarmonyOS và không có Windows; chưa tìm được tuyên bố chính thức nào. Kế hoạch D04 đã yêu cầu kiểm ở P0.3. | [CHƯA XÁC MINH] |

---

## 4.3 Suy luận và huấn luyện: "nhiều máy" có nghĩa khác nhau

Câu hỏi hay gặp: "suy luận không nên chạy nhiều máy, còn huấn luyện chịu được mạng chậm?" Cả hai vế đều cần chỉnh. Mọi mục dưới đây là [SUY LUẬN] trừ khi ghi khác; P6 (huấn luyện) chưa chạy lần nào trên dự án.

**Suy luận**

| Cách dùng nhiều máy | Mạng chậm có hại không | Khuyến nghị |
|---|---|---|
| Nhân bản độc lập (mỗi máy chạy trọn một model, gateway chia request) | Gần như không: không có dữ liệu đi giữa các máy khi decode | Mặc định |
| Chia một model theo layer qua nhiều máy | Có: mỗi token qua mạng ở từng bước nhảy; [TÀI LIỆU] decode chậm khoảng 15–50% | Chỉ khi model dense không vừa một máy, sau khi đo B03–B05 |
| Chia theo tensor (tensor parallel) qua LAN | Có, nặng: mỗi layer cần all-reduce [TÀI LIỆU] | Không dùng |

**Huấn luyện**

| Cách chạy | Chịu được mạng chậm không | Ghi chú |
|---|---|---|
| Công việc độc lập (mỗi máy một LoRA riêng, chạy thử nhiều cấu hình) | Có | Không cần đồng bộ |
| Đồng bộ thưa: DiLoCo / local SGD (mỗi máy tự chạy H bước rồi mới đồng bộ) | Có, đây là mục đích thiết kế | Kế hoạch đã chọn cho trường hợp băng thông thấp (Phụ lục B dòng 6). Các bài báo DiLoCo cho rằng chất lượng gần ngang mà giảm truyền thông rất nhiều: [CHƯA XÁC MINH] trong phiên này, cần đọc lại bài gốc trước khi dựa vào |
| DDP đồng bộ gradient mỗi bước, với LoRA | Chỉ khi mạng đủ nhanh: gradient chỉ gồm tham số adapter nên nhỏ; 10 GbE có thể chạy được, 1 GbE sẽ kéo chậm | Chưa đo |
| DDP, huấn luyện toàn bộ trọng số (full fine-tune) qua LAN | Không | Gradient bằng cỡ model |

**Hai điều kiện khác ngoài mạng**
- **VRAM:** huấn luyện cần thêm bộ nhớ cho activation và optimizer state, nên 12 GB thực tế chỉ vừa LoRA/QLoRA trên model nhỏ. Mạng nhanh không bù được chỗ này.
- **Máy văn phòng bị thu hồi GPU bất kỳ lúc nào:** huấn luyện đồng bộ treo cả nhóm khi một máy rút. Cần checkpoint thường xuyên; đồng bộ thưa chịu việc này tốt hơn vì các máy ít phụ thuộc nhau.

**Việc cần làm trước khi dựa vào phần này:** đọc lại bài DiLoCo và tài liệu `prime-diloco`; đo thời gian một bước LoRA trên 1 máy để ước lượng kích thước gradient thực và thời gian truyền trên 1 GbE / 10 GbE; kiểm thử "mất một máy giữa lúc huấn luyện".

---

## 5. Đề xuất sửa kế hoạch (chưa áp dụng, chờ bạn đồng ý)

1. Phụ lục B, dòng 3: thay "LAN ≥ 2 Gbps" bằng điều kiện đo được, ví dụ: "nhóm phân tán chỉ được chọn nếu tok/s decode đo được cao hơn phương án offload RAM một máy, trên mạng có dây ≥ 10 GbE cùng switch". Số cụ thể (ví dụ 2 Gbps) hiện chỉ là phỏng đoán, không nguồn nào đỡ.
2. Thêm cổng phần cứng vào `core/hardware_profiler` (compute capability, có Tensor Core hay không, VRAM) và hạng máy "không đủ điều kiện cho nhóm phân tán" (D5).
3. Thêm mục P0: đo một máy GTX (nếu có) cho flash attention FP32/FP16 và ghi kết quả (D6).
4. Thêm kiểm tra phiên bản CUDA / driver theo thế hệ GPU vào bước đóng gói (xem mục 2.2, chỉ một nguồn nêu Pascal bị bỏ ở CUDA 13).
5. Thêm kịch bản kiểm thử "mất một thành viên nhóm pipeline giữa chừng" (D8).

---

## 6. Giới hạn của đánh giá này

- Nguồn bên ngoài chủ yếu là blog và kho benchmark cá nhân; chỉ có bài prima.cpp và Sarathi-Serve là bài báo. Không nguồn nào đo GTX + Ethernet + llama.cpp.
- Tôi không tìm được nguồn cho: kernel DP4A trên Pascal, TP trên 10 GbE với card phổ thông, prima.cpp trên Windows, và việc CUDA 13 bỏ Pascal chỉ có một nguồn.
- Các phần ghi [SUY LUẬN] là lập luận hợp lý nhưng chưa được kiểm chứng; không nên coi là sự thật cho tới khi đo.
- Mọi điều "đã đo" ở trên chỉ đúng cho 1 máy RTX 3060 chạy Linux.

## Nguồn

- [llama cpp distributed benchmarks (kjaiswal)](https://github.com/kjaiswal/llama-cpp-distributed-benchmarks)
- [LTT Labs: cụm AMD Ryzen AI Halo](https://lttlabs.com/articles/2026/07/24/amd-ryzen-ai-halo-cluster)
- [Hướng dẫn multi-node local LLM inference 2026 (Fungies)](https://fungies.io/multi-node-local-llm-inference-guide-2026/), lưu ý bảng 70B trong bài có vẻ là vLLM chứ không phải RPC, nên tôi không dùng số đó
- [prima.cpp, bài báo arXiv 2504.08791 v2](https://arxiv.org/html/2504.08791v2)
- [Sarathi-Serve, arXiv 2403.02310](https://arxiv.org/pdf/2403.02310)
- [Tensor parallelism fundamentals (apxml)](https://apxml.com/courses/designing-and-sizing-production-llm-infrastructure/chapter-4-distributed-topologies-for-llms/tensor-parallelism-fundamentals) và [Interconnect requirements (apxml)](https://apxml.com/courses/designing-and-sizing-production-llm-infrastructure/chapter-4-distributed-topologies-for-llms/interconnect-requirements)
- [Tensor vs pipeline parallelism (Spheron)](https://www.spheron.network/blog/tensor-parallelism-vs-pipeline-parallelism/)
- [GTX 1080 Ti for Local LLM (Ariya)](https://ariya.io/2026/02/gtx-1080-ti-for-local-llm/)
- [GGUF flash attention không cần Tensor Core (HF discussion)](https://huggingface.co/AetherArchitectural/Community-Discussions/discussions/11)
