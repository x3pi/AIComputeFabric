package estimator

import "testing"

const gb = uint64(1_000_000_000)

func TestKVCacheQwen3_8B(t *testing.T) {
	// Qwen3-8B: 36 layers, 8 KV heads, head_dim 128, fp16, ctx 8192.
	got := KVCacheBytes(36, 8, 128, 8192, 1, 2)
	if got != 1_207_959_552 { // 1.125 GiB
		t.Fatalf("KV bytes = %d", got)
	}
}

func TestBucketsNeverUnderflow(t *testing.T) {
	// os_user + headroom > total must clamp allocatable to 0, not wrap.
	b := NewBuckets(12*gb, 11*gb, 4*gb, 1*gb, 1*gb, 2*gb)
	if b.Allocatable != 0 {
		t.Fatalf("allocatable = %d, want 0", b.Allocatable)
	}
	if got := OSUserFromEngine(1*gb, 4*gb, 1*gb, 1*gb); got != 0 {
		t.Fatalf("os_user = %d, want 0", got)
	}
	if SatAdd(^uint64(0), 1) != ^uint64(0) {
		t.Fatal("SatAdd must clamp")
	}
}

func TestBucketsArithmetic(t *testing.T) {
	b := NewBuckets(12288*1_000_000, 700_000_000, 4_900_000_000, 1_200_000_000, 600_000_000, 1_536_000_000)
	sum := b.OSUser + b.Weights + b.KVState + b.Compute + b.Headroom + b.Allocatable
	if sum != b.Total {
		t.Fatalf("buckets sum %d != total %d", sum, b.Total)
	}
}

func TestFitStatus(t *testing.T) {
	cases := []struct {
		req, alloc uint64
		offload    bool
		want       string
	}{
		{6600, 10200, false, FitGreen},
		{9300, 10200, false, FitYellow}, // >90%
		{10300, 10200, false, FitRed},
		{5000, 10200, true, FitYellow},
		{1, 0, false, FitRed},
	}
	for _, c := range cases {
		if got := FitStatus(c.req, c.alloc, c.offload); got != c.want {
			t.Errorf("FitStatus(%d,%d,%v)=%s want %s", c.req, c.alloc, c.offload, got, c.want)
		}
	}
}

// 2-6 GB: 5% (100-300 MB) is below the 300 MB floor, so the floor must decide.
func TestToleranceFloorBetween2And6GB(t *testing.T) {
	if !WithinTolerance(4_000_000_000, 4_280_000_000) { // 280MB; 5% would be 214MB
		t.Error("280MB on a 4.28GB peak must pass (floor 300MB)")
	}
	if WithinTolerance(4_000_000_000, 4_320_000_000) {
		t.Error("320MB must fail")
	}
}

func TestWithinTolerance(t *testing.T) {
	if !WithinTolerance(6_000_000_000, 6_250_000_000) { // 250MB < max(5%=312MB,300MB)
		t.Error("should be within")
	}
	if WithinTolerance(6_000_000_000, 6_400_000_000) { // 400MB > 320MB
		t.Error("should be outside")
	}
	if !WithinTolerance(1_000_000_000, 1_290_000_000) { // 290MB < 300MB floor
		t.Error("floor 300MB must apply to small models")
	}
	if WithinTolerance(1_000_000_000, 1_310_000_000) {
		t.Error("310MB over floor must fail")
	}
}

// The plan's corrected example: Qwen2.5-32B Q4_K_M, 64 layers, ~19.76 GB,
// 10.2 GB allocatable. The old (wrong) example put 42 layers on the GPU.
func TestQwen32BLayersOnOneGPU(t *testing.T) {
	m := ModelFacts{
		Layers: 64, NonRepeating: 600_000_000, WeightsTotal: 19_760_000_000,
		LayersAttn: 64, NKV: 8, HeadDim: 128, BytesPerKV: 2,
	}
	n, fits := MaxGPULayers(m, 10_200_000_000, 600_000_000, 4096, 1)
	if fits {
		t.Fatal("32B must not fit entirely in 10.2 GB")
	}
	if n < 28 || n > 30 {
		t.Fatalf("layers on GPU = %d, plan says ~28-30", n)
	}
	if n >= 42 {
		t.Fatal("42 layers cannot fit (the earlier arithmetic error)")
	}
}

