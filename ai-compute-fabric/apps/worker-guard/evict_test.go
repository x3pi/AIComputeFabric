package main

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeEvictor plays the ServerlessWorker: it holds `guests` MiB of the fake GPU as "external" usage and gives it back when asked.
type fakeEvictor struct {
	gpu        *fakeGPU
	guests     uint64
	n          int
	evictErr   error
	killErr    error
	evictCalls int
	killCalls  int
	slowAck    bool // the worker stops its containers but its reply arrives after the timeout
	mu         sync.Mutex
}

func (f *fakeEvictor) release() {
	f.gpu.mu.Lock()
	defer f.gpu.mu.Unlock()
	if f.guests <= f.gpu.external {
		f.gpu.external -= f.guests
	}
	f.guests, f.n = 0, 0
}
func (f *fakeEvictor) Evict(time.Duration) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.evictCalls++
	if f.slowAck {
		f.release()
		return 0, errors.New("timeout waiting for the reply")
	}
	if f.evictErr != nil {
		return 0, f.evictErr
	}
	n := f.n
	f.release()
	return n, nil
}
func (f *fakeEvictor) Kill() (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.killCalls++
	if f.killErr != nil {
		return 0, f.killErr
	}
	n := f.n
	f.release()
	return n, nil
}
func (f *fakeEvictor) Status() (uint64, int, error) { return f.guests, f.n, nil }

// rig: 12 GiB GPU, engine 6000 MiB, headroom 1536. Guests then take `guests` MiB so that free < headroom.
func evictRig(t *testing.T, guests uint64, n int) (*Guard, *fakeGPU, *fakeEngine, *fakeEvictor, *time.Time) {
	t.Helper()
	g, gpu, eng, clock := newRig(t)
	ev := &fakeEvictor{gpu: gpu, guests: guests, n: n}
	g.cfg.Evictor = ev
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick() // start
	g.Tick() // record own
	gpu.mu.Lock()
	gpu.external = guests
	gpu.mu.Unlock()
	return g, gpu, eng, ev, clock
}

func hasEvent(g *Guard, kind string) bool {
	for _, e := range g.events {
		if e.Kind == kind {
			return true
		}
	}
	return false
}

func TestGuestsAreEvictedBeforeTheEngineIsStopped(t *testing.T) {
	g, _, eng, ev, _ := evictRig(t, 5000, 2) // free = 12288-6000-5000 = 1288 < 1536
	g.Tick()
	if !eng.running || g.st != StRunning {
		t.Fatalf("engine was stopped although evicting the guests was enough (state=%s running=%v)", g.st, eng.running)
	}
	if ev.evictCalls != 1 || !hasEvent(g, "evict_serverless") || hasEvent(g, "reclaim") {
		t.Fatalf("evict=%d events=%+v", ev.evictCalls, g.events)
	}
}

func TestEngineIsStoppedWhenEvictingIsNotEnough(t *testing.T) {
	g, gpu, eng, ev, _ := evictRig(t, 1000, 1)
	gpu.mu.Lock()
	gpu.external += 5500 // the user also took VRAM: guests leaving cannot restore the headroom
	gpu.mu.Unlock()
	g.cfg.EvictWait = 50 * time.Millisecond
	g.now = time.Now
	g.Tick()
	if eng.running || g.st != StYielded {
		t.Fatalf("engine must be reclaimed when the guests were not the problem (state=%s)", g.st)
	}
	if ev.evictCalls != 1 || !hasEvent(g, "evict_insufficient") || !hasEvent(g, "reclaim") {
		t.Fatalf("events=%+v", g.events)
	}
}

func TestNothingToEvictDoesNotDelayTheReclaim(t *testing.T) {
	g, gpu, eng, ev, _ := evictRig(t, 0, 0)
	gpu.mu.Lock()
	gpu.external = 5000 // a human, not a guest
	gpu.mu.Unlock()
	g.cfg.EvictWait = 10 * time.Second // would be visible if the guard waited
	g.now = time.Now
	t0 := time.Now()
	g.Tick()
	if eng.running || time.Since(t0) > time.Second || ev.killCalls != 0 {
		t.Fatalf("reclaim delayed or kill used: running=%v took=%v kill=%d", eng.running, time.Since(t0), ev.killCalls)
	}
}

