package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ComputeAgent is the hardware-hiding inference endpoint abstraction exposed to orchestrators
// (Pha A, KE_HOACH_BEST_OF_N_CODE.md §2B).
//
// An orchestrator (such as bestofn/) sees ONLY ComputeAgents:
//   - kind: "single" = one standalone worker (mode split or smart_1gpu), agent_id = worker_id
//   - kind: "group"  = one merged multi-GPU group (mode merged), agent_id = group id (= primary_worker_id)
//
// RPC member workers (model "rpc://..." or listed in rpc_worker_ids) are NEVER ComputeAgents.
// Hardware details (gpu_name, vram_*, tensor_split) are omitted. The optional `members` slice is
// populated ONLY when an authenticated admin/operator explicitly passes ?include_members=1 for UI inspection;
// orchestrators and tenant API key callers never receive `members`.
type ComputeAgent struct {
	AgentID    string   `json:"agent_id"`
	Kind       string   `json:"kind"` // "single" | "group"
	Model      string   `json:"model"`
	CtxPerSlot int      `json:"ctx_per_slot"`
	Slots      int      `json:"slots"`
	State      string   `json:"state"` // "running", "switching", "paused", "offline", etc.
	Busy       bool     `json:"busy"`
	Inflight   int      `json:"inflight"`
	Labels     []string `json:"labels"`
	TokSRef    float64  `json:"tok_s_ref"`
	Members    []string `json:"members,omitempty"`
}

// CustomAgentLabels allows tests or operators to attach extra capability labels to specific worker/agent IDs.
var (
	customLabelsMu    sync.RWMutex
	CustomAgentLabels = map[string][]string{}
)

// SetCustomAgentLabels sets additional capability labels for an agent ID (used in tests and config).
func SetCustomAgentLabels(agentID string, labels []string) {
	customLabelsMu.Lock()
	defer customLabelsMu.Unlock()
	if len(labels) == 0 {
		delete(CustomAgentLabels, agentID)
		return
	}
	CustomAgentLabels[agentID] = append([]string{}, labels...)
}

// LabelsForAgent derives capability labels from the served model name plus any custom labels registered for agentID.
func LabelsForAgent(model, agentID string) []string {
	m := strings.ToLower(strings.TrimSpace(model))
	seen := map[string]bool{}
	var out []string
	add := func(l string) {
		if l != "" && !seen[l] {
			seen[l] = true
			out = append(out, l)
		}
	}
	if strings.Contains(m, "coder") || strings.Contains(m, "code") {
		add("code")
		add("coder")
	}
	if strings.Contains(m, "r1") || strings.Contains(m, "reason") {
		add("reasoning")
		add("code")
	}
	if strings.Contains(m, "qwen") || strings.Contains(m, "deepseek") || strings.Contains(m, "llama") {
		add("code")
	}
	if strings.Contains(m, "math") {
		add("math")
	}
	if m != "" && !strings.HasPrefix(m, "rpc://") && m != "rpc-server" {
		add("general")
	}
	customLabelsMu.RLock()
	extra := CustomAgentLabels[agentID]
	customLabelsMu.RUnlock()
	for _, l := range extra {
		add(strings.ToLower(strings.TrimSpace(l)))
	}
	if len(out) == 0 {
		out = []string{"general"}
	}
	return out
}

// HasLabel reports whether an agent with (model, agentID) carries the requested capability label.
func HasLabel(model, agentID, want string) bool {
	want = strings.ToLower(strings.TrimSpace(want))
	if want == "" {
		return true
	}
	for _, l := range LabelsForAgent(model, agentID) {
		if l == want {
			return true
		}
	}
	return false
}

