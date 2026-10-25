package main

import (
	"errors"
	"sync"
	"time"
)

// Admission errors map to HTTP statuses in the proxy.
var (
	ErrQueueFull       = errors.New("queue full")                  // 429
	ErrTenantQueueFull = errors.New("tenant queue share full")     // 429
	ErrWaitTimeout     = errors.New("queue wait expired")          // 503 with Retry-After
	ErrNoWorker        = errors.New("no worker serves this model") // 503
)

// Priorities: interactive requests are always dispatched before batch ones.
const (
	PrioInteractive = 0
	PrioBatch       = 1
)

// RouterConfig bounds the data plane. Everything is bounded on purpose: an
// unbounded queue turns overload into unbounded latency and memory.
type RouterConfig struct {
	MaxQueue          int           // requests allowed to wait (all tenants)
	MaxQueuePerTenant int           // one tenant's share of the queue (0 = MaxQueue/4, at least 1)
	MaxInflightTenant int           // one tenant's concurrent requests (0 = unlimited)
	MaxWait           time.Duration // longest a request may wait for a slot
	Cooldown          time.Duration // a worker that failed a request is skipped this long
	RefreshEach       time.Duration // routable-worker cache refresh
	Fair              bool          // false = one shared FIFO (the v1 behaviour, kept for A/B measurement)
	Weights           map[string]float64
}

// Req is one admission request.
type Req struct {
	Model  string
	Skip   string // worker this request already failed on (retry goes elsewhere if it can)
	Tenant string
	Prio   int
}

type waiter struct {
	Req
	seq  uint64
	ch   chan Route // buffered 1
	done bool
}

// Router assigns each request to a worker slot. Placement is least-loaded. Among
// waiting requests dispatch order is: priority class, then the tenant with the
// smallest virtual time (weighted fair queuing: each grant advances the tenant's
// clock by 1/weight), then arrival order. Per-tenant caps on queue share and on
// concurrent requests stop one tenant from filling the queue or the slots.
type Router struct {
	cfg RouterConfig
	reg *Registry
	now func() time.Time

	mu        sync.Mutex
	routes    []Route
	inflight  map[string]int // per worker
	tInflight map[string]int // per tenant
	tQueued   map[string]int // per tenant
	vtime     map[string]float64
	seq       uint64
	bad       map[string]time.Time
	fails     map[string]int // consecutive failures per worker
	queue     []*waiter
	stats     Stats
	served    map[string]uint64 // per tenant, bounded
}

type Stats struct {
	Requests, Served, RejectedQueueFull, RejectedTenantQueue, RejectedWait, RejectedNoWorker, RejectedRate, Failovers uint64
	WaitNanosTotal                                                                                                    uint64
}

