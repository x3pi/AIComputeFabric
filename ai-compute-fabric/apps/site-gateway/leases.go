package main

import (
	"context"
	"sort"
	"sync"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Leases reserve VRAM on one worker for a bounded time (plan P2.2). Invariants:
//   - the sum of live reservations on a worker never exceeds what it can allocate;
//   - a lease is live only while its TTL has not run out and its worker is routable;
//   - RequestLease is idempotent per key; ReleaseLease is idempotent;
//   - expiry/revocation frees the reservation, so a crashed client cannot hold VRAM forever.
type lease struct {
	tok     *pb.LeaseToken
	key     string
	expires time.Time
}

type LeaseManager struct {
	pb.UnimplementedLeaseServiceServer
	Reg    *Registry
	MaxTTL time.Duration
	now    func() time.Time

	mu    sync.Mutex
	byID  map[string]*lease
	byKey map[string]*lease
	// tombstones remember why a lease ended so ExtendLease can say expired vs revoked.
	ended map[string]string
}

func NewLeaseManager(reg *Registry, maxTTL time.Duration) *LeaseManager {
	if maxTTL <= 0 {
		maxTTL = 10 * time.Minute
	}
	return &LeaseManager{Reg: reg, MaxTTL: maxTTL, now: time.Now, byID: map[string]*lease{}, byKey: map[string]*lease{}, ended: map[string]string{}}
}

func (m *LeaseManager) reservedLocked() map[string]uint64 {
	out := map[string]uint64{}
	for _, l := range m.byID {
		out[l.tok.WorkerId] += l.tok.VramReservedBytes
	}
	return out
}

// sweepLocked ends leases that ran out of time or whose worker is no longer routable.
func (m *LeaseManager) sweepLocked() {
	now := m.now()
	live := map[string]bool{}
	for _, r := range m.Reg.Routable() {
		live[r.ID] = true
	}
	for id, l := range m.byID {
		switch {
		case !now.Before(l.expires):
			m.endLocked(id, l, "expired")
		case !live[l.tok.WorkerId]:
			m.endLocked(id, l, "revoked")
		}
	}
	if len(m.ended) > 100000 { // bounded memory for the reasons table
		m.ended = map[string]string{}
	}
}

func (m *LeaseManager) endLocked(id string, l *lease, why string) {
	delete(m.byID, id)
	if m.byKey[l.key] == l {
		delete(m.byKey, l.key)
	}
	m.ended[id] = why
}

func (m *LeaseManager) ttl(ms uint64) (time.Duration, bool) {
	d := time.Duration(ms) * time.Millisecond
	if d <= 0 {
		return 0, false
	}
	if d > m.MaxTTL {
		return m.MaxTTL, true // capped, not refused
	}
	return d, true
}

func (m *LeaseManager) RequestLease(_ context.Context, req *pb.RequestLeaseRequest) (*pb.RequestLeaseReply, error) {
	if req.IdempotencyKey == "" || req.VramRequiredBytes == 0 {
		return nil, status.Error(codes.InvalidArgument, "idempotency_key and vram_required_bytes are required")
	}
	d, ok := m.ttl(req.TtlMs)
	if !ok {
		return nil, status.Error(codes.InvalidArgument, "ttl_ms must be > 0")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sweepLocked()
	if l := m.byKey[req.IdempotencyKey]; l != nil { // retry of a request that already succeeded
		return &pb.RequestLeaseReply{Granted: true, Lease: l.tok}, nil
	}
	reserved := m.reservedLocked()
	workers := m.Reg.List()
	var best *Worker
	var bestFree uint64
	for i := range workers {
		w := &workers[i]
		if w.State != "running" || !w.Online || w.Buckets == nil || (req.Model != "" && w.Model != req.Model) {
			continue
		}
		have := w.Buckets.Allocatable
		if reserved[w.WorkerID] >= have {
			continue
		}
		free := have - reserved[w.WorkerID]
		if free >= req.VramRequiredBytes && free > bestFree { // most free room first: spreads load
			best, bestFree = w, free
		}
	}
	if best == nil {
		return &pb.RequestLeaseReply{Granted: false, Reason: "no_capacity"}, nil
	}
	now := m.now()
	tok := &pb.LeaseToken{LeaseId: "lease-" + newSecret(16), WorkerId: best.WorkerID, VramReservedBytes: req.VramRequiredBytes,
		TtlMs: uint64(d / time.Millisecond), IssuedUnixMs: uint64(now.UnixMilli())}
	l := &lease{tok: tok, key: req.IdempotencyKey, expires: now.Add(d)}
	m.byID[tok.LeaseId], m.byKey[req.IdempotencyKey] = l, l
	return &pb.RequestLeaseReply{Granted: true, Lease: tok}, nil
}

func (m *LeaseManager) ExtendLease(_ context.Context, req *pb.ExtendLeaseRequest) (*pb.ExtendLeaseReply, error) {
	d, ok := m.ttl(req.TtlMs)
	if !ok {
		return nil, status.Error(codes.InvalidArgument, "ttl_ms must be > 0")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sweepLocked()
	l := m.byID[req.LeaseId]
	if l == nil {
		why := m.ended[req.LeaseId]
		if why == "" {
			why = "unknown"
		}
		return &pb.ExtendLeaseReply{Ok: false, Reason: why}, nil
	}
	l.expires = m.now().Add(d) // extends from now, never beyond MaxTTL ahead
	l.tok.TtlMs = uint64(d / time.Millisecond)
	return &pb.ExtendLeaseReply{Ok: true, Lease: l.tok}, nil
}

func (m *LeaseManager) ReleaseLease(_ context.Context, req *pb.ReleaseLeaseRequest) (*pb.ReleaseLeaseReply, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	l := m.byID[req.LeaseId]
	if l == nil {
		return &pb.ReleaseLeaseReply{Released: false}, nil
	}
	m.endLocked(req.LeaseId, l, "released")
	return &pb.ReleaseLeaseReply{Released: true}, nil
}

// Active lists live leases for the dashboard, oldest first.
func (m *LeaseManager) Active() []*pb.LeaseToken {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sweepLocked()
	out := make([]*pb.LeaseToken, 0, len(m.byID))
	for _, l := range m.byID {
		out = append(out, l.tok)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].IssuedUnixMs < out[j].IssuedUnixMs || (out[i].IssuedUnixMs == out[j].IssuedUnixMs && out[i].LeaseId < out[j].LeaseId)
	})
	return out
}

func itoa(n uint64) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
