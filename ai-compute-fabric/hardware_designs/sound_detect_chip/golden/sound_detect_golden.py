"""Bit-accurate Fixed-Point (Q1.15) Golden Reference Model for the AICF Sound Detection Chip.

Models the exact cycle-by-cycle and frame-by-frame arithmetic of:
  1. `dc_blocker_iir`: 1st-order multiplier-less DC offset blocker
  2. `frame_feature_unit`: 64-sample window Short-Time Energy (STE), High-Frequency Energy (HFE), and Zero-Crossing Rate (ZCR)
  3. `sound_detect_fsm`: Adaptive EMA Noise Floor Tracker + 4-state Acoustic Event & VAD Classifier FSM
  4. `aicf_sound_detect_top`: Complete top-level digital ASIC/FPGA IP core
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

CLASS_NONE = 0
CLASS_SPEECH = 1
CLASS_IMPULSE = 2
CLASS_ALARM = 3

STATE_IDLE = 0
STATE_ONSET = 1
STATE_ACTIVE = 2
STATE_HANGOVER = 3


def sat16(val: int) -> int:
    """Saturate integer to signed 16-bit range [-32768, 32767]."""
    if val > 32767:
        return 32767
    if val < -32768:
        return -32768
    return int(val)


def abs16(val: int) -> int:
    """16-bit absolute value clamped to [0, 32767]."""
    if val == -32768:
        return 32767
    return abs(int(val))


@dataclass
class FrameTrace:
    frame_idx: int
    energy_total: int
    energy_hf: int
    zcr_count: int
    noise_floor: int
    state: int
    detected_class: int
    irq_wakeup: int


@dataclass
class SoundDetectGoldenModel:
    """Cycle-accurate & frame-accurate fixed-point model of `aicf_sound_detect_top`."""

    thresh_energy: int = 12000
    thresh_zcr: int = 18
    hangover_frames: int = 2

    # DC blocker state
    x_prev: int = 0
    y_prev: int = 0

    # Frame feature accumulator state (64 samples per frame)
    sample_cnt: int = 0
    prev_y: int = 0
    acc_energy: int = 0
    acc_hf: int = 0
    acc_zcr: int = 0

    # FSM & Noise floor state
    noise_floor: int = 1024
    state: int = STATE_IDLE
    detected_class: int = CLASS_NONE
    irq_wakeup: int = 0
    hangover_cnt: int = 0
    frame_idx: int = 0
    traces: list[FrameTrace] = field(default_factory=list)

    def reset(self) -> None:
        self.x_prev = 0
        self.y_prev = 0
        self.sample_cnt = 0
        self.prev_y = 0
        self.acc_energy = 0
        self.acc_hf = 0
        self.acc_zcr = 0
        self.noise_floor = 1024
        self.state = STATE_IDLE
        self.detected_class = CLASS_NONE
        self.irq_wakeup = 0
        self.hangover_cnt = 0
        self.frame_idx = 0
        self.traces.clear()

    def step_dc_blocker(self, pcm_in: int) -> int:
        """y[n] = sat16(x[n] - x[n-1] + y[n-1] - (y[n-1] >>> 4)) -> HPF cutoff ~159 Hz at fs=16 kHz."""
        x = sat16(pcm_in)
        diff = x - self.x_prev
        decay = self.y_prev >> 4  # Python arithmetic shift matches Verilog signed `>>> 4`
        y = sat16(diff + self.y_prev - decay)
        self.x_prev = x
        self.y_prev = y
        return y

    def step_sample(self, pcm_in: int) -> FrameTrace | None:
        """Process one 16-bit PCM sample; returns FrameTrace on the 64th sample of each frame."""
        y = self.step_dc_blocker(pcm_in)
        abs_y = abs16(y)
        diff_y = abs(y - self.prev_y)
        if diff_y > 65535:
            diff_y = 65535
        sign_y = 1 if y < 0 else 0
        sign_prev = 1 if self.prev_y < 0 else 0
        zc = 1 if (self.sample_cnt > 0 and sign_y != sign_prev) else 0

        new_energy = (self.acc_energy + abs_y) & 0xFFFFFF
        new_hf = (self.acc_hf + diff_y) & 0xFFFFFF
        new_zcr = (self.acc_zcr + zc) & 0xFF

        self.prev_y = y

        if self.sample_cnt == 63:
            self.sample_cnt = 0
            self.acc_energy = 0
            self.acc_hf = 0
            self.acc_zcr = 0
            return self._step_fsm(new_energy, new_hf, new_zcr)
        else:
            self.sample_cnt += 1
            self.acc_energy = new_energy
            self.acc_hf = new_hf
            self.acc_zcr = new_zcr
            return None

    def _step_fsm(self, energy_total: int, energy_hf: int, zcr_count: int) -> FrameTrace:
        is_loud = (energy_total > (self.noise_floor + self.thresh_energy)) and (zcr_count >= 2)

        # Event classification when frame is loud
        if is_loud:
            if energy_hf > (energy_total + (energy_total >> 2)) and zcr_count >= self.thresh_zcr:
                frame_class = CLASS_IMPULSE
            elif zcr_count >= self.thresh_zcr:
                frame_class = CLASS_ALARM
            else:
                frame_class = CLASS_SPEECH
        else:
            frame_class = CLASS_NONE

        # Update adaptive noise floor when quiet in IDLE
        next_noise = self.noise_floor
        if self.state == STATE_IDLE and not is_loud:
            next_noise = self.noise_floor - (self.noise_floor >> 4) + (energy_total >> 4)

        next_state = self.state
        next_irq = self.irq_wakeup
        next_class = self.detected_class
        next_hang = self.hangover_cnt

        if self.state == STATE_IDLE:
            if is_loud:
                if frame_class == CLASS_IMPULSE:
                    next_state = STATE_ACTIVE
                    next_irq = 1
                    next_class = frame_class
                    next_hang = self.hangover_frames
                else:
                    next_state = STATE_ONSET
                    next_irq = 0
                    next_class = frame_class
            else:
                next_irq = 0
                next_class = CLASS_NONE
        elif self.state == STATE_ONSET:
            if is_loud:
                next_state = STATE_ACTIVE
                next_irq = 1
                next_class = frame_class
                next_hang = self.hangover_frames
            else:
                next_state = STATE_IDLE
                next_irq = 0
                next_class = CLASS_NONE
        elif self.state == STATE_ACTIVE:
            if is_loud:
                next_state = STATE_ACTIVE
                next_irq = 1
                next_class = frame_class
                next_hang = self.hangover_frames
            else:
                if self.hangover_frames > 0:
                    next_state = STATE_HANGOVER
                    next_hang = self.hangover_frames - 1
                    next_irq = 1
                else:
                    next_state = STATE_IDLE
                    next_irq = 0
                    next_class = CLASS_NONE
        elif self.state == STATE_HANGOVER:
            if is_loud:
                next_state = STATE_ACTIVE
                next_irq = 1
                next_class = frame_class
                next_hang = self.hangover_frames
            elif self.hangover_cnt == 0:
                next_state = STATE_IDLE
                next_irq = 0
                next_class = CLASS_NONE
            else:
                next_hang = self.hangover_cnt - 1
                next_irq = 1

        self.noise_floor = next_noise & 0xFFFFFF
        self.state = next_state
        self.irq_wakeup = next_irq
        self.detected_class = next_class
        self.hangover_cnt = next_hang

        tr = FrameTrace(
            frame_idx=self.frame_idx,
            energy_total=energy_total,
            energy_hf=energy_hf,
            zcr_count=zcr_count,
            noise_floor=self.noise_floor,
            state=self.state,
            detected_class=self.detected_class,
            irq_wakeup=self.irq_wakeup,
        )
        self.traces.append(tr)
        self.frame_idx += 1
        return tr


def generate_acoustic_stimulus() -> tuple[list[int], list[str]]:
    """Generate a 16-frame (1024-sample) acoustic test scenario covering 5 real-world regimes:
      - Frames 0..2: Silence + strong DC offset (+1200) -> verifies DC blocker & noise floor convergence
      - Frames 3..4: Sharp high-frequency Impulse / Clap / Glass break -> verifies instant IMPULSE trigger (class=2)
      - Frames 5..7: Quiet recovery -> verifies HANGOVER hold & return to IDLE (irq=0)
      - Frames 8..11: Low-frequency Vowel Speech (fundamental ~350 Hz) -> verifies ONSET debounce -> SPEECH trigger (class=1)
      - Frames 12..13: Quiet recovery -> return to IDLE
      - Frames 14..15: High-ZCR Continuous Alarm Siren (~3.5 kHz) -> verifies ALARM trigger (class=3)
    """
    samples: list[int] = []
    frame_labels: list[str] = []

    # Frames 0..2: Silence with DC offset + small thermal noise
    for f in range(3):
        frame_labels.append("SILENCE_DC_OFFSET")
        for n in range(64):
            samples.append(1200 + ((n % 5) - 2) * 8)

    # Frame 3: Sharp Impulse / Glass Break (alternating high-amplitude transient)
    frame_labels.append("IMPULSE_CLAP_BURST")
    for n in range(64):
        amp = 6500 if (n % 2 == 0) else -6500
        samples.append(1200 + amp)

    # Frames 4..7: Silence recovery (4 frames so hangover expires cleanly)
    for f in range(4):
        frame_labels.append("QUIET_RECOVERY_1")
        for n in range(64):
            samples.append(1200)

    # Frames 8..10: Sustained Speech vowel (~375 Hz smooth sinusoid, low ZCR, high STE)
    for f in range(3):
        frame_labels.append("SPEECH_VOICE_ACTIVE")
        for n in range(64):
            t = f * 64 + n
            val = int(4200 * math.sin(2.0 * math.pi * t / 32.0))
            samples.append(1200 + val)

    # Frames 11..13: Quiet recovery
    for f in range(3):
        frame_labels.append("QUIET_RECOVERY_2")
        for n in range(64):
            samples.append(1200)

    # Frames 14..15: High-ZCR Continuous Alarm Tone (~3.0 kHz sinusoid, ZCR >= 18, HFE <= STE)
    for f in range(2):
        frame_labels.append("ALARM_TONE_ACTIVE")
        for n in range(64):
            t = f * 64 + n
            val = int(4500 * math.sin(2.0 * math.pi * t / 6.0))
            samples.append(1200 + val)

    return samples, frame_labels


def run_golden_verification() -> dict[str, Any]:
    model = SoundDetectGoldenModel()
    samples, labels = generate_acoustic_stimulus()
    for s in samples:
        model.step_sample(s)
    return {
        "total_samples": len(samples),
        "total_frames": len(model.traces),
        "labels": labels,
        "traces": model.traces,
    }


if __name__ == "__main__":
    res = run_golden_verification()
    for lbl, tr in zip(res["labels"], res["traces"]):
        print(
            f"Frame {tr.frame_idx:02d} [{lbl:20s}] | STE={tr.energy_total:6d} HFE={tr.energy_hf:6d} "
            f"ZCR={tr.zcr_count:2d} NF={tr.noise_floor:5d} | State={tr.state} Class={tr.detected_class} IRQ={tr.irq_wakeup}"
        )
