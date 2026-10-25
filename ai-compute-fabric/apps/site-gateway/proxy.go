package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

// Inference is the OpenAI-compatible data-plane entry point: admission control
// (rate limit, bounded queue, 429/503 + Retry-After), least-loaded routing and
// a streaming reverse proxy to the chosen worker's llama-server.
//
// Tenant identity comes from the X-Tenant-ID header and is NOT authenticated
// here: this must sit behind the organisation's SSO/API-key layer (plan P2.2 / P7).
type Inference struct {
	R       *Router
	L       *Limiter
	Client  *http.Client
	MaxBody int64
}

func NewInference(r *Router, l *Limiter) *Inference {
	// Keep-alive to llama-server is OFF on purpose: measured on the real engine, reused
	// connections to its streaming endpoint go stale ("EOF" / "server closed idle
	// connection": 24 of 88 requests failed at 8 concurrent clients, 0 of 72 with keep-alive
	// disabled, same throughput). A fresh LAN connection costs well under a millisecond.
	tr := &http.Transport{DisableKeepAlives: true, ResponseHeaderTimeout: 120 * time.Second}
	return &Inference{R: r, L: l, Client: &http.Client{Transport: tr}, MaxBody: 1 << 20}
}

func reject(w http.ResponseWriter, code int, retry time.Duration, msg string) {
	if retry > 0 {
		secs := int(retry.Seconds() + 0.999)
		w.Header().Set("Retry-After", strconv.Itoa(secs))
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"message": msg, "code": code}})
}

func (in *Inference) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	tenant := r.Header.Get("X-Tenant-ID")
	if tenant == "" {
		tenant = "anonymous"
	}
	prio := PrioInteractive
	if r.Header.Get("X-Priority") == "batch" {
		prio = PrioBatch
	}
	if ok, wait := in.L.Allow(tenant); !ok {
		in.R.mu.Lock()
		in.R.stats.RejectedRate++
		in.R.mu.Unlock()
		reject(w, http.StatusTooManyRequests, wait, "tenant rate limit exceeded")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, in.MaxBody))
	if err != nil {
		reject(w, http.StatusRequestEntityTooLarge, 0, "request body too large or unreadable")
		return
	}
	var probe struct {
		Model string `json:"model"`
	}
	if json.Unmarshal(body, &probe) != nil {
		reject(w, http.StatusBadRequest, 0, "body must be JSON")
		return
	}

	skip := ""
	for attempt := 0; attempt < 2; attempt++ { // one retry on another worker, only before any byte is sent
		rt, err := in.R.Acquire(Req{Model: probe.Model, Skip: skip, Tenant: tenant, Prio: prio}, r.Context().Done())
		switch err {
		case nil:
		case ErrQueueFull:
			reject(w, http.StatusTooManyRequests, time.Second, "server busy: queue full")
			return
		case ErrTenantQueueFull:
			reject(w, http.StatusTooManyRequests, time.Second, "too many waiting requests for this tenant")
			return
		case ErrWaitTimeout:
			if r.Context().Err() == nil {
				reject(w, http.StatusServiceUnavailable, 2*time.Second, "no capacity within the wait limit")
			}
			return
		default:
			reject(w, http.StatusServiceUnavailable, 5*time.Second, "no worker serves this model")
			return
		}
		done, retry := in.forward(w, r, rt, body)
		in.R.Release(rt, tenant, !done)
		skip = rt.ID
		if done || !retry {
			return
		}
	}
	reject(w, http.StatusBadGateway, time.Second, "workers failed the request")
}

// forward proxies one attempt. done=true means a response was delivered (success or
// client-side end). retry=true means nothing was sent to the client yet.
func (in *Inference) forward(w http.ResponseWriter, r *http.Request, rt Route, body []byte) (done, retry bool) {
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, rt.URL+r.URL.Path, bytes.NewReader(body))
	if err != nil {
		return false, true
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := in.Client.Do(req)
	if err != nil {
		if r.Context().Err() != nil {
			return true, false // client left
		}
		return false, true
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 500 {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		return false, true
	}
	for k, v := range resp.Header {
		if k == "Content-Length" {
			continue
		}
		w.Header()[k] = v
	}
	w.Header().Set("X-AICF-Worker", rt.ID)
	w.WriteHeader(resp.StatusCode)
	fl, _ := w.(http.Flusher)
	buf := make([]byte, 8192)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return true, false
			}
			if fl != nil {
				fl.Flush()
			}
		}
		if rerr != nil {
			// A mid-stream upstream failure cannot be retried: bytes already reached the client.
			return rerr == io.EOF || r.Context().Err() != nil, false
		}
	}
}

// Metrics renders Prometheus text exposition for the data plane.
func (in *Inference) Metrics(w http.ResponseWriter, _ *http.Request) {
	inflight, queued, s := in.R.Snapshot()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	p := func(name, typ, help string, v any) {
		fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n%s %v\n", name, help, name, typ, name, v)
	}
	p("aicf_inflight_requests", "gauge", "Requests currently on a worker", inflight)
	p("aicf_queued_requests", "gauge", "Requests waiting for a slot", queued)
	p("aicf_requests_total", "counter", "Inference requests received", s.Requests)
	p("aicf_served_total", "counter", "Requests completed", s.Served)
	p("aicf_failovers_total", "counter", "Attempts that failed on a worker", s.Failovers)
	fmt.Fprintf(w, "# TYPE aicf_rejected_total counter\n")
	fmt.Fprintf(w, "aicf_rejected_total{reason=\"queue_full\"} %d\n", s.RejectedQueueFull)
	fmt.Fprintf(w, "aicf_rejected_total{reason=\"tenant_queue_full\"} %d\n", s.RejectedTenantQueue)
	fmt.Fprintf(w, "aicf_rejected_total{reason=\"wait_timeout\"} %d\n", s.RejectedWait)
	fmt.Fprintf(w, "aicf_rejected_total{reason=\"no_worker\"} %d\n", s.RejectedNoWorker)
	fmt.Fprintf(w, "aicf_rejected_total{reason=\"rate_limit\"} %d\n", s.RejectedRate)
	p("aicf_queue_wait_seconds_total", "counter", "Total time served requests spent queued", float64(s.WaitNanosTotal)/1e9)
}
