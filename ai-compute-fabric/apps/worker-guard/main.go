// worker-guard: per-PC agent that runs llama-server only while the GPU is not
// needed by the person sitting at the machine (plan P2.1, decision D01).
// This is the Linux proof-of-concept of the reclaim loop; the Windows
// implementation (PDH counters, WDDM) is NOT done and is the main open risk.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"
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
		poll     = flag.Duration("poll", 100*time.Millisecond, "control-loop period")
		listen   = flag.String("listen", "127.0.0.1:8099", "dashboard/API address (loopback by default)")
		logPath  = flag.String("engine-log", "", "write engine log here")
		gpu      = flag.Int("gpu", 0, "GPU index")
	)
	flag.Parse()
	if *bin == "" || *model == "" {
		log.Fatal("--llama-server and --model are required")
	}
	eng := &LlamaEngine{Bin: *bin, Model: *model, Port: *port, Ctx: *ctx, Parallel: *par, NGL: *ngl,
		LogPath: *logPath, Grace: 500 * time.Millisecond}
	g := NewGuard(Config{HeadroomMiB: *headroom, ResumeStable: *stable, Poll: *poll}, SMISampler{Index: *gpu}, eng)
	go g.Run(nil)
	log.Printf("worker-guard on http://%s", *listen)
	log.Fatal(http.ListenAndServe(*listen, Handler(g)))
}
