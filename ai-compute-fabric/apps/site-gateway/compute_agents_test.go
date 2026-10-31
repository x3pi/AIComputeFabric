package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

func TestComputeAgentsSingleAndGroupExcludesRPCMembers(t *testing.T) {
	reg := NewRegistry(5 * time.Second)
	// Register 3 workers: pc-001 (master of merged group), pc-win-01 (RPC member), pc-003 (standalone)
	reg.Register(&pb.RegisterRequest{
		WorkerId: "pc-001", SiteId: "hn", Hostname: "pc-001", GpuName: "NVIDIA GeForce RTX 3060",
		VramTotalBytes: 12 << 30, EngineUrl: "http://127.0.0.1:18090?ctx_per_slot=32768", ParallelSlots: 1,
	})
	reg.Heartbeat(&pb.HeartbeatRequest{
		State: "running", Model: "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
		Capacity: &pb.WorkerCapacity{WorkerId: "pc-001", VramFreeBytes: 2 << 30},
	})

	reg.Register(&pb.RegisterRequest{
		WorkerId: "pc-win-01", SiteId: "hn", Hostname: "pc-win-01", GpuName: "NVIDIA GeForce RTX 5070",
		VramTotalBytes: 12 << 30, EngineUrl: "http://127.0.0.1:18091", ParallelSlots: 1,
	})
	reg.Heartbeat(&pb.HeartbeatRequest{
		State: "running", Model: "rpc://50052",
		Capacity: &pb.WorkerCapacity{WorkerId: "pc-win-01", VramFreeBytes: 1 << 30},
	})

	reg.Register(&pb.RegisterRequest{
		WorkerId: "pc-003", SiteId: "hn", Hostname: "pc-003", GpuName: "NVIDIA GeForce RTX 3060",
		VramTotalBytes: 12 << 30, EngineUrl: "http://127.0.0.1:18092?ctx_per_slot=8192", ParallelSlots: 4,
	})
	reg.Heartbeat(&pb.HeartbeatRequest{
		State: "running", Model: "Qwen3-8B-Q4_K_M.gguf",
		Capacity: &pb.WorkerCapacity{WorkerId: "pc-003", VramFreeBytes: 4 << 30},
	})

	fleet := NewFleet(reg)
	ts, _ := NewTenantStore("")
	_, tenantKey, err := ts.Create("bon-client", 0.5, 8)
	if err != nil {
		t.Fatal(err)
	}
	rt := NewRouter(RouterConfig{MaxQueue: 16, MaxWait: 2 * time.Second}, reg)
	h := NewAPIStudio(fleet, ts, NewAuth("secret-admin"), nil, nil, nil, rt)

	// Record merged group on the topology coordinator inside BuildComputeAgents test
	topo := NewTopologyCoordinator(reg, nil)
	topo.groups["pc-001"] = topoPlan{
		mode: TopologyModeMerged, primaryID: "pc-001", rpcIDs: []string{"pc-win-01"},
		model: "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", tensorSplit: "48,52", rpcPort: 50052,
	}

	// 1. Orchestrator view (includeMembers=false): RPC member pc-win-01 must NEVER appear, and members must be nil
	agents := BuildComputeAgents(reg, topo, rt, false)
	if len(agents) != 2 {
		t.Fatalf("expected 2 compute agents (1 group + 1 single, excluding RPC member), got %d: %+v", len(agents), agents)
	}
	for _, a := range agents {
		if a.AgentID == "pc-win-01" || strings.HasPrefix(a.Model, "rpc://") {
			t.Fatalf("RPC member leaked into ComputeAgents: %+v", a)
		}
		if len(a.Members) != 0 {
			t.Fatalf("orchestrator view must omit Members, got %+v", a.Members)
		}
	}
	if agents[0].AgentID != "pc-001" || agents[0].Kind != "group" || agents[0].CtxPerSlot != 32768 || agents[0].TokSRef != 126.7 {
		t.Fatalf("unexpected group agent: %+v", agents[0])
	}
	if agents[1].AgentID != "pc-003" || agents[1].Kind != "single" || agents[1].Slots != 4 || agents[1].CtxPerSlot != 8192 {
		t.Fatalf("unexpected single agent: %+v", agents[1])
	}

	// 2. HTTP endpoint auth checks: unauthenticated -> 401, tenant key -> 200 (no members), admin -> 200
	reqUnauth := httptest.NewRequest(http.MethodGet, "/api/compute-agents", nil)
	wUnauth := httptest.NewRecorder()
	h.ServeHTTP(wUnauth, reqUnauth)
	if wUnauth.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d", wUnauth.Code)
	}

	reqTenant := httptest.NewRequest(http.MethodGet, "/api/compute-agents?include_members=1&label=code", nil)
	reqTenant.Header.Set("Authorization", "Bearer "+tenantKey)
	wTenant := httptest.NewRecorder()
	h.ServeHTTP(wTenant, reqTenant)
	if wTenant.Code != http.StatusOK {
		t.Fatalf("expected 200 with tenant key, got %d: %s", wTenant.Code, wTenant.Body.String())
	}
	var gotTenant []ComputeAgent
	if err := json.Unmarshal(wTenant.Body.Bytes(), &gotTenant); err != nil {
		t.Fatal(err)
	}
	if len(gotTenant) != 2 {
		t.Fatalf("expected 2 code agents (RPC excluded), got %d", len(gotTenant))
	}
	for _, a := range gotTenant {
		if len(a.Members) != 0 {
			t.Fatalf("tenant caller must NOT receive members even with ?include_members=1: %+v", a)
		}
	}
}

