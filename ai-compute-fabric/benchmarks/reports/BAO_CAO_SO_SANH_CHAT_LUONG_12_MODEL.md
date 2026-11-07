# BÁO CÁO SO SÁNH CHẤT LƯỢNG SINH CODE CỦA 13 MODEL / 14 CẤU HÌNH (HUMANEVAL 164 BÀI)

- **Ngày đo:** 2026-11-06 → 2026-11-07, trên cụm `pc-001` (RTX 3060) + `pc-win-01` (RTX 5070). Báo cáo đi kèm: `BAO_CAO_FULL164_PHAN_TAN_VS_1_CARD.md` (hiệu năng, điện, phân tán).
- **Dữ liệu:** vector đúng/sai từng bài của 23 cấu hình trong `benchmarks/reports/full164/*.json`; bảng sinh tự động bởi `benchmarks/hardware/build_quality_tables.py`.
- **Phạm vi:** chỉ **chất lượng**. Bài toán là *hoàn thành hàm Python* từ mô tả, chấm bằng bộ unit test ẩn của HumanEval trong sandbox.
- Nhãn: `[ĐO]` đo trực tiếp lần này; `[SUY RA]` suy luận từ số đo; `[NGOÀI]` thông tin ngoài phép đo (ví dụ số tham số do nhà phát hành công bố, chưa kiểm chứng ở đây); `[ĐO CŨ]` lấy từ báo cáo trước.

**Cách đọc nhanh:** *Pass@1* = số bài đúng ngay lần sinh đầu tiên (`temperature=0`) trên 164. *CI 95%* là khoảng tin cậy: với 164 bài nó rộng khoảng ±6–7 điểm phần trăm, nên hai model cách nhau vài bài **chưa chắc khác nhau thật**. Để so hai model cho đúng, báo cáo dùng **kiểm định cặp (McNemar chính xác)**: chỉ đếm những bài mà *một model đúng, model kia sai*, rồi hỏi liệu chênh lệch có vượt may rủi không. Vì so 91 cặp cùng lúc, tôi còn hiệu chỉnh **Holm** để tránh kết luận nhầm.

---

## 1. Kết luận trong một trang

**Xếp hạng (cấu hình `merged`, 164 bài)** `[ĐO]`:

| Nhóm | Model (số bài đúng) | Ý nghĩa |
|:-:|:--|:--|
| **1 — Mạnh nhất** | Qwen3.8-27B (**158**), Qwen3-Coder-30B-A3B (**154**), GLM-4-32B (**150**), Qwen3-30B-A3B (**147**) | **Bốn model này không tách được nhau** sau hiệu chỉnh Holm (khác biệt giữa các cặp nhỏ hơn mức may rủi). |
| **2 — Khá** | GLM-4-9B (136), Qwen3-8B (135), Kimi-k3-9B (133), GLM-4.7-Flash (130), deepseek-coder-6.7B (129), Qwen3-4B (125), DeepSeek-R1-32B IQ4_XS (123), DeepSeek-R1-14B (118) | Tám model này cũng **không tách được nhau**. Nhóm 1 hơn nhóm 2 rõ ở đầu (Coder-30B và Qwen3.8-27B hơn cả tám model); phần giáp ranh (GLM-4-32B, Qwen3-30B-A3B so với GLM-4-9B) thì chưa đủ bằng chứng. |
| **3 — Yếu** | DeepSeek-V2-Lite Q4 (**86**), DeepSeek-V2-Lite Q3 (**66**) | Cả hai thua rõ mọi model còn lại. Bản Q4 hơn bản Q3 có ý nghĩa thống kê (27 bài chỉ Q4 đúng, 7 bài chỉ Q3 đúng, p = 0.0008 trước hiệu chỉnh; vẫn có ý nghĩa sau Holm). |

**Năm điều đáng nhớ:**

