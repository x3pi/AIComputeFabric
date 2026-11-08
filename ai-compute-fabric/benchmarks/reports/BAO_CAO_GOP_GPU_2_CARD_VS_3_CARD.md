# Gộp GPU qua mạng: 2 card so với 3 card (llama.cpp GGML RPC, chế độ `merged`)

Ngày đo 08/11/2026. Chỉ đo hiệu suất, không đo chất lượng code. Mọi số liệu là số đo thực; phần suy luận được gắn [ƯỚC TÍNH].

## 1. Kết luận

1. **Thêm card thứ ba qua Wi-Fi làm chậm đi ở mọi chỉ số.** Qwen3-Coder-30B: decode giảm 61% (125,8 → 48,9 tok/s), prefill giảm 62%, tải model lâu gấp 2,6 lần, điện năng mỗi token gần gấp đôi. Qwen3.8-27B: decode giảm 30%, prefill giảm 64%.
2. **Nguyên nhân là đường mạng, không phải GPU.** Hai card nối qua Wi-Fi (3060 + 3060) chỉ đạt 53 tok/s, gần bằng 3 card (49 tok/s), trong khi hai card nối Ethernet đạt 126 tok/s. Tốc độ do đường mạng chậm nhất quyết định. Cùng cặp GPU 5070 + 3060 như production, chỉ đổi đường tới 5070 từ Ethernet sang Wi-Fi thật đã làm decode giảm 43% (125,8 → 72,1 tok/s).
3. **2 card chỉ qua Wi-Fi (5070 + 3060, đo trên Wi-Fi thật) vẫn chậm hơn Ethernet rõ rệt:** Coder-30B 72,1 tok/s (−43%), Qwen3.8-27B 20,3 tok/s (−15%), prefill giảm 81–83%, tải model lâu gấp 6–6,5 lần; nhưng decode nhanh hơn 3 card (48,9 và 16,7 tok/s).
4. **Giảm số tầng đặt lên card Wi-Fi gần như không giúp** (từ 32% xuống 18% tầng: 48,9 → 50,5 tok/s), vì mỗi nút từ xa tốn một chi phí cố định cho mỗi token.
5. **Card thứ ba có ích khi model không vừa 2 card.** Với Qwen3-Coder-30B bản Q6_K (25,1 GB), 2 card không nạp nổi; 3 card chạy được ở 49,6 tok/s, nhanh hơn phương án 2 card cộng đẩy expert sang CPU (30,1 tok/s) 65% ở một luồng. Tuy vậy Q4_K_M trên 2 card vẫn nhanh gấp 2,5 lần (125,8 tok/s).
6. **Khuyến nghị:** giữ 2 card qua Ethernet cho production; chỉ thêm card thứ ba khi nó nối Ethernet cùng cấp hoặc khi model không vừa 2 card.

## 2. Điều kiện đo

| Nút | GPU | Đường nối | RTT trung bình (tối đa) | Băng thông TCP đo thực (đi / về) |
|---|---|---|---|---|
| Linux (master) | RTX 3060 12 GB | không áp dụng | không áp dụng | không áp dụng |
| Windows `pc-win-01` | RTX 5070 12 GB | **Ethernet 2,5 GbE** | **0,32 ms** (0,66 ms) | **297 / 297 MB/s** |
| Windows `fi-ai03` | RTX 3060 12 GB | **Wi-Fi** | **4,9 ms** (20,3 ms) | **36,7 / 50,6 MB/s** |
| Windows `pc-win-01` (đo lại qua Wi-Fi) | RTX 5070 12 GB | **Wi-Fi 802.11ax** (cùng máy, đổi sang card Wi-Fi) | **4,0 ms** (19,2 ms) | **25,0 / 29,5 MB/s** |

Wi-Fi chỉ đạt 8–17% băng thông của Ethernet và có độ trễ gấp 10–15 lần. Trường hợp "5070 qua Wi-Fi" đo thật: máy 5070 kết nối Wi-Fi (192.168.1.78) và master gọi rpc-server qua địa chỉ Wi-Fi đó; điều khiển vẫn qua Ethernet. Cấu hình giống production: chia theo tầng, KV f16, flash attention; trọng số đẩy qua RPC từ trạng thái nguội. Số đo 2 card khớp benchmark production trước đó trong khoảng 1–2%.

## 3. Kết quả: 2 card so với 3 card (Q4_K_M)

**Qwen3-Coder-30B-A3B (MoE, 18,6 GB)**