// ReferenceTokS returns the single-stream reference throughput (tok/s) from measured hardware tables
// (benchmarks/reports/P3_BANG_SO_LIEU_CUOI.md, P3_CODER_30B_GOP_2_CARD.md, P9_CPU_AFFINITY_KV.md).
// Fake test workers may also pass ?tok_s_ref=<float> on their EngineURL to simulate heterogeneous speeds.
func ReferenceTokS(model, kind, gpuName, engineURL string) float64 {
	if engineURL != "" {
		if u, err := url.Parse(engineURL); err == nil {
			if v, err := strconv.ParseFloat(u.Query().Get("tok_s_ref"), 64); err == nil && v > 0 {
				return v
			}
			for _, seg := range strings.Split(u.Path, "/") {
				if strings.HasPrefix(seg, "toks_") {
					if v, err := strconv.ParseFloat(strings.TrimPrefix(seg, "toks_"), 64); err == nil && v > 0 {
						return v
					}
				}
			}
		}
	}
	m := strings.ToLower(strings.TrimSpace(model))
	g := strings.ToLower(gpuName)
	is5070 := strings.Contains(g, "5070")
	switch {
	case strings.Contains(m, "qwen3-coder-30b-a3b"):
		if kind == "group" {
			return 126.7 // [ĐO] P3_CODER_30B_GOP_2_CARD.md (merged 2 GPU @ 32k)
		}
		if is5070 {
			return 52.2 // [ĐO] P3_BANG_SO_LIEU_CUOI.md (-ncmoe on RTX 5070)
		}
		return 46.2 // [ĐO] P9_CPU_AFFINITY_KV.md (-ncmoe on RTX 3060 + P-core pin)
	case strings.Contains(m, "qwen3-30b-a3b"):
		if kind == "group" {
			return 132.4 // [ĐO] P3_BANG_SO_LIEU_CUOI.md
		}
		if is5070 {
			return 52.2 // [ĐO]
		}
		return 41.5 // [ĐO]
	case strings.Contains(m, "deepseek-r1-distill-qwen-14b"):
		if is5070 {
			return 76.8 // [ĐO]
		}
		return 41.1 // [ĐO]
	case strings.Contains(m, "deepseek-v2-lite"):
		if is5070 {
			return 145.6 // [ĐO]
		}
		return 78.5 // [ĐO]
	case strings.Contains(m, "qwen3-8b-q8_0"):
		return 41.6 // [ĐO]
	case strings.Contains(m, "qwen3-8b"):
		if is5070 {
			return 110.9 // [ĐO]
		}
		return 63.2 // [ĐO]
	case strings.Contains(m, "qwen3-4b-q8_0"):
		return 68.4 // [ĐO]
	case strings.Contains(m, "qwen3-4b-f16"):
		return 39.8 // [ĐO]
	case strings.Contains(m, "qwen3-4b"):
		if is5070 {
			return 170.3 // [ĐO]
		}
		return 106.1 // [ĐO]
	case strings.Contains(m, "qwen3-1.7b"):
		return 135.0
	case strings.Contains(m, "qwen3-0.6b"):
		return 210.0
	default:
		if kind == "group" {
			return 120.0
		}
		return 50.0
	}
}

type ctxCacheEntry struct {
	model      string
	slots      int
	ctxPerSlot int
	at         time.Time
}

var (
	ctxCacheMu     sync.Mutex
	ctxCache       = map[string]ctxCacheEntry{}
	ctxProbeClient = &http.Client{Timeout: 250 * time.Millisecond}
)