1. **Kích thước vẫn là yếu tố lớn nhất** `[ĐO]`: model ≥ 27B (hoặc MoE 30B) loại không suy luận đạt 147–158 bài, model 8–9B đạt 129–136, model 4–7B đạt 125–129. Nhưng MoE rất hiệu quả: Qwen3-30B-A3B hơn Qwen3-8B 12 bài trong khi `[NGOÀI]` chỉ kích hoạt ~3.3B tham số mỗi token.
2. **Bản chuyên code hơn bản tổng quát cùng cỡ, nhưng chưa đủ chắc** `[ĐO]`: Coder-30B (154) so với Qwen3-30B-A3B (147) cùng kiến trúc và cùng cỡ: 12 bài chỉ Coder đúng, 5 bài chỉ bản kia đúng, **p = 0.14**, chưa có ý nghĩa thống kê ở 164 bài.
3. **Model suy luận dài thua khi ngân sách token hạn chế, nhưng rất chính xác khi suy nghĩ xong** `[ĐO]`: DeepSeek-R1-14B đúng 98.3% những bài nó tự kết thúc, R1-32B (IQ4_XS) đúng 99.2% (122/123), Kimi-9B đúng 93.7%; nhưng 47, 41 và 22 bài (29%, 25% và 13%) hết ngân sách 3072 token giữa chừng. Ở ngân sách tương tác, chúng không hơn model thường cùng cỡ mà tốn 8–14 lần token. Tăng cỡ trong họ R1 gần như không giúp: R1-32B hơn R1-14B 5 bài (17 bài chỉ 32B đúng, 12 bài chỉ 14B đúng, p = 0.46, không có ý nghĩa) và tốn thêm 66% điện mỗi bài đúng (25,791 J so với 15,566 J).
4. **Model đắt nhất không phải lúc nào cũng đáng tiền** `[ĐO]`: Qwen3.8-27B hơn Coder-30B 4 bài (chưa có ý nghĩa, p = 0.34) nhưng tốn **6.4 lần điện** và **5.7 lần thời gian** mỗi bài đúng.
5. **Các model sai ở những chỗ khác nhau** `[ĐO]`: nếu có bộ chọn đáp án đúng hoàn hảo, ghép 3 model (Qwen3.8-27B + GLM-4.7-Flash + Coder-30B) phủ được **163/164 bài**, trong khi model tốt nhất đơn lẻ là 158. Đây chỉ là cận trên (xem mục 9).

**Chọn model theo mục tiêu chất lượng:**

| Mục tiêu | Chọn | Lý do (số đo) |
|:--|:--|:--|
| Chất lượng cao trên mỗi đơn vị điện và thời gian | **Qwen3-Coder-30B-A3B** `merged` | 154/164, **382 J và 2.2 giây mỗi bài đúng**, nằm trên đường Pareto. |
| Rẻ nhất trong nhóm mạnh | **Qwen3-30B-A3B** `merged` | 147/164 ở **336 J/bài đúng**, thấp nhất trong mọi cấu hình. |
| Chất lượng tối đa, bất kể chi phí | **Qwen3.8-27B** `merged` | 158/164, sai chỉ 6 bài. |
| Chỉ có 1 card 12 GB | **GLM-4-9B** hoặc **Qwen3-8B** | 136 và 135 bài, ngang nhau về chất lượng. GLM-4-9B ngắn gọn hơn (105 so với 148 token/bài). |
| Tránh | **DeepSeek-V2-Lite** (Q4: 86, Q3: 66) và họ **DeepSeek-R1** (14B: 118, 32B: 123) cho code tương tác; R1 rất tốn token và điện (13,882–25,791 J/bài đúng) | |

---

## 2. Bảng xếp hạng đầy đủ

*Cấu hình dùng* theo quy tắc cố định: `merged` nếu có, nếu không thì cấu hình duy nhất. *Pass@1 mỗi chế độ* cho thấy chế độ chạy ít ảnh hưởng đến chất lượng (xem mục 8).

| Hạng | Model | Loại | Tham số | Cỡ file (GiB) | Pass@1 | CI 95% | Cấu hình dùng | Pass@1 mỗi chế độ |
|--:|:--|:--|:--|--:|--:|:--:|:--|:--|
| 1 | **Qwen3.8-27B** | dense | 27B | 15.3 | **158** (96.3%) | 92–98% | `merged` | `merged` 158 |
| 2 | **Qwen3-Coder-30B-A3B** | MoE | 30.5B / 3.3B active | 17.3 | **154** (93.9%) | 89–97% | `merged` | `merged` 154 · `smart_1gpu` 154 |
| 3 | **GLM-4-32B-0414** | dense | 32B | 18.3 | **150** (91.5%) | 86–95% | `merged` | `merged` 150 |
| 4 | **Qwen3-30B-A3B** | MoE | 30.5B / 3.3B active | 17.3 | **147** (89.6%) | 84–93% | `merged` | `merged` 147 · `smart_1gpu` 146 |
| 5 | **GLM-4-9B-0414** | dense | 9B | 5.7 | **136** (82.9%) | 76–88% | `merged` | `merged` 136 · `split` 132 |
| 6 | **Qwen3-8B** | dense | 8B | 4.7 | **135** (82.3%) | 76–87% | `merged` | `merged` 135 · `split` 134 |
| 7 | **Kimi-k3-9B** | hybrid, reasoning | 9.2B | 5.4 | **133** (81.1%) | 74–86% | `merged` | `merged` 133 · `split` 129 |
| 8 | **GLM-4.7-Flash** | MoE | ~30B / ~3B active | 17.1 | **130** (79.3%) | 72–85% | `merged` | `merged` 130 · `smart_1gpu` 135 |
| 9 | **deepseek-coder-6.7B** | dense | 6.7B | 3.8 | **129** (78.7%) | 72–84% | `merged` | `merged` 129 · `split` 128 |
| 10 | **Qwen3-4B** | dense | 4B | 2.3 | **125** (76.2%) | 69–82% | `merged` | `merged` 125 · `split` 128 |
| 11 | **DeepSeek-R1-32B (IQ4_XS)** | dense, reasoning | 32.8B | 16.5 | **123** (75.0%) | 68–81% | `merged` | `merged` 123 |
| 12 | **DeepSeek-R1-14B** | dense, reasoning | 14.8B | 8.4 | **118** (72.0%) | 65–78% | `merged` | `merged` 118 · `split` 121 |
| 13 | **DeepSeek-V2-Lite (Q4)** | MoE | 15.7B / 2.4B active | 9.7 | **86** (52.4%) | 45–60% | `smart_1gpu` | `smart_1gpu` 86 |
| 14 | **DeepSeek-V2-Lite (Q3)** | MoE | 15.7B / 2.4B active | 7.6 | **66** (40.2%) | 33–48% | `smart_1gpu` | `smart_1gpu` 66 |

