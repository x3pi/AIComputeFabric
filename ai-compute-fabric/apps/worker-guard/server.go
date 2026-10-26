package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

//go:embed web
var webFS embed.FS

// Handler serves the JSON API, the 1 Hz SSE stream and the embedded dashboard.
// Control endpoints are POST-only and bound to loopback by main (no auth yet:
// RBAC is a P2.4 item, see plan checklist).
func Handler(g *Guard) http.Handler { return HandlerWith(g, EngineDirs{}) }

// EngineDirs are the only places a model or adapter may be chosen from. Without them /api/engine cannot switch anything.
type EngineDirs struct{ Models, Adapters string }

// pick resolves a bare file name inside dir: no separators, .gguf only, must exist. It never accepts a path from the caller.
func pick(dir, name string) (string, error) {
	if dir == "" {
		return "", fmt.Errorf("no directory configured for this kind of file")
	}
	if name == "" || filepath.Base(name) != name || !strings.HasSuffix(name, ".gguf") || strings.HasPrefix(name, ".") {
		return "", fmt.Errorf("%q is not a plain .gguf file name", name)
	}
	p := filepath.Join(dir, name)
	if st, err := os.Stat(p); err != nil || !st.Mode().IsRegular() {
		return "", fmt.Errorf("%q not found", name)
	}
	return p, nil
}

func ggufNames(dir string) []string {
	var out []string
	es, _ := os.ReadDir(dir)
	for _, e := range es {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".gguf") && !strings.HasPrefix(e.Name(), ".") {
			out = append(out, e.Name())
		}
	}
	sort.Strings(out)
	return out
}

func HandlerWith(g *Guard, dirs EngineDirs) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/engine", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.Method {
		case http.MethodGet:
			snap := g.Snapshot()
			json.NewEncoder(w).Encode(map[string]any{"switchable": dirs.Models != "", "model": snap.Model, "lora": snap.Lora, "state": snap.State,
				"models": ggufNames(dirs.Models), "adapters": ggufNames(dirs.Adapters)})
		case http.MethodPost:
			if dirs.Models == "" {
				http.Error(w, `{"error":"model switching is not enabled on this worker (--models-dir)"}`, http.StatusForbidden)
				return
			}
			var in struct{ Model, Lora string }
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				http.Error(w, `{"error":"bad JSON"}`, http.StatusBadRequest)
				return
			}
			m, err := pick(dirs.Models, in.Model)
			if err != nil {
				http.Error(w, fmt.Sprintf(`{"error":%q}`, "model: "+err.Error()), http.StatusBadRequest)
				return
			}
			l := ""
			if in.Lora != "" {
				if l, err = pick(dirs.Adapters, in.Lora); err != nil {
					http.Error(w, fmt.Sprintf(`{"error":%q}`, "adapter: "+err.Error()), http.StatusBadRequest)
					return
				}
			}
			g.Switch(m, l)
			w.WriteHeader(http.StatusAccepted)
			json.NewEncoder(w).Encode(map[string]any{"accepted": true, "model": in.Model, "lora": in.Lora})
		default:
			http.Error(w, "GET or POST", http.StatusMethodNotAllowed)
		}
	})
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