| Chỉ số | 2 card (Ethernet) | 3 card (+ Wi-Fi) | Thay đổi |
|---|---|---|---|
| Thời gian tải model | 71,5 s | 189,4 s | +165% |
| Prefill 2.079 token | 3.258 tok/s | 1.234 tok/s | −62% |
| Decode một luồng | **125,8 tok/s** | **48,9 tok/s** | **−61%** |
| TPOT | 7,95 ms | 20,44 ms | +157% |
| Độ giật p99 giữa hai token | 11,0 ms | 31,1 ms | gấp 2,8 lần |
| Thông lượng 4 luồng song song | 211,7 tok/s | 92,5 tok/s | −56% |
| Điện năng (3 GPU) | 1,60 J/token | 3,16 J/token | +97% |
| VRAM đỉnh mỗi card | Linux 9,2 · 5070 11,3 GB | Linux 6,1 · 5070 8,3 · 3060 6,7 GB | giảm 26–34% |

**Qwen3.8-27B (dense, 16,5 GB)**

| Chỉ số | 2 card (Ethernet) | 3 card (+ Wi-Fi) | Thay đổi |
|---|---|---|---|
| Thời gian tải model | 54,1 s | 131,2 s | +142% |
| Prefill 2.079 token | 718,5 tok/s | 258,7 tok/s | −64% |
| Decode một luồng | **23,8 tok/s** | **16,7 tok/s** | **−30%** |
| TPOT | 41,96 ms | 59,80 ms | +43% |
| Độ giật p99 giữa hai token | 45,6 ms | 80,4 ms | gấp 1,8 lần |
| Thông lượng 4 luồng song song | 46,0 tok/s | 18,3 tok/s | −60% |
| Điện năng (3 GPU) | 9,48 J/token | 12,48 J/token | +32% |
| VRAM đỉnh mỗi card | Linux 8,6 · 5070 9,3 GB | Linux 6,2 · 5070 6,6 · 3060 5,7 GB | giảm 28–29% |

### Trường hợp 2 card, đường tới 5070 chỉ qua Wi-Fi (đo thật)

Cùng cặp GPU production (RTX 3060 Linux + RTX 5070), chỉ thay đường Ethernet tới 5070 bằng Wi-Fi thật của máy 5070 (RTT 4,0 ms, 25,0 / 29,5 MB/s; xem mục 2).

**Qwen3-Coder-30B-A3B**

| Chỉ số | 2 card Ethernet | **2 card Wi-Fi (5070 + 3060)** | 3 card (Ethernet + Wi-Fi) |
|---|---|---|---|
| Thời gian tải model | 71,5 s | 466,9 s | 189,4 s |
| Prefill 2.079 token | 3.258 tok/s | 562 tok/s | 1.234 tok/s |
| Decode một luồng | 125,8 tok/s | **72,1 tok/s** | 48,9 tok/s |
| TPOT | 7,95 ms | 13,87 ms | 20,44 ms |
| Độ giật p99 giữa hai token | 11,0 ms | 18,4 ms | 31,1 ms |
| Thông lượng 4 luồng song song | 211,7 tok/s | 84,5 tok/s | 92,5 tok/s |
| Điện năng (GPU) | 1,60 J/token | 2,23 J/token | 3,16 J/token |

**Qwen3.8-27B**

| Chỉ số | 2 card Ethernet | **2 card Wi-Fi (5070 + 3060)** | 3 card (Ethernet + Wi-Fi) |
|---|---|---|---|
| Thời gian tải model | 54,1 s | 319,6 s | 131,2 s |
| Prefill 2.079 token | 718 tok/s | 139 tok/s | 259 tok/s |
| Decode một luồng | 23,8 tok/s | **20,3 tok/s** | 16,7 tok/s |
| TPOT | 41,96 ms | 49,18 ms | 59,80 ms |
| Độ giật p99 giữa hai token | 45,6 ms | 166,7 ms | 80,4 ms |
| Thông lượng 4 luồng song song | 46,0 tok/s | 13,4 tok/s | 18,3 tok/s |

Nhận xét: chuyển 2 card từ Ethernet sang Wi-Fi làm decode Coder-30B giảm 43% và 27B giảm 15%; prefill giảm 83% và 81%; thời gian tải gấp 6,5 và 5,9 lần; thông lượng 4 luồng giảm 60% và 71%. Dù vậy 2 card Wi-Fi vẫn nhanh hơn 3 card ở decode (+47% và +22%) và ở điện năng mỗi token trên Coder-30B. Ngược lại 3 card có prefill tốt hơn (1.234 so với 562 tok/s) vì băng thông được chia cho hai đường. Với 27B, Wi-Fi của máy 5070 làm độ giật p99 tăng lên 167 ms (Ethernet 45,6 ms).

