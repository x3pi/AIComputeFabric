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

## 7. Đối chiếu với tài liệu kiến trúc "Distributed AI Compute Fabric v1.0" (bản chia sẻ ChatGPT)

Ngày đối chiếu: 2026-10-26. Tôi đọc được nội dung bằng cách trích dữ liệu nhúng trong trang chia sẻ (trang không cần đăng nhập nhưng cần chạy JavaScript mới hiện chữ). Bản này khớp một phần với file `AI_Compute_Fabric_Kien_truc_ky_thuat_v1.docx` mà kế hoạch trích dẫn là [KT] (cùng có "Agent-level MoE", "80%", "2–5 Gbps", "vLLM PP"), nhưng không giống hệt; tôi chưa so từng dòng. Tôi không mở được file ZIP mà tài liệu nhắc tới (`sandbox:/mnt/data/...`), nên câu "Scheduler tham chiếu đã chạy 5 bài kiểm thử" là [CHƯA XÁC MINH].

### 7.1 Chỗ tài liệu tự ghi nhận đúng (khớp với số đo và đánh giá của chúng ta)
- "Không coi việc ghép 200 GPU là thành công nếu một yêu cầu chạy chậm hơn trên một máy" và nguyên tắc cuối "phân tán khi giúp tăng tốc/thông lượng/chạy được model lớn hơn, cục bộ khi mạng làm chậm": khớp với D1–D3, K3.
- "Số liệu TPOT giả định để minh họa, không phải benchmark RTX 3060 đã đo", "Level 2 chỉ nghiên cứu sau cùng", "mục tiêu 30% / 80% là ngưỡng R&D cần kiểm chứng", "không mở GGML RPC lên WAN", "không lấy trung bình A và B của LoRA rồi coi là trung bình trọng số": đều đúng hướng. Kế hoạch đã xử lý điểm LoRA bằng LoRA-FA (frozen A).

