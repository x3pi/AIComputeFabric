package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

func addWorker(reg *Registry, id, url, model string, slots uint32) {
	reg.Register(&pb.RegisterRequest{WorkerId: id, EngineUrl: url, ParallelSlots: slots})
	reg.Heartbeat(&pb.HeartbeatRequest{State: "running", Model: model, Capacity: &pb.WorkerCapacity{WorkerId: id}})
}

// upstream is a fake llama-server: it holds each request until release is closed.
type upstream struct {
	*httptest.Server
	mu       sync.Mutex
	hits     int
	inflight int
	maxSeen  int
	release  chan struct{}
}

func newUpstream() *upstream {
	u := &upstream{release: make(chan struct{})}
	u.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.mu.Lock()
		u.hits++
		u.inflight++
		if u.inflight > u.maxSeen {
			u.maxSeen = u.inflight
		}
		u.mu.Unlock()
		defer func() { u.mu.Lock(); u.inflight--; u.mu.Unlock() }()
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-u.release:
		case <-r.Context().Done():
			return
		}
		io.WriteString(w, "data: last\n\n")
	}))
	return u
}

func gwRig(t *testing.T, cfg RouterConfig, rate float64, burst int) (*Registry, *Router, *httptest.Server) {
	t.Helper()
	reg := NewRegistry(time.Hour)
	rt := NewRouter(cfg, reg)
	in := NewInference(rt, NewLimiter(rate, burst))
	srv := httptest.NewServer(in)
	t.Cleanup(srv.Close)
	return reg, rt, srv
}

func post(url, model, tenant string) (*http.Response, error) {
	req, _ := http.NewRequest("POST", url+"/v1/chat/completions", strings.NewReader(`{"model":"`+model+`","stream":true}`))
	if tenant != "" {
		req.Header.Set("X-Tenant-ID", tenant)
	}
	return http.DefaultClient.Do(req)
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	for i := 0; i < 400; i++ {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestStreamingIsNotBufferedAndSlotsAreRespected(t *testing.T) {
	up := newUpstream()
	defer up.Close()
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 8, MaxWait: 5 * time.Second}, 0, 0)
	addWorker(reg, "w1", up.URL, "m", 2)
	rt.Refresh()

	var wg sync.WaitGroup
	firstChunk := make(chan struct{}, 4)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := post(srv.URL, "m", "")
			if err != nil {
				t.Error(err)
				return
			}
			defer resp.Body.Close()
			buf := make([]byte, 64)
			resp.Body.Read(buf) // first chunk arrives while the upstream is still holding the request
			firstChunk <- struct{}{}
			io.Copy(io.Discard, resp.Body)
		}()
	}
	waitFor(t, "2 first chunks", func() bool { return len(firstChunk) == 2 })
	time.Sleep(100 * time.Millisecond)
	if len(firstChunk) != 2 {
		t.Fatalf("worker has 2 slots: only 2 requests may be in flight, got %d", len(firstChunk))
	}
	if _, q, _ := rt.Snapshot(); q != 2 {
		t.Fatalf("2 requests must be queued, got %d", q)
	}
	close(up.release) // lets everything finish, queued ones get slots in turn
	wg.Wait()
	up.mu.Lock()
	defer up.mu.Unlock()
	if up.maxSeen > 2 || up.hits != 4 {
		t.Fatalf("max concurrent at upstream %d (limit 2), hits %d (want 4)", up.maxSeen, up.hits)
	}
}

func TestQueueFullIs429WithRetryAfter(t *testing.T) {
	up := newUpstream()
	defer up.Close()
	defer close(up.release)
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 1, MaxWait: 5 * time.Second}, 0, 0)
	addWorker(reg, "w1", up.URL, "m", 1)
	rt.Refresh()
	go post(srv.URL, "m", "") // occupies the slot
	waitFor(t, "slot taken", func() bool { i, _, _ := rt.Snapshot(); return i == 1 })
	go post(srv.URL, "m", "") // fills the queue
	waitFor(t, "queued", func() bool { _, q, _ := rt.Snapshot(); return q == 1 })
	resp, err := post(srv.URL, "m", "")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 429 || resp.Header.Get("Retry-After") == "" {
		t.Fatalf("status %d retry-after %q", resp.StatusCode, resp.Header.Get("Retry-After"))
	}
}