// ResolveCtxPerSlot determines the per-slot context window for an agent.
//  1. Checks ?ctx_per_slot=<int> query parameter on engineURL (used by fake workers in tests).
//  2. Queries <engineURL>/props (llama-server's default_generation_settings.n_ctx) and caches the result.
//  3. Falls back to the known profile defaults (32768 for Qwen3-Coder-30B-A3B 1-slot, 8192 for 30B-A3B, 4096 otherwise).
func ResolveCtxPerSlot(workerID, model, engineURL string, slots int) int {
	if slots < 1 {
		slots = 1
	}
	if engineURL != "" {
		if u, err := url.Parse(engineURL); err == nil {
			if v, err := strconv.Atoi(u.Query().Get("ctx_per_slot")); err == nil && v > 0 {
				return v
			}
			for _, seg := range strings.Split(u.Path, "/") {
				if strings.HasPrefix(seg, "ctx_") {
					if v, err := strconv.Atoi(strings.TrimPrefix(seg, "ctx_")); err == nil && v > 0 {
						return v
					}
				}
			}
		}
	}
	now := time.Now()
	ctxCacheMu.Lock()
	if e, ok := ctxCache[workerID]; ok && e.model == model && e.slots == slots && now.Sub(e.at) < 30*time.Second && e.ctxPerSlot > 0 {
		ctxCacheMu.Unlock()
		return e.ctxPerSlot
	}
	ctxCacheMu.Unlock()

	ctx := defaultCtxForModel(model, slots)
	if engineURL != "" && strings.HasPrefix(engineURL, "http") {
		baseURL := strings.Split(engineURL, "?")[0]
		req, err := http.NewRequest(http.MethodGet, strings.TrimRight(baseURL, "/")+"/props", nil)
		if err == nil {
			if resp, err := ctxProbeClient.Do(req); err == nil {
				defer resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					var props struct {
						DefaultGenerationSettings struct {
							NCtx int `json:"n_ctx"`
						} `json:"default_generation_settings"`
						TotalSlots int `json:"total_slots"`
					}
					if json.NewDecoder(resp.Body).Decode(&props) == nil && props.DefaultGenerationSettings.NCtx > 0 {
						ctx = props.DefaultGenerationSettings.NCtx
					}
				}
			}
		}
	}
	ctxCacheMu.Lock()
	ctxCache[workerID] = ctxCacheEntry{model: model, slots: slots, ctxPerSlot: ctx, at: now}
	ctxCacheMu.Unlock()
	return ctx
}

func defaultCtxForModel(model string, slots int) int {
	m := strings.ToLower(model)
	if strings.Contains(m, "qwen3-coder-30b-a3b") && slots == 1 {
		return 32768
	}
	if strings.Contains(m, "30b-a3b") {
		return 8192
	}
	return 4096
}

