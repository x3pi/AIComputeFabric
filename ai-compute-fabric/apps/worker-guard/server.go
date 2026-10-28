package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
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
			json.NewEncoder(w).Encode(map[string]any{
				"switchable": dirs.Models != "", "model": snap.Model, "lora": snap.Lora, "state": snap.State,
				"mode": snap.Mode, "rpc_servers": snap.RPCServers, "tensor_split": snap.TensorSplit, "rpc_port": snap.RPCPort,
				"models": ggufNames(dirs.Models), "adapters": ggufNames(dirs.Adapters),
			})
		case http.MethodPost:
			if dirs.Models == "" {
				http.Error(w, `{"error":"model switching is not enabled on this worker (--models-dir)"}`, http.StatusForbidden)
				return
			}
			var in struct {
				Model       string `json:"model"`
				Lora        string `json:"lora"`
				Mode        string `json:"mode"`
				NCpuMoe     int    `json:"n_cpu_moe"`
				RPCServers  string `json:"rpc_servers"`
				SplitMode   string `json:"split_mode"`
				TensorSplit string `json:"tensor_split"`
				RPCPort     int    `json:"rpc_port"`
				KVType      string `json:"kv_type"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				http.Error(w, `{"error":"bad JSON"}`, http.StatusBadRequest)
				return
			}
			if msg := validateEngineRequest(in.Mode, in.NCpuMoe, in.RPCServers, in.SplitMode, in.TensorSplit, in.KVType, in.RPCPort); msg != "" {
				http.Error(w, fmt.Sprintf(`{"error":%q}`, msg), http.StatusBadRequest)
				return
			}
			m := ""
			if in.Mode == "rpc-worker" {
				m = in.Model
				if m == "" {
					port := in.RPCPort
					if port <= 0 {
						port = 50052
					}
					m = fmt.Sprintf("%s%d", rpcModelPrefix, port)
				}
			} else {
				if in.Model == "" {
					http.Error(w, `{"error":"model is required"}`, http.StatusBadRequest)
					return
				}
				var err error
				m, err = pick(dirs.Models, in.Model)
				if err != nil {
					http.Error(w, fmt.Sprintf(`{"error":%q}`, "model: "+err.Error()), http.StatusBadRequest)
					return
				}
			}
			l := ""
			if in.Lora != "" {
				var err error
				if l, err = pick(dirs.Adapters, in.Lora); err != nil {
					http.Error(w, fmt.Sprintf(`{"error":%q}`, "adapter: "+err.Error()), http.StatusBadRequest)
					return
				}
			}
			g.SwitchWithOptions(EngineOptions{
				Model:       m,
				Lora:        l,
				Mode:        in.Mode,
				NCpuMoe:     in.NCpuMoe,
				KVType:      in.KVType,
				RPCServers:  in.RPCServers,
				SplitMode:   in.SplitMode,
				TensorSplit: in.TensorSplit,
				RPCPort:     in.RPCPort,
			})
			w.WriteHeader(http.StatusAccepted)
			json.NewEncoder(w).Encode(map[string]any{
				"accepted": true, "model": in.Model, "lora": in.Lora, "mode": in.Mode,
				"rpc_servers": in.RPCServers, "split_mode": in.SplitMode, "tensor_split": in.TensorSplit,
			})
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

var (
	rpcServersRe  = regexp.MustCompile(`^[A-Za-z0-9._-]+:[0-9]{1,5}(,[A-Za-z0-9._-]+:[0-9]{1,5})*$`)
	tensorSplitRe = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?(,[0-9]+(\.[0-9]+)?)*$`)
)

// validateEngineRequest checks the multi-GPU fields of POST /api/engine; it returns "" when they are acceptable.
// Values end up on a process command line, so they are matched against strict patterns instead of passed through.
func validateEngineRequest(mode string, nCpuMoe int, rpcServers, splitMode, tensorSplit, kvType string, rpcPort int) string {
	switch mode {
	case "", "standalone", "split", "smart_1gpu", "merged", "rpc-worker":
	default:
		return "mode must be standalone, split, smart_1gpu, merged or rpc-worker"
	}
	if nCpuMoe < 0 || nCpuMoe > 512 {
		return "n_cpu_moe out of range"
	}
	if mode == "smart_1gpu" && nCpuMoe <= 0 {
		return "smart_1gpu needs n_cpu_moe > 0"
	}
	if mode == "merged" && rpcServers == "" {
		return "merged needs rpc_servers"
	}
	if rpcServers != "" && !rpcServersRe.MatchString(rpcServers) {
		return "rpc_servers must be host:port[,host:port...]"
	}
	if splitMode != "" && splitMode != "layer" && splitMode != "row" {
		return "split_mode must be layer or row"
	}
	if tensorSplit != "" && !tensorSplitRe.MatchString(tensorSplit) {
		return "tensor_split must be comma-separated numbers"
	}
	if kvType != "" && kvType != "f16" && kvType != "q8_0" {
		return "kv_type must be f16 or q8_0"
	}
	if rpcPort < 0 || rpcPort > 65535 {
		return "rpc_port out of range"
	}
	return ""
}
