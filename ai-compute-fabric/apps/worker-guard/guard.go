package main

import (
	"fmt"
	"log"
	"path/filepath"
	"sync"
	"time"

	est "github.com/aicf/ai-compute-fabric/core/vram-estimator"
)

type State string

const (
	StStopped    State = "stopped"
	StRunning    State = "running"
	StReclaiming State = "reclaiming"
	StYielded    State = "yielded" // GPU given back to the user, waiting for it to stay free
	StPaused     State = "paused"  // operator pressed pause
)

// Config is the policy (plan D07: headroom 1536 MB business hours, 512 MB off hours).
type Config struct {
	HeadroomMiB   uint64
	ResumeStable  time.Duration // GPU must stay free this long before the engine restarts
	Poll          time.Duration // normal cadence
	FastPoll      time.Duration // cadence when free VRAM is within NearMarginMiB of the headroom (0 = same as Poll)
	NearMarginMiB uint64
	FreedFraction float64 // fraction of the engine's VRAM that must disappear to call it "freed"
	FreedTimeout  time.Duration
}

// Event is one entry of the reclaim / lifecycle log.
type Event struct {
	At       time.Time `json:"at"`
	Kind     string    `json:"kind"`
	Detail   string    `json:"detail"`
	DetectMS float64   `json:"detect_to_freed_ms,omitempty"` // reclaim latency measured by the guard
	StopMS   float64   `json:"stop_ms,omitempty"`
	Signal   string    `json:"signal,omitempty"`
}

// Snapshot is what the API and dashboard read.
type Snapshot struct {
	State     State       `json:"state"`
	Sample    GPUSample   `json:"gpu"`
	EngineMiB uint64      `json:"engine_mib"`
	EnginePID int         `json:"engine_pid"`
	External  uint64      `json:"external_mib"`
	Buckets   est.Buckets `json:"buckets_bytes"`
	Events    []Event     `json:"events"`
	Headroom  uint64      `json:"headroom_mib"`
	Model     string      `json:"model"`
	Lora      string      `json:"lora"`
	Switching bool        `json:"switching"`
	Updated   time.Time   `json:"updated"`
}

type Guard struct {
	cfg       Config
	smp       Sampler
	eng       Engine
	now       func() time.Time
	mu        sync.Mutex
	st        State
	last      GPUSample
	own       uint64 // engine VRAM seen at last running sample
	lastOwn   uint64
	freeSince time.Time
	procs     []Proc
	procsAt   time.Time
	events    []Event
	switching bool // a model/adapter change was requested and the engine has not started with it yet
}

func NewGuard(cfg Config, s Sampler, e Engine) *Guard {
	if cfg.FreedFraction == 0 {
		cfg.FreedFraction = 0.9
	}
	if cfg.FreedTimeout == 0 {
		cfg.FreedTimeout = 5 * time.Second
	}
	return &Guard{cfg: cfg, smp: s, eng: e, now: time.Now, st: StStopped}
}

func (g *Guard) emit(ev Event) {
	ev.At = g.now()
	g.mu.Lock()
	g.events = append(g.events, ev)
	if len(g.events) > 200 {
		g.events = g.events[len(g.events)-200:]
	}
	g.mu.Unlock()
}

func (g *Guard) setState(s State) { g.mu.Lock(); g.st = s; g.mu.Unlock() }

// Switch changes the model and adapter the engine serves. The running engine is stopped, then the normal control loop starts
// it again with the new settings once the GPU has stayed free for ResumeStable, so a switch obeys the same rule as every other
// start: it never takes VRAM the user is using. A paused worker is resumed (the operator asked for a model to be served).
func (g *Guard) Switch(model, lora string) {
	if g.eng.Running() {
		info, _ := g.eng.Stop()
		g.emit(Event{Kind: "switch", Detail: "stopped for a model change", StopMS: ms(info.Elapsed), Signal: info.Signal})
	}
	g.eng.Configure(model, lora)
	g.mu.Lock()
	g.switching = true
	g.mu.Unlock()
	g.setState(StStopped)
	d := model
	if lora != "" {
		d += " + " + lora
	}
	g.emit(Event{Kind: "switch", Detail: "will serve " + d})
}

