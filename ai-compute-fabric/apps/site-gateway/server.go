package main

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"strconv"
	"strings"

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
//
//	GET  /api/fleet/stream           SSE, one shared compact summary per second (heatmap)
//	GET  /api/workers?offset&limit&state   paginated detail (limit default 100, max 500)
//	GET  /api/workers/{id}           one worker
//	POST /api/workers/{id}/pause|resume
func HTTPHandler(f *Fleet) http.Handler {
	reg := f.reg
	mux := http.NewServeMux()
	mux.HandleFunc("/api/fleet/stream", func(w http.ResponseWriter, r *http.Request) {
		fl, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", 500)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		for {
			frame, next := f.Frame()
			if _, err := fmt.Fprintf(w, "data: %s\n\n", frame); err != nil {
				return
			}
			fl.Flush()
			select {
			case <-r.Context().Done():
				return
			case <-next:
			}
		}
	})
	mux.HandleFunc("/api/workers", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		offset, _ := strconv.Atoi(q.Get("offset"))
		limit, err := strconv.Atoi(q.Get("limit"))
		if err != nil || limit <= 0 {
			limit = 100
		}
		if limit > 500 {
			limit = 500
		}
		if offset < 0 {
			offset = 0
		}
		ws, total := f.Page(offset, limit, q.Get("state"))
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Total-Count", strconv.Itoa(total))
		json.NewEncoder(w).Encode(ws)
	})
	mux.HandleFunc("/api/workers/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/workers/"), "/")
		if len(parts) == 1 && parts[0] != "" && r.Method == http.MethodGet {
			wk, ok := f.Get(parts[0])
			if !ok {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(wk)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
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
