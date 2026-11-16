package main

import (
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

// A worker whose guard reports "unhealthy" (engine alive, /health failing) must leave the routing set,
// and rejoin when the guard reports "running" again.
func TestUnhealthyWorkerIsNotRoutableUntilItRecovers(t *testing.T) {
	reg := NewRegistry(time.Minute)
	reg.Register(&pb.RegisterRequest{WorkerId: "w1", SiteId: "s", EngineUrl: "http://127.0.0.1:1", ParallelSlots: 1})
	hb := func(state string) {
		reg.Heartbeat(&pb.HeartbeatRequest{State: state, Model: "m", Capacity: &pb.WorkerCapacity{WorkerId: "w1"}})
	}
	hb("running")
	if len(reg.Routable()) != 1 {
		t.Fatal("running worker must be routable")
	}
	hb("unhealthy")
	if len(reg.Routable()) != 0 {
		t.Fatal("unhealthy worker must not receive traffic")
	}
	hb("running")
	if len(reg.Routable()) != 1 {
		t.Fatal("recovered worker must be routable again")
	}
}
