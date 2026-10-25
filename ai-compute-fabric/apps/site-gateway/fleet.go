package main

import (
	"encoding/json"
	"sync"
	"time"
)

// Fleet is the read side. A background loop rebuilds, once per second, a sorted
// worker list and ONE pre-serialized compact summary that every dashboard viewer
// shares. Per-viewer cost is therefore a socket write, not a JSON marshal of the
// whole fleet (the first version marshalled ~2.4 MB per viewer per second at
// 5,000 workers; see benchmarks/reports/p2_gateway_capacity.json).
type Fleet struct {
	reg *Registry

	mu      sync.RWMutex
	workers []Worker
	byID    map[string]Worker
	summary []byte
	gen     chan struct{} // closed (and replaced) on every refresh to wake SSE viewers
}

func NewFleet(reg *Registry) *Fleet {
	f := &Fleet{reg: reg, gen: make(chan struct{})}
	f.Refresh()
	return f
}

// state codes keep the heatmap payload small.
var stateCode = map[string]int{"offline": 0, "registered": 1, "stopped": 2, "paused": 3, "yielded": 4, "reclaiming": 5, "running": 6}

// Summary is the compact heatmap frame: counts plus one fixed-shape row per worker
// [id, stateCode, vramUsedPermille, tempC].
type Summary struct {
	At     int64          `json:"at"`
	Total  int            `json:"total"`
	Counts map[string]int `json:"counts"`
	Cells  [][4]any       `json:"cells"`
}

func (f *Fleet) Refresh() {
	ws := f.reg.List()
	by := make(map[string]Worker, len(ws))
	s := Summary{At: time.Now().UnixMilli(), Total: len(ws), Counts: map[string]int{}, Cells: make([][4]any, 0, len(ws))}
	for _, w := range ws {
		by[w.WorkerID] = w
		s.Counts[w.State]++
		used := 0
		if w.VRAMTotal > 0 && w.Online {
			used = int(1000 * (w.VRAMTotal - min64(w.VRAMFree, w.VRAMTotal)) / w.VRAMTotal)
		}
		s.Cells = append(s.Cells, [4]any{w.WorkerID, stateCode[w.State], used, w.TempC})
	}
	b, _ := json.Marshal(s)
	f.mu.Lock()
	f.workers, f.byID, f.summary = ws, by, b
	old := f.gen
	f.gen = make(chan struct{})
	f.mu.Unlock()
	close(old)
}

func min64(a, b uint64) uint64 {
	if a < b {
		return a
	}
	return b
}

// Run refreshes every interval until stop is closed.
func (f *Fleet) Run(interval time.Duration, stop <-chan struct{}) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			f.Refresh()
		}
	}
}

// Page returns workers[offset:offset+limit] filtered by state ("" = all) and the match total.
func (f *Fleet) Page(offset, limit int, state string) ([]Worker, int) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	src := f.workers
	if state != "" {
		src = make([]Worker, 0)
		for _, w := range f.workers {
			if w.State == state {
				src = append(src, w)
			}
		}
	}
	total := len(src)
	if offset > total {
		offset = total
	}
	end := offset + limit
	if end > total {
		end = total
	}
	out := make([]Worker, end-offset) // never nil: an empty page must encode as [] not null
	copy(out, src[offset:end])
	return out, total
}

func (f *Fleet) Get(id string) (Worker, bool) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	w, ok := f.byID[id]
	return w, ok
}

// Frame returns the shared serialized summary and a channel closed on the next refresh.
func (f *Fleet) Frame() ([]byte, <-chan struct{}) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.summary, f.gen
}
