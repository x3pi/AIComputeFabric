package main

import (
	"context"
	"log"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

// Reporter registers with site-gateway and sends one heartbeat per second.
// The gateway's pause directive is applied to the guard; a pause the operator
// pressed locally is never undone by the gateway.
type Reporter struct {
	Client            pb.FleetGatewayClient
	G                 *Guard
	WorkerID, SiteID  string
	Hostname, GPUName string
	Model             string
	EngineURL         string
	Slots             uint32
	Interval          time.Duration
	registered        bool
	pausedByGateway   bool
}

func (r *Reporter) register(ctx context.Context) error {
	s := r.G.Snapshot()
	rep, err := r.Client.Register(ctx, &pb.RegisterRequest{
		WorkerId: r.WorkerID, SiteId: r.SiteID, Hostname: r.Hostname, GpuName: r.GPUName,
		VramTotalBytes: s.Sample.TotalMiB << 20, SupportedEngines: []string{"llama.cpp"}, AgentVersion: "0.1-poc",
		EngineUrl: r.EngineURL, ParallelSlots: r.Slots,
	})
	if err != nil {
		return err
	}
	if rep.HeartbeatIntervalMs > 0 && r.Interval == 0 {
		r.Interval = time.Duration(rep.HeartbeatIntervalMs) * time.Millisecond
	}
	r.registered = true
	return nil
}

// Step does one register-if-needed + heartbeat round trip.
func (r *Reporter) Step(ctx context.Context) error {
	if !r.registered {
		if err := r.register(ctx); err != nil {
			return err
		}
	}
	s := r.G.Snapshot()
	b := s.Buckets
	hb := &pb.HeartbeatRequest{
		State: string(s.State), Model: servedModel(s, r.Model), Lora: s.Lora, Switching: s.Switching, GpuTempC: uint32(s.Sample.TempC),
		Capacity: &pb.WorkerCapacity{
			WorkerId: r.WorkerID, SiteId: r.SiteID, VramFreeBytes: s.Sample.FreeMiB() << 20,
			SupportedEngines: []string{"llama.cpp"},
			// "user is active" here means the user holds GPU memory the engine had to yield to.
			UserIsActive: s.State == StYielded || s.State == StReclaiming,
			Vram: &pb.VramBuckets{TotalBytes: b.Total, OsUserBytes: b.OSUser, WeightsBytes: b.Weights,
				KvStateBytes: b.KVState, ComputeBytes: b.Compute, HeadroomBytes: b.Headroom, AllocatableBytes: b.Allocatable},
		},
	}
	for i := len(s.Events) - 1; i >= 0; i-- {
		if s.Events[i].Kind == "reclaim" {
			hb.LastReclaim = &pb.ReclaimStat{AtUnixMs: uint64(s.Events[i].At.UnixMilli()), DetectToFreedMs: s.Events[i].DetectMS}
			break
		}
	}
	rep, err := r.Client.Heartbeat(ctx, hb)
	if err != nil {
		return err
	}
	if !rep.Registered { // gateway restarted and forgot us
		r.registered = false
		return nil
	}
	switch {
	case rep.PauseRequested && !r.pausedByGateway && s.State != StPaused:
		r.pausedByGateway = true
		r.G.Pause()
	case !rep.PauseRequested && r.pausedByGateway:
		r.pausedByGateway = false
		r.G.Resume()
	}
	return nil
}

// Run loops until ctx is done; transient gateway errors are logged, never fatal,
// because a worker must keep reclaiming GPU for its user even when the network is down.
func (r *Reporter) Run(ctx context.Context) {
	if r.Interval == 0 {
		r.Interval = time.Second
	}
	t := time.NewTicker(r.Interval)
	defer t.Stop()
	for {
		cctx, cancel := context.WithTimeout(ctx, 2*time.Second)
		if err := r.Step(cctx); err != nil {
			log.Printf("gateway: %v", err)
		}
		cancel()
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// servedModel is what the engine is configured with now (it changes when the operator switches model in the web UI);
// the startup value is only the fallback before the engine has ever been configured.
func servedModel(s Snapshot, startup string) string {
	if s.Model != "" {
		return s.Model
	}
	return startup
}