func TestPinnedAgentNeverFallsBackWhenBusyOrOffline(t *testing.T) {
	// Start 2 fake inference servers: w1 (blocks until released) and w2 (responds immediately)
	blockW1 := make(chan struct{})
	startedW1 := make(chan struct{}, 1)
	srv1 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case startedW1 <- struct{}{}:
		default:
		}
		<-blockW1
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"choices":[{"message":{"content":"from-w1"}}]}`))
	}))
	defer srv1.Close()

	srv2Called := false
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		srv2Called = true
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"choices":[{"message":{"content":"from-w2"}}]}`))
	}))
	defer srv2.Close()

	reg := NewRegistry(5 * time.Second)
	reg.Register(&pb.RegisterRequest{WorkerId: "w1", EngineUrl: srv1.URL, ParallelSlots: 1})
	reg.Heartbeat(&pb.HeartbeatRequest{State: "running", Model: "Qwen3-8B-Q4_K_M.gguf", Capacity: &pb.WorkerCapacity{WorkerId: "w1"}})
	reg.Register(&pb.RegisterRequest{WorkerId: "w2", EngineUrl: srv2.URL, ParallelSlots: 1})
	reg.Heartbeat(&pb.HeartbeatRequest{State: "running", Model: "Qwen3-8B-Q4_K_M.gguf", Capacity: &pb.WorkerCapacity{WorkerId: "w2"}})

	rt := NewRouter(RouterConfig{MaxQueue: 16, MaxWait: 2 * time.Second}, reg)
	inf := NewInference(rt, NewLimiter(0, 10))

	// Occupy w1's single slot
	done1 := make(chan int, 1)
	go func() {
		body := []byte(`{"model":"Qwen3-8B-Q4_K_M.gguf","messages":[{"role":"user","content":"hi"}]}`)
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
		req.Header.Set("X-AICF-Agent", "w1")
		rec := httptest.NewRecorder()
		inf.ServeHTTP(rec, req)
		done1 <- rec.Code
	}()
	<-startedW1

	// Mutation check: while w1 is busy and w2 is completely idle, a request pinned to w1 MUST get 503
	// and MUST NOT fall back to w2!
	body := []byte(`{"model":"Qwen3-8B-Q4_K_M.gguf","messages":[{"role":"user","content":"second"}]}`)
	reqBusy := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	reqBusy.Header.Set("X-AICF-Agent", "w1")
	recBusy := httptest.NewRecorder()
	inf.ServeHTTP(recBusy, reqBusy)
	if recBusy.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 when pinned agent w1 is busy, got %d: %s", recBusy.Code, recBusy.Body.String())
	}
	if srv2Called {
		t.Fatalf("pinned request to w1 illegally fell back to w2!")
	}

	// Request pinned to nonexistent / offline agent -> 503
	reqGhost := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	reqGhost.Header.Set("X-AICF-Agent", "ghost-agent")
	recGhost := httptest.NewRecorder()
	inf.ServeHTTP(recGhost, reqGhost)
	if recGhost.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 for unknown pinned agent, got %d", recGhost.Code)
	}

	close(blockW1)
	if code := <-done1; code != http.StatusOK {
		t.Fatalf("first request on w1 failed with %d", code)
	}
}

