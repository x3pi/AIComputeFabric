package main

import (
	"errors"
	"sync"
	"time"
)

// Admission errors map to HTTP statuses in the proxy.
var (
	ErrQueueFull   = errors.New("queue full")                  // 429
	ErrWaitTimeout = errors.New("queue wait expired")          // 429/503 with Retry-After
	ErrNoWorker    = errors.New("no worker serves this model") // 503
)

// RouterConfig bounds the data plane. Everything is bounded on purpose: an
// unbounded queue turns overload into unbounded latency and memory.
type RouterConfig struct {
	MaxQueue    int           // requests allowed to wait (across all models)
	MaxWait     time.Duration // longest a request may wait for a slot
	Cooldown    time.Duration // a worker that failed a request is skipped this long
	RefreshEach time.Duration // routable-worker cache refresh
}

type waiter struct {
	model string
	skip  string     // worker this request already failed on (retry goes elsewhere if it can)
	ch    chan Route // buffered 1
	done  bool
}

// Router assigns each request to a worker slot: least-loaded first, FIFO among
// waiters, bounded queue, per-worker slot limit equal to llama-server -np.
type Router struct {
	cfg RouterConfig
	reg *Registry
	now func() time.Time

	mu       sync.Mutex
	routes   []Route
	inflight map[string]int
	bad      map[string]time.Time
	fails    map[string]int // consecutive failures per worker
	queue    []*waiter
	stats    Stats
}

type Stats struct {
	Requests, Served, RejectedQueueFull, RejectedWait, RejectedNoWorker, RejectedRate, Failovers uint64
	WaitNanosTotal                                                                               uint64
}

func NewRouter(cfg RouterConfig, reg *Registry) *Router {
	if cfg.Cooldown == 0 {
		cfg.Cooldown = 5 * time.Second
	}
	if cfg.RefreshEach == 0 {
		cfg.RefreshEach = 500 * time.Millisecond
	}
	r := &Router{cfg: cfg, reg: reg, now: time.Now, inflight: map[string]int{}, bad: map[string]time.Time{}, fails: map[string]int{}}
	r.Refresh()
	return r
}

func (r *Router) Refresh() {
	rt := r.reg.Routable()
	r.mu.Lock()
	r.routes = rt
	r.dispatchLocked()
	r.mu.Unlock()
}

func (r *Router) Run(stop <-chan struct{}) {
	t := time.NewTicker(r.cfg.RefreshEach)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			r.Refresh()
		}
	}
}

func modelMatches(want, have string) bool { return want == "" || want == have }

// pickLocked returns the least-loaded routable worker with a free slot.
func (r *Router) pickLocked(model, skip string) (Route, bool) {
	var best Route
	bestLoad, found := 1<<30, false
	now := r.now()
	for _, rt := range r.routes {
		if !modelMatches(model, rt.Model) || (skip != "" && rt.ID == skip) {
			continue
		}
		if until, ok := r.bad[rt.ID]; ok && now.Before(until) {
			continue
		}
		l := r.inflight[rt.ID]
		if l >= rt.Slots {
			continue
		}
		if l < bestLoad {
			best, bestLoad, found = rt, l, true
		}
	}
	return best, found
}

func (r *Router) hasModelLocked(model, skip string) bool {
	for _, rt := range r.routes {
		if modelMatches(model, rt.Model) && rt.ID != skip {
			return true
		}
	}
	return false
}

// dispatchLocked hands free slots to waiters in FIFO order.
func (r *Router) dispatchLocked() {
	kept := r.queue[:0]
	for _, w := range r.queue {
		if w.done {
			continue
		}
		if rt, ok := r.pickLocked(w.model, w.skip); ok {
			r.inflight[rt.ID]++
			w.done = true
			w.ch <- rt
			continue
		}
		kept = append(kept, w)
	}
	for i := len(kept); i < len(r.queue); i++ {
		r.queue[i] = nil
	}
	r.queue = kept
}

