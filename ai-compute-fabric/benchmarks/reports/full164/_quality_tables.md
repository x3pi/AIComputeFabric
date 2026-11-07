## Q_RANK
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

## Q_SIG
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

## Q_TIERS
| Nhóm | Model (điểm) | Ghi chú |
|:-:|:--|:--|
| 1 | Qwen3.8-27B (158), Qwen3-Coder-30B-A3B (154), GLM-4-32B-0414 (150), Qwen3-30B-A3B (147) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |
| 2 | GLM-4-9B-0414 (136), Qwen3-8B (135), Kimi-k3-9B (133), GLM-4.7-Flash (130), deepseek-coder-6.7B (129), Qwen3-4B (125), DeepSeek-R1-32B (IQ4_XS) (123), DeepSeek-R1-14B (118) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |
| 3 | DeepSeek-V2-Lite (Q4) (86) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |
| 4 | DeepSeek-V2-Lite (Q3) (66) | Không model nào trong nhóm kém hơn đầu nhóm một cách có ý nghĩa (Holm) |

## Q_DIFF
- Giải được bởi **cả 14 model**: **43** bài (26%)
- Giải được bởi **không model nào**: **1** bài
- Chỉ 1–3 model giải được: **5** bài

| Số model giải được | 14 | 13 | 12 | 11 | 10 | 9 | 8 | 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0 |
|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Số bài | 43 | 21 | 24 | 16 | 19 | 12 | 3 | 5 | 11 | 3 | 1 | 1 | 2 | 2 | 1 |

Bài không model nào giải được: `HumanEval/145`

Bài chỉ 1 model giải được: `HumanEval/129` (Qwen3-Coder-30B-A3B), `HumanEval/132` (Qwen3-Coder-30B-A3B)

## Q_BAND
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

## Q_UNION
Độ phủ nếu *có bộ kiểm tra chọn đúng đáp án* (cận trên, như Best-of-N bằng unit test). Chọn tham lam model bổ sung nhiều bài mới nhất:

| Số model | Thêm model | Tổng bài phủ | Tỷ lệ |
|--:|:--|--:|--:|
| 1 | Qwen3.8-27B | 158 | 96.3% |
| 2 | GLM-4.7-Flash | 161 | 98.2% |
| 3 | Qwen3-Coder-30B-A3B | 163 | 99.4% |
| 4 | DeepSeek-R1-14B | 163 | 99.4% |
| 5 | DeepSeek-R1-32B (IQ4_XS) | 163 | 99.4% |

## Q_COND
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

## Q_FLIP
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

## Q_PARETO
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

## Q_FAIL
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