func TestWaitTimeoutNoWorkerAndBadBody(t *testing.T) {
	up := newUpstream()
	defer up.Close()
	defer close(up.release)
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 4, MaxWait: 150 * time.Millisecond}, 0, 0)
	addWorker(reg, "w1", up.URL, "m", 1)
	rt.Refresh()
	go post(srv.URL, "m", "")
	waitFor(t, "slot taken", func() bool { i, _, _ := rt.Snapshot(); return i == 1 })
	resp, _ := post(srv.URL, "m", "")
	if resp.StatusCode != 503 || resp.Header.Get("Retry-After") == "" {
		t.Fatalf("wait timeout -> %d", resp.StatusCode)
	}
	if _, q, _ := rt.Snapshot(); q != 0 {
		t.Fatalf("an expired request must leave the queue, queued=%d", q)
	}
	if resp, _ = post(srv.URL, "other-model", ""); resp.StatusCode != 503 {
		t.Fatalf("unknown model -> %d", resp.StatusCode)
	}
	req, _ := http.NewRequest("POST", srv.URL+"/v1/chat/completions", strings.NewReader("not json"))
	if resp, _ = http.DefaultClient.Do(req); resp.StatusCode != 400 {
		t.Fatalf("bad body -> %d", resp.StatusCode)
	}
}

func TestTenantRateLimit(t *testing.T) {
	l := NewLimiter(2, 2) // 2 req/s, burst 2
	clock := time.Unix(100, 0)
	l.now = func() time.Time { return clock }
	for i := 0; i < 2; i++ {
		if ok, _ := l.Allow("a"); !ok {
			t.Fatal("burst must pass")
		}
	}
	ok, wait := l.Allow("a")
	if ok || wait <= 0 || wait > time.Second {
		t.Fatalf("third request must be limited with a sensible wait, ok=%v wait=%v", ok, wait)
	}
	if ok, _ := l.Allow("b"); !ok {
		t.Fatal("another tenant must not be affected")
	}
	clock = clock.Add(600 * time.Millisecond)
	if ok, _ := l.Allow("a"); !ok {
		t.Fatal("tokens must refill over time")
	}
}

func TestRateLimitedRequestGets429(t *testing.T) {
	up := newUpstream()
	defer up.Close()
	close(up.release)
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 4, MaxWait: time.Second}, 0.001, 1)
	addWorker(reg, "w1", up.URL, "m", 4)
	rt.Refresh()
	if resp, _ := post(srv.URL, "m", "acme"); resp.StatusCode != 200 {
		t.Fatalf("first request: %d", resp.StatusCode)
	}
	resp, _ := post(srv.URL, "m", "acme")
	if resp.StatusCode != 429 || resp.Header.Get("Retry-After") == "" {
		t.Fatalf("second request must be rate limited, got %d", resp.StatusCode)
	}
}

func TestFailoverToAnotherWorkerBeforeFirstByte(t *testing.T) {
	good := newUpstream()
	defer good.Close()
	close(good.release)
	dead := httptest.NewServer(http.NotFoundHandler())
	deadURL := dead.URL
	dead.Close() // connection refused
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 4, MaxWait: time.Second, Cooldown: time.Minute}, 0, 0)
	addWorker(reg, "a-dead", deadURL, "m", 4) // sorted/least-loaded tie: tried first
	addWorker(reg, "b-good", good.URL, "m", 4)
	rt.Refresh()
	for i := 0; i < 3; i++ {
		resp, err := post(srv.URL, "m", "")
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("request %d should have failed over: %v %v", i, err, resp)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	if _, _, s := rt.Snapshot(); s.Failovers < 1 {
		t.Fatalf("expected at least one failover, stats %+v", s)
	}
	if good.hits != 3 {
		t.Fatalf("all 3 requests must end on the good worker, hits=%d", good.hits)
	}
}

