// Package estimator holds the pure arithmetic of the AICF VRAM estimator.
//
// It does NOT parse GGUF files: that is delegated to gguf-parser-go (P1.4,
// integration pending) which supplies the model facts used here. Everything in
// this file is deterministic arithmetic with saturating unsigned maths, so the
// 6-bucket accounting can never underflow (plan §B.2, test P1).
package estimator

import "fmt"

// Fit status labels shown in the UI (plan §B.1).
const (
	FitGreen  = "GREEN"  // required <= 90% of allocatable
	FitYellow = "YELLOW" // 90%..100% of allocatable, or RAM offload needed
	FitRed    = "RED"    // required > allocatable
)

// Confidence of a number (plan B.0 principle 4).
type Confidence int

const (
	Estimated Confidence = iota
	Calibrated
	Measured
)

// Buckets is the 6-bucket accounting of one GPU, in bytes.
type Buckets struct {
	Total       uint64
	OSUser      uint64
	Weights     uint64
	KVState     uint64
	Compute     uint64
	Headroom    uint64
	Allocatable uint64
}

// SatSub returns a-b, or 0 when b > a.
func SatSub(a, b uint64) uint64 {
	if b >= a {
		return 0
	}
	return a - b
}

// SatAdd returns a+b clamped to MaxUint64.
func SatAdd(a, b uint64) uint64 {
	s := a + b
	if s < a {
		return ^uint64(0)
	}
	return s
}

// NewBuckets computes Allocatable = Total - (OSUser+Headroom+Weights+KV+Compute),
// saturating at zero.
func NewBuckets(total, osUser, weights, kv, compute, headroom uint64) Buckets {
	used := SatAdd(SatAdd(SatAdd(osUser, headroom), SatAdd(weights, kv)), compute)
	return Buckets{
		Total: total, OSUser: osUser, Weights: weights, KVState: kv,
		Compute: compute, Headroom: headroom,
		Allocatable: SatSub(total, used),
	}
}

// OSUserFromEngine returns the OS/User bucket as total_used minus what the
// engine itself reports (weights+kv+compute); engine-reported numbers are used
// because per-process NVML is unreliable on Windows WDDM (plan §B.2).
func OSUserFromEngine(totalUsed, weights, kv, compute uint64) uint64 {
	return SatSub(totalUsed, SatAdd(SatAdd(weights, kv), compute))
}

// KVCacheBytes = 2 * layersAttn * nKV * headDim * ctx * parallel * bytesPerElem.
// ctx is per sequence; parallel multiplies it (llama.cpp n_ctx_total).
func KVCacheBytes(layersAttn, nKV, headDim, ctx, parallel, bytesPerElem uint64) uint64 {
	return 2 * layersAttn * nKV * headDim * ctx * parallel * bytesPerElem
}

// FitStatus classifies a requirement against the allocatable bytes.
func FitStatus(required, allocatable uint64, needsRAMOffload bool) string {
	switch {
	case required > allocatable:
		return FitRed
	case needsRAMOffload:
		return FitYellow
	case allocatable == 0:
		return FitRed
	case required*10 > allocatable*9:
		return FitYellow
	default:
		return FitGreen
	}
}

// WithinTolerance implements the G-VRAM gate:
// |est-measured| <= max(5% of measured, 300 MB).
func WithinTolerance(est, measured uint64) bool {
	const mb = uint64(1_000_000)
	diff := est - measured
	if measured > est {
		diff = measured - est
	}
	tol := measured / 20
	if tol < 300*mb {
		tol = 300 * mb
	}
	return diff <= tol
}

// ModelFacts is the subset of model metadata the layer-fit maths needs.
type ModelFacts struct {
	Layers       uint64 // repeating layers
	NonRepeating uint64 // embeddings/output norm etc. bytes, placed on GPU 0
	WeightsTotal uint64 // total weights bytes (all layers + non repeating)
	LayersAttn   uint64 // layers that carry a KV cache (hybrid models: < Layers)
	NKV, HeadDim uint64
	BytesPerKV   uint64
}

// perLayerWeights is the average weight bytes of one repeating layer.
func (m ModelFacts) perLayerWeights() uint64 {
	if m.Layers == 0 {
		return 0
	}
	return SatSub(m.WeightsTotal, m.NonRepeating) / m.Layers
}

// MaxGPULayers returns how many repeating layers fit in `allocatable` bytes of
// one GPU once the non-repeating tensors, the compute buffer and the KV cache
// of those layers are accounted for. Returns (layers, fitsEntirely).
func MaxGPULayers(m ModelFacts, allocatable, compute, ctx, parallel uint64) (uint64, bool) {
	fixed := SatAdd(m.NonRepeating, compute)
	if fixed >= allocatable || m.Layers == 0 {
		return 0, false
	}
	budget := allocatable - fixed
	kvPerLayer := uint64(0)
	if m.LayersAttn > 0 {
		// KV per attention layer; apportioned per repeating layer.
		kvPerLayer = KVCacheBytes(1, m.NKV, m.HeadDim, ctx, parallel, m.BytesPerKV) *
			m.LayersAttn / m.Layers
	}
	per := m.perLayerWeights() + kvPerLayer
	if per == 0 {
		return m.Layers, true
	}
	n := budget / per
	if n >= m.Layers {
		return m.Layers, true
	}
	return n, false
}

// LayerRange is an inclusive [Start, End] layer interval on one device.
type LayerRange struct{ Start, End uint64 }

func (r LayerRange) String() string { return fmt.Sprintf("%d-%d", r.Start, r.End) }

// SplitLayers divides n layers over k devices as evenly as possible; the first
// n%k devices take one extra layer. 64 layers over 3 -> 0-21, 22-42, 43-63.
func SplitLayers(n, k uint64) []LayerRange {
	if k == 0 || n == 0 {
		return nil
	}
	out := make([]LayerRange, 0, k)
	base, extra := n/k, n%k
	var start uint64
	for i := uint64(0); i < k; i++ {
		size := base
		if i < extra {
			size++
		}
		if size == 0 {
			continue
		}
		out = append(out, LayerRange{start, start + size - 1})
		start += size
	}
	return out
}

// CalibratedLlamaCppVRAM applies the calibration rule measured on an RTX 3060
// with llama.cpp 74bca39 (benchmarks/reports/p1_vram_calibration_rtx3060.json):
// llama.cpp sizes the compute buffer from the PER-SLOT context while the KV
// cache grows with the number of slots, so the right estimate is
//
//	oneSlotVRAM + (parallel-1) * kvPerSlot
//
// where oneSlotVRAM is the parser estimate for ctx=per-slot context, np=1.
// Feeding the parser the total context instead over-estimated by up to 1.7 GB.
func CalibratedLlamaCppVRAM(oneSlotVRAM, kvPerSlot, parallel uint64) uint64 {
	if parallel == 0 {
		parallel = 1
	}
	return SatAdd(oneSlotVRAM, (parallel-1)*kvPerSlot)
}

// ObservedCUDAOverheadMiB is the gap between nvidia-smi peak and the sum of the
// CUDA buffers llama.cpp logs, measured 131-133 MiB on that machine (the plan's
// original 350-500 MB assumption did not hold). Windows/WDDM is NOT yet measured.
const ObservedCUDAOverheadMiB = 133
