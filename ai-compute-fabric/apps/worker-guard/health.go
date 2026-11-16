package main

import (
	"fmt"
	"log"
	"net/http"
	"time"
)

// Engine health revalidation (idea borrowed from OpenTela service_health.go).
//
// Start() health-checks the engine once, at start-up. After that State stays "running" for as long as
// the process exists, so an engine whose HTTP side stopped answering kept being routed to until two
// requests had failed. This loop keeps checking while running; after HealthFailures consecutive
// failures the guard reports itself "unhealthy" in the heartbeat, and the gateway (Routable() needs
// State == "running") stops sending traffic. One success clears it.
//
// It does NOT restart the engine, and it only sees what /health sees: a process that is wedged in the
// decode loop but still answers /health stays "healthy" here.

const (
	defaultHealthInterval = 5 * time.Second
	defaultHealthFailures = 3
	healthProbeTimeout    = 3 * time.Second
)

// StUnhealthy is only ever reported in the heartbeat; the guard's own state machine does not use it.
const StUnhealthy State = "unhealthy"

// HealthChecker is optional on an Engine. applicable=false means "nothing to probe in this mode".
type HealthChecker interface {
	Healthy() (ok, applicable bool)
}

// Healthy probes llama-server's /health. An rpc-worker runs ggml-rpc-server, which has no HTTP endpoint.
func (e *LlamaEngine) Healthy() (ok, applicable bool) {
	opts := e.Options()
	if opts.Mode == "rpc-worker" {
		return false, false
	}
	host := e.Host
	if host == "" || host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	c := http.Client{Timeout: healthProbeTimeout}
	r, err := c.Get(fmt.Sprintf("http://%s:%d/health", host, e.Port))
	if err != nil {
		return false, true
	}
	r.Body.Close()
	return r.StatusCode == 200, true
}

// observeHealth records one probe result and reports whether the unhealthy flag changed. It changes it
// only on a transition so callers log and act once, not on every tick.
func (g *Guard) observeHealth(healthy bool, threshold int) (changed bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if healthy {
		g.healthFails = 0
		if g.unhealthy {
			g.unhealthy = false
			return true
		}
		return false
	}
	g.healthFails++
	if !g.unhealthy && g.healthFails >= threshold {
		g.unhealthy = true
		return true
	}
	return false
}

// resetHealth forgets failures when the engine is not in the running state (stopped, yielded, switching):
// a fresh start must not inherit the previous run's failure count.
func (g *Guard) resetHealth() {
	g.mu.Lock()
	g.healthFails, g.unhealthy = 0, false
	g.mu.Unlock()
}

// checkHealthOnce is one tick of the revalidation loop.
func (g *Guard) checkHealthOnce(threshold int) {
	hc, ok := g.eng.(HealthChecker)
	g.mu.Lock()
	st := g.st
	g.mu.Unlock()
	if !ok || st != StRunning {
		g.resetHealth()
		return
	}
	healthy, applicable := hc.Healthy()
	if !applicable {
		g.resetHealth()
		return
	}
	if g.observeHealth(healthy, threshold) {
		if healthy {
			log.Printf("guard: engine /health recovered; reporting running again")
		} else {
			log.Printf("guard: engine /health failed %d times in a row; reporting unhealthy (no traffic)", threshold)
		}
	}
}

// pollHealth runs until stop is closed. A negative interval disables it.
func (g *Guard) pollHealth(stop <-chan struct{}) {
	interval, threshold := g.cfg.HealthInterval, g.cfg.HealthFailures
	if interval < 0 {
		return
	}
	if interval == 0 {
		interval = defaultHealthInterval
	}
	if threshold <= 0 {
		threshold = defaultHealthFailures
	}
	for {
		select {
		case <-stop:
			return
		case <-time.After(interval):
		}
		g.checkHealthOnce(threshold)
	}
}