func TestEvictFailureFallsBackToKillAndKeepsTheEngine(t *testing.T) {
	g, _, eng, ev, _ := evictRig(t, 5000, 2)
	ev.evictErr = errors.New("worker not answering")
	g.Tick()
	if ev.killCalls != 1 || !eng.running || !hasEvent(g, "evict_serverless") {
		t.Fatalf("kill=%d running=%v events=%+v", ev.killCalls, eng.running, g.events)
	}
	if !strings.Contains(g.events[len(g.events)-1].Detail, "docker-kill") {
		t.Fatalf("the event must say the fallback was used: %+v", g.events[len(g.events)-1])
	}
}

func TestSlowAckFromAWorkerThatDidEvictDoesNotCostTheEngine(t *testing.T) {
	g, _, eng, ev, _ := evictRig(t, 5000, 2)
	ev.slowAck = true // containers are gone, the HTTP reply was late, so the fallback finds nothing to kill
	g.Tick()
	if !eng.running || !hasEvent(g, "evict_serverless") {
		t.Fatalf("the memory came back: the engine must stay (running=%v events=%+v)", eng.running, g.events)
	}
}

func TestEvictAndKillBothFailingStillProtectsTheUser(t *testing.T) {
	g, _, eng, ev, _ := evictRig(t, 5000, 2)
	ev.evictErr, ev.killErr = errors.New("hung"), errors.New("docker gone")
	g.Tick()
	if eng.running || g.st != StYielded || !hasEvent(g, "evict_failed") {
		t.Fatalf("the engine must yield when guests cannot be removed (state=%s)", g.st)
	}
}

func TestPauseEvictsGuestsFirst(t *testing.T) {
	g, gpu, eng, ev, _ := evictRig(t, 2000, 1)
	g.Pause()
	if ev.evictCalls != 1 || eng.running || g.st != StPaused {
		t.Fatalf("evict=%d running=%v state=%s", ev.evictCalls, eng.running, g.st)
	}
	gpu.mu.Lock()
	defer gpu.mu.Unlock()
	if gpu.external != 0 {
		t.Fatalf("guests still hold %d MiB after pause", gpu.external)
	}
}

func TestFeatureOffMeansNoEvictorCalls(t *testing.T) {
	g, gpu, eng, clock := newRig(t)
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick()
	g.Tick()
	gpu.mu.Lock()
	gpu.external = 12288 - 6000 - 1000
	gpu.mu.Unlock()
	g.Tick()
	if eng.running || g.st != StYielded || hasEvent(g, "evict_serverless") {
		t.Fatal("with no Evictor the guard must behave exactly as before")
	}
}

func TestSnapshotDoesNotCountGuestsAsOtherUsers(t *testing.T) {
	g, gpu, _, ev, _ := evictRig(t, 3000, 1)
	gpu.mu.Lock()
	gpu.external = 3000
	gpu.mu.Unlock()
	g.Tick()
	without := g.Snapshot().Buckets.OSUser
	g.mu.Lock()
	g.serverlessMiB = ev.guests
	g.mu.Unlock()
	snap := g.Snapshot()
	if snap.ServerlessMiB != 3000 || snap.Buckets.OSUser >= without {
		t.Fatalf("OS/user bucket must shrink by the guests' VRAM: before=%d after=%d serverless=%d", without, snap.Buckets.OSUser, snap.ServerlessMiB)
	}
}

// ---- HTTPEvictor ------------------------------------------------------------------------------------------------------------

