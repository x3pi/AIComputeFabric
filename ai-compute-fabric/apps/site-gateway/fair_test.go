package main

import (
	"testing"
	"time"
)

// fairRig: one worker with `slots` slots, no HTTP, so grant order is observable.
func fairRig(t *testing.T, slots uint32, cfg RouterConfig) *Router {
	t.Helper()
	reg := NewRegistry(time.Hour)
	cfg.MaxWait = 10 * time.Second
	if cfg.MaxQueue == 0 {
		cfg.MaxQueue = 100
	}
	rt := NewRouter(cfg, reg)
	addWorker(reg, "w1", "http://x", "m", slots)
	rt.Refresh()
	return rt
}

// enqueue starts a goroutine that waits for a slot and reports (tenant) on `order` when granted.
func enqueue(t *testing.T, rt *Router, order chan<- Req, q Req) {
	t.Helper()
	_, before, _ := rt.Snapshot()
	go func() {
		route, err := rt.Acquire(q, nil)
		if err != nil {
			t.Errorf("acquire %+v: %v", q, err)
			return
		}
		order <- q
		_ = route
	}()
	waitFor(t, "request queued", func() bool { _, n, _ := rt.Snapshot(); return n == before+1 })
}

func TestQuietTenantIsNotStarvedBehindANoisyOne(t *testing.T) {
	rt := fairRig(t, 1, RouterConfig{Fair: true, MaxQueuePerTenant: 10})
	held, _ := rt.Acquire(Req{Model: "m", Tenant: "holder"}, nil)
	order := make(chan Req, 32)
	for i := 0; i < 6; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "noisy"})
	}
	enqueue(t, rt, order, Req{Model: "m", Tenant: "quiet"})

	cur, tenant := held, "holder"
	pos := -1
	for i := 0; i < 7; i++ {
		rt.Release(cur, tenant, false)
		q := <-order
		if q.Tenant == "quiet" {
			pos = i
		}
		cur, tenant = Route{ID: "w1", Slots: 1}, q.Tenant
	}
	// FIFO would serve "quiet" 7th (index 6). Fair queuing serves it within the first two grants.
	if pos < 0 || pos > 1 {
		t.Fatalf("quiet tenant served at position %d, want <= 1", pos)
	}
}

func TestFIFOModeStarvesTheQuietTenant(t *testing.T) { // documents the v1 behaviour the fair mode replaces
	rt := fairRig(t, 1, RouterConfig{Fair: false})
	held, _ := rt.Acquire(Req{Model: "m", Tenant: "holder"}, nil)
	order := make(chan Req, 32)
	for i := 0; i < 6; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "noisy"})
	}
	enqueue(t, rt, order, Req{Model: "m", Tenant: "quiet"})
	cur, tenant, pos := held, "holder", -1
	for i := 0; i < 7; i++ {
		rt.Release(cur, tenant, false)
		q := <-order
		if q.Tenant == "quiet" {
			pos = i
		}
		cur, tenant = Route{ID: "w1", Slots: 1}, q.Tenant
	}
	if pos != 6 {
		t.Fatalf("FIFO should serve the late request last, got position %d", pos)
	}
}

func TestWeightedShare(t *testing.T) {
	rt := fairRig(t, 1, RouterConfig{Fair: true, MaxQueuePerTenant: 20, Weights: map[string]float64{"big": 3, "small": 1}})
	held, _ := rt.Acquire(Req{Model: "m", Tenant: "holder"}, nil)
	order := make(chan Req, 64)
	for i := 0; i < 12; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "big"})
		enqueue(t, rt, order, Req{Model: "m", Tenant: "small"})
	}
	cur, tenant := held, "holder"
	big := 0
	for i := 0; i < 12; i++ {
		rt.Release(cur, tenant, false)
		q := <-order
		if q.Tenant == "big" {
			big++
		}
		cur, tenant = Route{ID: "w1", Slots: 1}, q.Tenant
	}
	if big < 8 || big > 10 { // 3:1 => 9 of 12
		t.Fatalf("weight 3:1 should give ~9 of 12 grants to big, got %d", big)
	}
}