---

## 3. Ai thực sự hơn ai? (kiểm định cặp)

Tổng 91 cặp; khác biệt có ý nghĩa **trước hiệu chỉnh** (p<0.05): 62; **sau hiệu chỉnh Holm**: 50.

| Model | Hơn có ý nghĩa (Holm) so với | Kém có ý nghĩa (Holm) so với |
|:--|:--|:--|
| **Qwen3.8-27B** (158) | GLM-4-9B-0414, Qwen3-8B, Kimi-k3-9B, GLM-4.7-Flash, deepseek-coder-6.7B, Qwen3-4B, DeepSeek-R1-32B (IQ4_XS), DeepSeek-R1-14B, DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | — |
| **Qwen3-Coder-30B-A3B** (154) | GLM-4-9B-0414, Qwen3-8B, Kimi-k3-9B, GLM-4.7-Flash, deepseek-coder-6.7B, Qwen3-4B, DeepSeek-R1-32B (IQ4_XS), DeepSeek-R1-14B, DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | — |
| **GLM-4-32B-0414** (150) | GLM-4.7-Flash, deepseek-coder-6.7B, Qwen3-4B, DeepSeek-R1-32B (IQ4_XS), DeepSeek-R1-14B, DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | — |
| **Qwen3-30B-A3B** (147) | GLM-4.7-Flash, Qwen3-4B, DeepSeek-R1-32B (IQ4_XS), DeepSeek-R1-14B, DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | — |
| **GLM-4-9B-0414** (136) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B |
| **Qwen3-8B** (135) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B |
| **Kimi-k3-9B** (133) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B |
| **GLM-4.7-Flash** (130) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414, Qwen3-30B-A3B |
| **deepseek-coder-6.7B** (129) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414 |
| **Qwen3-4B** (125) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414, Qwen3-30B-A3B |
| **DeepSeek-R1-32B (IQ4_XS)** (123) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414, Qwen3-30B-A3B |
| **DeepSeek-R1-14B** (118) | DeepSeek-V2-Lite (Q4), DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414, Qwen3-30B-A3B |
| **DeepSeek-V2-Lite (Q4)** (86) | DeepSeek-V2-Lite (Q3) | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414, Qwen3-30B-A3B, GLM-4-9B-0414, Qwen3-8B, Kimi-k3-9B, GLM-4.7-Flash, deepseek-coder-6.7B, Qwen3-4B, DeepSeek-R1-32B (IQ4_XS), DeepSeek-R1-14B |
| **DeepSeek-V2-Lite (Q3)** (66) | — | Qwen3.8-27B, Qwen3-Coder-30B-A3B, GLM-4-32B-0414, Qwen3-30B-A3B, GLM-4-9B-0414, Qwen3-8B, Kimi-k3-9B, GLM-4.7-Flash, deepseek-coder-6.7B, Qwen3-4B, DeepSeek-R1-32B (IQ4_XS), DeepSeek-R1-14B, DeepSeek-V2-Lite (Q4) |

**Nhóm gom theo kiểm định** (gom tham lam: một model vào nhóm hiện tại nếu không kém đầu nhóm một cách có ý nghĩa sau Holm; ranh giới giữa các nhóm không sắc nét):