// Acquire returns a slot or an admission error. cancel is closed when the caller
// goes away (client disconnect) so a dead request never keeps a place in the queue.
func (r *Router) Acquire(model, skip string, cancel <-chan struct{}) (Route, error) {
	r.mu.Lock()
	if skip == "" {
		r.stats.Requests++ // a retry is not a new request
	}
	if skip != "" && !r.hasModelLocked(model, skip) {
		skip = "" // no other worker exists: retrying the same one beats failing
	}
	if !r.hasModelLocked(model, "") {
		r.stats.RejectedNoWorker++
		r.mu.Unlock()
		return Route{}, ErrNoWorker
	}
	if rt, ok := r.pickLocked(model, skip); ok && len(r.queue) == 0 {
		r.inflight[rt.ID]++
		r.mu.Unlock()
		return rt, nil
	}
	if len(r.queue) >= r.cfg.MaxQueue {
		r.stats.RejectedQueueFull++
		r.mu.Unlock()
		return Route{}, ErrQueueFull
	}
	w := &waiter{model: model, skip: skip, ch: make(chan Route, 1)}
	r.queue = append(r.queue, w)
	start := r.now()
	r.mu.Unlock()

	timer := time.NewTimer(r.cfg.MaxWait)
	defer timer.Stop()
	select {
	case rt := <-w.ch:
		r.mu.Lock()
		r.stats.WaitNanosTotal += uint64(r.now().Sub(start))
		r.mu.Unlock()
		return rt, nil
	case <-timer.C:
	case <-cancel:
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if w.done { // dispatched between the select and the lock: give the slot back
		rt := <-w.ch
		r.inflight[rt.ID]--
		r.dispatchLocked()
		return Route{}, ErrWaitTimeout
	}
	w.done = true
	r.stats.RejectedWait++
	r.dispatchLocked() // purge the abandoned entry so it stops counting against MaxQueue
	return Route{}, ErrWaitTimeout
}

// Release frees the slot; failed marks the worker as bad for a cooldown.
func (r *Router) Release(rt Route, failed bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.inflight[rt.ID] > 0 {
		r.inflight[rt.ID]--
	}
	if failed {
		r.stats.Failovers++
		// One failure may be a blip; skipping the only worker for seconds would turn it into an outage.
		if r.fails[rt.ID]++; r.fails[rt.ID] >= 2 {
			r.bad[rt.ID] = r.now().Add(r.cfg.Cooldown)
		}
	} else {
		r.stats.Served++
		r.fails[rt.ID] = 0
	}
	r.dispatchLocked()
}

// Snapshot of live gauges + counters (for /metrics and tests).
func (r *Router) Snapshot() (inflight, queued int, s Stats) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, v := range r.inflight {
		inflight += v
	}
	for _, w := range r.queue {
		if !w.done {
			queued++
		}
	}
	return inflight, queued, r.stats
}

// Limiter is a per-tenant token bucket.
type Limiter struct {
	mu      sync.Mutex
	rate    float64 // tokens per second
	burst   float64
	buckets map[string]*bucket
	now     func() time.Time
}

type bucket struct {
	tokens float64
	at     time.Time
}

func NewLimiter(rate float64, burst int) *Limiter {
	return &Limiter{rate: rate, burst: float64(burst), buckets: map[string]*bucket{}, now: time.Now}
}

// Allow reports whether the tenant may send a request now; retryAfter is how long until it can.
func (l *Limiter) Allow(tenant string) (bool, time.Duration) {
	if l.rate <= 0 {
		return true, 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	if len(l.buckets) > 10000 { // tenant ids come from a header: never let the map grow without bound
		for k, v := range l.buckets {
			if now.Sub(v.at).Seconds()*l.rate >= l.burst {
				delete(l.buckets, k)
			}
		}
	}
	b := l.buckets[tenant]
	if b == nil {
		b = &bucket{tokens: l.burst, at: now}
		l.buckets[tenant] = b
	}
	b.tokens += now.Sub(b.at).Seconds() * l.rate
	if b.tokens > l.burst {
		b.tokens = l.burst
	}
	b.at = now
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	return false, time.Duration((1 - b.tokens) / l.rate * float64(time.Second))
}
