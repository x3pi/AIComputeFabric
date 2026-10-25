package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
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
	h := HTTPHandler(reg)
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
	h := HTTPHandler(reg)
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
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil || len(out) != 0 {
		t.Fatalf("empty fleet must be [] : %q %v", rec.Body.String(), err)
	}
}