func NewRouter(cfg RouterConfig, reg *Registry) *Router {
	if cfg.Cooldown == 0 {
		cfg.Cooldown = 5 * time.Second
	}
	if cfg.RefreshEach == 0 {
		cfg.RefreshEach = 500 * time.Millisecond
	}
	if cfg.MaxQueuePerTenant == 0 {
		cfg.MaxQueuePerTenant = cfg.MaxQueue / 4
		if cfg.MaxQueuePerTenant < 1 {
			cfg.MaxQueuePerTenant = 1
		}
	}
	r := &Router{cfg: cfg, reg: reg, now: time.Now, inflight: map[string]int{}, tInflight: map[string]int{},
		tQueued: map[string]int{}, vtime: map[string]float64{}, bad: map[string]time.Time{}, fails: map[string]int{},
		served: map[string]uint64{}}
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

// key collapses all tenants into one when fairness is off.
func (r *Router) key(t string) string {
	if !r.cfg.Fair {
		return ""
	}
	return t
}

func (r *Router) weight(t string) float64 {
	if w, ok := r.cfg.Weights[t]; ok && w > 0 {
		return w
	}
	return 1
}

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

func (r *Router) tenantAtCapLocked(t string) bool {
	return r.cfg.Fair && r.cfg.MaxInflightTenant > 0 && r.tInflight[t] >= r.cfg.MaxInflightTenant
}

// grantLocked books the slot for a tenant and advances its virtual clock.
func (r *Router) grantLocked(t string, rt Route) {
	r.inflight[rt.ID]++
	r.tInflight[t]++
	r.vtime[t] += 1 / r.weight(t)
}

// startVtimeLocked: a tenant that was idle re-enters at the current minimum, so
// it neither hoards credit from its idle time nor starts behind everyone.
func (r *Router) startVtimeLocked(t string) {
	if r.tQueued[t] > 0 || r.tInflight[t] > 0 {
		return
	}
	min, any := 0.0, false
	for k, v := range r.vtime {
		if r.tQueued[k] > 0 || r.tInflight[k] > 0 {
			if !any || v < min {
				min, any = v, true
			}
		}
	}
	if any && r.vtime[t] < min {
		r.vtime[t] = min
	}
}

// dispatchLocked repeatedly gives free slots to the best dispatchable waiter.
func (r *Router) dispatchLocked() {
	for {
		bestIdx, bestRoute := -1, Route{}
		var best *waiter
		for i, w := range r.queue {
			if w.done || r.tenantAtCapLocked(w.Tenant) {
				continue
			}
			rt, ok := r.pickLocked(w.Model, w.Skip)
			if !ok {
				continue
			}
			if best == nil || r.beforeLocked(w, best) {
				best, bestIdx, bestRoute = w, i, rt
			}
		}
		if best == nil {
			break
		}
		best.done = true
		r.grantLocked(best.Tenant, bestRoute)
		r.tQueued[best.Tenant]--
		best.ch <- bestRoute
		r.queue = append(r.queue[:bestIdx], r.queue[bestIdx+1:]...)
	}
	kept := r.queue[:0]
	for _, w := range r.queue {
		if !w.done {
			kept = append(kept, w)
		}
	}
	for i := len(kept); i < len(r.queue); i++ {
		r.queue[i] = nil
	}
	r.queue = kept
}

func (r *Router) beforeLocked(a, b *waiter) bool {
	if a.Prio != b.Prio {
		return a.Prio < b.Prio
	}
	va, vb := r.vtime[a.Tenant], r.vtime[b.Tenant]
	if va != vb {
		return va < vb
	}
	return a.seq < b.seq
}

// Acquire returns a slot or an admission error. cancel is closed when the caller
// goes away (client disconnect) so a dead request never keeps a place in the queue.
func (r *Router) Acquire(q Req, cancel <-chan struct{}) (Route, error) {
	q.Tenant = r.key(q.Tenant)
	r.mu.Lock()
	if q.Skip == "" {
		r.stats.Requests++ // a retry is not a new request
	}
	if q.Skip != "" && !r.hasModelLocked(q.Model, q.Skip) {
		q.Skip = "" // no other worker exists: retrying the same one beats failing
	}
	if !r.hasModelLocked(q.Model, "") {
		r.stats.RejectedNoWorker++
		r.mu.Unlock()
		return Route{}, ErrNoWorker
	}
	r.startVtimeLocked(q.Tenant)
	if rt, ok := r.pickLocked(q.Model, q.Skip); ok && len(r.queue) == 0 && !r.tenantAtCapLocked(q.Tenant) {
		r.grantLocked(q.Tenant, rt)
		r.mu.Unlock()
		return rt, nil
	}
	if len(r.queue) >= r.cfg.MaxQueue {
		r.stats.RejectedQueueFull++
		r.mu.Unlock()
		return Route{}, ErrQueueFull
	}
	if r.cfg.Fair && r.tQueued[q.Tenant] >= r.cfg.MaxQueuePerTenant {
		r.stats.RejectedTenantQueue++
		r.mu.Unlock()
		return Route{}, ErrTenantQueueFull
	}
	r.seq++
	w := &waiter{Req: q, seq: r.seq, ch: make(chan Route, 1)}
	r.queue = append(r.queue, w)
	r.tQueued[q.Tenant]++
	start := r.now()
	r.dispatchLocked() // a free slot may exist that earlier waiters cannot use
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
		r.tInflight[q.Tenant]--
		r.dispatchLocked()
		return Route{}, ErrWaitTimeout
	}
	w.done = true
	r.tQueued[q.Tenant]--
	r.stats.RejectedWait++
	r.dispatchLocked() // purge the abandoned entry so it stops counting against MaxQueue
	return Route{}, ErrWaitTimeout
}

// Release frees the slot; failed marks the worker as bad after repeated failures.
func (r *Router) Release(rt Route, tenant string, failed bool) {
	tenant = r.key(tenant)
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.inflight[rt.ID] > 0 {
		r.inflight[rt.ID]--
	}
	if r.tInflight[tenant] > 0 {
		r.tInflight[tenant]--
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
		if len(r.served) < 1000 || r.served[tenant] > 0 {
			r.served[tenant]++
		}
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

// ServedByTenant returns completed requests per tenant (at most 1000 tenants tracked).
func (r *Router) ServedByTenant() map[string]uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]uint64, len(r.served))
	for k, v := range r.served {
		out[k] = v
	}
	return out
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
