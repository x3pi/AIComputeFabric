package main

import (
	"encoding/json"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	estimator "github.com/aicf/ai-compute-fabric/core/vram-estimator"
)

const mib = uint64(1024 * 1024)

// CatalogModel describes a curated Hugging Face / GGUF model and its architectural
// facts used by the GPUStack-inspired 6-bucket VRAM estimator.
type CatalogModel struct {
	ID                string `json:"id"`
	Name              string `json:"name"`
	Family            string `json:"family"`
	Architecture      string `json:"architecture"`
	Quantization      string `json:"quantization"`
	GGUFFile          string `json:"gguf_file"`
	Layers            uint64 `json:"layers"`
	LayersAttn        uint64 `json:"layers_attn"`
	NKV               uint64 `json:"n_kv"`
	HeadDim           uint64 `json:"head_dim"`
	BytesPerKV        uint64 `json:"bytes_per_kv"`
	WeightsBytes      uint64 `json:"weights_bytes"`
	NonRepeatingBytes uint64 `json:"non_repeating_bytes"`
	Confidence        string `json:"confidence"`
	CalibrationNote   string `json:"calibration_note"`
	RecommendedGPUMiB uint64 `json:"recommended_gpu_mib"`
	License           string `json:"license"`
}

var modelCatalog = []CatalogModel{
	{
		ID:                "Qwen/Qwen3-4B-GGUF",
		Name:              "Qwen3-4B (Q4_K_M)",
		Family:            "Qwen3",
		Architecture:      "Dense GQA (36L, 8 KV heads)",
		Quantization:      "Q4_K_M",
		GGUFFile:          "Qwen3-4B-Q4_K_M.gguf",
		Layers:            36,
		LayersAttn:        36,
		NKV:               8,
		HeadDim:           128,
		BytesPerKV:        2,
		WeightsBytes:      2491322368, // 2375.91 MiB logged by llama.cpp (p1_vram_calibration_rtx3060.json)
		NonRepeatingBytes: 320 * mib,
		Confidence:        "CALIBRATED",
		CalibrationNote:   "Hiệu chuẩn thực nghiệm trên RTX 3060 12GB (llama.cpp 74bca39): 14/14 cấu hình đạt ngưỡng G-VRAM (sai số tối đa 195 MiB).",
		RecommendedGPUMiB: 8192,
		License:           "Apache-2.0",
	},
	{
		ID:                "Qwen/Qwen3-8B-GGUF",
		Name:              "Qwen3-8B (Q4_K_M)",
		Family:            "Qwen3",
		Architecture:      "Dense GQA (36L, 8 KV heads)",
		Quantization:      "Q4_K_M",
		GGUFFile:          "Qwen3-8B-Q4_K_M.gguf",
		Layers:            36,
		LayersAttn:        36,
		NKV:               8,
		HeadDim:           128,
		BytesPerKV:        2,
		WeightsBytes:      4671762432, // 4455.34 MiB logged by llama.cpp (p1_vram_calibration_rtx3060.json)
		NonRepeatingBytes: 640 * mib,
		Confidence:        "CALIBRATED",
		CalibrationNote:   "Hiệu chuẩn thực nghiệm trên RTX 3060 12GB (llama.cpp 74bca39): 14/14 cấu hình đạt ngưỡng G-VRAM (sai số tối đa 197 MiB).",
		RecommendedGPUMiB: 12288,
		License:           "Apache-2.0",
	},
	{
		ID:                "Qwen/Qwen2.5-32B-Instruct-GGUF",
		Name:              "Qwen2.5-32B-Instruct (Q4_K_M)",
		Family:            "Qwen2.5",
		Architecture:      "Dense GQA (64L, 8 KV heads)",
		Quantization:      "Q4_K_M",
		GGUFFile:          "qwen2.5-32b-instruct-q4_k_m-00001-of-00005.gguf",
		Layers:            64,
		LayersAttn:        64,
		NKV:               8,
		HeadDim:           128,
		BytesPerKV:        2,
		WeightsBytes:      19845358592, // 18926.01 MiB total across GPU+host (p1_vram_hybrid_32b_rtx3060.json)
		NonRepeatingBytes: 597 * mib,
		Confidence:        "CALIBRATED",
		CalibrationNote:   "Đo kiểm thực tế chế độ LOCAL_HYBRID (ngl=24..36) trên RTX 3060 12GB (5/5 cấu hình đạt G-VRAM, sai số ~258 MiB).",
		RecommendedGPUMiB: 24576,
		License:           "Apache-2.0",
	},
	{
		ID:                "Qwen/Qwen3-30B-A3B-GGUF",
		Name:              "Qwen3-30B-A3B MoE (Q4_K_M)",
		Family:            "Qwen3 MoE",
		Architecture:      "Sparse MoE 128E/8A (48L, 4 KV heads)",
		Quantization:      "Q4_K_M",
		GGUFFile:          "Qwen3-30B-A3B-Q4_K_M.gguf",
		Layers:            48,
		LayersAttn:        48,
		NKV:               4,
		HeadDim:           128,
		BytesPerKV:        2,
		WeightsBytes:      17750 * mib,
		NonRepeatingBytes: 620 * mib,
		Confidence:        "ESTIMATED",
		CalibrationNote:   "Tính theo cấu trúc Hugging Face Qwen/Qwen3-30B-A3B (48 layers, 4 KV heads, head_dim=128) và công thức core/vram-estimator.",
		RecommendedGPUMiB: 24576,
		License:           "Apache-2.0",
	},
	{
		ID:                "Qwen/Qwen3.5-35B-A3B-GGUF",
		Name:              "Qwen3.5-35B-A3B Hybrid (Q4_K_M)",
		Family:            "Qwen3.5 Hybrid",
		Architecture:      "Gated DeltaNet + Sparse MoE (40L, 10 Full-Attn)",
		Quantization:      "Q4_K_M",
		GGUFFile:          "Qwen3.5-35B-A3B-Q4_K_M.gguf",
		Layers:            40,
		LayersAttn:        10, // full_attention_interval = 4 -> 10 full attention layers + 30 linear recurrent layers
		NKV:               2,
		HeadDim:           256,
		BytesPerKV:        2,
		WeightsBytes:      20400 * mib,
		NonRepeatingBytes: 780 * mib,
		Confidence:        "ESTIMATED",
		CalibrationNote:   "Kiến trúc Hybrid Gated DeltaNet (full_attention_interval=4): chỉ 10/40 layer giữ KV cache tiêu chuẩn; tiết kiệm 75% bộ nhớ KV ở ngữ cảnh dài.",
		RecommendedGPUMiB: 24576,
		License:           "Apache-2.0",
	},
}