func TestKillUsesFixedArgvScopedToThisGPUAndRefusesOddIDs(t *testing.T) {
	var calls [][]string
	h := &HTTPEvictor{GPU: 1, Docker: func(args ...string) (string, error) {
		calls = append(calls, args)
		if args[0] == "ps" {
			return "0123456789ab\nabcdef012345\n", nil
		}
		return "", nil
	}}
	n, err := h.Kill()
	if err != nil || n != 2 {
		t.Fatalf("n=%d err=%v", n, err)
	}
	ps := strings.Join(calls[0], " ")
	if ps != "ps -q --filter label=aicf.sl=1 --filter label=aicf.gpu=1" {
		t.Fatalf("docker ps argv: %q", ps)
	}
	if strings.Join(calls[1], " ") != "kill 0123456789ab abcdef012345" {
		t.Fatalf("docker kill argv: %q", calls[1])
	}
	for _, bad := range []string{"--all", "x; rm -rf /", "../../etc", "ABCDEF012345", "short"} {
		calls = nil
		h.Docker = func(args ...string) (string, error) {
			calls = append(calls, args)
			if args[0] == "ps" {
				return bad + "\n", nil
			}
			return "", nil
		}
		if _, err := h.Kill(); err == nil || len(calls) != 1 {
			t.Fatalf("id %q must be refused before docker kill runs (err=%v calls=%v)", bad, err, calls)
		}
	}
	h.Docker = func(args ...string) (string, error) { return "", nil }
	if n, err := h.Kill(); n != 0 || err != nil {
		t.Fatalf("no containers: n=%d err=%v", n, err)
	}
}

func TestHTTPEvictorSendsSecretAndParsesReplies(t *testing.T) {
	var gotSecret atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotSecret.Store(r.Header.Get("X-Aicf-Evict-Secret"))
		switch r.URL.Path {
		case "/v1/evict":
			w.Write([]byte(`{"evicted":3}`))
		case "/v1/status":
			w.Write([]byte(`{"vram_mib":4096,"containers":2}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	h := &HTTPEvictor{URL: srv.URL, Secret: "s3cret-s3cret-s3cret"}
	if n, err := h.Evict(time.Second); n != 3 || err != nil || gotSecret.Load() != "s3cret-s3cret-s3cret" {
		t.Fatalf("n=%d err=%v secret=%v", n, err, gotSecret.Load())
	}
	if u, c, err := h.Status(); u != 4096 || c != 2 || err != nil {
		t.Fatalf("%d %d %v", u, c, err)
	}
}

func TestHungWorkerIsAbandonedAtTheTimeoutNotLater(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-release }))
	defer srv.Close()
	defer close(release)
	h := &HTTPEvictor{URL: srv.URL, Secret: "s3cret-s3cret-s3cret"}
	t0 := time.Now()
	_, err := h.Evict(200 * time.Millisecond)
	if err == nil || time.Since(t0) > 600*time.Millisecond {
		t.Fatalf("a hung /v1/evict must fail at the timeout: err=%v took=%v", err, time.Since(t0))
	}
}

func TestWrongSecretOrErrorStatusIsAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Error(w, "no", http.StatusForbidden) }))
	defer srv.Close()
	h := &HTTPEvictor{URL: srv.URL, Secret: "x"}
	if _, err := h.Evict(time.Second); err == nil {
		t.Fatal("a refusal must not be read as 'evicted 0 containers'")
	}
}

func TestHungWorkerThenKillFreesTheGPUWithinTwoSeconds(t *testing.T) {
	g, _, eng, ev, _ := evictRig(t, 5000, 2)
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-release }))
	defer srv.Close()
	defer close(release)
	h := &HTTPEvictor{URL: srv.URL, Secret: "s3cret-s3cret-s3cret", Docker: func(args ...string) (string, error) {
		if args[0] == "ps" {
			return "0123456789ab\n", nil
		}
		ev.release() // `docker kill` returns the VRAM
		return "", nil
	}}
	g.cfg.Evictor = h
	g.cfg.EvictTimeout = 300 * time.Millisecond
	g.now = time.Now
	t0 := time.Now()
	g.Tick()
	if took := time.Since(t0); took > 2*time.Second || !eng.running || !hasEvent(g, "evict_serverless") {
		t.Fatalf("took=%v running=%v events=%+v", took, eng.running, g.events)
	}
}
