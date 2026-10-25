// worker-guard: per-PC agent that runs llama-server only while the GPU is not
// needed by the person sitting at the machine (plan P2.1, decision D01).
// One code base for Linux and Windows (platform files: proc_*.go, service_*.go).
// Verified on Linux only; the Windows build compiles but has never run on Windows
// (WDDM behaviour, PDH counters, the service wrapper): see plan B.8 and I.4.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	if isService() {
		runService(serve)
		return
	}
	stop := make(chan struct{})
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	go func() { <-sig; close(stop) }()
	serve(stop)
}

// serve runs the agent until stop is closed, then stops the engine so no llama-server is
// left holding the user's GPU.
func serve(stop <-chan struct{}) {
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
		advert   = flag.String("advertise-host", "127.0.0.1", "address the gateway uses to reach this engine")
		tlsCA    = flag.String("tls-ca", "", "CA that signed the gateway certificate (enables mTLS)")
		tlsCert  = flag.String("tls-cert", "", "this worker's client certificate")
		tlsKey   = flag.String("tls-key", "", "this worker's private key")
		tlsName  = flag.String("tls-server-name", "", "expected gateway certificate name (default: host of --gateway)")
		workerID = flag.String("worker-id", "", "worker id (default: hostname)")
	)
	flag.Parse()
	if *bin == "" || *model == "" {
		log.Fatal("--llama-server and --model are required")
	}
	eng := &LlamaEngine{Bin: *bin, Model: *model, Port: *port, Ctx: *ctx, Parallel: *par, NGL: *ngl,
		LogPath: *logPath, Grace: 500 * time.Millisecond}
	g := NewGuard(Config{HeadroomMiB: *headroom, ResumeStable: *stable, Poll: *poll, FastPoll: *fastPoll, NearMarginMiB: *near}, SMISampler{Index: *gpu}, eng)
	go g.Run(stop)
	if *gateway != "" {
		host, _ := os.Hostname()
		if *workerID == "" {
			*workerID = host
		}
		creds := insecure.NewCredentials()
		if *tlsCA != "" {
			c, err := ClientCreds(*tlsCA, *tlsCert, *tlsKey, *tlsName)
			if err != nil {
				log.Fatal("mTLS: ", err)
			}
			creds = c
		}
		conn, err := grpc.NewClient(*gateway, grpc.WithTransportCredentials(creds))
		if err != nil {
			log.Fatal(err)
		}
		r := &Reporter{Client: pb.NewFleetGatewayClient(conn), G: g, WorkerID: *workerID, SiteID: "dev", Hostname: host,
			GPUName: "NVIDIA GPU", Model: filepath.Base(*model),
			EngineURL: fmt.Sprintf("http://%s:%d", *advert, *port), Slots: uint32(*par)}
		rctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		go r.Run(rctx)
	}
	srv := &http.Server{Addr: *listen, Handler: Handler(g)}
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	log.Printf("worker-guard on http://%s", *listen)
	select {
	case err := <-errc:
		log.Printf("http server: %v", err)
	case <-stop:
	}
	srv.Close()
	if info, _ := eng.Stop(); info.Signal != "" {
		log.Printf("engine stopped on shutdown (%s, %v)", info.Signal, info.Elapsed)
	}
}
