package main

import (
	"sort"
	"strings"
	"sync"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

// Worker is the gateway's view of one registered worker-guard.
type Worker struct {
	WorkerID    string    `json:"worker_id"`
	SiteID      string    `json:"site_id"`
	Hostname    string    `json:"hostname"`
	GPUName     string    `json:"gpu_name"`
	Model       string    `json:"model"`
	Lora        string    `json:"lora"`      // adapter the engine is configured with ("" = none)
	Switching   bool      `json:"switching"` // model/adapter change requested, engine not started with it yet
	State       string    `json:"state"`     // as reported by the worker
	Online      bool      `json:"online"`    // false when heartbeats stopped
	VRAMTotal   uint64    `json:"vram_total_bytes"`
	VRAMFree    uint64    `json:"vram_free_bytes"`
	Buckets     *Buckets  `json:"buckets,omitempty"`
	TempC       uint32    `json:"gpu_temp_c"`
	UserActive  bool      `json:"user_is_active"`
	LastReclaim float64   `json:"last_reclaim_ms,omitempty"`
	PauseWanted bool      `json:"pause_requested"`
	LastSeen    time.Time `json:"last_seen"`
	Version     string    `json:"agent_version"`
	EngineURL   string    `json:"engine_url"`
	Slots       uint32    `json:"parallel_slots"`
}

type Buckets struct {
	Total, OSUser, Weights, KVState, Compute, Headroom, Allocatable uint64
}

// Registry is in-memory state. PostgreSQL persistence is a later P2.2 item:
// after a gateway restart workers get registered=false on their next heartbeat
// and re-register, so no state is lost that matters for scheduling.
type Registry struct {
	mu      sync.RWMutex
	workers map[string]*Worker
	now     func() time.Time
	// OfflineAfter is how long without a heartbeat before a worker counts as offline.
	OfflineAfter time.Duration
}

func NewRegistry(offlineAfter time.Duration) *Registry {
	return &Registry{workers: map[string]*Worker{}, now: time.Now, OfflineAfter: offlineAfter}
}

func (r *Registry) Register(req *pb.RegisterRequest) {
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.workers[req.WorkerId]
	if w == nil {
		w = &Worker{}
		r.workers[req.WorkerId] = w
	}
	w.WorkerID, w.SiteID, w.Hostname, w.GPUName = req.WorkerId, req.SiteId, req.Hostname, req.GpuName
	w.VRAMTotal, w.Version, w.LastSeen, w.State = req.VramTotalBytes, req.AgentVersion, r.now(), "registered"
	w.EngineURL, w.Slots = req.EngineUrl, req.ParallelSlots
}

// Heartbeat records capacity and returns (registered, pauseRequested).
func (r *Registry) Heartbeat(req *pb.HeartbeatRequest) (bool, bool) {
	c := req.GetCapacity()
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.workers[c.GetWorkerId()]
	if w == nil {
		return false, false
	}
	w.State, w.Model, w.TempC, w.LastSeen = req.State, req.Model, req.GpuTempC, r.now()
	w.Lora, w.Switching = req.Lora, req.Switching
	w.VRAMFree, w.UserActive = c.VramFreeBytes, c.UserIsActive
	if v := c.GetVram(); v != nil {
		w.VRAMTotal = v.TotalBytes
		w.Buckets = &Buckets{v.TotalBytes, v.OsUserBytes, v.WeightsBytes, v.KvStateBytes, v.ComputeBytes, v.HeadroomBytes, v.AllocatableBytes}
	}
	if rc := req.GetLastReclaim(); rc != nil {
		w.LastReclaim = rc.DetectToFreedMs
	}
	return true, w.PauseWanted
}

// SetPause records the operator's wish; it is delivered on the next heartbeat.
func (r *Registry) SetPause(id string, pause bool) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.workers[id]
	if w == nil {
		return false
	}
	w.PauseWanted = pause
	return true
}

// SiteOf returns the site a worker registered with, read live (not from the 1 s fleet snapshot), or "".
func (r *Registry) SiteOf(id string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.workers[id]; w != nil {
		return w.SiteID
	}
	return ""
}

// List returns a stable-ordered copy with Online computed from LastSeen.
func (r *Registry) List() []Worker {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]Worker, 0, len(r.workers))
	for _, w := range r.workers {
		c := *w
		c.Online = r.now().Sub(w.LastSeen) <= r.OfflineAfter
		if !c.Online {
			c.State = "offline"
		}
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].WorkerID < out[j].WorkerID })
	return out
}

// Route is the minimum the data plane needs about a worker.
type Route struct {
	ID, URL, Model string
	Slots          int
}

// Routable returns workers that can take inference right now: online, engine
// running, URL known. Unsorted and allocation-light; called by the router's cache
// refresh, never per request.
func (r *Registry) Routable() []Route {
	r.mu.RLock()
	defer r.mu.RUnlock()
	now := r.now()
	var out []Route
	for _, w := range r.workers {
		if w.State == "running" && w.EngineURL != "" && now.Sub(w.LastSeen) <= r.OfflineAfter {
			if strings.HasPrefix(w.Model, "rpc://") || w.Model == "rpc-server" {
				continue
			}
			slots := int(w.Slots)
			if slots < 1 {
				slots = 1
			}
			out = append(out, Route{w.WorkerID, w.EngineURL, w.Model, slots})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID }) // deterministic tie-breaking
	return out
}