## 4. So sánh nguyên nhân

Năm cấu hình trên Coder-30B, chỉ khác đường nối và số card:

| Cấu hình | Decode | TPOT | Prefill 2.079 | Tải model | Thông lượng 4 luồng |
|---|---|---|---|---|---|
| 2 card, Ethernet (3060 + 5070) | **125,8 tok/s** | 7,95 ms | 3.258 tok/s | 71,5 s | 211,7 tok/s |
| 2 card, Wi-Fi thật (5070 + 3060) | 72,1 tok/s | 13,87 ms | 562 tok/s | 466,9 s | 84,5 tok/s |
| 2 card, Wi-Fi thật (3060 + 3060) | 53,3 tok/s | 18,75 ms | 827 tok/s | 214,4 s | 83,4 tok/s |
| 3 card (32 / 36 / 32% tầng) | 48,9 tok/s | 20,44 ms | 1.234 tok/s | 189,4 s | 92,5 tok/s |
| 3 card, giảm tải Wi-Fi (18% tầng) | 50,5 tok/s | 19,79 ms | 1.270 tok/s | 110,2 s | 102,2 tok/s |

Các cấu hình có Wi-Fi thật đều chỉ đạt 49–53 tok/s dù khác nhau về số card và tỷ lệ chia tầng. Hai kết quả từ cấu hình 5070 + 3060 trên Wi-Fi thật:

- **Riêng đường mạng đã gây ra phần lớn sự sụt giảm.** Cùng cặp 5070 + 3060, đổi từ Ethernet sang Wi-Fi: decode giảm 43% (125,8 → 72,1 tok/s), prefill giảm 83% (3.258 → 562), thông lượng 4 luồng giảm 60% (211,7 → 84,5), thời gian tải model gấp 6,5 lần (71,5 → 466,9 s).
- **GPU mạnh hơn chỉ giúp một phần.** Cặp có RTX 5070 trên Wi-Fi nhanh hơn cặp 3060 + 3060 trên Wi-Fi 35% (72,1 so với 53,3 tok/s) nhưng vẫn kém Ethernet gần một nửa. Với 2 card, tốc độ trên Wi-Fi (72,1) cao hơn 3 card 47% (48,9): card thứ ba gây thiệt hại lớn hơn việc chỉ hạ chất lượng một đường.
- **Băng thông Wi-Fi quyết định prefill và thời gian tải.** Wi-Fi của máy 5070 đo 25,0 / 29,5 MB/s, thấp hơn Wi-Fi của `fi-ai03` (36,7 / 50,6 MB/s), nên tải model mất 467 s (so với 214 s của cặp 3060 + 3060 chỉ gửi một nửa model qua Wi-Fi).

Ba cơ chế giải thích:

- **Decode bị chi phí cố định mỗi token chi phối.** Mỗi token kéo theo khoảng 0,3 MB qua mỗi đường RPC. Qua Wi-Fi, truyền 0,3 MB mất khoảng 6–8 ms, cộng chênh lệch RTT khoảng 4,6 ms, tổng khoảng 11–13 ms [ƯỚC TÍNH]; thực tế TPOT tăng 10,8 ms (7,95 → 18,75). Chi phí này không phụ thuộc số tầng đặt trên card từ xa, nên giảm tầng không giúp.
- **Prefill bị giới hạn băng thông.** Prefill chuyển lượng dữ liệu tỷ lệ với số token: Ethernet dùng khoảng 1.100–1.230 Mbps, còn Wi-Fi chạm trần ở khoảng 225–295 Mbps, sát băng thông đo được.
- **Tải model bị giới hạn bởi nhánh chậm nhất.** Trọng số đẩy qua Ethernet đạt khoảng 137–140 MB/s, qua Wi-Fi chỉ 31–46 MB/s; thời gian tải của 3 card do phần 6,2 GB gửi sang card Wi-Fi quyết định.

**Model dense Qwen3.8-27B** nhạy với đường mạng theo cách khác:

| Cấu hình | Decode | TPOT | Prefill 2.079 | Tải model | Thông lượng 4 luồng |
|---|---|---|---|---|---|
| 2 card, Ethernet (3060 + 5070) | **23,8 tok/s** | 41,96 ms | 718 tok/s | 54,1 s | 46,0 tok/s |
| 2 card, Wi-Fi thật (5070 + 3060) | 20,3 tok/s | 49,18 ms | 139 tok/s | 319,6 s | 13,4 tok/s |
| 3 card (+ Wi-Fi) | 16,7 tok/s | 59,80 ms | 259 tok/s | 131,2 s | 18,3 tok/s |