| Nhóm | Model (điểm) | Ghi chú |
|:-:|:--|:--|
| 1 | Qwen3.8-27B (158), Qwen3-Coder-30B-A3B (154), GLM-4-32B-0414 (150), Qwen3-30B-A3B (147) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |
| 2 | GLM-4-9B-0414 (136), Qwen3-8B (135), Kimi-k3-9B (133), GLM-4.7-Flash (130), deepseek-coder-6.7B (129), Qwen3-4B (125), DeepSeek-R1-32B (IQ4_XS) (123), DeepSeek-R1-14B (118) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |
| 3 | DeepSeek-V2-Lite (Q4) (86) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |
| 4 | DeepSeek-V2-Lite (Q3) (66) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |

**Một số cặp đáng chú ý** (`chỉ A` = số bài chỉ A đúng, `chỉ B` = số bài chỉ B đúng; p chưa hiệu chỉnh) `[ĐO]`:

| Cặp (A so với B) | Chỉ A | Chỉ B | p | Sau Holm (91 cặp) | Diễn giải |
|:--|--:|--:|--:|:-:|:--|
| Qwen3.8-27B vs Coder-30B | 7 | 3 | 0.34 | không | Đầu bảng, **không phân biệt được** |
| Coder-30B vs GLM-4-32B | 9 | 5 | 0.42 | không | Không phân biệt được |
| Coder-30B vs Qwen3-30B-A3B | 12 | 5 | 0.14 | không | Xu hướng nghiêng về bản Coder, chưa đủ chắc |
| GLM-4-32B vs Qwen3-30B-A3B | 11 | 8 | 0.65 | không | Hai model ngang nhau |
| Qwen3.8-27B vs GLM-4-32B | 9 | 1 | 0.02 | không | Có vẻ khác nếu chỉ xét một cặp, mất ý nghĩa khi hiệu chỉnh |
| Qwen3-30B-A3B vs Qwen3-8B | 16 | 4 | 0.01 | không | Có vẻ khác, mất ý nghĩa khi hiệu chỉnh |
| Coder-30B vs GLM-4-9B | 20 | 2 | 0.0001 | **có** | Khác thật |

---

## 4. Yếu tố nào quyết định chất lượng?

1. **Quy mô** `[ĐO]` + `[NGOÀI]`. Số bài đúng tăng theo cỡ model nhưng bão hòa ở đầu: từ 4B (125) lên 8–9B (129–136) lên ≥27B (147–158), nếu bỏ qua các model suy luận (họ R1 bị giới hạn bởi ngân sách token chứ không bởi cỡ). Hai model dense lớn (27B, 32B) và hai MoE 30B nằm cùng nhóm đầu, tức **MoE 3.3B-active đạt gần bằng dense 27–32B** ở bài toán này, với tốc độ và điện tốt hơn nhiều (xem báo cáo hiệu năng).
2. **Chuyên hóa code** `[ĐO]`. Cặp kiểm soát tốt nhất là Coder-30B và Qwen3-30B-A3B (cùng kiến trúc, cùng cỡ): +7 bài cho bản Coder (p = 0.14). Đúng hướng kỳ vọng nhưng **cần nhiều bài hơn** để khẳng định.
3. **Suy luận chuỗi dài** `[ĐO]`. Kimi-9B (133) ≈ Qwen3-8B chế độ không suy luận (135): cùng chất lượng nhưng Kimi dùng 1,223 token/bài so với 148. R1-14B (118) và R1-32B (123) thấp hơn cả Qwen3-4B (125) dưới ngân sách 3072 token. Nguyên nhân hầu như hoàn toàn là hết ngân sách: R1-14B sai 46 bài thì 44 bài là bị cắt, R1-32B sai 41 bài thì 40 bài bị cắt, Kimi sai 31 bài thì 22 bài bị cắt.
4. **Lượng tử hóa** `[ĐO]` nhưng **không tách riêng được**. DeepSeek-V2-Lite được đo ở cả hai mức: Q3_K_M đạt 66 bài (40.2%, 25 lỗi cú pháp/chạy), Q4_K_M đạt 86 bài (52.4%, 12 lỗi cú pháp/chạy), cùng chế độ 1 card + RAM. Vậy lượng tử hóa Q3 làm mất khoảng 20 bài, nhưng **ngay cả ở Q4 model này vẫn đứng gần chót** (86, thua mọi model khác có ý nghĩa), nên `[SUY RA]` phần lớn độ yếu là do bản thân model (`[NGOÀI]` khoảng 2.4B tham số kích hoạt). Riêng R1-32B phải dùng bản IQ4_XS (nhẹ hơn Q4_K_M 2.2 GB) vì Q4_K_M không vừa VRAM; ảnh hưởng của việc này tới điểm không tách riêng được.
5. **Chế độ chạy (1 card hay gộp 2 card) không phải yếu tố chất lượng** (mục 8).

---

## 5. Độ khó của bài toán

