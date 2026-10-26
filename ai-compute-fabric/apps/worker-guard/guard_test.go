package main

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// fakeGPU lets a test play the role of "the user" by raising external usage.
type fakeGPU struct {
	mu       sync.Mutex
	total    uint64
	external uint64
	engine   uint64
	pid      int

	hideProcs bool // Windows/WDDM: per-process VRAM is not available
	freeAfter int  // samples the driver needs after Stop before the memory is really free
	pending   int
	samples   int
}

func (f *fakeGPU) Sample() (GPUSample, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.samples++
	if f.pending > 0 {
		if f.pending--; f.pending == 0 {
			f.engine = 0
		}
	}
	s := GPUSample{TotalMiB: f.total, UsedMiB: f.external + f.engine, TempC: 50}
	if f.engine > 0 && !f.hideProcs {
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
	e.running = false
	if e.g.freeAfter > 0 {
		e.g.pending = e.g.freeAfter
		e.g.samples = 0
	} else {
		e.g.engine = 0
	}
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

// --- reporter / gateway directive ---

type fakeGateway struct {
	pb.UnimplementedFleetGatewayServer
	mu       sync.Mutex
	pause    bool
	known    bool
	got      []*pb.HeartbeatRequest
	regCount int
}

func (f *fakeGateway) Register(context.Context, *pb.RegisterRequest) (*pb.RegisterReply, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.known = true
	f.regCount++
	return &pb.RegisterReply{HeartbeatIntervalMs: 1000}, nil
}
func (f *fakeGateway) Heartbeat(_ context.Context, r *pb.HeartbeatRequest) (*pb.HeartbeatReply, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.got = append(f.got, r)
	return &pb.HeartbeatReply{Registered: f.known, PauseRequested: f.pause}, nil
}

func reporterRig(t *testing.T) (*Reporter, *fakeGateway, *Guard, *fakeEngine, *time.Time) {
	t.Helper()
	g, _, eng, clock := newRig(t)
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick() // running
	fg := &fakeGateway{}
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := grpc.NewServer()
	pb.RegisterFleetGatewayServer(s, fg)
	go s.Serve(lis)
	t.Cleanup(s.Stop)
	conn, _ := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	t.Cleanup(func() { conn.Close() })
	return &Reporter{Client: pb.NewFleetGatewayClient(conn), G: g, WorkerID: "w1", Model: "m"}, fg, g, eng, clock
}

func TestReporterSendsBucketsAndAppliesPauseThenResume(t *testing.T) {
	r, fg, g, eng, _ := reporterRig(t)
	ctx := context.Background()
	if err := r.Step(ctx); err != nil {
		t.Fatal(err)
	}
	hb := fg.got[len(fg.got)-1]
	if hb.State != "running" || hb.Capacity.Vram.GetWeightsBytes() == 0 || hb.Capacity.WorkerId != "w1" {
		t.Fatalf("%+v", hb)
	}
	fg.mu.Lock()
	fg.pause = true
	fg.mu.Unlock()
	r.Step(ctx)
	if g.st != StPaused || eng.running {
		t.Fatalf("gateway pause must stop the engine: state=%s running=%v", g.st, eng.running)
	}
	fg.mu.Lock()
	fg.pause = false
	fg.mu.Unlock()
	r.Step(ctx)
	if g.st != StStopped {
		t.Fatalf("gateway resume must lift the pause, state=%s", g.st)
	}
}

func TestReporterDoesNotUndoLocalPauseAndReRegisters(t *testing.T) {
	r, fg, g, _, _ := reporterRig(t)
	ctx := context.Background()
	r.Step(ctx)
	g.Pause() // operator pressed pause on the machine itself
	r.Step(ctx)
	if g.st != StPaused {
		t.Fatal("gateway without a directive must not resume a locally paused worker")
	}
	fg.mu.Lock()
	fg.known = false // gateway restarted
	fg.mu.Unlock()
	r.Step(ctx)
	r.Step(ctx)
	if fg.regCount != 2 {
		t.Fatalf("worker must re-register after the gateway forgot it, registrations=%d", fg.regCount)
	}
}

func TestPollCadenceSpeedsUpOnlyNearHeadroom(t *testing.T) {
	g, gpu, _, _ := newRig(t)
	g.cfg.Poll, g.cfg.FastPoll, g.cfg.NearMarginMiB = 500*time.Millisecond, 100*time.Millisecond, 2048
	g.Tick() // GPU empty: 12288 MiB free
	if got := g.NextInterval(); got != 500*time.Millisecond {
		t.Fatalf("idle GPU should poll slowly, got %v", got)
	}
	gpu.mu.Lock()
	gpu.external = 12288 - 1536 - 1000 // free = 2536 < headroom + 2048
	gpu.mu.Unlock()
	g.Tick()
	if got := g.NextInterval(); got != 100*time.Millisecond {
		t.Fatalf("near the headroom should poll fast, got %v", got)
	}
}

// Windows/WDDM: nvidia-smi cannot say how much VRAM the engine holds, so the guard must use the
// engine's own report as the target and wait until that much is actually returned.
func TestReclaimWithoutPerProcessVRAMWaitsForTheMemoryToReturn(t *testing.T) {
	g, gpu, eng, clock := newRig(t)
	gpu.hideProcs, gpu.freeAfter = true, 5
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick() // running
	g.Tick()
	gpu.mu.Lock()
	gpu.external = 12288 - 6000 - 1000 // free = 1000 < headroom
	gpu.mu.Unlock()
	g.Tick()
	if g.st != StYielded || eng.running {
		t.Fatalf("state=%s running=%v", g.st, eng.running)
	}
	gpu.mu.Lock()
	n := gpu.samples
	gpu.mu.Unlock()
	if n < 5 {
		t.Fatalf("reclaim declared the GPU free after %d samples although the driver needs 5: it trusted a 0-byte target", n)
	}
	for _, e := range g.events {
		if e.Kind == "reclaim" && e.Detail != "" && contains(e.Detail, "NOT confirmed") {
			t.Fatalf("memory did come back, it must be confirmed: %s", e.Detail)
		}
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func TestEngineArgsFlashAttentionAndKVType(t *testing.T) {
	has := func(a []string, k, v string) bool {
		for i := 0; i+1 < len(a); i++ {
			if a[i] == k && a[i+1] == v {
				return true
			}
		}
		return false
	}
	e := &LlamaEngine{Model: "m.gguf", Ctx: 4096, Parallel: 2, NGL: 99, Port: 1}
	a, err := e.Args()
	if err != nil || !has(a, "-fa", "on") || !has(a, "-ctk", "f16") || !has(a, "-c", "8192") {
		t.Fatalf("defaults must be flash attention on, f16 KV, ctx*slots: %v %v", a, err)
	}
	e.FlashAttn, e.KVType = "on", "q8_0"
	if a, err = e.Args(); err != nil || !has(a, "-ctk", "q8_0") || !has(a, "-ctv", "q8_0") {
		t.Fatalf("q8_0 KV: %v %v", a, err)
	}
	for _, bad := range [][2]string{{"off", "q8_0"}, {"maybe", "f16"}, {"on", "q4_0"}} {
		e.FlashAttn, e.KVType = bad[0], bad[1]
		if _, err := e.Args(); err == nil {
			t.Fatalf("%v must be refused before llama-server is started", bad)
		}
	}
}
