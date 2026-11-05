package main

import (
	"encoding/json"
	"os"
	"strconv"
	"testing"
	"time"
)

// realStubEngine stands in for llama-server: it "runs" and records whether the guard stopped it. The GPU, the sampler (nvidia-smi) and the
// guests (real containers) are real; only the LLM process is a stub, because this test must never touch a running llama-server.
type realStubEngine struct{ stops int }

func (e *realStubEngine) Start() error             { return nil }
func (e *realStubEngine) Stop() (StopInfo, error)  { e.stops++; return StopInfo{Signal: "stub"}, nil }
func (e *realStubEngine) Running() bool            { return true }
func (e *realStubEngine) PID() int                 { return 1 }
func (e *realStubEngine) Buffers() Buffers         { return Buffers{} }
func (e *realStubEngine) Configure(string, string) {}
func (e *realStubEngine) Describe() (string, string) {
	return "", ""
}

// Driven by tests/serverless/test_gpu_real.py (G-RECLAIM). Skipped unless AICF_SL_REAL_URL is set.
func TestRealGPUReclaimEvictsGuestsNotEngine(t *testing.T) {
	url := os.Getenv("AICF_SL_REAL_URL")
	if url == "" {
		t.Skip("AICF_SL_REAL_URL not set (driven by tests/serverless/test_gpu_real.py)")
	}
	smp := SMISampler{Index: 0}
	s0, err := smp.Sample()
	if err != nil {
		t.Fatal(err)
	}
	extra, _ := strconv.Atoi(os.Getenv("AICF_SL_REAL_EXTRA_MIB")) // headroom = free now + extra: only the guests leaving can satisfy it
	if extra == 0 {
		extra = 1024
	}
	eng := &realStubEngine{}
	ev := &HTTPEvictor{URL: url, Secret: os.Getenv("AICF_SL_REAL_SECRET"), GPU: 0}
	g := NewGuard(Config{HeadroomMiB: s0.FreeMiB() + uint64(extra), Evictor: ev, EvictTimeout: 400 * time.Millisecond, EvictWait: 3 * time.Second, FreedTimeout: 200 * time.Millisecond}, smp, eng)
	g.st = StRunning
	t0 := time.Now()
	g.Tick()
	el := time.Since(t0)
	res := map[string]any{"tick_ms": float64(el.Microseconds()) / 1000, "engine_stops": eng.stops, "state": string(g.st), "free_before_mib": s0.FreeMiB()}
	for _, e := range g.events {
		res["event_"+e.Kind] = e.Detail
		if e.Kind == "evict_serverless" {
			res["detect_to_freed_ms"] = e.DetectMS
		}
	}
	b, _ := json.Marshal(res)
	t.Logf("REAL_RESULT %s", b)
}