- Giải được bởi **cả 14 model**: **43** bài (26%)
- Giải được bởi **không model nào**: **1** bài
- Chỉ 1–3 model giải được: **5** bài

| Số model giải được | 14 | 13 | 12 | 11 | 10 | 9 | 8 | 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0 |
|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Số bài | 43 | 21 | 24 | 16 | 19 | 12 | 3 | 5 | 11 | 3 | 1 | 1 | 2 | 2 | 1 |

Bài không model nào giải được: `HumanEval/145`

Bài chỉ 1 model giải được: `HumanEval/129` (Qwen3-Coder-30B-A3B), `HumanEval/132` (Qwen3-Coder-30B-A3B)

**Chất lượng theo độ dài lời giải chuẩn** (đại diện cho độ phức tạp; mỗi nhóm chỉ 47–60 bài nên còn nhiễu):

Độ khó đo bằng độ dài lời giải chuẩn (số dòng khác rỗng): ngắn ≤ 3 dòng (57 bài), vừa ≤ 8 dòng (60 bài), dài > 8 dòng (47 bài).

| Model | Ngắn | Vừa | Dài | Tụt từ ngắn → dài |
|:--|--:|--:|--:|--:|
| Qwen3.8-27B | 98% | 97% | 94% | −5 điểm |
| Qwen3-Coder-30B-A3B | 93% | 97% | 91% | −1 điểm |
| GLM-4-32B-0414 | 95% | 90% | 89% | −5 điểm |
| Qwen3-30B-A3B | 91% | 92% | 85% | −6 điểm |
| GLM-4-9B-0414 | 88% | 85% | 74% | −13 điểm |
| Qwen3-8B | 89% | 83% | 72% | −17 điểm |
| Kimi-k3-9B | 88% | 80% | 74% | −13 điểm |
| GLM-4.7-Flash | 84% | 78% | 74% | −10 điểm |
| deepseek-coder-6.7B | 79% | 83% | 72% | −7 điểm |
| Qwen3-4B | 86% | 77% | 64% | −22 điểm |
| DeepSeek-R1-32B (IQ4_XS) | 81% | 80% | 62% | −19 điểm |
| DeepSeek-R1-14B | 74% | 77% | 64% | −10 điểm |
| DeepSeek-V2-Lite (Q4) | 65% | 55% | 34% | −31 điểm |
| DeepSeek-V2-Lite (Q3) | 58% | 43% | 15% | −43 điểm |

**Nhận xét** `[ĐO]`: model mạnh gần như không suy giảm theo độ dài lời giải (Coder-30B giảm 1 điểm, Qwen3.8-27B 5 điểm), còn model nhỏ giảm mạnh (Qwen3-8B 17, Qwen3-4B 22, R1-32B 19, V2-Lite Q4 31 và Q3 43 điểm). Nghĩa là **khoảng cách giữa model lớn và nhỏ nằm chủ yếu ở các bài dài/phức tạp**, không phải bài ngắn. `HumanEval/145` là bài duy nhất không model nào giải được.

---

## 6. Model sai kiểu gì?

| Model | Pass | Sai logic | Lỗi cú pháp/chạy | Bị cắt (chạm trần mà sai) | Tổng sai |
|:--|--:|--:|--:|--:|--:|
| Qwen3.8-27B | 158 | 2 | 0 | 4 | 6 |
| Qwen3-Coder-30B-A3B | 154 | 8 | 0 | 2 | 10 |
| GLM-4-32B-0414 | 150 | 13 | 1 | 0 | 14 |
| Qwen3-30B-A3B | 147 | 15 | 2 | 0 | 17 |
| GLM-4-9B-0414 | 136 | 25 | 3 | 0 | 28 |
| Qwen3-8B | 135 | 25 | 4 | 0 | 29 |
| Kimi-k3-9B | 133 | 7 | 2 | 22 | 31 |
| GLM-4.7-Flash | 130 | 32 | 2 | 0 | 34 |
| deepseek-coder-6.7B | 129 | 31 | 4 | 0 | 35 |
| Qwen3-4B | 125 | 30 | 8 | 1 | 39 |
| DeepSeek-R1-32B (IQ4_XS) | 123 | 1 | 0 | 40 | 41 |
| DeepSeek-R1-14B | 118 | 2 | 0 | 44 | 46 |
| DeepSeek-V2-Lite (Q4) | 86 | 65 | 12 | 1 | 78 |
| DeepSeek-V2-Lite (Q3) | 66 | 70 | 25 | 3 | 98 |

**Đúng khi đã kết thúc và mức "ngốn" token** `[ĐO]` (lưu ý: các bài model tự dừng thường là bài dễ hơn, nên con số này thiên lệch có lợi cho model hay bị cắt):