func TestClientDisconnectLeavesQueue(t *testing.T) {
	up := newUpstream()
	defer up.Close()
	defer close(up.release)
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 4, MaxWait: 10 * time.Second}, 0, 0)
	addWorker(reg, "w1", up.URL, "m", 1)
	rt.Refresh()
	go post(srv.URL, "m", "")
	waitFor(t, "slot taken", func() bool { i, _, _ := rt.Snapshot(); return i == 1 })
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "POST", srv.URL+"/v1/chat/completions", strings.NewReader(`{"model":"m"}`))
	go http.DefaultClient.Do(req)
	waitFor(t, "queued", func() bool { _, q, _ := rt.Snapshot(); return q == 1 })
	cancel()
	waitFor(t, "abandoned request leaves the queue", func() bool { _, q, _ := rt.Snapshot(); return q == 0 })
}

func TestModelRoutingAndMetrics(t *testing.T) {
	a, b := newUpstream(), newUpstream()
	defer a.Close()
	defer b.Close()
	close(a.release)
	close(b.release)
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 4, MaxWait: time.Second}, 0, 0)
	addWorker(reg, "wa", a.URL, "qwen-8b", 2)
	addWorker(reg, "wb", b.URL, "qwen-4b", 2)
	rt.Refresh()
	for i := 0; i < 2; i++ {
		resp, _ := post(srv.URL, "qwen-4b", "")
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	if a.hits != 0 || b.hits != 2 {
		t.Fatalf("model routing wrong: a=%d b=%d", a.hits, b.hits)
	}
	in := NewInference(rt, NewLimiter(0, 0))
	rec := httptest.NewRecorder()
	in.Metrics(rec, nil)
	for _, want := range []string{"aicf_served_total 2", "aicf_inflight_requests 0", `aicf_rejected_total{reason="queue_full"} 0`} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Errorf("metrics missing %q:\n%s", want, rec.Body.String())
		}
	}
}

// Regression for the llama-server keep-alive finding: an upstream that advertises
// keep-alive but closes the socket after every response must not cause failures.
func TestUpstreamThatClosesAfterEachResponseNeverFailsRequests(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				br := bufio.NewReader(c)
				req, err := http.ReadRequest(br)
				if err != nil {
					return
				}
				io.Copy(io.Discard, req.Body)
				body := "data: ok\n\n"
				fmt.Fprintf(c, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: %d\r\nConnection: keep-alive\r\n\r\n%s", len(body), body)
				// ...and then hang up anyway, slightly later: a client that reuses the connection in
				// the meantime has its next request swallowed (the llama-server failure mode).
				time.Sleep(50 * time.Millisecond)
			}()
		}
	}()
	reg, rt, srv := gwRig(t, RouterConfig{MaxQueue: 4, MaxWait: time.Second}, 0, 0)
	addWorker(reg, "w1", "http://"+ln.Addr().String(), "m", 4)
	rt.Refresh()
	for i := 0; i < 30; i++ {
		resp, err := post(srv.URL, "m", "")
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("request %d failed: %v %v", i, err, resp)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	if _, _, s := rt.Snapshot(); s.Failovers != 0 {
		t.Fatalf("no request should have failed over, stats %+v", s)
	}
}

func TestSingleBlipDoesNotTakeTheOnlyWorkerOutButRepeatedFailuresDo(t *testing.T) {
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 4, MaxWait: time.Second, Cooldown: time.Minute}, reg)
	addWorker(reg, "w1", "http://x", "m", 2)
	rt.Refresh()
	r1, _ := rt.Acquire("m", "", nil)
	rt.Release(r1, true)
	if _, err := rt.Acquire("m", "", nil); err != nil {
		t.Fatalf("one failure must not trigger a cooldown: %v", err)
	}
	rt.Release(r1, true) // second consecutive failure
	cancel := make(chan struct{})
	time.AfterFunc(50*time.Millisecond, func() { close(cancel) })
	if _, err := rt.Acquire("m", "", cancel); err != ErrWaitTimeout {
		t.Fatalf("after 2 consecutive failures the worker must be skipped (request waits, then gives up): %v", err)
	}
}
