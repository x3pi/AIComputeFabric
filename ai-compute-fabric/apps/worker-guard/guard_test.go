package main

import (
	"sync"
	"testing"
	"time"
)

// fakeGPU lets a test play the role of "the user" by raising external usage.
type fakeGPU struct {
	mu       sync.Mutex
	total    uint64
	external uint64
	engine   uint64
	pid      int
}

func (f *fakeGPU) Sample() (GPUSample, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s := GPUSample{TotalMiB: f.total, UsedMiB: f.external + f.engine, TempC: 50}
	if f.engine > 0 {
		s.Procs = []Proc{{f.pid, f.engine}}
	}
	return s, nil
}

type fakeEngine struct {
	g       *fakeGPU
	running bool
	size    uint64
	starts  int
}

func (e *fakeEngine) Start() error {
	e.g.mu.Lock()
	defer e.g.mu.Unlock()
	e.running, e.g.engine, e.g.pid = true, e.size, 4242
	e.starts++
	return nil
}
func (e *fakeEngine) Stop() (StopInfo, error) {
	e.g.mu.Lock()
	defer e.g.mu.Unlock()
	e.running, e.g.engine = false, 0
	return StopInfo{Signal: "SIGTERM"}, nil
}
func (e *fakeEngine) Running() bool    { return e.running }
func (e *fakeEngine) PID() int         { return 4242 }
func (e *fakeEngine) Buffers() Buffers { return Buffers{ModelMiB: 4700, KVMiB: 576, ComputeMiB: 300} }

func newRig(t *testing.T) (*Guard, *fakeGPU, *fakeEngine, *time.Time) {
	t.Helper()
	gpu := &fakeGPU{total: 12288}
	eng := &fakeEngine{g: gpu, size: 6000}
	g := NewGuard(Config{HeadroomMiB: 1536, ResumeStable: 10 * time.Second, Poll: time.Millisecond}, gpu, eng)
	clock := time.Unix(1000, 0)
	g.now = func() time.Time { return clock }
	return g, gpu, eng, &clock
}

func TestStartsWhenGPUIsFreeAfterStableWindow(t *testing.T) {
	g, _, eng, clock := newRig(t)
	g.Tick()
	if eng.starts != 0 {
		t.Fatal("must wait for the stable window, not start on first sample")
	}
	*clock = clock.Add(11 * time.Second)
	g.Tick()
	if eng.starts != 1 || g.st != StRunning {
		t.Fatalf("starts=%d state=%s", eng.starts, g.st)
	}
}

func TestReclaimWhenUserTakesVRAMThenResume(t *testing.T) {
	g, gpu, eng, clock := newRig(t)
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick() // start
	g.Tick() // record own
	gpu.mu.Lock()
	gpu.external = 12288 - 6000 - 1000 // free = 1000 < 1536 headroom
	gpu.mu.Unlock()
	g.Tick()
	if g.st != StYielded || eng.running {
		t.Fatalf("state=%s running=%v, expected yielded", g.st, eng.running)
	}
	var rec *Event
	for i := range g.events {
		if g.events[i].Kind == "reclaim" {
			rec = &g.events[i]
		}
	}
	if rec == nil || rec.Signal != "SIGTERM" {
		t.Fatalf("missing reclaim event: %+v", g.events)
	}
	// User still holds the GPU: no restart however long we wait.
	*clock = clock.Add(60 * time.Second)
	g.Tick()
	if eng.starts != 1 {
		t.Fatal("must not restart while the user still needs the GPU")
	}
	// User leaves; restart only after the stable window.
	gpu.mu.Lock()
	gpu.external = 0
	gpu.mu.Unlock()
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick()
	if eng.starts != 2 {
		t.Fatalf("expected restart after user left, starts=%d", eng.starts)
	}
}

func TestPauseBlocksRestartUntilResume(t *testing.T) {
	g, _, eng, clock := newRig(t)
	g.Pause()
	*clock = clock.Add(60 * time.Second)
	g.Tick()
	if eng.starts != 0 {
		t.Fatal("paused worker must not start")
	}
	g.Resume()
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick()
	if eng.starts != 1 {
		t.Fatal("must start after resume")
	}
}

func TestSnapshotBucketsSumToTotal(t *testing.T) {
	g, _, _, clock := newRig(t)
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick()
	g.Tick()
	b := g.Snapshot().Buckets
	if sum := b.OSUser + b.Weights + b.KVState + b.Compute + b.Headroom + b.Allocatable; sum != b.Total {
		t.Fatalf("sum %d != total %d", sum, b.Total)
	}
	if b.Weights == 0 {
		t.Fatal("weights bucket must come from engine-reported buffers")
	}
}

func TestParseBuffers(t *testing.T) {
	var b Buffers
	for _, l := range []string{
		"load_tensors:        CUDA0 model buffer size =  4455.04 MiB",
		"load_tensors:   CUDA_Host model buffer size =   303.75 MiB",
		"llama_kv_cache:      CUDA0 KV buffer size =   576.00 MiB",
		"sched_reserve:      CUDA0 compute buffer size =   296.75 MiB",
	} {
		ParseBuffers(l, &b)
	}
	if b.ModelMiB != 4455.04 || b.KVMiB != 576 || b.ComputeMiB != 296.75 {
		t.Fatalf("%+v", b)
	}
}