*Chạm trần* = số bài dùng hết ngân sách token (1024; 2048 cho GLM-4.7-Flash; 3072 cho model suy luận). *Đúng khi đã kết thúc* = tỷ lệ đúng trong các bài model tự dừng.

| Model | Token sinh/bài | Chạm trần | Đúng khi đã kết thúc | Pass@1 | Đúng trên 1,000 token sinh |
|:--|--:|--:|--:|--:|--:|
| Qwen3.8-27B | 243.8 | 4 | 98.8% (158/160) | 96.3% | 3.95 bài |
| Qwen3-Coder-30B-A3B | 222.8 | 2 | 95.1% (154/162) | 93.9% | 4.21 bài |
| GLM-4-32B-0414 | 198.4 | 0 | 91.5% (150/164) | 91.5% | 4.61 bài |
| Qwen3-30B-A3B | 186.0 | 0 | 89.6% (147/164) | 89.6% | 4.82 bài |
| GLM-4-9B-0414 | 105.0 | 0 | 82.9% (136/164) | 82.9% | 7.90 bài |
| Qwen3-8B | 147.5 | 0 | 82.3% (135/164) | 82.3% | 5.58 bài |
| Kimi-k3-9B | 1222.6 | 22 | 93.7% (133/142) | 81.1% | 0.66 bài |
| GLM-4.7-Flash | 202.7 | 0 | 79.3% (130/164) | 79.3% | 3.91 bài |
| deepseek-coder-6.7B | 196.3 | 0 | 78.7% (129/164) | 78.7% | 4.01 bài |
| Qwen3-4B | 170.7 | 1 | 76.7% (125/163) | 76.2% | 4.46 bài |
| DeepSeek-R1-32B (IQ4_XS) | 1958.6 | 41 | 99.2% (122/123) | 75.0% | 0.38 bài |
| DeepSeek-R1-14B | 2095.8 | 47 | 98.3% (115/117) | 72.0% | 0.34 bài |
| DeepSeek-V2-Lite (Q4) | 106.0 | 1 | 52.8% (86/163) | 52.4% | 4.95 bài |
| DeepSeek-V2-Lite (Q3) | 186.4 | 3 | 41.0% (66/161) | 40.2% | 2.16 bài |

**Nhận xét:**

- Model mạnh sai ít và sai kiểu "chạm trần token" (Qwen3.8-27B: 4 trong 6 bài sai) hoặc sai logic hiếm. Model trung bình sai chủ yếu do **sai logic** (25–32 bài). Model yếu nhất sai nhiều do **lỗi cú pháp/chạy**.
- **Cận trên cho model suy luận** `[SUY RA]`: nếu *mọi* bài chạm trần đều được giải, R1-14B đạt tối đa 162/164, R1-32B tối đa 163/164 và Kimi-9B tối đa 155/164. Đây là giới hạn trên thuần lý thuyết, không phải ước lượng điểm khi tăng ngân sách.
- **GLM-4-9B** hiệu quả token nhất (7.9 bài đúng trên 1,000 token sinh), vì chỉ viết ~105 token/bài.

---

## 7. Chất lượng trên chi phí

Một cấu hình *tối ưu Pareto* nếu không có cấu hình nào vừa đúng nhiều hơn (hoặc bằng) vừa rẻ hơn. ★ = tối ưu theo trục đó.

| Model | Chế độ | Pass@1 | Điện/bài đúng (J) | Thời gian/bài đúng (s) | Pareto theo điện | Pareto theo thời gian |
|:--|:--|--:|--:|--:|:-:|:-:|
| Qwen3.8-27B | `merged` | 158 | 2,455 | 12.51 | ★ | ★ |
| Qwen3-Coder-30B-A3B | `merged` | 154 | 382 | 2.18 | ★ | ★ |
| Qwen3-Coder-30B-A3B | `smart_1gpu` | 154 | 440 | 6.81 |  |  |
| GLM-4-32B-0414 | `merged` | 150 | 2,426 | 10.89 |  |  |
| Qwen3-30B-A3B | `merged` | 147 | 336 | 1.93 | ★ | ★ |
| Qwen3-30B-A3B | `smart_1gpu` | 146 | 397 | 5.99 |  |  |
| GLM-4-9B-0414 | `merged` | 136 | 461 | 2.31 |  |  |
| GLM-4.7-Flash | `smart_1gpu` | 135 | 536 | 7.01 |  |  |
| Qwen3-8B | `merged` | 135 | 539 | 2.78 |  |  |
| Qwen3-8B | `split` | 134 | 500 | 2.99 |  |  |
| Kimi-k3-9B | `merged` | 133 | 4,927 | 25.36 |  |  |
| GLM-4-9B-0414 | `split` | 132 | 444 | 2.64 |  |  |
| GLM-4.7-Flash | `merged` | 130 | 506 | 2.99 |  |  |
| Kimi-k3-9B | `split` | 129 | 4,980 | 29.47 |  |  |
| deepseek-coder-6.7B | `merged` | 129 | 705 | 4.06 |  |  |
| Qwen3-4B | `split` | 128 | 382 | 2.31 |  |  |
| deepseek-coder-6.7B | `split` | 128 | 641 | 3.93 |  |  |
| Qwen3-4B | `merged` | 125 | 408 | 2.3 |  |  |
| DeepSeek-R1-32B (IQ4_XS) | `merged` | 123 | 25,791 | 119.24 |  |  |
| DeepSeek-R1-14B | `split` | 121 | 13,882 | 82.09 |  |  |
| DeepSeek-R1-14B | `merged` | 118 | 15,566 | 74.33 |  |  |
| DeepSeek-V2-Lite (Q4) | `smart_1gpu` | 86 | 503 | 8.8 |  |  |
| DeepSeek-V2-Lite (Q3) | `smart_1gpu` | 66 | 1,134 | 20.91 |  |  |