func TestLabelRoutingSelectsMatchingAgent(t *testing.T) {
	srvCode := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"choices":[{"message":{"content":"code-ok"}}]}`))
	}))
	defer srvCode.Close()

	srvReason := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"choices":[{"message":{"content":"reason-ok"}}]}`))
	}))
	defer srvReason.Close()

	reg := NewRegistry(5 * time.Second)
	reg.Register(&pb.RegisterRequest{WorkerId: "w-code", EngineUrl: srvCode.URL, ParallelSlots: 1})
	reg.Heartbeat(&pb.HeartbeatRequest{State: "running", Model: "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", Capacity: &pb.WorkerCapacity{WorkerId: "w-code"}})
	reg.Register(&pb.RegisterRequest{WorkerId: "w-reason", EngineUrl: srvReason.URL, ParallelSlots: 1})
	reg.Heartbeat(&pb.HeartbeatRequest{State: "running", Model: "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf", Capacity: &pb.WorkerCapacity{WorkerId: "w-reason"}})

	rt := NewRouter(RouterConfig{MaxQueue: 16, MaxWait: 2 * time.Second}, reg)
	inf := NewInference(rt, NewLimiter(0, 10))

	// Request with X-AICF-Label: reasoning (empty model) must land on w-reason
	body := []byte(`{"model":"","messages":[{"role":"user","content":"prove"}]}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	req.Header.Set("X-AICF-Label", "reasoning")
	rec := httptest.NewRecorder()
	inf.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || rec.Header().Get("X-AICF-Agent") != "w-reason" {
		t.Fatalf("expected 200 on w-reason, got code=%d agent=%q", rec.Code, rec.Header().Get("X-AICF-Agent"))
	}

	// Request with non-existent label must return 503
	reqBad := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	reqBad.Header.Set("X-AICF-Label", "nonexistent-capability")
	recBad := httptest.NewRecorder()
	inf.ServeHTTP(recBad, reqBad)
	if recBad.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 for unknown label, got %d", recBad.Code)
	}
}

func TestBestOfNProxyAuthAndForwarding(t *testing.T) {
	var seenTenant, seenAuth string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenTenant = r.Header.Get("X-Aicf-Tenant")
		seenAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true,"path":"` + r.URL.Path + `"}`))
	}))
	defer upstream.Close()

	bonProxy, err := BestOfNProxy(upstream.URL, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(5 * time.Second)
	fleet := NewFleet(reg)
	ts, _ := NewTenantStore("")
	ten, apiKey, _ := ts.Create("ide-bon", 1.0, 4)
	h := NewAPIBestOfN(fleet, ts, NewAuth("admin-tok"), nil, nil, nil, bonProxy)

	// 1. Missing key -> 401
	r1 := httptest.NewRequest(http.MethodPost, "/api/bestofn/v1/chat/completions", strings.NewReader(`{}`))
	w1 := httptest.NewRecorder()
	h.ServeHTTP(w1, r1)
	if w1.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without key, got %d", w1.Code)
	}

	// 2. Wrong key -> 401
	r2 := httptest.NewRequest(http.MethodPost, "/api/bestofn/v1/chat/completions", strings.NewReader(`{}`))
	r2.Header.Set("Authorization", "Bearer aicf_wrong_key")
	w2 := httptest.NewRecorder()
	h.ServeHTTP(w2, r2)
	if w2.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 with bad key, got %d", w2.Code)
	}

	// 3. Valid tenant key -> 200, X-Aicf-Tenant forwarded, Authorization stripped
	r3 := httptest.NewRequest(http.MethodPost, "/api/bestofn/v1/chat/completions", strings.NewReader(`{"messages":[]}`))
	r3.Header.Set("Authorization", "Bearer "+apiKey)
	w3 := httptest.NewRecorder()
	h.ServeHTTP(w3, r3)
	if w3.Code != http.StatusOK {
		t.Fatalf("expected 200 with valid tenant key, got %d: %s", w3.Code, w3.Body.String())
	}
	if seenTenant != ten.ID {
		t.Fatalf("expected X-Aicf-Tenant=%q, got %q", ten.ID, seenTenant)
	}
	if seenAuth != "" {
		t.Fatalf("Authorization header must be stripped by proxy, got %q", seenAuth)
	}
}
