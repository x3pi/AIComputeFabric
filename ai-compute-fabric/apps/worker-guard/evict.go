package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

// Serverless co-tenancy (ADR-003 D10). The LLM engine is the product; Serverless containers are guests on the same GPU. When free VRAM
// falls below the headroom the guard takes the guests' VRAM back FIRST and stops the engine only if that was not enough.
//
// Everything here is opt-in: with no Evictor configured the guard behaves exactly as before (the existing tests pin that).

// Evictor stops the Serverless guests of this GPU. Both calls return how many containers they stopped, so the guard can skip the wait
// entirely when there was nobody to evict (the LLM reclaim must not get slower just because the feature exists).
type Evictor interface {
	// Evict asks the cooperating ServerlessWorker to stop its containers (cheap, orderly). It must give up after timeout.
	Evict(timeout time.Duration) (int, error)
	// Kill is the fallback when Evict failed or hung: it does not depend on the worker process being alive.
	Kill() (int, error)
	// Status reports the VRAM the guests hold, so the guard does not count it as "another user" (OS/user bucket).
	Status() (usedMiB uint64, containers int, err error)
}

var containerID = regexp.MustCompile(`^[0-9a-f]{12,64}$`)

// HTTPEvictor talks to the local ServerlessWorker (loopback + shared secret) and falls back to `docker kill` by label.
type HTTPEvictor struct {
	URL    string // http://127.0.0.1:18111
	Secret string
	GPU    int
	Client *http.Client
	// Docker runs the docker CLI with a fixed argv; replaced in tests.
	Docker func(args ...string) (string, error)
}

func (h *HTTPEvictor) client(timeout time.Duration) *http.Client {
	if h.Client != nil {
		return h.Client
	}
	return &http.Client{Timeout: timeout}
}

func (h *HTTPEvictor) call(method, path string, timeout time.Duration, out any) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(h.URL, "/")+path, bytes.NewReader(nil))
	if err != nil {
		return err
	}
	req.Header.Set("X-Aicf-Evict-Secret", h.Secret)
	resp, err := h.client(timeout).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("serverless worker answered %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (h *HTTPEvictor) Evict(timeout time.Duration) (int, error) {
	var r struct {
		Evicted int `json:"evicted"`
	}
	if err := h.call(http.MethodPost, "/v1/evict", timeout, &r); err != nil {
		return 0, err
	}
	return r.Evicted, nil
}

func (h *HTTPEvictor) Status() (uint64, int, error) {
	var r struct {
		VRAMMiB    uint64 `json:"vram_mib"`
		Containers int    `json:"containers"`
	}
	if err := h.call(http.MethodGet, "/v1/status", 300*time.Millisecond, &r); err != nil {
		return 0, 0, err
	}
	return r.VRAMMiB, r.Containers, nil
}

func (h *HTTPEvictor) docker(args ...string) (string, error) {
	if h.Docker != nil {
		return h.Docker(args...)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "docker", args...).Output()
	return string(out), err
}

// Kill stops every container labelled aicf.sl=1 AND aicf.gpu=<this GPU>. Fixed argv, ids validated as hex: nothing from the network or from a
// container name reaches a shell. Containers of other GPUs and anything not labelled by the serverless runtime are never touched.
func (h *HTTPEvictor) Kill() (int, error) {
	out, err := h.docker("ps", "-q", "--filter", "label=aicf.sl=1", "--filter", fmt.Sprintf("label=aicf.gpu=%d", h.GPU))
	if err != nil {
		return 0, fmt.Errorf("docker ps: %w", err)
	}
	ids := strings.Fields(out)
	if len(ids) == 0 {
		return 0, nil
	}
	args := []string{"kill"}
	for _, id := range ids {
		if !containerID.MatchString(id) {
			return 0, fmt.Errorf("unexpected container id %q", id)
		}
		args = append(args, id)
	}
	if _, err := h.docker(args...); err != nil {
		return 0, fmt.Errorf("docker kill: %w", err)
	}
	return len(ids), nil
}

// evictServerless takes the guests' VRAM back and reports whether free VRAM is above the headroom again. It never waits when there was
// nothing to evict, and never longer than EvictWait otherwise.
func (g *Guard) evictServerless(reason string, pre GPUSample) bool {
	ev := g.cfg.Evictor
	if ev == nil {
		return false
	}
	t0 := g.now()
	timeout := g.cfg.EvictTimeout
	if timeout <= 0 {
		timeout = 1500 * time.Millisecond
	}
	n, err := ev.Evict(timeout)
	how := "evict"
	if err != nil {
		var kerr error
		n, kerr = ev.Kill()
		how = "docker-kill"
		if kerr != nil {
			g.emit(Event{Kind: "evict_failed", Detail: fmt.Sprintf("%s: evict: %v; kill: %v", reason, err, kerr)})
			return false
		}
	}
	if err != nil && n == 0 {
		n = 1 // the worker may have stopped its containers just before the call timed out: give the memory a moment to come back
	}
	if n == 0 {
		return false // nothing to evict: the engine reclaim must not wait
	}
	wait := g.cfg.EvictWait
	if wait <= 0 {
		wait = 2 * time.Second
	}
	deadline := t0.Add(wait)
	for {
		s, serr := g.smp.Sample()
		if serr == nil {
			g.mu.Lock()
			g.last = s
			g.mu.Unlock()
			if s.FreeMiB() >= g.cfg.HeadroomMiB {
				g.emit(Event{Kind: "evict_serverless", Detail: fmt.Sprintf("%s: stopped %d serverless container(s) via %s; free %d MiB >= headroom %d MiB (was %d); engine kept",
					reason, n, how, s.FreeMiB(), g.cfg.HeadroomMiB, pre.FreeMiB()), DetectMS: ms(g.now().Sub(t0))})
				return true
			}
		}
		if !g.now().Before(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	g.emit(Event{Kind: "evict_insufficient", Detail: fmt.Sprintf("%s: stopped %d serverless container(s) via %s but free VRAM is still below the headroom; stopping the engine", reason, n, how)})
	return false
}

// pollServerless keeps the Serverless VRAM figure fresh without ever blocking the control loop.
func (g *Guard) pollServerless(stop <-chan struct{}) {
	ev := g.cfg.Evictor
	if ev == nil {
		return
	}
	for {
		used, _, err := ev.Status()
		g.mu.Lock()
		if err != nil {
			used = 0
		}
		g.serverlessMiB = used
		g.mu.Unlock()
		select {
		case <-stop:
			return
		case <-time.After(time.Second):
		}
	}
}
