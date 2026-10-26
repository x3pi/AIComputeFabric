package main

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

const gib = uint64(1) << 30

func leaseRig(t *testing.T, workers map[string]uint64) (*LeaseManager, *Registry, *time.Time) {
	t.Helper()
	reg := NewRegistry(3 * time.Second)
	clock := time.Unix(10000, 0)
	reg.now = func() time.Time { return clock }
	for id, alloc := range workers {
		reg.Register(&pb.RegisterRequest{WorkerId: id, EngineUrl: "http://x", ParallelSlots: 1})
		beat(reg, id, "running", alloc)
	}
	m := NewLeaseManager(reg, time.Minute)
	m.now = func() time.Time { return clock }
	return m, reg, &clock
}

func beat(reg *Registry, id, state string, alloc uint64) {
	reg.Heartbeat(&pb.HeartbeatRequest{State: state, Model: "qwen", Capacity: &pb.WorkerCapacity{WorkerId: id,
		Vram: &pb.VramBuckets{TotalBytes: 12 * gib, AllocatableBytes: alloc}}})
}

func ask(m *LeaseManager, key string, need uint64, ttlMs uint64) *pb.RequestLeaseReply {
	r, err := m.RequestLease(context.Background(), &pb.RequestLeaseRequest{IdempotencyKey: key, VramRequiredBytes: need, TtlMs: ttlMs})
	if err != nil {
		panic(err)
	}
	return r
}

func TestLeaseIsIdempotentPerKeyAndReleaseIsIdempotent(t *testing.T) {
	m, _, _ := leaseRig(t, map[string]uint64{"pc-1": 8 * gib})
	a, b := ask(m, "job-1", 5*gib, 30000), ask(m, "job-1", 5*gib, 30000)
	if !a.Granted || a.Lease.LeaseId != b.Lease.LeaseId {
		t.Fatalf("a retried request must return the same lease: %v %v", a, b)
	}
	if ask(m, "job-2", 5*gib, 30000).Granted {
		t.Fatal("5+5 GiB must not fit in 8 GiB: the retry must not have reserved twice")
	}
	r1, _ := m.ReleaseLease(context.Background(), &pb.ReleaseLeaseRequest{LeaseId: a.Lease.LeaseId})
	r2, _ := m.ReleaseLease(context.Background(), &pb.ReleaseLeaseRequest{LeaseId: a.Lease.LeaseId})
	if !r1.Released || r2.Released {
		t.Fatalf("release: first %v, second %v (second must be a harmless no-op)", r1.Released, r2.Released)
	}
	if !ask(m, "job-2", 5*gib, 30000).Granted {
		t.Fatal("released VRAM must be reservable again")
	}
}

func TestTTLIsStrictAndExpiryFreesVRAM(t *testing.T) {
	m, reg, clock := leaseRig(t, map[string]uint64{"pc-1": 8 * gib})
	l := ask(m, "j", 6*gib, 10000).Lease
	*clock = clock.Add(9 * time.Second)
	beat(reg, "pc-1", "running", 8*gib) // the worker is alive; only the lease clock matters here
	if e, _ := m.ExtendLease(context.Background(), &pb.ExtendLeaseRequest{LeaseId: l.LeaseId, TtlMs: 10000}); !e.Ok {
		t.Fatal("extend before expiry must work")
	}
	*clock = clock.Add(10*time.Second + time.Millisecond) // past the extended deadline
	beat(reg, "pc-1", "running", 8*gib)
	e, _ := m.ExtendLease(context.Background(), &pb.ExtendLeaseRequest{LeaseId: l.LeaseId, TtlMs: 10000})
	if e.Ok || e.Reason != "expired" {
		t.Fatalf("extend after expiry must fail with reason expired, got %+v", e)
	}
	if !ask(m, "k", 6*gib, 1000).Granted {
		t.Fatal("expired lease must free its VRAM")
	}
}

