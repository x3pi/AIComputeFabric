package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func rig(t *testing.T) (pb.FleetGatewayClient, *Registry, *time.Time) {
	t.Helper()
	reg := NewRegistry(3 * time.Second)
	clock := time.Unix(5000, 0)
	reg.now = func() time.Time { return clock }
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := grpc.NewServer()
	pb.RegisterFleetGatewayServer(s, &GRPC{Reg: reg})
	go s.Serve(lis)
	t.Cleanup(s.Stop)
	conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return pb.NewFleetGatewayClient(conn), reg, &clock
}

func hb(id, state string) *pb.HeartbeatRequest {
	return &pb.HeartbeatRequest{State: state, Model: "q", Capacity: &pb.WorkerCapacity{
		WorkerId: id, VramFreeBytes: 7 << 30,
		Vram: &pb.VramBuckets{TotalBytes: 12 << 30, AllocatableBytes: 7 << 30}}}
}

func TestRegisterHeartbeatAndOffline(t *testing.T) {
	c, reg, clock := rig(t)
	ctx := context.Background()
	if r, _ := c.Heartbeat(ctx, hb("w1", "running")); r.Registered {
		t.Fatal("heartbeat from an unknown worker must ask it to register")
	}
	if _, err := c.Register(ctx, &pb.RegisterRequest{WorkerId: "w1", Hostname: "pc1", VramTotalBytes: 12 << 30}); err != nil {
		t.Fatal(err)
	}
	r, err := c.Heartbeat(ctx, hb("w1", "running"))
	if err != nil || !r.Registered {
		t.Fatalf("%v %v", r, err)
	}
	ws := reg.List()
	if len(ws) != 1 || !ws[0].Online || ws[0].State != "running" || ws[0].Buckets == nil {
		t.Fatalf("%+v", ws)
	}
	*clock = clock.Add(4 * time.Second)
	if ws = reg.List(); ws[0].Online || ws[0].State != "offline" {
		t.Fatalf("worker silent for 4 s must be offline: %+v", ws[0])
	}
}

func TestPauseDirectiveDeliveredOnHeartbeat(t *testing.T) {
	c, reg, _ := rig(t)
	ctx := context.Background()
	c.Register(ctx, &pb.RegisterRequest{WorkerId: "w1"})
	if r, _ := c.Heartbeat(ctx, hb("w1", "running")); r.PauseRequested {
		t.Fatal("no pause requested yet")
	}
	h := HTTPHandler(NewFleet(reg))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/workers/w1/pause", nil))
	if rec.Code != 204 {
		t.Fatalf("pause -> %d", rec.Code)
	}
	if r, _ := c.Heartbeat(ctx, hb("w1", "running")); !r.PauseRequested {
		t.Fatal("pause directive must arrive on the next heartbeat")
	}
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/api/workers/w1/resume", nil))
	if r, _ := c.Heartbeat(ctx, hb("w1", "paused")); r.PauseRequested {
		t.Fatal("resume must clear the directive")
	}
}