### 7.2 Mâu thuẫn hoặc chỗ chưa hợp lý
| # | Vấn đề | Chi tiết | Mức |
|---|---|---|---|
| T1 | **Ngưỡng "80% tổng thông lượng của các Worker độc lập" phụ thuộc độ dài tác vụ** | Đo mô phỏng (không phải máy thật): hiệu suất điều phối 0,855 / 0,741 / 0,568 với 8 / 20 / 40 worker khi tác vụ dài 200 ms, và 0,94 ở 40 worker khi tác vụ dài 2 s (`p5_orchestrator_scale_simulated*.json`). Trần điều phối khoảng 115 tác vụ/s trên máy này. Suy ra: 200 worker cần tác vụ dài khoảng 1,7 s trở lên để không vượt trần (200 ÷ 115). Ngưỡng 80% chỉ đạt với tác vụ đủ dài hoặc gom lô; tài liệu không nêu điều kiện này. | [ĐO] mô phỏng + [SUY LUẬN] |
| T2 | **Mô phỏng chọn tuyến dễ bị đọc như bằng chứng** | Ví dụ 32B: một PC 210 ms/token, Prima.cpp 4×3060 115 ms, MoLink 160 ms. Tài liệu có ghi là giả định. Nhưng 210 ms gần với số đo thật B02 (252 ms, 3,97 tok/s), còn 115 ms và 160 ms không có số đo nào đỡ; thứ tự "Prima nhanh hơn MoLink" cũng chưa có căn cứ. Đo thật trên 3–4 PC (B04, B05) mới trả lời được. Nguồn ngoài cho thấy phân tán có thể mất 15–50% decode (mục 2.1), nên giả định "nhanh gần gấp đôi" là lạc quan. | [TÀI LIỆU] + [CHƯA ĐO] |
| T3 | **"Hạ tầng mạng 2–5 Gbps" và nhóm 3–4 PC** | Tài liệu nói "cần đo thực tế", đúng. Nhưng toàn bộ phương án phân tán 32B dựa trên mạng này mà chưa có số RTT; nguồn ngoài chỉ có số cho 1 GbE và 10 GbE (mục 2.1), không có cho 2–5 Gbps. Băng thông chung của switch khi 200 PC cùng dùng cũng chưa được xét trong công thức. | [CHƯA XÁC MINH] |
| T4 | **Cấu hình mục tiêu "Core i5 + 32 GB RAM" khác máy chúng ta đã đo** | Số B06 (49,9 tok/s) đo trên Core Ultra 7 265K, 4 luồng, không phải i5; offload expert bị giới hạn bởi băng thông RAM và CPU (RAM một kênh làm chậm hơn). Không nên dùng 49,9 tok/s làm kỳ vọng cho cả đội máy i5. Cần đo trên một máy i5 thật. Đây cũng là sửa cho chính ghi chú của tôi ở D2 và mục 3. | [ĐO] trên máy khác cấu hình |
| T5 | **Hệ điều hành của fleet bị bỏ qua** | Cây monorepo có `deployments/windows`, nhưng vLLM, SGLang và MoLink (xây trên vLLM) theo hiểu biết của tôi chỉ chạy native trên Linux; Prima.cpp: không tìm thấy xác nhận Windows. Nếu PC văn phòng chạy Windows, hai nhánh phân tán có thể không dùng được nếu không qua WSL2. Kế hoạch D04 đã nêu rủi ro; tài liệu kiến trúc thì không. | [CHƯA XÁC MINH] |
| T6 | **Công thức chi phí thiếu rủi ro bị thu hồi GPU** | $T_{total}$ có queue, prefill, decode, network, sync, loading, nhưng không có xác suất một thành viên nhóm bị người dùng lấy lại GPU. Nhóm phân tán hỏng khi mất bất kỳ máy nào; chi phí dựng lại nhóm nên là một số hạng. Ngoài ra các số hạng network/sync/prefetch có thể chồng lấn (piped-ring), nên tổng chỉ là cận trên. | [SUY LUẬN] |
| T7 | **"Level 3 gần tinh thần DiLoCo"** | DiLoCo là phương pháp huấn luyện (đồng bộ thưa); Level 3 là định tuyến suy luận giữa các model/agent chuyên biệt. Ví von này dễ gây hiểu nhầm. Gọi là "MoE" cũng dễ nhầm vì đây là hệ thống mới, tài liệu đã tự nói không tương đương toán học. Câu "chậm như Tensor Parallelism trước đây" trỏ tới một ngữ cảnh không có trong tài liệu. | thuật ngữ |
| T8 | **Khẳng định "tăng tốc mà không đổi chất lượng" vs speculative decoding** | Tài liệu dùng nguyên tắc cùng model/quantization thì chất lượng không đổi, và liệt kê FlowSpec/PipeSD. Số đo B07 của chúng ta: đầu ra greedy chỉ khớp 25–33% giữa có và không suy đoán, acceptance 0,49, chỉ 1,185×. Theo lý thuyết suy đoán chính xác là không mất mát, nhưng trong llama.cpp thực tế đầu ra bị đổi; vì vậy "không đổi chất lượng" không được mặc định, phải kiểm bằng KL/perplexity. | [ĐO] |
| T9 | **Huấn luyện Qwen3-ASR/TTS trên 12 GB** | Tài liệu nói script fine-tuning có sẵn nhưng không nói cần bao nhiêu VRAM. Nếu script là full fine-tune thì 12 GB khó đủ; cần LoRA/QLoRA và kiểm tra thực tế. Tôi chưa đọc script của Qwen3-ASR/TTS. | [CHƯA XÁC MINH] |
| T10 | **Ví dụ 10.000 cuộc gọi dùng nhiều model trên cùng một máy** | Các bước ASR, Qwen nhỏ, embedding đều ghi RTX 3060. Nếu một máy phải nạp lần lượt nhiều model thì $T_{loading}$ lớn và tranh VRAM; cần chuyên môn hóa worker theo loại task hoặc giữ model thường trú. Tài liệu chưa nói. | [SUY LUẬN] |
| T11 | **Giấy phép** | Tài liệu nói Prima.cpp, MoLink, Prime, LangGraph là MIT/Apache; tôi chưa kiểm từng repo. Kế hoạch có mục kiểm giấy phép ở P0. | [CHƯA XÁC MINH] |
| T12 | **MoE mục tiêu "Qwen MoE 35B"** | B06 đo Qwen3-30B-A3B, không phải bản 35B (kế hoạch ghi Qwen3.5-35B-A3B cần xử lý DeltaNet). Số đo không chuyển sang 35B. | [ĐO] khác model |

### 7.3 Đề xuất sửa tài liệu kiến trúc (chưa áp dụng)
1. Ghi điều kiện cho ngưỡng 80%: độ dài tác vụ tối thiểu hoặc gom lô, và trần điều phối đã đo.
2. Gắn nhãn rõ trên mô phỏng chọn tuyến rằng 115/160 ms chưa có đo; thay 210 ms bằng số B02 (252 ms) khi giới thiệu baseline.
3. Thêm số hạng "xác suất thu hồi GPU / chi phí dựng lại nhóm" vào công thức chi phí.
4. Thêm ràng buộc hệ điều hành cho từng engine.
5. Bỏ hoặc sửa câu so sánh Level 3 với DiLoCo.
6. Thêm bước đo B06 trên một máy i5 thật trước khi dùng số MoE làm kỳ vọng cho đội máy.

---

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