func findCatalogModel(id string) CatalogModel {
	q := strings.ToLower(strings.TrimSpace(id))
	for _, m := range modelCatalog {
		if strings.ToLower(m.ID) == q || strings.ToLower(m.GGUFFile) == q || strings.ToLower(m.Name) == q {
			return m
		}
	}
	for _, m := range modelCatalog {
		if strings.Contains(strings.ToLower(m.ID), q) || strings.Contains(strings.ToLower(m.GGUFFile), q) {
			return m
		}
	}
	return modelCatalog[1] // default Qwen3-8B
}

// EstimateRequest represents the parameters for the 6-bucket VRAM calculator.
type EstimateRequest struct {
	ModelID        string `json:"model_id"`
	CtxPerSlot     uint64 `json:"ctx_per_slot"`
	Parallel       uint64 `json:"parallel"`
	FlashAttn      bool   `json:"flash_attn"`
	KVBytesPerElem uint64 `json:"kv_bytes_per_elem"`
	GPUTotalMiB    uint64 `json:"gpu_total_mib"`
	OSUserMiB      uint64 `json:"os_user_mib"`
	HeadroomMiB    uint64 `json:"headroom_mib"`
	GPUCount       uint64 `json:"gpu_count"`
}

// LayerSplitView represents one GPU's layer assignment in multi-GPU or single-GPU mode.
type LayerSplitView struct {
	GPUIndex   uint64 `json:"gpu_index"`
	StartLayer uint64 `json:"start_layer"`
	EndLayer   uint64 `json:"end_layer"`
	LayerCount uint64 `json:"layer_count"`
	RangeLabel string `json:"range_label"`
	VRAMBytes  uint64 `json:"vram_bytes"`
}

