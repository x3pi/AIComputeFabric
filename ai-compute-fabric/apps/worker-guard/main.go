// worker-guard: per-PC agent that runs llama-server only while the GPU is not
// needed by the person sitting at the machine (plan P2.1, decision D01).
// This is the Linux proof-of-concept of the reclaim loop; the Windows
// implementation (PDH counters, WDDM) is NOT done and is the main open risk.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	var (
		bin      = flag.String("llama-server", "", "path to llama-server")
		model    = flag.String("model", "", "GGUF model path")
		port     = flag.Int("engine-port", 18090, "llama-server port")
		ctx      = flag.Int("ctx", 4096, "context per slot")
		par      = flag.Int("parallel", 1, "slots")
		ngl      = flag.Int("ngl", 99, "GPU layers")
		headroom = flag.Uint64("headroom-mib", 1536, "free VRAM that must stay available to the user (D07)")
		stable   = flag.Duration("resume-stable", 10*time.Second, "GPU must stay free this long before restart")
		poll     = flag.Duration("poll", 500*time.Millisecond, "normal control-loop period")
		fastPoll = flag.Duration("fast-poll", 100*time.Millisecond, "period when free VRAM is near the headroom")
		near     = flag.Uint64("near-margin-mib", 2048, "\"near\" = free VRAM below headroom + this")
		listen   = flag.String("listen", "127.0.0.1:8099", "dashboard/API address (loopback by default)")
		logPath  = flag.String("engine-log", "", "write engine log here")
		gpu      = flag.Int("gpu", 0, "GPU index")
		gateway  = flag.String("gateway", "", "site-gateway gRPC address (empty = standalone)")
		workerID = flag.String("worker-id", "", "worker id (default: hostname)")
	)
	flag.Parse()
	if *bin == "" || *model == "" {
		log.Fatal("--llama-server and --model are required")
	}
	eng := &LlamaEngine{Bin: *bin, Model: *model, Port: *port, Ctx: *ctx, Parallel: *par, NGL: *ngl,
		LogPath: *logPath, Grace: 500 * time.Millisecond}
	g := NewGuard(Config{HeadroomMiB: *headroom, ResumeStable: *stable, Poll: *poll, FastPoll: *fastPoll, NearMarginMiB: *near}, SMISampler{Index: *gpu}, eng)
	go g.Run(nil)
	if *gateway != "" {
		host, _ := os.Hostname()
		if *workerID == "" {
			*workerID = host
		}
		// Plain gRPC for now: mTLS (step-ca) is an open P2.2 item.
		conn, err := grpc.NewClient(*gateway, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			log.Fatal(err)
		}
		r := &Reporter{Client: pb.NewFleetGatewayClient(conn), G: g, WorkerID: *workerID, SiteID: "dev", Hostname: host,
			GPUName: "NVIDIA GPU", Model: filepath.Base(*model)}
		go r.Run(context.Background())
	}
	log.Printf("worker-guard on http://%s", *listen)
	log.Fatal(http.ListenAndServe(*listen, Handler(g)))
}
