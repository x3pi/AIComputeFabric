package main

import (
	"context"
	"crypto/hmac"
	"encoding/json"
	"net/http"
	"sort"
	"strings"
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
	tenant  string
	kind    string
	expires time.Time
}

// LeaseRecord is the JSON representation returned by the HTTP lease bridge and admin inspection.
type LeaseRecord struct {
	LeaseID           string `json:"lease_id"`
	WorkerID          string `json:"worker_id"`
	TenantID          string `json:"tenant_id,omitempty"`
	WorkloadKind      string `json:"workload_kind,omitempty"`
	VRAMReservedBytes uint64 `json:"vram_reserved_bytes"`
	TTLMs             uint64 `json:"ttl_ms"`
	IssuedUnixMs      uint64 `json:"issued_unix_ms"`
	ExpiresUnixMs     uint64 `json:"expires_unix_ms"`
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

func normGPU(s string) string {
	s = strings.ToLower(s)
	s = strings.ReplaceAll(s, " ", "")
	s = strings.ReplaceAll(s, "-", "")
	return s
}

func matchesJobConstraint(w *Worker, job *pb.WorkloadJob) bool {
	if job == nil || job.ArtifactRef == "" {
		return true
	}
	ref := job.ArtifactRef
	if strings.HasPrefix(ref, "worker:") {
		return w.WorkerID == strings.TrimPrefix(ref, "worker:")
	}
	if strings.HasPrefix(ref, "gpu:") {
		want := normGPU(strings.TrimPrefix(ref, "gpu:"))
		if want == "" || want == "any" {
			return true
		}
		return strings.Contains(normGPU(w.GPUName), want) || strings.Contains(normGPU(w.WorkerID), want)
	}
	return true
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
		if w.State != "running" || !w.Online || w.Buckets == nil || (req.Model != "" && w.Model != req.Model) || !matchesJobConstraint(w, req.Job) {
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
	var tenant, kind, depID string
	if req.Job != nil {
		tenant = req.Job.TenantId
		kind = req.Job.ApprovedTaskType
		depID = req.Job.JobId
	}
	tok := &pb.LeaseToken{LeaseId: "lease-" + newSecret(16), WorkerId: best.WorkerID, DeploymentId: depID, VramReservedBytes: req.VramRequiredBytes,
		TtlMs: uint64(d / time.Millisecond), IssuedUnixMs: uint64(now.UnixMilli())}
	l := &lease{tok: tok, key: req.IdempotencyKey, tenant: tenant, kind: kind, expires: now.Add(d)}
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
	return m.ActiveByTenant("")
}

// ActiveByTenant lists live leases filtered by tenant ("" = all tenants), oldest first.
func (m *LeaseManager) ActiveByTenant(tenant string) []*pb.LeaseToken {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sweepLocked()
	out := make([]*pb.LeaseToken, 0, len(m.byID))
	for _, l := range m.byID {
		if tenant == "" || l.tenant == tenant {
			out = append(out, l.tok)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].IssuedUnixMs < out[j].IssuedUnixMs || (out[i].IssuedUnixMs == out[j].IssuedUnixMs && out[i].LeaseId < out[j].LeaseId)
	})
	return out
}

// ActiveRecords returns live lease metadata including tenant_id, workload_kind and expires_unix_ms.
func (m *LeaseManager) ActiveRecords(tenant string) []LeaseRecord {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sweepLocked()
	out := make([]LeaseRecord, 0, len(m.byID))
	for _, l := range m.byID {
		if tenant != "" && l.tenant != tenant {
			continue
		}
		out = append(out, LeaseRecord{
			LeaseID:           l.tok.LeaseId,
			WorkerID:          l.tok.WorkerId,
			TenantID:          l.tenant,
			WorkloadKind:      l.kind,
			VRAMReservedBytes: l.tok.VramReservedBytes,
			TTLMs:             l.tok.TtlMs,
			IssuedUnixMs:      l.tok.IssuedUnixMs,
			ExpiresUnixMs:     uint64(l.expires.UnixMilli()),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].IssuedUnixMs < out[j].IssuedUnixMs || (out[i].IssuedUnixMs == out[j].IssuedUnixMs && out[i].LeaseID < out[j].LeaseID)
	})
	return out
}

// StartSweeper actively sweeps expired/revoked leases on a periodic ticker (not only lazily on RPC calls).
func (m *LeaseManager) StartSweeper(interval time.Duration, stop <-chan struct{}) {
	if interval <= 0 {
		interval = time.Second
	}
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-stop:
				return
			case <-t.C:
				m.mu.Lock()
				m.sweepLocked()
				m.mu.Unlock()
			}
		}
	}()
}