func TestTTLIsCapped(t *testing.T) {
	m, _, _ := leaseRig(t, map[string]uint64{"pc-1": 8 * gib})
	if l := ask(m, "j", gib, 24*3600*1000).Lease; l.TtlMs != 60000 {
		t.Fatalf("ttl must be capped at the gateway maximum (60 s here), got %d ms", l.TtlMs)
	}
	if _, err := m.RequestLease(context.Background(), &pb.RequestLeaseRequest{IdempotencyKey: "z", VramRequiredBytes: gib}); err == nil {
		t.Fatal("ttl 0 must be rejected")
	}
	if _, err := m.RequestLease(context.Background(), &pb.RequestLeaseRequest{VramRequiredBytes: gib, TtlMs: 1000}); err == nil {
		t.Fatal("missing idempotency key must be rejected")
	}
}

// A worker that goes offline, is paused/reclaimed by its user, or stops its engine loses its
// leases: the client learns "revoked" on its next extend and re-runs only that job.
func TestWorkerChurnRevokesOnlyThatWorkersLeases(t *testing.T) {
	for _, how := range []string{"offline", "yielded", "paused"} {
		t.Run(how, func(t *testing.T) {
			m, reg, clock := leaseRig(t, map[string]uint64{"pc-1": 8 * gib, "pc-2": 8 * gib})
			a := ask(m, "a", 6*gib, 30000).Lease
			b := ask(m, "b", 6*gib, 30000).Lease
			if a.WorkerId == b.WorkerId {
				t.Fatal("6+6 GiB cannot share one 8 GiB worker")
			}
			lost := a.WorkerId
			if how == "offline" {
				*clock = clock.Add(10 * time.Second)
				beat(reg, b.WorkerId, "running", 8*gib) // the other worker keeps beating
			} else {
				beat(reg, lost, how, 8*gib)
			}
			ea, _ := m.ExtendLease(context.Background(), &pb.ExtendLeaseRequest{LeaseId: a.LeaseId, TtlMs: 10000})
			eb, _ := m.ExtendLease(context.Background(), &pb.ExtendLeaseRequest{LeaseId: b.LeaseId, TtlMs: 10000})
			if ea.Ok || ea.Reason != "revoked" {
				t.Fatalf("lease on the lost worker must be revoked, got %+v", ea)
			}
			if !eb.Ok {
				t.Fatalf("lease on the healthy worker must survive, got %+v", eb)
			}
		})
	}
}

func TestPlacementRespectsModelAndNeverOverbooksUnderConcurrency(t *testing.T) {
	m, _, _ := leaseRig(t, map[string]uint64{"pc-1": 10 * gib, "pc-2": 10 * gib, "pc-3": 10 * gib})
	if r, _ := m.RequestLease(context.Background(), &pb.RequestLeaseRequest{IdempotencyKey: "m", VramRequiredBytes: gib, TtlMs: 1000, Model: "other"}); r.Granted || r.Reason != "no_capacity" {
		t.Fatalf("no worker serves model 'other': %+v", r)
	}
	var granted int64
	var wg sync.WaitGroup
	for i := 0; i < 60; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if r, _ := m.RequestLease(context.Background(), &pb.RequestLeaseRequest{IdempotencyKey: "c" + itoa(uint64(i)), VramRequiredBytes: 3 * gib, TtlMs: 30000}); r.Granted {
				atomic.AddInt64(&granted, 1)
			}
		}(i)
	}
	wg.Wait()
	if granted != 9 { // 3 workers x floor(10/3)
		t.Fatalf("exactly 9 leases of 3 GiB fit on 3x10 GiB, granted %d", granted)
	}
	perWorker := map[string]uint64{}
	for _, l := range m.Active() {
		perWorker[l.WorkerId] += l.VramReservedBytes
	}
	for w, v := range perWorker {
		if v > 10*gib {
			t.Fatalf("worker %s is overbooked: %d GiB reserved", w, v/gib)
		}
	}
}

func TestLeaseNeedsAnEstimateAndARunningOnlineWorker(t *testing.T) {
	m, reg, _ := leaseRig(t, map[string]uint64{"pc-1": 8 * gib})
	reg.Register(&pb.RegisterRequest{WorkerId: "pc-2"}) // registered, never reported capacity
	beat(reg, "pc-1", "stopped", 8*gib)
	if ask(m, "j", gib, 1000).Granted {
		t.Fatal("a stopped engine must not receive leases")
	}
}
