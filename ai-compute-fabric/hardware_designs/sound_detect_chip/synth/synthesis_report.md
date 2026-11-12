# Báo Cáo Thiết Kế & Tổng Hợp Phần Cứng: Chip Phát Hiện Âm Thanh (`aicf_sound_detect_top`)

- **Mô hình AI Thiết kế**: `Qwen3.8-27B-UD-Q4_K_M.gguf` (trên AI Compute Fabric Gateway)
- **Phương pháp**: Thiết kế phân cấp 4 khối RTL (`dc_blocker_iir`, `frame_feature_unit`, `sound_detect_fsm`, `aicf_sound_detect_top`) + Kiểm định EDA khép kín (`Verilator 5.020` + `Icarus Verilog 12.0` + `Yosys 0.33`)
- **Trạng thái mô phỏng (`iverilog` + `vvp`)**: `PASS (16/16 khung âm thanh, 1024 mẫu PCM khớp 100% bit-accurate với Golden Model)`
- **Kiểm tra tĩnh (`verilator --lint-only -Wall`)**: `0 cảnh báo`
- **Số Latch suy diễn ngoài ý muốn (Inferred Latches)**: `0` (Đạt chuẩn ASIC/FPGA = 0)
- **File dạng sóng (Waveform)**: `sound_detect.vcd` (`424709 bytes`)
- **Gate-Level Netlist**: `aicf_sound_detect_netlist.v` (`150425 bytes`)

## 1. Thống kê Cổng Logic sau Tổng hợp (`Yosys 0.33`)

| Chỉ số Phần cứng | Giá trị |
| :--- | :--- |
| **Top Module** | `aicf_sound_detect_top` |
| **Tổng số cổng logic nguyên thủy (Total Cells)** | **2609 cells** |
| **Số lượng Thanh ghi / Flip-Flop (DFF)** | **202 bits** |
| **Tổng số dây tín hiệu (Wire Bits)** | **3292 bits** |
| **Số bộ nhân phần cứng (DSP Multipliers)** | **0** (Thiết kế Multiplier-less hoàn toàn bằng dịch bit & cộng) |

### Chi tiết các loại cổng logic (`num_cells_by_type`):
| Loại cổng (Cell Type) | Số lượng |
| :--- | :--- |
| `$_AND_` | 987 |
| `$_MUX_` | 213 |
| `$_NOT_` | 226 |
| `$_OR_` | 477 |
| `$_SDFFE_PP0P_` | 198 |
| `$_SDFFE_PP1P_` | 1 |
| `$_SDFF_PP0_` | 3 |
| `$_XOR_` | 504 |

## 2. Kết quả Mô phỏng Chu kỳ xung nhịp (Cycle-Accurate Simulation Log)

```text
VCD info: dumpfile /mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/hardware_designs/sound_detect_chip/tb/sound_detect.vcd opened for output.
FRAME 00 | STE=19262 (exp 19262) ZCR=0 (exp 0) NF=2163 | State=0 (exp 0) Class=0 (exp 0) IRQ=0 (exp 0)
FRAME 01 | STE=937 (exp 937) ZCR=20 (exp 20) NF=2086 | State=0 (exp 0) Class=0 (exp 0) IRQ=0 (exp 0)
FRAME 02 | STE=729 (exp 729) ZCR=25 (exp 25) NF=2001 | State=0 (exp 0) Class=0 (exp 0) IRQ=0 (exp 0)
FRAME 03 | STE=429314 (exp 429314) ZCR=63 (exp 63) NF=2001 | State=2 (exp 2) Class=2 (exp 2) IRQ=1 (exp 1)
FRAME 04 | STE=3788 (exp 3788) ZCR=0 (exp 0) NF=2001 | State=3 (exp 3) Class=2 (exp 2) IRQ=1 (exp 1)
FRAME 05 | STE=960 (exp 960) ZCR=0 (exp 0) NF=2001 | State=3 (exp 3) Class=2 (exp 2) IRQ=1 (exp 1)
FRAME 06 | STE=960 (exp 960) ZCR=0 (exp 0) NF=2001 | State=0 (exp 0) Class=0 (exp 0) IRQ=0 (exp 0)
FRAME 07 | STE=960 (exp 960) ZCR=0 (exp 0) NF=1936 | State=0 (exp 0) Class=0 (exp 0) IRQ=0 (exp 0)
FRAME 08 | STE=159639 (exp 159639) ZCR=4 (exp 4) NF=1936 | State=1 (exp 1) Class=1 (exp 1) IRQ=0 (exp 0)
FRAME 09 | STE=167943 (exp 167943) ZCR=4 (exp 4) NF=1936 | State=2 (exp 2) Class=1 (exp 1) IRQ=1 (exp 1)
FRAME 10 | STE=168064 (exp 168064) ZCR=4 (exp 4) NF=1936 | State=2 (exp 2) Class=1 (exp 1) IRQ=1 (exp 1)
FRAME 11 | STE=20682 (exp 20682) ZCR=0 (exp 0) NF=1936 | State=3 (exp 3) Class=1 (exp 1) IRQ=1 (exp 1)
FRAME 12 | STE=1065 (exp 1065) ZCR=0 (exp 0) NF=1936 | State=3 (exp 3) Class=1 (exp 1) IRQ=1 (exp 1)
FRAME 13 | STE=960 (exp 960) ZCR=0 (exp 0) NF=1936 | State=0 (exp 0) Class=0 (exp 0) IRQ=0 (exp 0)
FRAME 14 | STE=173744 (exp 173744) ZCR=21 (exp 21) NF=1936 | State=1 (exp 1) Class=3 (exp 3) IRQ=0 (exp 0)
FRAME 15 | STE=178017 (exp 178017) ZCR=21 (exp 21) NF=1936 | State=2 (exp 2) Class=3 (exp 3) IRQ=1 (exp 1)
PASS: All 16 acoustic frames (1024 samples) matched bit-accurate Golden Model!
/tmp/aicf_eda_hvs09ahs/tb_0.sv:1135: $finish called at 20635000 (1ps)
[Yosys Synthesis] top=aicf_sound_detect_top cells=2609 dff=202 latches=0 wire_bits=3292
```