// LeaseHTTPHandler serves the internal HTTP lease bridge (ADR-003 D-LEASE-HTTP) under /api/serverless/v1/_lease/*.
// Only requests carrying the valid X-Aicf-Proxy-Secret are accepted.
func LeaseHTTPHandler(m *LeaseManager, secret string) http.Handler {
	secretBytes := []byte(secret)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := []byte(r.Header.Get("X-Aicf-Proxy-Secret"))
		if len(secretBytes) < 16 || !hmac.Equal(got, secretBytes) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": "valid X-Aicf-Proxy-Secret required"})
			return
		}
		sub := strings.TrimPrefix(r.URL.Path, "/api/serverless/v1/_lease/")
		switch {
		case sub == "active" && r.Method == http.MethodGet:
			tenant := r.URL.Query().Get("tenant")
			writeJSON(w, http.StatusOK, map[string]any{"leases": m.ActiveRecords(tenant)})
		case sub == "request" && r.Method == http.MethodPost:
			var in struct {
				IdempotencyKey    string `json:"idempotency_key"`
				TenantID          string `json:"tenant_id"`
				WorkloadKind      string `json:"workload_kind"`
				Constraint        string `json:"constraint"` // e.g. "gpu:rtx3060" or "worker:pc-001"
				Model             string `json:"model"`
				VRAMRequiredBytes uint64 `json:"vram_required_bytes"`
				TTLMs             uint64 `json:"ttl_ms"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": "bad JSON"})
				return
			}
			rep, err := m.RequestLease(r.Context(), &pb.RequestLeaseRequest{
				IdempotencyKey:    in.IdempotencyKey,
				VramRequiredBytes: in.VRAMRequiredBytes,
				TtlMs:             in.TTLMs,
				Model:             in.Model,
				Job:               &pb.WorkloadJob{JobId: in.IdempotencyKey, TenantId: in.TenantID, ApprovedTaskType: in.WorkloadKind, ArtifactRef: in.Constraint},
			})
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
				return
			}
			var rec *LeaseRecord
			if rep.Granted && rep.Lease != nil {
				rec = &LeaseRecord{
					LeaseID:           rep.Lease.LeaseId,
					WorkerID:          rep.Lease.WorkerId,
					TenantID:          in.TenantID,
					WorkloadKind:      in.WorkloadKind,
					VRAMReservedBytes: rep.Lease.VramReservedBytes,
					TTLMs:             rep.Lease.TtlMs,
					IssuedUnixMs:      rep.Lease.IssuedUnixMs,
					ExpiresUnixMs:     rep.Lease.IssuedUnixMs + rep.Lease.TtlMs,
				}
			}
			writeJSON(w, http.StatusOK, map[string]any{"granted": rep.Granted, "lease": rec, "reason": rep.Reason})
		case sub == "extend" && r.Method == http.MethodPost:
			var in struct {
				LeaseID string `json:"lease_id"`
				TTLMs   uint64 `json:"ttl_ms"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": "bad JSON"})
				return
			}
			rep, err := m.ExtendLease(r.Context(), &pb.ExtendLeaseRequest{LeaseId: in.LeaseID, TtlMs: in.TTLMs})
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"ok": rep.Ok, "reason": rep.Reason})
		case sub == "release" && r.Method == http.MethodPost:
			var in struct {
				LeaseID string `json:"lease_id"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": "bad JSON"})
				return
			}
			rep, err := m.ReleaseLease(r.Context(), &pb.ReleaseLeaseRequest{LeaseId: in.LeaseID})
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"released": rep.Released})
		default:
			http.NotFound(w, r)
		}
	})
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
