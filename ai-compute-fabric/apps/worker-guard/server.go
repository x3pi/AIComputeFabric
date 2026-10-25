package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"time"
)

//go:embed web
var webFS embed.FS

// Handler serves the JSON API, the 1 Hz SSE stream and the embedded dashboard.
// Control endpoints are POST-only and bound to loopback by main (no auth yet:
// RBAC is a P2.4 item, see plan checklist).
func Handler(g *Guard) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/state", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(g.Snapshot())
	})
	mux.HandleFunc("/api/events", func(w http.ResponseWriter, r *http.Request) {
		fl, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", 500)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		t := time.NewTicker(time.Second)
		defer t.Stop()
		for {
			b, _ := json.Marshal(g.Snapshot())
			fmt.Fprintf(w, "data: %s\n\n", b)
			fl.Flush()
			select {
			case <-r.Context().Done():
				return
			case <-t.C:
			}
		}
	})
	post := func(f func()) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				http.Error(w, "POST only", http.StatusMethodNotAllowed)
				return
			}
			f()
			w.WriteHeader(http.StatusNoContent)
		}
	}
	mux.HandleFunc("/api/pause", post(g.Pause))
	mux.HandleFunc("/api/resume", post(g.Resume))
	sub, _ := fs.Sub(webFS, "web")
	mux.Handle("/", http.FileServer(http.FS(sub)))
	return mux
}
