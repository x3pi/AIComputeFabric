package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Topology modes supported by AICF.
const (
	TopologyModeSplit   = "split"
	TopologyModeSmart1G = "smart_1gpu"
	TopologyModeMerged  = "merged"
	TopologyModeRPCWrk  = "rpc-worker"
)

// rpcModelMarker is what a worker reports as its model while it serves as a GGML RPC member (see worker-guard baseName).
const rpcModelMarker = "rpc://"

// ErrTopologyBusy is returned while another switch is still running; the API maps it to 409.
var ErrTopologyBusy = errors.New("a topology switch is already in progress")

// topologyRequestError marks a request the operator can fix (HTTP 400).
type topologyRequestError struct{ msg string }

func (e *topologyRequestError) Error() string { return e.msg }

func badTopology(format string, a ...any) error {
	return &topologyRequestError{fmt.Sprintf(format, a...)}
}

var tensorSplitPattern = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?(,[0-9]+(\.[0-9]+)?)*$`)

// TopologyWorkerStatus describes the role and state of one worker in the current topology.
type TopologyWorkerStatus struct {
	WorkerID  string `json:"worker_id"`
	Group     string `json:"group_id,omitempty"` // id of the merged group the worker belongs to
	Role      string `json:"role"`               // master, member, standalone
	Mode      string `json:"mode"`               // split, smart_1gpu, merged, rpc-worker
	Model     string `json:"model"`
	Online    bool   `json:"online"`
	State     string `json:"state"` // running, reclaiming, yielded, etc.
	GPUName   string `json:"gpu_name"`
	VRAMTotal uint64 `json:"vram_total_bytes"`
	VRAMFree  uint64 `json:"vram_free_bytes"`
	EngineURL string `json:"engine_url"`
	Badge     string `json:"badge"`
}

// TopologyGroupStatus is one merged group: a master plus its RPC members serving one model together.
type TopologyGroupStatus struct {
	ID           string   `json:"id"` // = primary worker id
	PrimaryID    string   `json:"primary_worker_id"`
	RPCWorkerIDs []string `json:"rpc_worker_ids"`
	Model        string   `json:"model"`
	KVType       string   `json:"kv_type,omitempty"`
	TensorSplit  string   `json:"tensor_split,omitempty"`
	RPCPort      int      `json:"rpc_port,omitempty"`
	VRAMTotal    uint64   `json:"vram_total_bytes"`
}

// ClusterTopologyStatus is returned by GET /api/cluster/topology.
//
// Groups lists every merged group; workers that are in no group run on their own (split or smart_1gpu). The top-level
// Mode/PrimaryWorkerID/... fields are the legacy single-group view: the first merged group if there is one, else
// smart_1gpu if any worker is in it, else split.
type ClusterTopologyStatus struct {
	Mode            string                          `json:"mode"` // split, smart_1gpu, merged
	PrimaryWorkerID string                          `json:"primary_worker_id,omitempty"`
	RPCWorkerIDs    []string                        `json:"rpc_worker_ids"`
	Model           string                          `json:"model"`
	NCpuMoe         int                             `json:"n_cpu_moe,omitempty"`
	KVType          string                          `json:"kv_type,omitempty"`
	TensorSplit     string                          `json:"tensor_split,omitempty"`
	RPCPort         int                             `json:"rpc_port,omitempty"`
	Status          string                          `json:"status"` // ready, switching, error
	LastError       string                          `json:"last_error,omitempty"`
	UpdatedAt       time.Time                       `json:"updated_at"`
	Groups          []TopologyGroupStatus           `json:"groups"`
	Busy            []string                        `json:"busy_worker_ids"`
	Workers         map[string]TopologyWorkerStatus `json:"workers"`
}

// TopologySwitchRequest is received by POST /api/cluster/topology.
//
// It (re)configures exactly the listed workers (primary_worker_id + rpc_worker_ids); other workers and groups are not
// touched, so several groups can be built independently. A request that touches any member of an existing merged
// group must list the whole group (a group is dissolved or replaced as a unit).
type TopologySwitchRequest struct {
	Mode            string   `json:"mode"` // split, smart_1gpu, merged
	PrimaryWorkerID string   `json:"primary_worker_id,omitempty"`
	RPCWorkerIDs    []string `json:"rpc_worker_ids,omitempty"`
	Model           string   `json:"model,omitempty"`
	NCpuMoe         int      `json:"n_cpu_moe,omitempty"`
	KVType          string   `json:"kv_type,omitempty"`
	TensorSplit     string   `json:"tensor_split,omitempty"`
	RPCPort         int      `json:"rpc_port,omitempty"`
}

// TopologyCoordinator coordinates topology switches across cluster workers.
//
// Control channel: it POSTs worker-guard's /api/engine at the engine URL the worker registered. That is the interim
// design of the plan (D-TOPO): the worker API is unauthenticated, so keep it on a trusted LAN. A switch is only
// reported successful once the workers' own heartbeats show the new model/state, never on the HTTP 202 alone.
//
// State is per worker, not global: `groups` holds the merged groups (key = master id), `indep` the standalone workers'
// last applied configuration, and `locked` the workers a running switch is touching. Switches on disjoint worker sets
// run concurrently; one that overlaps a locked worker gets ErrTopologyBusy.
// DefaultTopologyFile is the optional JSON file path where NewTopologyCoordinator persists and reloads
// cluster topology state across gateway restarts (set by -topology-file in main).
var DefaultTopologyFile = ""

type persistedTopoPlan struct {
	Mode        string   `json:"mode"`
	PrimaryID   string   `json:"primary_worker_id"`
	RPCIDs      []string `json:"rpc_worker_ids,omitempty"`
	Model       string   `json:"model"`
	NCpuMoe     int      `json:"n_cpu_moe,omitempty"`
	KVType      string   `json:"kv_type,omitempty"`
	TensorSplit string   `json:"tensor_split,omitempty"`
	RPCPort     int      `json:"rpc_port,omitempty"`
	RPCServers  string   `json:"rpc_servers,omitempty"`
}

type persistedTopologyState struct {
	Groups    map[string]persistedTopoPlan `json:"groups,omitempty"`
	Indep     map[string]persistedTopoPlan `json:"indep,omitempty"`
	UpdatedAt time.Time                    `json:"updated_at"`
}

func toPersistedPlan(p topoPlan) persistedTopoPlan {
	return persistedTopoPlan{
		Mode: p.mode, PrimaryID: p.primaryID, RPCIDs: append([]string{}, p.rpcIDs...),
		Model: p.model, NCpuMoe: p.nCpuMoe, KVType: p.kvType,
		TensorSplit: p.tensorSplit, RPCPort: p.rpcPort, RPCServers: p.rpcServers,
	}
}

func fromPersistedPlan(p persistedTopoPlan) topoPlan {
	return topoPlan{
		mode: p.Mode, primaryID: p.PrimaryID, rpcIDs: append([]string{}, p.RPCIDs...),
		model: p.Model, nCpuMoe: p.NCpuMoe, kvType: p.KVType,
		tensorSplit: p.TensorSplit, rpcPort: p.RPCPort, rpcServers: p.RPCServers,
	}
}

type TopologyCoordinator struct {
	// Timeouts and poll interval for waiting on heartbeats; exported so tests (and later flags) can change them.
	RPCReadyTimeout   time.Duration
	LoadTimeout       time.Duration
	Poll              time.Duration
	ReconcileCooldown time.Duration

	reg    *Registry
	audit  *AuditLog
	client *http.Client

	mu              sync.RWMutex // guards the fields below; never held across network calls
	stateFile       string
	groups          map[string]topoPlan
	indep           map[string]topoPlan // key = worker id; plan.primaryID = that worker, no rpcIDs
	locked          map[string]bool
	reconciledIndep map[string]bool
	lastReconcile   map[string]time.Time
	status          string
	lastError       string
	updatedAt       time.Time
}

func NewTopologyCoordinator(reg *Registry, audit *AuditLog) *TopologyCoordinator {
	tc := &TopologyCoordinator{
		RPCReadyTimeout:   30 * time.Second,
		LoadTimeout:       300 * time.Second, // a 18 GB model through RPC on first load is slow
		Poll:              500 * time.Millisecond,
		ReconcileCooldown: 30 * time.Second,
		reg:               reg,
		audit:             audit,
		client:            &http.Client{Timeout: 30 * time.Second},
		groups:            map[string]topoPlan{},
		indep:             map[string]topoPlan{},
		locked:            map[string]bool{},
		reconciledIndep:   map[string]bool{},
		lastReconcile:     map[string]time.Time{},
		status:            "ready",
		updatedAt:         time.Now(),
	}
	if DefaultTopologyFile != "" {
		_ = tc.SetStateFile(DefaultTopologyFile)
	}
	return tc
}

// SetStateFile configures the JSON file used to persist topology state and loads any existing state from disk.
func (tc *TopologyCoordinator) SetStateFile(path string) error {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	tc.stateFile = path
	return tc.loadStateLocked()
}

func (tc *TopologyCoordinator) loadStateLocked() error {
	if tc.stateFile == "" {
		return nil
	}
	data, err := os.ReadFile(tc.stateFile)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	var ps persistedTopologyState
	if err := json.Unmarshal(data, &ps); err != nil {
		log.Printf("topology state file %s: %v", tc.stateFile, err)
		return err
	}
	tc.groups = map[string]topoPlan{}
	for k, v := range ps.Groups {
		tc.groups[k] = fromPersistedPlan(v)
	}
	tc.indep = map[string]topoPlan{}
	for k, v := range ps.Indep {
		tc.indep[k] = fromPersistedPlan(v)
	}
	tc.reconciledIndep = map[string]bool{}
	if !ps.UpdatedAt.IsZero() {
		tc.updatedAt = ps.UpdatedAt
	}
	return nil
}

func (tc *TopologyCoordinator) saveStateLocked() {
	if tc.stateFile == "" {
		return
	}
	ps := persistedTopologyState{
		Groups:    make(map[string]persistedTopoPlan, len(tc.groups)),
		Indep:     make(map[string]persistedTopoPlan, len(tc.indep)),
		UpdatedAt: tc.updatedAt,
	}
	for k, v := range tc.groups {
		ps.Groups[k] = toPersistedPlan(v)
	}
	for k, v := range tc.indep {
		ps.Indep[k] = toPersistedPlan(v)
	}
	data, err := json.MarshalIndent(ps, "", "  ")
	if err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(tc.stateFile), 0o755); err != nil {
		return
	}
	tmp := tc.stateFile + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return
	}
	_ = os.Rename(tmp, tc.stateFile)
}

// GetStatus returns the current topology state merged with live worker status.
func (tc *TopologyCoordinator) GetStatus() ClusterTopologyStatus {
	tc.mu.RLock()
	defer tc.mu.RUnlock()
	return tc.statusLocked()
}

func (tc *TopologyCoordinator) statusLocked() ClusterTopologyStatus {
	workers := tc.reg.List()
	vram := map[string]uint64{}
	for _, w := range workers {
		vram[w.WorkerID] = w.VRAMTotal
	}
	gids := make([]string, 0, len(tc.groups))
	for id := range tc.groups {
		gids = append(gids, id)
	}
	sort.Strings(gids)
	groups := make([]TopologyGroupStatus, 0, len(gids))
	groupOf := map[string]string{}
	for _, id := range gids {
		g := tc.groups[id]
		gs := TopologyGroupStatus{ID: id, PrimaryID: g.primaryID, RPCWorkerIDs: append([]string{}, g.rpcIDs...), Model: g.model,
			KVType: g.kvType, TensorSplit: g.tensorSplit, RPCPort: g.rpcPort}
		for _, m := range append([]string{g.primaryID}, g.rpcIDs...) {
			gs.VRAMTotal += vram[m]
			groupOf[m] = id
		}
		groups = append(groups, gs)
	}
	workersMap := make(map[string]TopologyWorkerStatus, len(workers))
	for _, w := range workers {
		st := TopologyWorkerStatus{
			WorkerID: w.WorkerID, Model: w.Model, Online: w.Online, State: w.State, GPUName: w.GPUName,
			VRAMTotal: w.VRAMTotal, VRAMFree: w.VRAMFree, EngineURL: w.EngineURL,
			Role: "standalone", Mode: TopologyModeSplit, Badge: "SPLIT WORKER",
		}
		if gid, ok := groupOf[w.WorkerID]; ok {
			g, gs := tc.groups[gid], groups[sort.SearchStrings(gids, gid)]
			st.Group = gid
			if w.WorkerID == g.primaryID {
				st.Role, st.Mode = "master", TopologyModeMerged
				st.Badge = fmt.Sprintf("MASTER (MERGED %d GPU, %.0f GB)", 1+len(g.rpcIDs), float64(gs.VRAMTotal)/(1<<30))
			} else {
				st.Role, st.Mode = "member", TopologyModeRPCWrk
				st.Badge = fmt.Sprintf("RPC GPU MEMBER -> %s", g.primaryID)
			}
		} else if c, ok := tc.indep[w.WorkerID]; ok && c.mode == TopologyModeSmart1G {
			st.Mode, st.Badge = TopologyModeSmart1G, "SMART 1-GPU HYBRID"
		} else if len(groups) > 0 {
			st.Badge = "STANDALONE"
		}
		workersMap[w.WorkerID] = st
	}
	busy := make([]string, 0, len(tc.locked))
	for id := range tc.locked {
		busy = append(busy, id)
	}
	sort.Strings(busy)
	out := ClusterTopologyStatus{Mode: TopologyModeSplit, RPCWorkerIDs: []string{}, Status: tc.status, LastError: tc.lastError,
		UpdatedAt: tc.updatedAt, Groups: groups, Busy: busy, Workers: workersMap}
	legacy := topoPlan{}
	switch {
	case len(gids) > 0:
		legacy, out.Mode = tc.groups[gids[0]], TopologyModeMerged
	default:
		ids := make([]string, 0, len(tc.indep))
		for id := range tc.indep {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		for _, id := range ids {
			if tc.indep[id].mode == TopologyModeSmart1G {
				legacy, out.Mode = tc.indep[id], TopologyModeSmart1G
				break
			}
		}
		if legacy.model == "" && len(ids) > 0 {
			legacy = tc.indep[ids[0]]
		}
	}
	out.Model, out.NCpuMoe, out.KVType, out.TensorSplit, out.RPCPort = legacy.model, legacy.nCpuMoe, legacy.kvType, legacy.tensorSplit, legacy.rpcPort
	if out.Mode == TopologyModeMerged {
		out.PrimaryWorkerID, out.RPCWorkerIDs = legacy.primaryID, append([]string{}, legacy.rpcIDs...)
	} else if legacy.primaryID != "" {
		out.PrimaryWorkerID = legacy.primaryID
	}
	return out
}

// topoPlan is a validated request with every default resolved (also the stored record of an applied configuration).
type topoPlan struct {
	mode        string
	primaryID   string
	rpcIDs      []string
	model       string
	nCpuMoe     int
	kvType      string
	tensorSplit string
	rpcPort     int
	rpcServers  string
}

func (p topoPlan) ids() []string { return append([]string{p.primaryID}, p.rpcIDs...) }

func (tc *TopologyCoordinator) validate(req TopologySwitchRequest, workers map[string]Worker) (topoPlan, error) {
	p := topoPlan{mode: strings.TrimSpace(req.Mode), primaryID: req.PrimaryWorkerID, rpcIDs: req.RPCWorkerIDs,
		model: strings.TrimSpace(req.Model), nCpuMoe: req.NCpuMoe, kvType: req.KVType,
		tensorSplit: strings.TrimSpace(req.TensorSplit), rpcPort: req.RPCPort}
	if p.mode != TopologyModeSplit && p.mode != TopologyModeSmart1G && p.mode != TopologyModeMerged {
		return p, badTopology("invalid mode %q: must be split, smart_1gpu or merged", req.Mode)
	}
	if p.model == "" {
		return p, badTopology("model is required")
	}
	if p.kvType != "" && p.kvType != "f16" && p.kvType != "q8_0" {
		return p, badTopology("kv_type must be f16 or q8_0")
	}
	if p.nCpuMoe < 0 {
		return p, badTopology("n_cpu_moe must be >= 0")
	}
	if p.mode == TopologyModeSmart1G && p.nCpuMoe <= 0 {
		return p, badTopology("smart_1gpu needs n_cpu_moe > 0: no default is guessed (measure N for this card first)")
	}
	if p.rpcPort <= 0 {
		p.rpcPort = 50052
	}
	if p.rpcPort > 65535 {
		return p, badTopology("rpc_port out of range")
	}
	if p.primaryID == "" {
		return p, badTopology("primary_worker_id is required")
	}
	seen := map[string]bool{}
	for _, id := range p.ids() {
		if seen[id] {
			return p, badTopology("worker %q listed twice", id)
		}
		seen[id] = true
		w, ok := workers[id]
		if !ok {
			return p, badTopology("worker %q is not registered", id)
		}
		if !w.Online || w.EngineURL == "" {
			return p, badTopology("worker %q is offline or has no engine URL", id)
		}
	}
	if p.mode == TopologyModeMerged {
		if len(p.rpcIDs) == 0 {
			return p, badTopology("merged needs at least one rpc_worker_ids entry")
		}
		n := 1 + len(p.rpcIDs)
		if p.tensorSplit == "" {
			p.tensorSplit = strings.TrimSuffix(strings.Repeat("1,", n), ",")
		}
		if !tensorSplitPattern.MatchString(p.tensorSplit) || len(strings.Split(p.tensorSplit, ",")) != n {
			return p, badTopology("tensor_split must have %d comma-separated numbers (primary first, then rpc workers in order; the gateway reorders it for llama.cpp)", n)
		}
	} else {
		p.tensorSplit = ""
	}
	// smart_1gpu and split run the model on every listed worker independently.
	return p, nil
}

// SwitchTopology (re)configures the listed workers as split, smart_1gpu or one merged group. It returns once the workers'
// heartbeats confirm the new state; on failure it restores the previous configuration (best effort) and reports both.
func (tc *TopologyCoordinator) SwitchTopology(ctx context.Context, req TopologySwitchRequest, caller Principal, remoteAddr string) (*ClusterTopologyStatus, error) {
	workers := map[string]Worker{}
	for _, w := range tc.reg.List() {
		workers[w.WorkerID] = w
	}
	plan, err := tc.validate(req, workers)
	if err != nil {
		tc.record(caller, remoteAddr, http.StatusBadRequest)
		return nil, err
	}
	ids := plan.ids()

	tc.mu.Lock()
	for _, id := range ids {
		if tc.locked[id] {
			tc.mu.Unlock()
			tc.record(caller, remoteAddr, http.StatusConflict)
			return nil, fmt.Errorf("worker %s: %w", id, ErrTopologyBusy)
		}
	}
	// a merged group is changed as a unit: touching one member without the others would orphan them
	var prevGroups []topoPlan
	for gid, g := range tc.groups {
		touched := false
		for _, m := range g.ids() {
			touched = touched || containsString(ids, m)
		}
		if !touched {
			continue
		}
		for _, m := range g.ids() {
			if !containsString(ids, m) {
				tc.mu.Unlock()
				tc.record(caller, remoteAddr, http.StatusBadRequest)
				return nil, badTopology("worker %q is in merged group %q: list the whole group (%s) to change or dissolve it",
					m, gid, strings.Join(g.ids(), ", "))
			}
		}
		prevGroups = append(prevGroups, g)
	}
	prevIndep := map[string]topoPlan{}
	for _, id := range ids {
		if c, ok := tc.indep[id]; ok {
			prevIndep[id] = c
		}
		tc.locked[id] = true
	}
	tc.status, tc.lastError = "switching", ""
	tc.mu.Unlock()
	prev := topoPrev{groups: prevGroups, indep: prevIndep}

	// What each touched worker reports now, used to restore it if the switch fails.
	before := map[string]Worker{}
	for _, id := range ids {
		before[id] = workers[id]
	}

	var rpcServers string
	switch plan.mode {
	case TopologyModeMerged:
		rpcServers, err = tc.applyMerged(ctx, plan, workers)
	default:
		err = tc.applyIndependent(ctx, plan, prev, workers)
	}

	if err != nil {
		rbErr := tc.rollback(ctx, plan, prev, before)
		msg := err.Error()
		if rbErr != nil {
			msg += "; rollback incomplete: " + rbErr.Error()
		} else {
			msg += "; previous configuration restored"
		}
		tc.mu.Lock()
		tc.unlockLocked(ids, "error", msg)
		tc.mu.Unlock()
		tc.record(caller, remoteAddr, http.StatusInternalServerError)
		log.Printf("topology switch to %s failed: %s", plan.mode, msg)
		return nil, errors.New(msg)
	}

	tc.mu.Lock()
	for _, g := range prevGroups {
		delete(tc.groups, g.primaryID)
	}
	plan.rpcServers = rpcServers
	if plan.mode == TopologyModeMerged {
		tc.groups[plan.primaryID] = plan
		for _, id := range ids {
			delete(tc.indep, id)
			delete(tc.reconciledIndep, id)
		}
	} else {
		for _, id := range ids {
			c := plan
			c.primaryID, c.rpcIDs = id, nil
			tc.indep[id] = c
			tc.reconciledIndep[id] = true
		}
	}
	tc.unlockLocked(ids, "ready", "")
	tc.saveStateLocked()
	cur := tc.statusLocked()
	tc.mu.Unlock()
	tc.record(caller, remoteAddr, http.StatusOK)
	return &cur, nil
}

// ReconcileOnce checks whether online, idle workers match the persisted topology state (e.g. after a worker or
// gateway restart when a worker lacked its local state file) and re-applies any out-of-sync group or un-reconciled
// independent configuration.
func (tc *TopologyCoordinator) ReconcileOnce(ctx context.Context) {
	workers := map[string]Worker{}
	for _, w := range tc.reg.List() {
		workers[w.WorkerID] = w
	}

	var toApply []TopologySwitchRequest
	now := time.Now()
	cooldown := tc.ReconcileCooldown
	if cooldown <= 0 {
		cooldown = 30 * time.Second
	}

	tc.mu.Lock()
	for id := range tc.indep {
		if w, ok := workers[id]; !ok || !w.Online {
			delete(tc.reconciledIndep, id)
		}
	}
	for _, g := range tc.groups {
		ready := true
		for _, id := range g.ids() {
			w, ok := workers[id]
			if !ok || !w.Online || w.State != "running" || w.Switching || tc.locked[id] {
				ready = false
				break
			}
		}
		if !ready {
			continue
		}
		port := g.rpcPort
		if port <= 0 {
			port = 50052
		}
		marker := fmt.Sprintf("%s%d", rpcModelMarker, port)
		outOfSync := workers[g.primaryID].Model != g.model
		for _, id := range g.rpcIDs {
			if workers[id].Model != marker {
				outOfSync = true
			}
		}
		key := "group:" + g.primaryID
		if outOfSync && now.Sub(tc.lastReconcile[key]) >= cooldown {
			tc.lastReconcile[key] = now
			toApply = append(toApply, TopologySwitchRequest{
				Mode:            TopologyModeMerged,
				PrimaryWorkerID: g.primaryID,
				RPCWorkerIDs:    append([]string{}, g.rpcIDs...),
				Model:           g.model,
				KVType:          g.kvType,
				TensorSplit:     g.tensorSplit,
				RPCPort:         port,
			})
		}
	}
	for id, c := range tc.indep {
		w, ok := workers[id]
		if !ok || !w.Online || w.State != "running" || w.Switching || tc.locked[id] {
			continue
		}
		if tc.reconciledIndep[id] {
			continue
		}
		if w.Model == c.model {
			tc.reconciledIndep[id] = true
			continue
		}
		key := "indep:" + id
		if now.Sub(tc.lastReconcile[key]) >= cooldown {
			tc.lastReconcile[key] = now
			tc.reconciledIndep[id] = true
			toApply = append(toApply, TopologySwitchRequest{
				Mode:            c.mode,
				PrimaryWorkerID: c.primaryID,
				Model:           c.model,
				NCpuMoe:         c.nCpuMoe,
				KVType:          c.kvType,
			})
		}
	}
	tc.mu.Unlock()

	for _, req := range toApply {
		_, _ = tc.SwitchTopology(ctx, req, Principal{Name: "system:reconcile", Role: RoleAdmin}, "127.0.0.1")
	}
}

// RunReconciler periodically reconciles persisted topology state with online workers until stop is closed.
func (tc *TopologyCoordinator) RunReconciler(stop <-chan struct{}) {
	t := time.NewTicker(3 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			tc.ReconcileOnce(context.Background())
		}
	}
}

// unlockLocked releases a switch's workers. The overall status only leaves "switching" when no other switch is running.
func (tc *TopologyCoordinator) unlockLocked(ids []string, status, lastErr string) {
	for _, id := range ids {
		delete(tc.locked, id)
	}
	if len(tc.locked) == 0 {
		tc.status = status
	}
	if lastErr != "" || status == "ready" {
		tc.lastError = lastErr
	}
	tc.updatedAt = time.Now()
}

// topoPrev is what the touched workers were doing before a switch: the merged groups they were in and, for the others,
// their last independent configuration.
type topoPrev struct {
	groups []topoPlan
	indep  map[string]topoPlan
}

func (pv topoPrev) groupOf(id string) (topoPlan, bool) {
	for _, g := range pv.groups {
		if containsString(g.ids(), id) {
			return g, true
		}
	}
	return topoPlan{}, false
}

func (tc *TopologyCoordinator) record(caller Principal, remoteAddr string, code int) {
	if tc.audit != nil {
		_ = tc.audit.Record(caller.Name, caller.Role, "POST", "/api/cluster/topology", code, remoteAddr)
	}
}

// applyMerged: every RPC member first (and wait until each reports rpc-ready), then the master.
func (tc *TopologyCoordinator) applyMerged(ctx context.Context, p topoPlan, workers map[string]Worker) (string, error) {
	var addrs []string
	for _, id := range p.rpcIDs {
		w := workers[id]
		host := extractHost(w.EngineURL)
		if host == "" || host == "127.0.0.1" || host == "localhost" {
			host = w.Hostname
		}
		addrs = append(addrs, net.JoinHostPort(host, strconv.Itoa(p.rpcPort)))
		if err := tc.postEngine(ctx, w.EngineURL, map[string]any{"mode": TopologyModeRPCWrk, "rpc_port": p.rpcPort}); err != nil {
			return "", fmt.Errorf("rpc worker %s: %w", id, err)
		}
	}
	marker := fmt.Sprintf("%s%d", rpcModelMarker, p.rpcPort)
	for _, id := range p.rpcIDs {
		if err := tc.waitFor(ctx, id, tc.RPCReadyTimeout, func(w Worker) bool { return w.State == "running" && w.Model == marker }); err != nil {
			return "", fmt.Errorf("rpc worker %s not rpc-ready: %w", id, err)
		}
	}
	servers := strings.Join(addrs, ",")
	payload := map[string]any{"mode": TopologyModeMerged, "model": p.model, "rpc_servers": servers,
		"split_mode": "layer", "tensor_split": llamaTensorSplit(p.tensorSplit)}
	if p.kvType != "" {
		payload["kv_type"] = p.kvType
	}
	if err := tc.postEngine(ctx, workers[p.primaryID].EngineURL, payload); err != nil {
		return "", fmt.Errorf("master %s: %w", p.primaryID, err)
	}
	if err := tc.waitFor(ctx, p.primaryID, tc.LoadTimeout, modelRunning(p.model, time.Now())); err != nil {
		return "", fmt.Errorf("master %s did not start %s: %w", p.primaryID, p.model, err)
	}
	return servers, nil
}

// applyIndependent covers split and smart_1gpu: every listed worker serves the model on its own.
// A former merged master goes first so it releases its RPC connections before its members change.
func (tc *TopologyCoordinator) applyIndependent(ctx context.Context, p topoPlan, prev topoPrev, workers map[string]Worker) error {
	order := p.ids()
	sort.SliceStable(order, func(i, j int) bool { return isMaster(prev, order[i]) && !isMaster(prev, order[j]) })
	posted := map[string]time.Time{}
	for _, id := range order {
		payload := map[string]any{"mode": p.mode, "model": p.model, "n_cpu_moe": p.nCpuMoe}
		if p.kvType != "" {
			payload["kv_type"] = p.kvType
		}
		if err := tc.postEngine(ctx, workers[id].EngineURL, payload); err != nil {
			return fmt.Errorf("worker %s: %w", id, err)
		}
		posted[id] = time.Now()
		if isMaster(prev, id) {
			if err := tc.waitFor(ctx, id, tc.LoadTimeout, modelRunning(p.model, posted[id])); err != nil {
				return fmt.Errorf("former master %s did not restart: %w", id, err)
			}
		}
	}
	for _, id := range order {
		if err := tc.waitFor(ctx, id, tc.LoadTimeout, modelRunning(p.model, posted[id])); err != nil {
			return fmt.Errorf("worker %s did not start %s: %w", id, p.model, err)
		}
	}
	return nil
}

func isMaster(prev topoPrev, id string) bool {
	g, ok := prev.groupOf(id)
	return ok && g.primaryID == id
}

// rollback puts every touched worker back as it was reported before the switch (and, for a former merged master,
// with its merged configuration). It does not wait for the restore: the guard restarts engines when the GPU is free.
func (tc *TopologyCoordinator) rollback(ctx context.Context, p topoPlan, prev topoPrev, before map[string]Worker) error {
	var failed []string
	ids := p.ids()
	// restore RPC members first when they were in a merged group, so the master can reconnect
	sort.SliceStable(ids, func(i, j int) bool { return !isMaster(prev, ids[i]) && isMaster(prev, ids[j]) })
	for _, id := range ids {
		b := before[id]
		if b.EngineURL == "" {
			continue
		}
		g, inGroup := prev.groupOf(id)
		pi := prev.indep[id]
		var payload map[string]any
		switch {
		case strings.HasPrefix(b.Model, rpcModelMarker):
			port, _ := strconv.Atoi(strings.TrimPrefix(b.Model, rpcModelMarker))
			payload = map[string]any{"mode": TopologyModeRPCWrk, "rpc_port": port}
		case b.Model == "":
			continue // nothing known to restore
		case inGroup && g.primaryID == id:
			payload = map[string]any{"mode": TopologyModeMerged, "model": b.Model, "rpc_servers": g.rpcServers,
				"split_mode": "layer", "tensor_split": llamaTensorSplit(g.tensorSplit)}
		case pi.mode == TopologyModeSmart1G:
			payload = map[string]any{"mode": TopologyModeSmart1G, "model": b.Model, "n_cpu_moe": pi.nCpuMoe}
		default:
			payload = map[string]any{"mode": TopologyModeSplit, "model": b.Model}
		}
		kv := pi.kvType
		if inGroup {
			kv = g.kvType
		}
		if kv != "" && payload["mode"] != TopologyModeRPCWrk {
			payload["kv_type"] = kv
		}
		if err := tc.postEngine(ctx, b.EngineURL, payload); err != nil {
			failed = append(failed, fmt.Sprintf("%s: %v", id, err))
		}
	}
	if len(failed) > 0 {
		return errors.New(strings.Join(failed, "; "))
	}
	return nil
}

// llamaTensorSplit converts the API order (primary first, then the rpc workers in order) to llama.cpp's device order.
// Measured 2026-10-27 (one RPC server): llama-server lists "RPC0 ... then CUDA0", and --tensor-split 4,6 gave RPC0 40 % /
// CUDA0 60 % (p3_mode3_4b_split46.log), i.e. the RPC devices come first and the local GPU last.
func llamaTensorSplit(apiOrder string) string {
	parts := strings.Split(apiOrder, ",")
	if len(parts) < 2 {
		return apiOrder
	}
	return strings.Join(append(append([]string{}, parts[1:]...), parts[0]), ",")
}

// modelRunning holds when the worker serves model and is not switching, judged from a heartbeat received AFTER `after`
// (the moment the directive was accepted). Without that, a change that keeps the model name (e.g. only -ncmoe N)
// would match the stale heartbeat from before the change and report success while the engine is still reloading.
func modelRunning(model string, after time.Time) func(Worker) bool {
	return func(w Worker) bool {
		return w.State == "running" && w.Model == model && !w.Switching && w.LastSeen.After(after)
	}
}

// waitFor polls the registry (fed by worker heartbeats) until ok(worker) holds or the timeout/ctx ends.
func (tc *TopologyCoordinator) waitFor(ctx context.Context, id string, timeout time.Duration, ok func(Worker) bool) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	tick := time.NewTicker(tc.Poll)
	defer tick.Stop()
	for {
		for _, w := range tc.reg.List() {
			if w.WorkerID == id && w.Online && ok(w) {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline.C:
			return fmt.Errorf("timed out after %s", timeout)
		case <-tick.C:
		}
	}
}

// WorkerControlPort is the port of the worker-guard control API (worker-guard --listen). engine_url that workers
// register is the INFERENCE endpoint (llama-server), which has no /api/engine, so the control URL is the same host
// with this port. 0 = the control API is served at engine_url itself (tests). Interim until D-TOPO moves the
// directive into the heartbeat reply (then no inbound connection to the worker is needed).
var WorkerControlPort = 0

func controlURL(engineURL string) string {
	if WorkerControlPort == 0 {
		return engineURL
	}
	return "http://" + net.JoinHostPort(extractHost(engineURL), strconv.Itoa(WorkerControlPort))
}

func (tc *TopologyCoordinator) postEngine(ctx context.Context, engineURL string, body map[string]any) error {
	endpoint := strings.TrimRight(controlURL(engineURL), "/") + "/api/engine"
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(data))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := tc.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusAccepted {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<12))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return nil
}

func extractHost(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	h, _, err := net.SplitHostPort(u.Host)
	if err == nil {
		return h
	}
	return u.Host
}

func containsString(list []string, item string) bool {
	for _, s := range list {
		if s == item {
			return true
		}
	}
	return false
}
