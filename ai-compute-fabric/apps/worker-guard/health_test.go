package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"
)

// probedEngine is the fake engine plus a scripted /health answer.
type probedEngine struct {
	*fakeEngine
	healthy    bool
	applicable bool
}

func (p *probedEngine) Healthy() (bool, bool) { return p.healthy, p.applicable }

func healthRig(t *testing.T) (*Reporter, *fakeGateway, *Guard, *probedEngine) {
	t.Helper()
	r, fg, g, eng, _ := reporterRig(t)
	pe := &probedEngine{fakeEngine: eng, healthy: true, applicable: true}
	g.eng = pe
	return r, fg, g, pe
}

func lastState(t *testing.T, r *Reporter, fg *fakeGateway) string {
	t.Helper()
	if err := r.Step(context.Background()); err != nil {
		t.Fatal(err)
	}
	return fg.got[len(fg.got)-1].State
}

func TestEngineThatStopsAnsweringHealthIsReportedUnhealthyAfterThreeFailures(t *testing.T) {
	r, fg, g, pe := healthRig(t)
	if s := lastState(t, r, fg); s != "running" {
		t.Fatalf("baseline %q", s)
	}
	pe.healthy = false
	for i := 1; i <= 2; i++ {
		g.checkHealthOnce(3)
		if s := lastState(t, r, fg); s != "running" {
			t.Fatalf("failure %d of 3 must not withdraw yet, got %q", i, s)
		}
	}
	g.checkHealthOnce(3)
	if s := lastState(t, r, fg); s != "unhealthy" {
		t.Fatalf("3rd failure must report unhealthy, got %q", s)
	}
	pe.healthy = true
	g.checkHealthOnce(3)
	if s := lastState(t, r, fg); s != "running" {
		t.Fatalf("one success must restore running, got %q", s)
	}
}

func TestIsolatedHealthBlipsNeverWithdraw(t *testing.T) {
	r, fg, g, pe := healthRig(t)
	for _, healthy := range []bool{false, false, true, false, false, true} {
		pe.healthy = healthy
		g.checkHealthOnce(3)
		if s := lastState(t, r, fg); s != "running" {
			t.Fatalf("blips must be absorbed, got %q", s)
		}
	}
}

func TestFailureCountDoesNotSurviveAStop(t *testing.T) {
	_, _, g, pe := healthRig(t)
	pe.healthy = false
	g.checkHealthOnce(3)
	g.checkHealthOnce(3)
	g.setState(StStopped)
	g.checkHealthOnce(3) // not running: must reset, not count
	g.setState(StRunning)
	g.checkHealthOnce(3)
	if g.Snapshot().Unhealthy {
		t.Fatal("a restart must start from zero failures")
	}
}

func TestEngineWithoutProbeOrWithNothingToProbeIsNeverUnhealthy(t *testing.T) {
	_, _, g, pe := healthRig(t)
	pe.healthy, pe.applicable = false, false // e.g. rpc-worker
	for i := 0; i < 10; i++ {
		g.checkHealthOnce(3)
	}
	if g.Snapshot().Unhealthy {
		t.Fatal("not applicable must never withdraw")
	}
	g.eng = pe.fakeEngine // no HealthChecker at all
	for i := 0; i < 10; i++ {
		g.checkHealthOnce(3)
	}
	if g.Snapshot().Unhealthy {
		t.Fatal("engine without a probe must never withdraw")
	}
}

func TestLlamaEngineHealthyReadsTheRealHTTPStatus(t *testing.T) {
	code := 200
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(code) }))
	host, portStr, _ := net.SplitHostPort(srv.Listener.Addr().String())
	port, _ := strconv.Atoi(portStr)
	e := &LlamaEngine{Host: host, Port: port}
	if ok, app := e.Healthy(); !ok || !app {
		t.Fatalf("200 must be healthy, got ok=%v app=%v", ok, app)
	}
	code = 503
	if ok, _ := e.Healthy(); ok {
		t.Fatal("503 (still loading / broken) must be unhealthy")
	}
	srv.Close()
	if ok, app := e.Healthy(); ok || !app {
		t.Fatalf("closed port must be unhealthy, got ok=%v app=%v", ok, app)
	}
	e.Mode = "rpc-worker"
	if _, app := e.Healthy(); app {
		t.Fatal("rpc-worker has no HTTP /health, must be not applicable")
	}
}

func TestReporterFollowsTheHeartbeatIntervalTheGatewayAsks(t *testing.T) {
	r, _, _, _, _ := reporterRig(t)
	if r.pace() != time.Second {
		t.Fatalf("before registering the default is 1 s, got %v", r.pace())
	}
	r.gwInterval = 10 * time.Second
	if r.pace() != 10*time.Second {
		t.Fatalf("must follow the gateway, got %v", r.pace())
	}
	r.Interval = 2 * time.Second
	if r.pace() != 2*time.Second {
		t.Fatalf("an explicit Interval must win, got %v", r.pace())
	}
}

func TestRegisterStoresTheGatewayInterval(t *testing.T) {
	r, _, _, _, _ := reporterRig(t) // fakeGateway answers HeartbeatIntervalMs: 1000
	if err := r.register(context.Background()); err != nil {
		t.Fatal(err)
	}
	if r.gwInterval != time.Second {
		t.Fatalf("register must record the gateway's interval, got %v", r.gwInterval)
	}
}