func TestHTTPValidation(t *testing.T) {
	_, reg, _ := rig(t)
	h := HTTPHandler(NewFleet(reg))
	for _, tc := range []struct {
		method, path string
		want         int
	}{
		{"GET", "/api/workers/w1/pause", 405},
		{"POST", "/api/workers/ghost/pause", 404},
		{"POST", "/api/workers/w1/explode", 404},
		{"POST", "/api/workers/", 404},
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, nil))
		if rec.Code != tc.want {
			t.Errorf("%s %s = %d want %d", tc.method, tc.path, rec.Code, tc.want)
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/workers", nil))
	var out []Worker
	if strings.TrimSpace(rec.Body.String()) != "[]" {
		t.Fatalf("empty fleet must encode as [], got %q", rec.Body.String())
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil || len(out) != 0 {
		t.Fatalf("empty fleet must be [] : %q %v", rec.Body.String(), err)
	}
}

func TestPaginationFilterAndSharedFrame(t *testing.T) {
	_, reg, _ := rig(t)
	for i := 0; i < 250; i++ {
		id := fmt.Sprintf("w%04d", i)
		reg.Register(&pb.RegisterRequest{WorkerId: id, VramTotalBytes: 12 << 30})
		st := "running"
		if i%5 == 0 {
			st = "paused"
		}
		reg.Heartbeat(&pb.HeartbeatRequest{State: st, Capacity: &pb.WorkerCapacity{WorkerId: id, VramFreeBytes: 3 << 30}})
	}
	f := NewFleet(reg)
	h := HTTPHandler(f)
	get := func(url string) (*httptest.ResponseRecorder, []Worker) {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("GET", url, nil))
		var ws []Worker
		json.Unmarshal(rec.Body.Bytes(), &ws)
		return rec, ws
	}
	rec, ws := get("/api/workers")
	if len(ws) != 100 || rec.Header().Get("X-Total-Count") != "250" {
		t.Fatalf("default page must be 100 of 250, got %d / %s", len(ws), rec.Header().Get("X-Total-Count"))
	}
	if _, ws = get("/api/workers?limit=100000"); len(ws) != 250 {
		t.Fatalf("limit is capped at 500, got %d", len(ws))
	}
	if rec, ws = get("/api/workers?state=paused&limit=500"); len(ws) != 50 || rec.Header().Get("X-Total-Count") != "50" {
		t.Fatalf("paused filter: %d", len(ws))
	}
	if _, ws = get("/api/workers?offset=240&limit=50"); len(ws) != 10 {
		t.Fatalf("last page: %d", len(ws))
	}
	frame, next := f.Frame()
	var s Summary
	if err := json.Unmarshal(frame, &s); err != nil || s.Total != 250 || len(s.Cells) != 250 || s.Counts["running"] != 200 {
		t.Fatalf("summary: %v total=%d counts=%v", err, s.Total, s.Counts)
	}
	if per := len(frame) / 250; per > 60 {
		t.Fatalf("compact frame should be <60 B/worker, got %d", per)
	}
	select {
	case <-next:
		t.Fatal("next must not be closed before a refresh")
	default:
	}
	f.Refresh()
	select {
	case <-next:
	default:
		t.Fatal("refresh must wake SSE viewers")
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/workers/w0003", nil))
	if rec.Code != 200 {
		t.Fatalf("detail: %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/workers/ghost", nil))
	if rec.Code != 404 {
		t.Fatalf("unknown detail: %d", rec.Code)
	}
}

func TestVRAMEstimatorCatalogAndClusterStats(t *testing.T) {
	_, reg, _ := rig(t)
	reg.Register(&pb.RegisterRequest{
		WorkerId: "pc-001", Hostname: "ws-01", GpuName: "NVIDIA GeForce RTX 3060", VramTotalBytes: 12 << 30, EngineUrl: "http://127.0.0.1:18590", ParallelSlots: 4,
	})
	reg.Heartbeat(&pb.HeartbeatRequest{
		State: "running", Model: "Qwen3-8B-Q4_K_M.gguf", GpuTempC: 64,
		LastReclaim: &pb.ReclaimStat{DetectToFreedMs: 280},
		Capacity: &pb.WorkerCapacity{
			WorkerId: "pc-001", VramFreeBytes: 6 << 30,
			Vram: &pb.VramBuckets{
				TotalBytes:       12 << 30,
				OsUserBytes:      512 << 20,
				WeightsBytes:     4455 << 20,
				KvStateBytes:     1152 << 20,
				ComputeBytes:     245 << 20,
				HeadroomBytes:    1536 << 20,
				AllocatableBytes: 4388 << 20,
			},
		},
	})
	f := NewFleet(reg)
	store, _ := NewTenantStore("")
	store.Create("eng", 2, 4)
	rt := NewRouter(RouterConfig{MaxQueue: 16, MaxWait: time.Second, Fair: true, Policy: store.Policy}, reg)
	h := NewAPI(f, store, "", rt)

	// 1. Model catalog
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/models/catalog", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("catalog status %d", rec.Code)
	}
	var cat []CatalogModel
	if err := json.Unmarshal(rec.Body.Bytes(), &cat); err != nil || len(cat) < 5 {
		t.Fatalf("catalog decode: %v len=%d", err, len(cat))
	}

	// 2. Single-GPU GREEN fit (Qwen3-8B @ 8k, np=2, flash_attn=true on 12 GiB)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/vram/estimate?model_id=Qwen/Qwen3-8B-GGUF&ctx_per_slot=8192&parallel=2&flash_attn=true&gpu_total_mib=12288&headroom_mib=1536&os_user_mib=512", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("estimate GET status %d", rec.Code)
	}
	var est EstimateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &est); err != nil {
		t.Fatalf("estimate decode: %v", err)
	}
	if est.FitStatus != "GREEN" || est.ExecutionMode != "LOCAL_FULL" || !est.FitsEntirely || est.CompatibleWorkersOnline != 1 {
		t.Fatalf("unexpected estimate for Qwen3-8B: %+v", est)
	}

	// 3. Hybrid CPU+GPU offload (Qwen2.5-32B on 1x 12 GiB GPU -> LOCAL_HYBRID / YELLOW)
	rec = httptest.NewRecorder()
	body := `{"model_id":"Qwen/Qwen2.5-32B-Instruct-GGUF","ctx_per_slot":4096,"parallel":1,"flash_attn":true,"gpu_total_mib":12288,"os_user_mib":512,"headroom_mib":1536,"gpu_count":1}`
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/vram/estimate", strings.NewReader(body)))
	if rec.Code != http.StatusOK {
		t.Fatalf("estimate POST status %d", rec.Code)
	}
	json.Unmarshal(rec.Body.Bytes(), &est)
	if est.ExecutionMode != "LOCAL_HYBRID" || est.FitStatus != "YELLOW" || est.MaxGPULayers == 0 || est.MaxGPULayers >= 64 || est.OffloadRAMBytes == 0 {
		t.Fatalf("expected LOCAL_HYBRID for 32B on 12GB, got mode=%s fit=%s maxLayers=%d offloadRAM=%d",
			est.ExecutionMode, est.FitStatus, est.MaxGPULayers, est.OffloadRAMBytes)
	}

	// 4. Multi-GPU split (Qwen2.5-32B across 3 GPUs -> ranges 0-21, 22-42, 43-63)
	rec = httptest.NewRecorder()
	body3 := `{"model_id":"Qwen/Qwen2.5-32B-Instruct-GGUF","ctx_per_slot":4096,"parallel":2,"flash_attn":true,"gpu_total_mib":12288,"os_user_mib":512,"headroom_mib":1536,"gpu_count":3}`
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/vram/estimate", strings.NewReader(body3)))
	json.Unmarshal(rec.Body.Bytes(), &est)
	if est.ExecutionMode != "DISTRIBUTED_MODEL" || len(est.LayerSplits) != 3 ||
		est.LayerSplits[0].RangeLabel != "0-21" || est.LayerSplits[1].RangeLabel != "22-42" || est.LayerSplits[2].RangeLabel != "43-63" {
		t.Fatalf("unexpected 3-GPU split: %+v", est.LayerSplits)
	}

	// 5. Cluster stats endpoint
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/stats", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("stats status %d", rec.Code)
	}
	var st ClusterStatsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &st); err != nil {
		t.Fatalf("stats decode: %v", err)
	}
	if st.WorkersTotal != 1 || st.WorkersRunning != 1 || st.TenantsActive != 1 || st.TempMaxC != 64 || st.VRAMPool.Total != 12<<30 {
		t.Fatalf("unexpected cluster stats: %+v", st)
	}
}