// EstimateResponse is returned by /api/vram/estimate.
type EstimateResponse struct {
	Model                   CatalogModel      `json:"model"`
	CtxPerSlot              uint64            `json:"ctx_per_slot"`
	Parallel                uint64            `json:"parallel"`
	FlashAttn               bool              `json:"flash_attn"`
	KVBytesPerElem          uint64            `json:"kv_bytes_per_elem"`
	GPUCount                uint64            `json:"gpu_count"`
	KVPerSlotBytes          uint64            `json:"kv_per_slot_bytes"`
	KVTotalBytes            uint64            `json:"kv_total_bytes"`
	ComputeBytes            uint64            `json:"compute_bytes"`
	CUDAOverheadBytes       uint64            `json:"cuda_overhead_bytes"`
	OneSlotVRAMBytes        uint64            `json:"one_slot_vram_bytes"`
	CalibratedVRAMBytes     uint64            `json:"calibrated_vram_bytes"`
	RequiredPerGPUBytes     uint64            `json:"required_per_gpu_bytes"`
	AllocatablePerGPUBytes  uint64            `json:"allocatable_per_gpu_bytes"`
	BucketsBytes            estimator.Buckets `json:"buckets_bytes"`
	FitStatus               string            `json:"fit_status"`
	ExecutionMode           string            `json:"execution_mode"`
	MaxGPULayers            uint64            `json:"max_gpu_layers"`
	FitsEntirely            bool              `json:"fits_entirely"`
	OffloadRAMBytes         uint64            `json:"offload_ram_bytes"`
	LayerSplits             []LayerSplitView  `json:"layer_splits"`
	CompatibleWorkersOnline int               `json:"compatible_workers_online"`
	Confidence              string            `json:"confidence"`
}

// computeBufferBytes estimates the llama.cpp compute buffer + observed CUDA context overhead
// from the RTX 3060 calibration curves in benchmarks/reports/p1_vram_calibration_rtx3060.json.
func computeBufferBytes(m CatalogModel, ctxPerSlot uint64, flashAttn bool) (computeBuf, cudaOverhead uint64) {
	cudaOverhead = estimator.ObservedCUDAOverheadMiB * mib
	baseMiB := uint64(80)
	if m.WeightsBytes > 10*1024*mib {
		baseMiB = 220
	} else if m.WeightsBytes > 4*1024*mib {
		baseMiB = 104
	}
	if flashAttn {
		// With flash-attn on, compute buffer grows very slowly with per-slot context (~1 MiB per 1024 tokens).
		extraMiB := ctxPerSlot / 1024
		return (baseMiB + extraMiB) * mib, cudaOverhead
	}
	// Without flash-attn, compute buffer scales linearly with per-slot context (~64 MiB per 1024 tokens).
	noFAMiB := baseMiB + (ctxPerSlot*64)/1024
	return noFAMiB * mib, cudaOverhead
}

func computeEstimate(req EstimateRequest, f *Fleet) EstimateResponse {
	return computeEstimateFor(findCatalogModel(req.ModelID), req, f)
}