// BuildComputeAgents constructs the hardware-hiding list of ComputeAgents from the current Registry,
// TopologyCoordinator, and Router state.
//
// Rules (§2B):
//   - Merged groups become a single ComputeAgent with kind="group" and agent_id=group.ID (= primary_worker_id).
//   - Standalone workers become a ComputeAgent with kind="single" and agent_id=worker_id.
//   - RPC member workers (in rpc_worker_ids or reporting model "rpc://..." / "rpc-server") are NEVER included.
//   - `members` is populated ONLY when includeMembers is true (admin/operator diagnostic view).
func BuildComputeAgents(reg *Registry, topo *TopologyCoordinator, rt *Router, includeMembers bool) []ComputeAgent {
	if reg == nil {
		return []ComputeAgent{}
	}
	workers := reg.List()
	byID := make(map[string]Worker, len(workers))
	for _, w := range workers {
		byID[w.WorkerID] = w
	}

	var topoStatus ClusterTopologyStatus
	if topo != nil {
		topoStatus = topo.GetStatus()
	}
	busySet := map[string]bool{}
	for _, id := range topoStatus.Busy {
		busySet[id] = true
	}

	inflightMap := map[string]int{}
	badSet := map[string]bool{}
	if rt != nil {
		rt.mu.Lock()
		now := rt.now()
		for k, v := range rt.inflight {
			inflightMap[k] = v
		}
		for k, until := range rt.bad {
			if now.Before(until) {
				badSet[k] = true
			}
		}
		rt.mu.Unlock()
	}

	inGroup := map[string]bool{}
	out := make([]ComputeAgent, 0, len(workers))

	for _, g := range topoStatus.Groups {
		members := append([]string{g.PrimaryID}, g.RPCWorkerIDs...)
		for _, m := range members {
			inGroup[m] = true
		}
		pw, ok := byID[g.PrimaryID]
		slots := 1
		if ok && pw.Slots > 0 {
			slots = int(pw.Slots)
		}
		groupState := "running"
		groupBusy := false
		for _, m := range members {
			if busySet[m] {
				groupBusy = true
				groupState = "switching"
			}
			mw, mok := byID[m]
			if !mok || !mw.Online {
				groupState = "offline"
				break
			}
			if mw.Switching {
				groupBusy = true
				groupState = "switching"
			} else if mw.State != "running" && groupState == "running" {
				groupState = mw.State
			}
		}
		model := g.Model
		if ok && pw.Model != "" && !strings.HasPrefix(pw.Model, rpcModelMarker) && pw.Model != "rpc-server" {
			model = pw.Model
		}
		inf := inflightMap[g.ID]
		if inf >= slots || badSet[g.ID] {
			groupBusy = true
		}
		ca := ComputeAgent{
			AgentID:    g.ID,
			Kind:       "group",
			Model:      model,
			CtxPerSlot: ResolveCtxPerSlot(g.ID, model, pw.EngineURL, slots),
			Slots:      slots,
			State:      groupState,
			Busy:       groupBusy,
			Inflight:   inf,
			Labels:     LabelsForAgent(model, g.ID),
			TokSRef:    ReferenceTokS(model, "group", pw.GPUName, pw.EngineURL),
		}
		if includeMembers {
			ca.Members = members
		}
		out = append(out, ca)
	}

	for _, w := range workers {
		if inGroup[w.WorkerID] {
			continue
		}
		if strings.HasPrefix(strings.ToLower(w.Model), rpcModelMarker) || strings.EqualFold(w.Model, "rpc-server") || strings.HasPrefix(strings.ToLower(w.EngineURL), "rpc://") {
			continue // RPC member is NEVER a compute agent
		}
		slots := int(w.Slots)
		if slots < 1 {
			slots = 1
		}
		state := w.State
		if !w.Online {
			state = "offline"
		} else if busySet[w.WorkerID] || w.Switching {
			state = "switching"
		}
		inf := inflightMap[w.WorkerID]
		busy := busySet[w.WorkerID] || w.Switching || inf >= slots || badSet[w.WorkerID] || state != "running"
		kind := "single"
		if strings.Contains(w.EngineURL, "/kind_group/") {
			kind = "group"
		}
		ca := ComputeAgent{
			AgentID:    w.WorkerID,
			Kind:       kind,
			Model:      w.Model,
			CtxPerSlot: ResolveCtxPerSlot(w.WorkerID, w.Model, w.EngineURL, slots),
			Slots:      slots,
			State:      state,
			Busy:       busy,
			Inflight:   inf,
			Labels:     LabelsForAgent(w.Model, w.WorkerID),
			TokSRef:    ReferenceTokS(w.Model, kind, w.GPUName, w.EngineURL),
		}
		if includeMembers {
			ca.Members = []string{w.WorkerID}
		}
		out = append(out, ca)
	}

	sort.Slice(out, func(i, j int) bool { return out[i].AgentID < out[j].AgentID })
	return out
}

// FilterAndPageComputeAgents applies optional label/model/state filters and offset/limit pagination.
func FilterAndPageComputeAgents(agents []ComputeAgent, label, model, state string, offset, limit int) ([]ComputeAgent, int) {
	label = strings.ToLower(strings.TrimSpace(label))
	model = strings.TrimSpace(model)
	state = strings.TrimSpace(state)
	filtered := make([]ComputeAgent, 0, len(agents))
	for _, a := range agents {
		if state != "" && a.State != state {
			continue
		}
		if model != "" && a.Model != model {
			continue
		}
		if label != "" {
			matched := false
			for _, l := range a.Labels {
				if l == label {
					matched = true
					break
				}
			}
			if !matched {
				continue
			}
		}
		filtered = append(filtered, a)
	}
	total := len(filtered)
	if offset < 0 {
		offset = 0
	}
	if offset > total {
		offset = total
	}
	if limit <= 0 {
		limit = 100
	}
	if limit > 500 {
		limit = 500
	}
	end := offset + limit
	if end > total {
		end = total
	}
	page := make([]ComputeAgent, end-offset)
	copy(page, filtered[offset:end])
	return page, total
}