// Pause stops the engine on operator request; Resume allows it to start again.
func (g *Guard) Pause() {
	if g.eng.Running() {
		info, _ := g.eng.Stop()
		g.emit(Event{Kind: "pause", Detail: "operator pause", StopMS: ms(info.Elapsed), Signal: info.Signal})
	}
	g.setState(StPaused)
}

func (g *Guard) Resume() {
	g.mu.Lock()
	if g.st == StPaused {
		g.st = StStopped
	}
	g.mu.Unlock()
	g.emit(Event{Kind: "resume", Detail: "operator resume"})
}

// baseName is filepath.Base that keeps "" as "" (Base("") is ".").
func baseName(p string) string {
	if p == "" {
		return ""
	}
	return filepath.Base(p)
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

// Tick runs one control-loop step.
func (g *Guard) Tick() error {
	s, err := g.smp.Sample()
	if err != nil {
		return err
	}
	g.mu.Lock()
	st := g.st
	g.mu.Unlock()
	if st == StRunning {
		s.Procs = g.cachedProcs(false)
	}
	g.mu.Lock()
	g.last = s
	g.mu.Unlock()

	switch st {
	case StRunning:
		if !g.eng.Running() {
			g.emit(Event{Kind: "crash", Detail: "engine exited unexpectedly"})
			g.setState(StStopped)
			return nil
		}
		own := s.OwnMiB(map[int]bool{g.eng.PID(): true})
		g.mu.Lock()
		g.own = own
		if own > 0 {
			g.lastOwn = own
		}
		g.mu.Unlock()
		if s.FreeMiB() < g.cfg.HeadroomMiB {
			g.reclaim(s, own)
		}
	case StStopped, StYielded:
		g.mu.Lock()
		need := g.lastOwn + g.cfg.HeadroomMiB
		g.mu.Unlock()
		if need == g.cfg.HeadroomMiB { // never ran: use buffers if known, else start optimistically
			need = g.cfg.HeadroomMiB + 1
		}
		now := g.now()
		if s.FreeMiB() >= need {
			if g.freeSince.IsZero() {
				g.freeSince = now
			}
			if now.Sub(g.freeSince) >= g.cfg.ResumeStable {
				g.freeSince = time.Time{}
				return g.start()
			}
		} else {
			g.freeSince = time.Time{}
		}
	}
	return nil
}

// cachedProcs returns per-process VRAM, refreshed at most once per second unless
// force is set. Samplers without a ProcSampler (tests) carry Procs in the sample.
func (g *Guard) cachedProcs(force bool) []Proc {
	ps, ok := g.smp.(ProcSampler)
	if !ok {
		s, _ := g.smp.Sample()
		return s.Procs
	}
	if force || g.now().Sub(g.procsAt) >= time.Second {
		if p, err := ps.Procs(); err == nil {
			g.procs, g.procsAt = p, g.now()
		}
	}
	return g.procs
}

// NextInterval is the poll cadence for the next tick: fast only when the user is
// close to taking the headroom, otherwise slow to keep CPU use negligible.
func (g *Guard) NextInterval() time.Duration {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.cfg.FastPoll > 0 && g.last.TotalMiB > 0 && g.last.FreeMiB() < g.cfg.HeadroomMiB+g.cfg.NearMarginMiB {
		return g.cfg.FastPoll
	}
	return g.cfg.Poll
}

func (g *Guard) start() error {
	t0 := g.now()
	if err := g.eng.Start(); err != nil {
		g.emit(Event{Kind: "start_failed", Detail: err.Error()})
		g.setState(StStopped)
		return err
	}
	g.mu.Lock()
	g.switching = false
	g.mu.Unlock()
	g.setState(StRunning)
	g.emit(Event{Kind: "start", Detail: fmt.Sprintf("engine healthy after %.1fs", g.now().Sub(t0).Seconds())})
	return nil
}

// reclaim stops the engine and measures detect -> VRAM freed.
func (g *Guard) reclaim(pre GPUSample, own uint64) {
	detect := g.now()
	pid := g.eng.PID()
	g.setState(StReclaiming)
	info, _ := g.eng.Stop()
	// Per-process VRAM is unreliable on Windows/WDDM (nvidia-smi prints [N/A]), so own can be
	// 0. Then the engine's own report of what it allocated stands in for it, otherwise a
	// target of 0 would count the reclaim as "freed" on the very first sample.
	expected := own
	if expected == 0 {
		b := g.eng.Buffers()
		expected = uint64(b.ModelMiB + b.KVMiB + b.ComputeMiB)
	}
	if expected == 0 {
		expected = 256
	}
	target := uint64(float64(expected) * g.cfg.FreedFraction)
	deadline := detect.Add(g.cfg.FreedTimeout)
	freedMS, ok := 0.0, false
	for g.now().Before(deadline) {
		s, err := g.smp.Sample()
		if err == nil {
			s.Procs = g.cachedProcs(true)
			g.mu.Lock()
			g.last = s
			g.mu.Unlock()
			// Prefer per-process truth (Linux NVML): the user may still be growing, which
			// would mask the drop in total usage. Fall back to the total-usage drop.
			gone := own > 0 && s.OwnMiB(map[int]bool{pid: true}) == 0 // per-process truth where the platform has it
			dropped := pre.UsedMiB >= s.UsedMiB && pre.UsedMiB-s.UsedMiB >= target
			if gone || (own == 0 && dropped) {
				freedMS, ok = ms(g.now().Sub(detect)), true
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	d := fmt.Sprintf("free %d MiB < headroom %d MiB; engine held %d MiB", pre.FreeMiB(), g.cfg.HeadroomMiB, expected)
	if !ok {
		d += "; VRAM NOT confirmed freed within timeout"
	}
	g.emit(Event{Kind: "reclaim", Detail: d, DetectMS: freedMS, StopMS: ms(info.Elapsed), Signal: info.Signal})
	g.setState(StYielded)
}

// Snapshot builds the 6-bucket view (bytes) from engine-reported buffers.
func (g *Guard) Snapshot() Snapshot {
	g.mu.Lock()
	defer g.mu.Unlock()
	const mib = uint64(1 << 20)
	b := g.eng.Buffers()
	w, kv, c := uint64(b.ModelMiB)*mib, uint64(b.KVMiB)*mib, uint64(b.ComputeMiB)*mib
	own := g.own
	if g.st != StRunning {
		w, kv, c, own = 0, 0, 0, 0
	}
	model, lora := g.eng.Describe()
	osUser := est.OSUserFromEngine(g.last.UsedMiB*mib, w, kv, c)
	ev := make([]Event, len(g.events))
	copy(ev, g.events)
	return Snapshot{
		State: g.st, Sample: g.last, EngineMiB: own, EnginePID: g.eng.PID(), External: g.last.UsedMiB - min64(g.last.UsedMiB, own),
		Buckets: est.NewBuckets(g.last.TotalMiB*mib, osUser, w, kv, c, g.cfg.HeadroomMiB*mib),
		Events:  ev, Headroom: g.cfg.HeadroomMiB, Updated: g.now(), Model: baseName(model), Lora: baseName(lora), Switching: g.switching,
	}
}

func min64(a, b uint64) uint64 {
	if a < b {
		return a
	}
	return b
}

// Run ticks until stop is closed, adapting the cadence to how close the user is
// to the headroom (see NextInterval).
func (g *Guard) Run(stop <-chan struct{}) {
	failing := false
	for {
		// A failed GPU sample is logged when it starts and when it ends, not once per tick.
		if err := g.Tick(); err != nil && !failing {
			log.Printf("guard: GPU sampling failed: %v", err)
			failing = true
		} else if err == nil && failing {
			log.Printf("guard: GPU sampling recovered")
			failing = false
		}
		select {
		case <-stop:
			return
		case <-time.After(g.NextInterval()):
		}
	}
}