func computeEstimateFor(m CatalogModel, req EstimateRequest, f *Fleet) EstimateResponse {
	if req.CtxPerSlot == 0 {
		req.CtxPerSlot = 8192
	}
	if req.CtxPerSlot > 131072 {
		req.CtxPerSlot = 131072
	}
	if req.Parallel == 0 {
		req.Parallel = 4
	}
	if req.Parallel > 32 {
		req.Parallel = 32
	}
	if req.KVBytesPerElem == 0 {
		req.KVBytesPerElem = m.BytesPerKV
	}
	if req.GPUTotalMiB == 0 {
		req.GPUTotalMiB = 12288
	}
	if req.HeadroomMiB == 0 {
		req.HeadroomMiB = 1536
	}
	if req.GPUCount == 0 {
		req.GPUCount = 1
	}
	if req.GPUCount > 8 {
		req.GPUCount = 8
	}

	facts := estimator.ModelFacts{
		Layers:       m.Layers,
		NonRepeating: m.NonRepeatingBytes,
		WeightsTotal: m.WeightsBytes,
		LayersAttn:   m.LayersAttn,
		NKV:          m.NKV,
		HeadDim:      m.HeadDim,
		BytesPerKV:   req.KVBytesPerElem,
	}

	kvOneSlot := estimator.KVCacheBytes(m.LayersAttn, m.NKV, m.HeadDim, req.CtxPerSlot, 1, req.KVBytesPerElem)
	kvTotal := estimator.KVCacheBytes(m.LayersAttn, m.NKV, m.HeadDim, req.CtxPerSlot, req.Parallel, req.KVBytesPerElem)
	computeBuf, cudaOverhead := computeBufferBytes(m, req.CtxPerSlot, req.FlashAttn)
	totalCompute := estimator.SatAdd(computeBuf, cudaOverhead)

	oneSlotVRAM := estimator.SatAdd(estimator.SatAdd(m.WeightsBytes, kvOneSlot), totalCompute)
	calibratedVRAM := estimator.CalibratedLlamaCppVRAM(oneSlotVRAM, kvOneSlot, req.Parallel)

	gpuTotalBytes := req.GPUTotalMiB * mib
	osUserBytes := req.OSUserMiB * mib
	headroomBytes := req.HeadroomMiB * mib
	allocatablePerGPU := estimator.SatSub(gpuTotalBytes, estimator.SatAdd(osUserBytes, headroomBytes))

	maxLayers, fitsSingleGPU := estimator.MaxGPULayers(facts, allocatablePerGPU, totalCompute, req.CtxPerSlot, req.Parallel)

	splits := estimator.SplitLayers(m.Layers, req.GPUCount)
	splitViews := make([]LayerSplitView, 0, len(splits))
	repeatingWeights := estimator.SatSub(m.WeightsBytes, m.NonRepeatingBytes)

	var maxPerGPUReq uint64
	for idx, r := range splits {
		count := r.End - r.Start + 1
		devWeights := (repeatingWeights * count) / m.Layers
		if idx == 0 {
			devWeights = estimator.SatAdd(devWeights, m.NonRepeatingBytes)
		}
		devKV := (kvTotal * count) / m.Layers
		devVRAM := estimator.SatAdd(estimator.SatAdd(devWeights, devKV), totalCompute)
		if devVRAM > maxPerGPUReq {
			maxPerGPUReq = devVRAM
		}
		splitViews = append(splitViews, LayerSplitView{
			GPUIndex:   uint64(idx),
			StartLayer: r.Start,
			EndLayer:   r.End,
			LayerCount: count,
			RangeLabel: r.String(),
			VRAMBytes:  devVRAM,
		})
	}
	if maxPerGPUReq == 0 {
		maxPerGPUReq = calibratedVRAM
	}

	// Determine execution mode and fit status
	execMode := "LOCAL_FULL"
	needsRAMOffload := false
	var offloadRAMBytes uint64

	if req.GPUCount > 1 {
		execMode = "DISTRIBUTED_MODEL"
	} else if !fitsSingleGPU {
		if maxLayers >= m.Layers/3 && maxLayers > 0 {
			execMode = "LOCAL_HYBRID"
			needsRAMOffload = true
			offloadedLayers := m.Layers - maxLayers
			offloadedWeights := (repeatingWeights * offloadedLayers) / m.Layers
			offloadedKV := (kvTotal * offloadedLayers) / m.Layers
			// Include host buffer overhead observed in p1_vram_hybrid_32b_rtx3060.json
			offloadRAMBytes = estimator.SatAdd(offloadedWeights, offloadedKV)
		}
	}

	var fit string
	if req.GPUCount == 1 && execMode == "LOCAL_HYBRID" {
		fit = estimator.FitYellow
	} else {
		fit = estimator.FitStatus(maxPerGPUReq, allocatablePerGPU, needsRAMOffload)
	}

	// Build the 6-bucket view for GPU 0 (or hybrid capped on GPU 0)
	bWeights := m.WeightsBytes
	bKV := kvTotal
	if len(splitViews) > 1 {
		count := splitViews[0].LayerCount
		bWeights = estimator.SatAdd((repeatingWeights*count)/m.Layers, m.NonRepeatingBytes)
		bKV = (kvTotal * count) / m.Layers
	} else if execMode == "LOCAL_HYBRID" && maxLayers < m.Layers {
		bWeights = estimator.SatAdd((repeatingWeights*maxLayers)/m.Layers, m.NonRepeatingBytes)
		bKV = (kvTotal * maxLayers) / m.Layers
	}
	buckets := estimator.NewBuckets(gpuTotalBytes, osUserBytes, bWeights, bKV, totalCompute, headroomBytes)

	compatibleOnline := 0
	if f != nil {
		ws, _ := f.Page(0, 500, "")
		for _, w := range ws {
			if !w.Online {
				continue
			}
			avail := w.VRAMFree
			if w.Buckets != nil && w.Buckets.Allocatable > 0 {
				avail = estimator.SatAdd(w.Buckets.Allocatable, estimator.SatAdd(w.Buckets.Weights, estimator.SatAdd(w.Buckets.KVState, w.Buckets.Compute)))
			}
			if avail >= maxPerGPUReq {
				compatibleOnline++
			}
		}
	}

	return EstimateResponse{
		Model:                   m,
		CtxPerSlot:              req.CtxPerSlot,
		Parallel:                req.Parallel,
		FlashAttn:               req.FlashAttn,
		KVBytesPerElem:          req.KVBytesPerElem,
		GPUCount:                req.GPUCount,
		KVPerSlotBytes:          kvOneSlot,
		KVTotalBytes:            kvTotal,
		ComputeBytes:            totalCompute,
		CUDAOverheadBytes:       cudaOverhead,
		OneSlotVRAMBytes:        oneSlotVRAM,
		CalibratedVRAMBytes:     calibratedVRAM,
		RequiredPerGPUBytes:     maxPerGPUReq,
		AllocatablePerGPUBytes:  allocatablePerGPU,
		BucketsBytes:            buckets,
		FitStatus:               fit,
		ExecutionMode:           execMode,
		MaxGPULayers:            maxLayers,
		FitsEntirely:            fitsSingleGPU || (req.GPUCount > 1 && maxPerGPUReq <= allocatablePerGPU),
		OffloadRAMBytes:         offloadRAMBytes,
		LayerSplits:             splitViews,
		CompatibleWorkersOnline: compatibleOnline,
		Confidence:              m.Confidence,
	}
}

