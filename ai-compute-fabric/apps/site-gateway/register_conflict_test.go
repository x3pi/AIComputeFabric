package main

import (
	"context"
	"errors"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

func regRig() (*Registry, *time.Time) {
	reg := NewRegistry(3 * time.Second)
	clock := time.Unix(1000, 0)
	reg.now = func() time.Time { return clock }
	return reg, &clock
}

func req(host, url string) *pb.RegisterRequest {
	return &pb.RegisterRequest{WorkerId: "pc-001", Hostname: host, EngineUrl: url, ParallelSlots: 1}
}

func TestSecondHostCannotTakeOverALiveWorkerID(t *testing.T) {
	reg, clock := regRig()
	if err := reg.Register(req("pc-a", "http://a:1")); err != nil {
		t.Fatal(err)
	}
	reg.Heartbeat(&pb.HeartbeatRequest{State: "running", Model: "m", Capacity: &pb.WorkerCapacity{WorkerId: "pc-001"}})
	*clock = clock.Add(time.Second)
	err := reg.Register(req("pc-b", "http://b:1"))
	if !errors.Is(err, ErrWorkerIDInUse) {
		t.Fatalf("second host must be refused, got %v", err)
	}
	w := reg.List()[0]
	if w.Hostname != "pc-a" || w.EngineURL != "http://a:1" || w.State != "running" {
		t.Fatalf("the refused registration must not touch the live entry: %+v", w)
	}
}

func TestSameHostMayReRegisterAfterAGuardRestart(t *testing.T) {
	reg, clock := regRig()
	reg.Register(req("pc-a", "http://a:1"))
	*clock = clock.Add(time.Second)
	if err := reg.Register(req("pc-a", "http://a:2")); err != nil {
		t.Fatalf("same host must be accepted even with a new engine URL: %v", err)
	}
	if reg.List()[0].EngineURL != "http://a:2" {
		t.Fatal("re-registration must refresh the entry")
	}
}

func TestAnotherHostMayTakeOverOnceTheOldOneWentSilent(t *testing.T) {
	reg, clock := regRig()
	reg.Register(req("pc-a", "http://a:1"))
	*clock = clock.Add(4 * time.Second) // beyond OfflineAfter
	if err := reg.Register(req("pc-b", "http://b:1")); err != nil {
		t.Fatalf("replacement of a dead worker must work: %v", err)
	}
	if reg.List()[0].Hostname != "pc-b" {
		t.Fatal("takeover must replace the entry")
	}
}

func TestUnknownHostnameIsNotTreatedAsAConflict(t *testing.T) {
	reg, _ := regRig()
	reg.Register(req("pc-a", "http://a:1"))
	if err := reg.Register(req("", "http://a:1")); err != nil {
		t.Fatalf("a missing hostname carries no evidence: %v", err)
	}
}

func TestGRPCRegisterAnswersAlreadyExistsForASecondHost(t *testing.T) {
	reg, _ := regRig()
	g := &GRPC{Reg: reg}
	if _, err := g.Register(context.Background(), req("pc-a", "http://a:1")); err != nil {
		t.Fatal(err)
	}
	_, err := g.Register(context.Background(), req("pc-b", "http://b:1"))
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("want AlreadyExists, got %v", err)
	}
}
