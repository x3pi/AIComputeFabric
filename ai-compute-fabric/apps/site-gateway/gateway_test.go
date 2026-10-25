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