// ClusterVRAMPool aggregates the 6-bucket VRAM telemetry across all online workers.
type ClusterVRAMPool struct {
	Total       uint64 `json:"total_bytes"`
	Used        uint64 `json:"used_bytes"`
	Free        uint64 `json:"free_bytes"`
	OSUser      uint64 `json:"os_user_bytes"`
	Weights     uint64 `json:"weights_bytes"`
	KVState     uint64 `json:"kv_state_bytes"`
	Compute     uint64 `json:"compute_bytes"`
	Headroom    uint64 `json:"headroom_bytes"`
	Allocatable uint64 `json:"allocatable_bytes"`
}

// RouterStatsView exposes live data-plane metrics in JSON for the dashboard.
type RouterStatsView struct {
	Inflight       int               `json:"inflight"`
	Queued         int               `json:"queued"`
	RequestsTotal  uint64            `json:"requests_total"`
	ServedTotal    uint64            `json:"served_total"`
	FailoversTotal uint64            `json:"failovers_total"`
	AvgQueueWaitMs float64           `json:"avg_queue_wait_ms"`
	Rejected       map[string]uint64 `json:"rejected"`
	ServedByTenant map[string]uint64 `json:"served_by_tenant"`
}

// ClusterStatsResponse is returned by GET /api/stats.
type ClusterStatsResponse struct {
	At                int64           `json:"at"`
	WorkersTotal      int             `json:"workers_total"`
	WorkersOnline     int             `json:"workers_online"`
	WorkersRunning    int             `json:"workers_running"`
	WorkersReclaiming int             `json:"workers_reclaiming"`
	WorkersYielded    int             `json:"workers_yielded"`
	WorkersPaused     int             `json:"workers_paused"`
	WorkersOffline    int             `json:"workers_offline"`
	Counts            map[string]int  `json:"counts"`
	VRAMPool          ClusterVRAMPool `json:"vram_pool"`
	TempAvgC          float64         `json:"temp_avg_c"`
	TempMaxC          uint32          `json:"temp_max_c"`
	ReclaimP95Ms      float64         `json:"reclaim_p95_ms"`
	ReclaimMaxMs      float64         `json:"reclaim_max_ms"`
	TenantsActive     int             `json:"tenants_active"`
	Router            RouterStatsView `json:"router"`
}