Decode chỉ giảm 15% vì mỗi token tốn nhiều thời gian tính toán (42 ms), nên 7,2 ms thêm từ mạng chiếm tỷ lệ nhỏ; ngược lại prefill giảm 81% vì bị giới hạn băng thông. Ở model này, 2 card trên Wi-Fi vẫn tốt hơn 3 card về decode (+22%) nhưng kém về prefill (−46%) và thông lượng 4 luồng (−27%).

Hệ quả phụ: GPU nhàn rỗi hơn khi chờ mạng (công suất card Linux từ 101 W xuống 50 W) nhưng mỗi token tốn thời gian hơn, nên điện năng mỗi token tăng; độ giật giữa hai token cũng tăng theo jitter của Wi-Fi.

## 5. Model lớn hơn dung lượng 2 card: Qwen3-Coder-30B-A3B bản Q6_K (25,1 GB)

Q6_K vượt VRAM dùng được của 2 card (khoảng 22,7 GB) nhưng vừa 3 card (khoảng 34,5 GB).

| Chỉ số | 2 card thuần | 2 card + 14 lớp expert trên CPU | 3 card | 3 card so với 2 card + CPU |
|---|---|---|---|---|
| Chạy được | **Không** (hết bộ nhớ GPU khi cấp bộ đệm tính toán) | Có | Có | không áp dụng |
| Thời gian tải model | không áp dụng | 69,3 s | 247,5 s | gấp 3,6 lần (chậm hơn) |
| Prefill 2.079 token | không áp dụng | 396 tok/s | 1.074 tok/s | gấp 2,7 lần |
| Decode một luồng | không áp dụng | 30,1 tok/s | **49,6 tok/s** | +65% |
| Độ giật p99 giữa hai token | không áp dụng | 78,6 ms | 35,6 ms | ổn định hơn |
| Thông lượng 4 luồng song song | không áp dụng | 58,7 tok/s | 60,3 tok/s | ngang nhau (+3%) |
| Điện năng (GPU) | không áp dụng | 3,32 J/token | 3,26 J/token | ngang nhau |

- 3 card cho phép chạy toàn bộ trên GPU và nhanh hơn phương án đẩy expert sang CPU ở độ trễ một luồng, nhưng lợi thế biến mất khi có 4 luồng song song và việc tải model chậm hơn nhiều.
- Q6_K trên 3 card (49,6 tok/s) gần bằng Q4_K_M trên 3 card (48,9 tok/s): khi nút thắt là mạng, tăng độ chính xác lượng tử hóa gần như không tốn thêm tốc độ.
- So với Q4_K_M trên 2 card (125,8 tok/s), Q6_K trên 3 card chậm hơn 61% và tốn điện gấp đôi mỗi token. Chỉ nên dùng nếu Q6_K cho kết quả tốt hơn rõ rệt, điều chưa được đo.

## 6. Khuyến nghị

1. Giữ `merged` 2 card qua Ethernet cho production.
2. Không đưa nút Wi-Fi vào nhóm `merged` nếu cần tốc độ.
3. Dùng card thứ ba cho việc khác (bản sao độc lập cho tác vụ song song) hoặc chỉ gộp khi model không vừa 2 card.
4. Nếu muốn 3 card trong một nhóm `merged`, nối card thứ ba bằng Ethernet cùng cấp (RTT dưới 1 ms, trên 250 MB/s) rồi đo lại; dự đoán tốc độ khi đó gần 2 card là [ƯỚC TÍNH].

## 7. Giới hạn

- Wi-Fi là môi trường chung, chất lượng thay đổi theo thời điểm; mỗi cấu hình Wi-Fi chỉ đo một lượt.
- Mỗi cấu hình chạy một lượt (mỗi phép đo lặp 2–3 lần, lấy trung vị); biến động của Wi-Fi chưa được định lượng. Băng thông Wi-Fi đo riêng dao động 36,7–50,6 MB/s.
- Dùng prompt tổng hợp có độ dài cố định, nên không đại diện cho tải thực; lưu lượng Wi-Fi lấy từ bộ đếm card mạng có thể lẫn lưu lượng khác; điện năng chỉ gồm công suất board GPU.
- Card thứ ba là RTX 3060 (cùng loại card master). Chất lượng của Q6_K so với Q4_K_M chưa được đo.

Dữ liệu thô: `benchmarks/reports/perf3node/*.json`.