**Nhận xét** `[ĐO]`: chỉ **3 cấu hình tối ưu Pareto** (cả điện lẫn thời gian): Qwen3.8-27B `merged`, Qwen3-Coder-30B `merged`, Qwen3-30B-A3B `merged`. Coder-30B `merged` đúng nhiều hơn Qwen3-4B `split` 26 bài với **cùng mức điện mỗi bài đúng** (382 J). Nghĩa là trên cụm này, **chạy MoE 30B ở chế độ gộp 2 card không đắt hơn chạy model nhỏ trên 1 card**, trong khi cho chất lượng cao hơn hẳn. Qwen3.8-27B mua thêm 4 bài (chưa có ý nghĩa thống kê) bằng 6.4 lần điện. Lưu ý điện chỉ tính GPU (xem báo cáo hiệu năng).

---

## 8. Nhiễu nền: chế độ chạy có làm đổi chất lượng không?

Cùng trọng số, `temperature=0`, khác chế độ chạy. Mỗi cặp cho biết số bài **đổi kết quả** và hướng đổi; đây là **mức nhiễu nền** để đánh giá chênh lệch điểm giữa các model.

| Model | Cặp chế độ | Bài đúng ở cả hai | Chỉ đúng ở chế độ 1 card | Chỉ đúng ở `merged` | Đổi kết quả | Chênh điểm |
|:--|:--|--:|--:|--:|--:|--:|
| Qwen3-Coder-30B-A3B | `smart_1gpu` ↔ `merged` | 152 | 2 | 2 | 4 | +0 |
| Qwen3-30B-A3B | `smart_1gpu` ↔ `merged` | 144 | 2 | 3 | 5 | +1 |
| GLM-4-9B-0414 | `split` ↔ `merged` | 132 | 0 | 4 | 4 | +4 |
| Qwen3-8B | `split` ↔ `merged` | 132 | 2 | 3 | 5 | +1 |
| Kimi-k3-9B | `split` ↔ `merged` | 119 | 10 | 14 | 24 | +4 |
| GLM-4.7-Flash | `smart_1gpu` ↔ `merged` | 129 | 6 | 1 | 7 | -5 |
| deepseek-coder-6.7B | `split` ↔ `merged` | 126 | 2 | 3 | 5 | +1 |
| Qwen3-4B | `split` ↔ `merged` | 122 | 6 | 3 | 9 | -3 |
| DeepSeek-R1-14B | `split` ↔ `merged` | 106 | 15 | 12 | 27 | -3 |

Model không suy luận: đổi kết quả 4–9 bài/164 (trung vị 5). Model suy luận: 24–27 bài/164.

**Nhận xét** `[ĐO]`:

- Cùng trọng số, `temperature=0`, chỉ khác chế độ chạy mà vẫn có **4–9 bài đổi kết quả** (trung vị 5) ở model thường, và **24–27 bài** ở model suy luận (chuỗi suy luận dài khuếch đại sai khác số học nhỏ). `[SUY RA]` Nguyên nhân khả dĩ là phép tính trên hai GPU khác thế hệ cho sai số làm tròn khác nhau, chưa kiểm chứng.
- Hệ quả thực tế: **chênh lệch ≤ ~5 bài giữa hai model nằm trong nhiễu nền**, ngay cả trước khi tính sai số lấy mẫu. Ví dụ GLM-4.7-Flash được 130 ở `merged` nhưng 135 ở `smart_1gpu`; xếp hạng của nó (hạng 8 hay hạng 6) phụ thuộc vào chế độ.
- **Chênh lệch điểm do chế độ không có hướng nhất quán** (+4, +1, −5, −3, ...), nên phân tán không làm model tốt hơn hay tệ hơn.