func buildClusterStats(f *Fleet, ts *TenantStore, rt *Router) ClusterStatsResponse {
	res := ClusterStatsResponse{
		At:     time.Now().UnixMilli(),
		Counts: map[string]int{},
		Router: RouterStatsView{
			Rejected:       map[string]uint64{},
			ServedByTenant: map[string]uint64{},
		},
	}
	if f != nil {
		ws, _ := f.Page(0, 5000, "")
		res.WorkersTotal = len(ws)
		var tempSum uint64
		var tempCount int
		var reclaims []float64
		for _, w := range ws {
			res.Counts[w.State]++
			if !w.Online {
				res.WorkersOffline++
				continue
			}
			res.WorkersOnline++
			switch w.State {
			case "running":
				res.WorkersRunning++
			case "reclaiming":
				res.WorkersReclaiming++
			case "yielded":
				res.WorkersYielded++
			case "paused", "stopped":
				res.WorkersPaused++
			}
			res.VRAMPool.Total = estimator.SatAdd(res.VRAMPool.Total, w.VRAMTotal)
			res.VRAMPool.Free = estimator.SatAdd(res.VRAMPool.Free, w.VRAMFree)
			used := estimator.SatSub(w.VRAMTotal, w.VRAMFree)
			res.VRAMPool.Used = estimator.SatAdd(res.VRAMPool.Used, used)
			if w.Buckets != nil && w.Buckets.Total > 0 {
				res.VRAMPool.OSUser = estimator.SatAdd(res.VRAMPool.OSUser, w.Buckets.OSUser)
				res.VRAMPool.Weights = estimator.SatAdd(res.VRAMPool.Weights, w.Buckets.Weights)
				res.VRAMPool.KVState = estimator.SatAdd(res.VRAMPool.KVState, w.Buckets.KVState)
				res.VRAMPool.Compute = estimator.SatAdd(res.VRAMPool.Compute, w.Buckets.Compute)
				res.VRAMPool.Headroom = estimator.SatAdd(res.VRAMPool.Headroom, w.Buckets.Headroom)
				res.VRAMPool.Allocatable = estimator.SatAdd(res.VRAMPool.Allocatable, w.Buckets.Allocatable)
			} else {
				// Fallback decomposition when worker has not yet reported fine-grained buckets
				res.VRAMPool.Weights = estimator.SatAdd(res.VRAMPool.Weights, used)
				res.VRAMPool.Allocatable = estimator.SatAdd(res.VRAMPool.Allocatable, w.VRAMFree)
			}
			if w.TempC > 0 {
				tempSum += uint64(w.TempC)
				tempCount++
				if w.TempC > res.TempMaxC {
					res.TempMaxC = w.TempC
				}
			}
			if w.LastReclaim > 0 {
				reclaims = append(reclaims, w.LastReclaim)
				if w.LastReclaim > res.ReclaimMaxMs {
					res.ReclaimMaxMs = w.LastReclaim
				}
			}
		}
		if tempCount > 0 {
			res.TempAvgC = math.Round((float64(tempSum)/float64(tempCount))*10) / 10
		}
		if len(reclaims) > 0 {
			sort.Float64s(reclaims)
			idx := int(math.Ceil(0.95*float64(len(reclaims)))) - 1
			if idx < 0 {
				idx = 0
			}
			res.ReclaimP95Ms = math.Round(reclaims[idx]*10) / 10
			res.ReclaimMaxMs = math.Round(res.ReclaimMaxMs*10) / 10
		}
	}
	if ts != nil {
		for _, t := range ts.List() {
			if !t.Disabled {
				res.TenantsActive++
			}
		}
	}
	if rt != nil {
		inflight, queued, s := rt.Snapshot()
		res.Router.Inflight = inflight
		res.Router.Queued = queued
		res.Router.RequestsTotal = s.Requests
		res.Router.ServedTotal = s.Served
		res.Router.FailoversTotal = s.Failovers
		if s.Served > 0 {
			res.Router.AvgQueueWaitMs = math.Round((float64(s.WaitNanosTotal)/1e6/float64(s.Served))*10) / 10
		}
		res.Router.Rejected = map[string]uint64{
			"queue_full":        s.RejectedQueueFull,
			"tenant_queue_full": s.RejectedTenantQueue,
			"deadline":          s.RejectedDeadline,
			"wait_timeout":      s.RejectedWait,
			"no_worker":         s.RejectedNoWorker,
			"rate_limit":        s.RejectedRate,
		}
		res.Router.ServedByTenant = rt.ServedByTenant()
	}
	return res
}

