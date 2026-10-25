package main

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"strings"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

//go:embed web
var webFS embed.FS

// GRPC implements aicf.v1.FleetGateway. Transport security (mTLS, step-ca) is a
// separate P2.2 item: today the listener is plain and meant for loopback/dev.
type GRPC struct {
	pb.UnimplementedFleetGatewayServer
	Reg *Registry
}

func (g *GRPC) Register(_ context.Context, r *pb.RegisterRequest) (*pb.RegisterReply, error) {
	g.Reg.Register(r)
	return &pb.RegisterReply{HeartbeatIntervalMs: 1000}, nil
}

func (g *GRPC) Heartbeat(_ context.Context, r *pb.HeartbeatRequest) (*pb.HeartbeatReply, error) {
	reg, pause := g.Reg.Heartbeat(r)
	return &pb.HeartbeatReply{Registered: reg, PauseRequested: pause}, nil
}

// HTTPHandler serves the fleet API and the embedded fleet page.
func HTTPHandler(reg *Registry) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/workers", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(reg.List())
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
			b, _ := json.Marshal(reg.List())
			fmt.Fprintf(w, "data: %s\n\n", b)
			fl.Flush()
			select {
			case <-r.Context().Done():
				return
			case <-t.C:
			}
		}
	})
	// POST /api/workers/{id}/pause and /resume
	mux.HandleFunc("/api/workers/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/workers/"), "/")
		if len(parts) != 2 || (parts[1] != "pause" && parts[1] != "resume") {
			http.NotFound(w, r)
			return
		}
		if !reg.SetPause(parts[0], parts[1] == "pause") {
			http.Error(w, "unknown worker", http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	sub, _ := fs.Sub(webFS, "web")
	mux.Handle("/", http.FileServer(http.FS(sub)))
	return mux
}