// registerComputeAgentsRoutes mounts GET /api/compute-agents and GET /api/compute-agents/stream on mux.
func registerComputeAgentsRoutes(mux *http.ServeMux, f *Fleet, topo *TopologyCoordinator, rt *Router) {
	mux.HandleFunc("/api/compute-agents", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		var reg *Registry
		if f != nil {
			reg = f.reg
		}
		q := r.URL.Query()
		role := r.Header.Get("X-Aicf-Role")
		includeMembers := q.Get("include_members") == "1" && (role == RoleAdmin || role == RoleOperator)
		all := BuildComputeAgents(reg, topo, rt, includeMembers)
		offset, _ := strconv.Atoi(q.Get("offset"))
		limit, err := strconv.Atoi(q.Get("limit"))
		if err != nil || limit <= 0 {
			limit = 100
		}
		page, total := FilterAndPageComputeAgents(all, q.Get("label"), q.Get("model"), q.Get("state"), offset, limit)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Total-Count", strconv.Itoa(total))
		json.NewEncoder(w).Encode(page)
	})

	mux.HandleFunc("/api/compute-agents/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		fl, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}
		var reg *Registry
		if f != nil {
			reg = f.reg
		}
		q := r.URL.Query()
		label, model, state := q.Get("label"), q.Get("model"), q.Get("state")
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			all := BuildComputeAgents(reg, topo, rt, false)
			page, _ := FilterAndPageComputeAgents(all, label, model, state, 0, 500)
			raw, _ := json.Marshal(page)
			if _, err := fmt.Fprintf(w, "data: %s\n\n", raw); err != nil {
				return
			}
			fl.Flush()
			if f != nil {
				_, next := f.Frame()
				select {
				case <-r.Context().Done():
					return
				case <-next:
				case <-ticker.C:
				}
			} else {
				select {
				case <-r.Context().Done():
					return
				case <-ticker.C:
				}
			}
		}
	})
}

// BestOfNProxy forwards /api/bestofn/* to the Best-of-N Python service (bestofn/, :18099, Pha 4).
// Authenticates callers via either a tenant API key (Authorization: Bearer aicf_...) or an admin/operator/viewer token.
func BestOfNProxy(target string, maxBody int64) (http.Handler, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("--bestofn-url must be an http(s) URL, got %q", target)
	}
	rp := &httputil.ReverseProxy{Rewrite: func(pr *httputil.ProxyRequest) {
		pr.SetURL(u)
		pr.Out.URL.Path = "/" + strings.TrimPrefix(pr.In.URL.Path, "/api/bestofn/")
		pr.Out.URL.RawPath = ""
		pr.Out.Header.Del("Authorization") // never forward raw credentials downstream
		if t := pr.In.Header.Get("X-Aicf-Tenant"); t != "" {
			pr.Out.Header.Set("X-Aicf-Tenant", t)
		}
		if u := pr.In.Header.Get("X-Aicf-User"); u != "" {
			pr.Out.Header.Set("X-Aicf-User", u)
		}
		if rl := pr.In.Header.Get("X-Aicf-Role"); rl != "" {
			pr.Out.Header.Set("X-Aicf-Role", rl)
		}
	}}
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "bestofn service unavailable", http.StatusBadGateway)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet, http.MethodPost:
		default:
			http.Error(w, "GET or POST only", http.StatusMethodNotAllowed)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		rp.ServeHTTP(w, r)
	}), nil
}