func registerEstimatorAndStatsRoutes(mux *http.ServeMux, f *Fleet, ts *TenantStore, rt *Router) {
	mux.HandleFunc("/api/models/catalog", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		writeJSON(w, http.StatusOK, modelCatalog)
	})
	mux.HandleFunc("/api/vram/estimate", func(w http.ResponseWriter, r *http.Request) {
		req := EstimateRequest{
			ModelID:        "Qwen/Qwen3-8B-GGUF",
			CtxPerSlot:     8192,
			Parallel:       4,
			FlashAttn:      true,
			KVBytesPerElem: 2,
			GPUTotalMiB:    12288,
			OSUserMiB:      512,
			HeadroomMiB:    1536,
			GPUCount:       1,
		}
		switch r.Method {
		case http.MethodGet:
			q := r.URL.Query()
			if v := q.Get("model_id"); v != "" {
				req.ModelID = v
			}
			if v, err := strconv.ParseUint(q.Get("ctx_per_slot"), 10, 64); err == nil && v > 0 {
				req.CtxPerSlot = v
			}
			if v, err := strconv.ParseUint(q.Get("parallel"), 10, 64); err == nil && v > 0 {
				req.Parallel = v
			}
			if v := q.Get("flash_attn"); v != "" {
				req.FlashAttn = v != "false" && v != "0"
			}
			if v, err := strconv.ParseUint(q.Get("kv_bytes_per_elem"), 10, 64); err == nil && (v == 1 || v == 2) {
				req.KVBytesPerElem = v
			}
			if v, err := strconv.ParseUint(q.Get("gpu_total_mib"), 10, 64); err == nil && v > 0 {
				req.GPUTotalMiB = v
			}
			if v, err := strconv.ParseUint(q.Get("os_user_mib"), 10, 64); err == nil {
				req.OSUserMiB = v
			}
			if v, err := strconv.ParseUint(q.Get("headroom_mib"), 10, 64); err == nil && v > 0 {
				req.HeadroomMiB = v
			}
			if v, err := strconv.ParseUint(q.Get("gpu_count"), 10, 64); err == nil && v > 0 {
				req.GPUCount = v
			}
		case http.MethodPost:
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req) != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
		default:
			http.Error(w, "GET or POST", http.StatusMethodNotAllowed)
			return
		}
		writeJSON(w, http.StatusOK, computeEstimate(req, f))
	})
	mux.HandleFunc("/api/v1/models/estimate-vram", estimateFromGGUF(f))
	mux.HandleFunc("/api/stats", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		writeJSON(w, http.StatusOK, buildClusterStats(f, ts, rt))
	})
}