func TestPerTenantQueueShareLeavesRoomForOthers(t *testing.T) {
	rt := fairRig(t, 1, RouterConfig{Fair: true, MaxQueue: 8, MaxQueuePerTenant: 3})
	rt.Acquire(Req{Model: "m", Tenant: "holder"}, nil)
	order := make(chan Req, 32)
	for i := 0; i < 3; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "noisy"})
	}
	if _, err := rt.Acquire(Req{Model: "m", Tenant: "noisy"}, nil); err != ErrTenantQueueFull {
		t.Fatalf("4th queued request of one tenant must be rejected, got %v", err)
	}
	// the global queue still has room, so another tenant can queue
	enqueue(t, rt, order, Req{Model: "m", Tenant: "quiet"})
}

func TestPerTenantInflightCap(t *testing.T) {
	rt := fairRig(t, 4, RouterConfig{Fair: true, MaxInflightTenant: 2, MaxQueuePerTenant: 4})
	for i := 0; i < 2; i++ {
		if _, err := rt.Acquire(Req{Model: "m", Tenant: "a"}, nil); err != nil {
			t.Fatal(err)
		}
	}
	order := make(chan Req, 4)
	enqueue(t, rt, order, Req{Model: "m", Tenant: "a"}) // free slots exist, but tenant a is at its cap
	if _, err := rt.Acquire(Req{Model: "m", Tenant: "b"}, nil); err != nil {
		t.Fatalf("another tenant must still get a slot immediately: %v", err)
	}
	if len(order) != 0 {
		t.Fatal("capped tenant must wait")
	}
	rt.Release(Route{ID: "w1", Slots: 4}, "a", false)
	select {
	case <-order:
	case <-time.After(2 * time.Second):
		t.Fatal("capped tenant's request must run once one of its own requests finishes")
	}
}

func TestInteractiveBeatsBatch(t *testing.T) {
	rt := fairRig(t, 1, RouterConfig{Fair: true, MaxQueuePerTenant: 10})
	held, _ := rt.Acquire(Req{Model: "m", Tenant: "holder"}, nil)
	order := make(chan Req, 8)
	for i := 0; i < 3; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "nightly", Prio: PrioBatch})
	}
	enqueue(t, rt, order, Req{Model: "m", Tenant: "alice", Prio: PrioInteractive})
	rt.Release(held, "holder", false)
	if q := <-order; q.Tenant != "alice" {
		t.Fatalf("interactive request must jump ahead of queued batch work, got %s", q.Tenant)
	}
}

func TestIdleTenantDoesNotHoardCredit(t *testing.T) {
	rt := fairRig(t, 1, RouterConfig{Fair: true, MaxQueuePerTenant: 20})
	// "busy" runs alone for a while and advances its virtual clock.
	for i := 0; i < 20; i++ {
		r, _ := rt.Acquire(Req{Model: "m", Tenant: "busy"}, nil)
		rt.Release(r, "busy", false)
	}
	held, _ := rt.Acquire(Req{Model: "m", Tenant: "busy"}, nil)
	order := make(chan Req, 64)
	for i := 0; i < 6; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "busy"})
	}
	for i := 0; i < 6; i++ {
		enqueue(t, rt, order, Req{Model: "m", Tenant: "newcomer"})
	}
	cur, tenant := held, "busy"
	newc := 0
	for i := 0; i < 6; i++ {
		rt.Release(cur, tenant, false)
		q := <-order
		if q.Tenant == "newcomer" {
			newc++
		}
		cur, tenant = Route{ID: "w1", Slots: 1}, q.Tenant
	}
	if newc < 2 || newc > 4 { // fair share ~3 of 6; unlimited credit would give all 6
		t.Fatalf("newcomer got %d of 6 grants, want roughly half", newc)
	}
}
