# Đặc Tả Kiến Trúc Vi Mạch: Chip Phát Hiện Âm Thanh Luôn Bật (`aicf_sound_detect_top`)

## 1. Tổng quan (Overview)
`aicf_sound_detect_top` là nhân IP phần cứng số (Digital ASIC / FPGA IP Core) siêu tiết kiệm năng lượng (Ultra-Low-Power Always-On Acoustic Event & Voice Activity Detector), được thiết kế tự động bởi **`Qwen3.8-27B-UD-Q4_K_M`** trên nền tảng **AI Compute Fabric** và kiểm định bit-accurate bằng bộ công cụ EDA (`Verilator 5.020`, `Icarus Verilog 12.0`, `Yosys 0.33`).

### Đặc trưng kỹ thuật chính:
- **Tần số lấy mẫu đầu vào ($f_s$)**: $16\text{ kHz}$, mẫu âm thanh PCM 16-bit có dấu (`Q1.15`, dải `[-32768, +32767]`).
- **Kiến trúc không bộ nhân (Multiplier-less Shift-Add DSP)**: Toàn bộ bộ lọc thông cao khử DC, trích xuất năng lượng và bám nền nhiễu EMA đều thực hiện bằng phép dịch bit số học (`>>>`) và phép cộng bảo toàn độ chính xác, giúp diện tích silicon cực nhỏ (**0 DSP Multiplier**).
- **Kích thước khung phân tích (Frame Window)**: $N = 64$ mẫu ($4\text{ ms}$ ở $16\text{ kHz}$), độ trễ phát hiện xung đột biến (tiếng vỗ tay / kính vỡ) $\le 4\text{ ms}$.
- **Phân loại trực tiếp 3 nhóm sự kiện âm thanh**:
  1. `CLASS_SPEECH (2'd1)`: Giọng nói con người (năng lượng cao duy trì qua $\ge 2$ khung liên tiếp, tốc độ qua điểm không $\text{ZCR}$ vừa phải).
  2. `CLASS_IMPULSE (2'd2)`: Âm thanh xung kích / tiếng vỗ tay / tiếng kính vỡ (năng lượng vi phân tần cao $E_{\text{hf}} > 1.25 E_{\text{total}}$ và $\text{ZCR} \ge \text{thresh\_zcr}$, kích hoạt ngắt tức thì).
  3. `CLASS_ALARM (2'd3)`: Còi báo động / âm sắc tần số cao liên tục ($\text{ZCR} \ge \text{thresh\_zcr}$ với năng lượng cao).

---

## 2. Sơ đồ giao diện tín hiệu (Pinout / Interface)

| Tên cổng (Port) | Hướng | Độ rộng | Mô tả |
| :--- | :---: | :---: | :--- |
| `clk` | Input | 1-bit | Xung nhịp hệ thống (System Clock) |
| `rst_n` | Input | 1-bit | Reset đồng bộ tích cực mức thấp (Active-Low Reset) |
| `cfg_enable` | Input | 1-bit | Cho phép bộ xử lý âm thanh hoạt động (`1` = Enable) |
| `cfg_thresh_energy` | Input | 24-bit | Ngưỡng năng lượng vượt trên nền nhiễu ($\Delta E_{\text{th}}$, mặc định `12000`) |
| `cfg_thresh_zcr` | Input | 8-bit | Ngưỡng số lần đổi dấu phân biệt âm tần cao/xung kích (mặc định `18`) |
| `cfg_hangover_frames` | Input | 4-bit | Số khung giữ trạng thái sau khi âm thanh kết thúc (mặc định `2`) |
| `pcm_valid_i` | Input | 1-bit | Xung báo mẫu âm thanh đầu vào hợp lệ |
| `pcm_data_i` | Input | 16-bit signed | Mẫu âm thanh đầu vào `Q1.15` |
| `frame_done_o` | Output | 1-bit | Xung báo kết thúc 1 khung 64 mẫu (rộng 1 chu kỳ `clk`) |
| `frame_energy_o` | Output | 24-bit | Tổng năng lượng tuyệt đối của khung ($E_{\text{total}}$) |
| `frame_hf_energy_o` | Output | 24-bit | Năng lượng vi phân tần số cao của khung ($E_{\text{hf}}$) |
| `frame_zcr_o` | Output | 8-bit | Số lần qua điểm 0 trong khung ($\text{ZCR}$) |
| `noise_floor_o` | Output | 24-bit | Mức nền nhiễu thích nghi hiện tại ($N_{\text{floor}}$) |
| `fsm_state_o` | Output | 2-bit | Trạng thái FSM (`0=IDLE`, `1=ONSET`, `2=ACTIVE`, `3=HANGOVER`) |
| `detected_class_o` | Output | 2-bit | Nhóm âm thanh (`0=NONE`, `1=SPEECH`, `2=IMPULSE`, `3=ALARM`) |
| `irq_wakeup_o` | Output | 1-bit | Chân ngắt đánh thức vi xử lý chính (Active-High Wakeup Interrupt) |

---

## 3. Toán học Dấu phẩy tĩnh (Fixed-Point Arithmetic)

1. **Khối Khử DC (`dc_blocker_iir`)**:
   - Bộ lọc thông cao IIR bậc 1 với tần số cắt $f_c \approx \frac{f_s}{2\pi \cdot 16} \approx 159\text{ Hz}$:
     $$y[n] = \text{sat}_{16}\Bigl(x[n] - x[n-1] + y[n-1] - (y[n-1] \gg 4)\Bigr)$$
2. **Khối Trích xuất đặc trưng (`frame_feature_unit`)**:
   - Năng lượng toàn dải:
     $$E_{\text{total}}[m] = \sum_{n=0}^{63} |y[n]|$$
   - Năng lượng vi phân bậc 1 (nhấn mạnh dải tần cao):
     $$E_{\text{hf}}[m] = \sum_{n=0}^{63} \min\bigl(65535,\ |y[n] - y[n-1]|\bigr)$$
   - Tốc độ qua điểm không (Zero-Crossing Rate):
     $$\text{ZCR}[m] = \sum_{n=1}^{63} \mathbb{I}\bigl(\text{sign}(y[n]) \neq \text{sign}(y[n-1])\bigr)$$
3. **Khối Bám nền nhiễu & FSM (`sound_detect_fsm`)**:
   - Khi `state == IDLE` và không có âm thanh lớn:
     $$N_{\text{floor}}[m] = N_{\text{floor}}[m-1] - \bigl(N_{\text{floor}}[m-1] \gg 4\bigr) + \bigl(E_{\text{total}}[m] \gg 4\bigr)$$
   - Điều kiện phát hiện âm thanh thực sự (loại bỏ bước nhảy DC thuần túy):
     $$\text{is\_loud} = \bigl(E_{\text{total}}[m] > N_{\text{floor}}[m-1] + \Delta E_{\text{th}}\bigr) \land \bigl(\text{ZCR}[m] \ge 2\bigr)$$