func TestQwen8BFitsEntirely(t *testing.T) {
	m := ModelFacts{
		Layers: 36, NonRepeating: 400_000_000, WeightsTotal: 4_900_000_000,
		LayersAttn: 36, NKV: 8, HeadDim: 128, BytesPerKV: 2,
	}
	n, fits := MaxGPULayers(m, 10_200_000_000, 600_000_000, 8192, 1)
	if !fits || n != 36 {
		t.Fatalf("got %d fits=%v", n, fits)
	}
}

func TestMaxGPULayersMonotonicInContext(t *testing.T) {
	m := ModelFacts{Layers: 64, NonRepeating: 600_000_000, WeightsTotal: 19_760_000_000,
		LayersAttn: 64, NKV: 8, HeadDim: 128, BytesPerKV: 2}
	prev := uint64(1 << 30)
	for _, ctx := range []uint64{2048, 8192, 32768} {
		n, _ := MaxGPULayers(m, 10_200_000_000, 600_000_000, ctx, 1)
		if n > prev {
			t.Fatalf("layers must not grow with ctx: ctx=%d n=%d prev=%d", ctx, n, prev)
		}
		prev = n
	}
}

func TestSplitLayers(t *testing.T) {
	got := SplitLayers(64, 3)
	want := []string{"0-21", "22-42", "43-63"}
	if len(got) != 3 {
		t.Fatalf("len=%d", len(got))
	}
	var total uint64
	for i, r := range got {
		if r.String() != want[i] {
			t.Errorf("range %d = %s want %s", i, r, want[i])
		}
		total += r.End - r.Start + 1
	}
	if total != 64 {
		t.Fatalf("total layers = %d (the old plan counted 65)", total)
	}
	if SplitLayers(0, 3) != nil || SplitLayers(3, 0) != nil {
		t.Error("degenerate input must return nil")
	}
	if len(SplitLayers(2, 4)) != 2 {
		t.Error("fewer layers than devices must not create empty ranges")
	}
}

// Real measurements (RTX 3060, llama.cpp 74bca39, Qwen3-8B Q4_K_M, flash-attn off).
func TestCalibratedLlamaCppVRAMAgainstMeasurements(t *testing.T) {
	const mib = uint64(1 << 20)
	cases := []struct {
		name                 string
		oneSlot, kv, par     uint64 // MiB, MiB, slots
		rawParserTotal, meas uint64 // MiB
	}{
		{"8B c8192 np4", 6417, 1152, 4, 11457, 9768},
		{"8B c4096 np3", 5577, 576, 3, 7257, 6624}, // held-out batch
		{"8B c2048 np4", 5157, 288, 4, 6417, 5916},
	}
	for _, c := range cases {
		got := CalibratedLlamaCppVRAM(c.oneSlot*mib, c.kv*mib, c.par)
		if !WithinTolerance(got, c.meas*mib) {
			t.Errorf("%s: calibrated %d MiB vs measured %d MiB outside tolerance", c.name, got/mib, c.meas)
		}
		if WithinTolerance(c.rawParserTotal*mib, c.meas*mib) {
			t.Logf("%s: raw parser total would also pass", c.name)
		}
	}
	// The old (uncalibrated) 8B c8192 np4 figure was 1.7 GB too high and must fail.
	if WithinTolerance(11457*mib, 9768*mib) {
		t.Error("raw parser figure should be outside tolerance (documents why calibration exists)")
	}
	if CalibratedLlamaCppVRAM(100, 10, 0) != 100 {
		t.Error("parallel=0 must behave like 1")
	}
}