---

## 9. Các model bổ sung cho nhau

Độ phủ nếu *có bộ kiểm tra chọn đúng đáp án* (cận trên, như Best-of-N bằng unit test). Chọn tham lam model bổ sung nhiều bài mới nhất:

| Số model | Thêm model | Tổng bài phủ | Tỷ lệ |
|--:|:--|--:|--:|
| 1 | Qwen3.8-27B | 158 | 96.3% |
| 2 | GLM-4.7-Flash | 161 | 98.2% |
| 3 | Qwen3-Coder-30B-A3B | 163 | 99.4% |
| 4 | DeepSeek-R1-14B | 163 | 99.4% |
| 5 | DeepSeek-R1-32B (IQ4_XS) | 163 | 99.4% |

**Nhận xét** `[ĐO]` và `[SUY RA]`:

- Ba model (Qwen3.8-27B, GLM-4.7-Flash, Coder-30B) cùng phủ 163/164 bài, trong khi model tốt nhất đơn lẻ chỉ 158. GLM-4.7-Flash yếu hơn về điểm tổng nhưng giải được 3 bài mà Qwen3.8-27B bỏ sót, cho thấy **lỗi của các họ model khác nhau ít trùng nhau**.
- **Đây là cận trên, không phải kết quả đạt được**: nó giả định bộ chọn biết chính xác đáp án nào đúng (ở đây là bộ test ẩn của HumanEval). Trong thực tế bộ chọn chỉ thấy ví dụ trong mô tả hàm hoặc test tự sinh, nên mức tăng thực sẽ thấp hơn.
- `[ĐO CŨ]` Best-of-8 trên *cùng một model* chỉ nâng Coder-30B từ 90.85% lên 91.46% (+0.6 điểm). Giả thuyết đáng thử: **ghép các họ model khác nhau** (có sandbox làm bộ chọn) có thể mang lại nhiều hơn lấy mẫu lặp lại một model. Chưa kiểm chứng.

---

## 10. Hạn chế và bước tiếp theo

1. **Chỉ một bộ đề.** HumanEval có 164 bài hoàn thành hàm Python, công khai từ 2021, nên **có thể đã nằm trong dữ liệu huấn luyện** của một số model (rò rỉ dữ liệu), và nhóm đầu đã gần bão hòa (94–96%). Điểm tuyệt đối vì vậy có thể cao hơn khả năng thật; **thứ hạng tương đối đáng tin hơn điểm tuyệt đối**.
2. **Cỡ mẫu nhỏ.** 164 bài chỉ phân biệt được chênh lệch ≳ 10–15 bài giữa hai model. Nhiều cặp kề nhau không thể xếp hạng chắc chắn.
3. **Mỗi cấu hình một lần chạy, một mẫu mỗi bài** (greedy, không có pass@k). Mức nhiễu giữa các lần chạy chỉ ước lượng gián tiếp qua mục 8.
4. **Không công bằng tuyệt đối về điều kiện:** Qwen3 và GLM-4.7-Flash chạy với thinking tắt (có thể đánh giá thấp GLM-4.7-Flash); ngân sách token khác nhau (1024 / 2048 / 3072) và slot ngữ cảnh 4096 chặn việc tăng ngân sách cho model suy luận; lượng tử hóa khác nhau (V2-Lite đo cả Q3 và Q4, R1-32B là IQ4_XS vì Q4_K_M không vừa VRAM, Qwen3.8-27B là bản UD dynamic).
5. **Cách trích code đổi giữa chừng** (v1 cho 8 cấu hình đầu, v2 sau đó); kiểm tra lại trên Coder-30B `merged` cho kết quả giống hệt 164/164 bài, nhưng chỉ kiểm chứng một model.
6. **Số tham số `[NGOÀI]`**: lấy từ nhà phát hành hoặc báo cáo cũ, riêng "~30B / ~3B" của GLM-4.7-Flash chưa kiểm chứng ở đây.
7. **Chưa đo:** sửa lỗi/chỉnh sửa code có sẵn, đa tệp, ngữ cảnh dài, gọi công cụ, yêu cầu bằng tiếng Việt, tính an toàn, và chất lượng khi bật thinking cho Qwen3/GLM-4.7.

**Bước tiếp theo đề xuất** (để kết luận chắc hơn): chạy thêm một bộ đề độc lập (ví dụ MBPP sanitized có sẵn trong `benchmarks/data/`) và/hoặc bộ test chặt hơn như HumanEval+, đo thêm ít nhất 2–3 lần chạy cho các cặp sát nhau, và thử model suy luận với ngân sách token lớn hơn bằng cách tăng ngữ cảnh slot.
