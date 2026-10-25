// loadgen drives a running site-gateway with N SIMULATED workers (1 Hz heartbeats)
// plus M SSE dashboard viewers, to measure the gateway's capacity. The workers are
// synthetic by design: this measures the control plane, not worker-guard.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	grpcAddr := flag.String("grpc", "127.0.0.1:7443", "")
	httpAddr := flag.String("http", "127.0.0.1:8080", "")
	n := flag.Int("workers", 200, "")
	viewers := flag.Int("viewers", 0, "SSE dashboard clients")
	dur := flag.Duration("duration", 20*time.Second, "")
	out := flag.String("out", "", "write JSON result here")
	flag.Parse()

	var mu sync.Mutex
	var lat []float64
	var errs, hbs, sseBytes, sseMsgs int64
	ctx, cancel := context.WithTimeout(context.Background(), *dur)
	defer cancel()
	var wg sync.WaitGroup
	for i := 0; i < *n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			conn, err := grpc.NewClient(*grpcAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				atomic.AddInt64(&errs, 1)
				return
			}
			defer conn.Close()
			c := pb.NewFleetGatewayClient(conn)
			id := fmt.Sprintf("sim-%05d", i)
			c.Register(ctx, &pb.RegisterRequest{WorkerId: id, VramTotalBytes: 12 << 30, Hostname: id})
			time.Sleep(time.Duration(i%1000) * time.Millisecond) // spread phases like real PCs
			t := time.NewTicker(time.Second)
			defer t.Stop()
			for {
				s := time.Now()
				_, err := c.Heartbeat(ctx, &pb.HeartbeatRequest{State: "running", Model: "Qwen3-8B-Q4_K_M.gguf", GpuTempC: 55,
					Capacity: &pb.WorkerCapacity{WorkerId: id, VramFreeBytes: 5 << 30,
						Vram: &pb.VramBuckets{TotalBytes: 12 << 30, WeightsBytes: 4 << 30, KvStateBytes: 1 << 29, ComputeBytes: 1 << 28,
							HeadroomBytes: 3 << 29, AllocatableBytes: 5 << 30}}})
				if ctx.Err() != nil {
					return
				}
				d := float64(time.Since(s).Microseconds()) / 1000
				mu.Lock()
				lat = append(lat, d)
				mu.Unlock()
				atomic.AddInt64(&hbs, 1)
				if err != nil {
					atomic.AddInt64(&errs, 1)
				}
				select {
				case <-ctx.Done():
					return
				case <-t.C:
				}
			}
		}(i)
	}
	time.Sleep(3 * time.Second) // let the fleet register before viewers join
	for v := 0; v < *viewers; v++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			req, _ := http.NewRequestWithContext(ctx, "GET", "http://"+*httpAddr+"/api/fleet/stream", nil)
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				return
			}
			defer resp.Body.Close()
			sc := bufio.NewScanner(resp.Body)
			sc.Buffer(make([]byte, 64<<20), 64<<20)
			for sc.Scan() {
				if l := sc.Bytes(); len(l) > 6 && string(l[:6]) == "data: " {
					atomic.AddInt64(&sseBytes, int64(len(l)))
					atomic.AddInt64(&sseMsgs, 1)
				}
			}
		}()
	}
	wg.Wait()
	sort.Float64s(lat)
	q := func(p float64) float64 {
		if len(lat) == 0 {
			return 0
		}
		return lat[int(p*float64(len(lat)-1))]
	}
	res := map[string]any{"workers": *n, "viewers": *viewers, "duration_s": dur.Seconds(), "heartbeats": hbs, "errors": errs,
		"hb_p50_ms": q(0.5), "hb_p99_ms": q(0.99), "hb_max_ms": q(1)}
	if *viewers > 0 && sseMsgs > 0 {
		res["sse_avg_msg_bytes"] = sseBytes / sseMsgs
		res["sse_msgs"] = sseMsgs
	}
	b, _ := json.MarshalIndent(res, "", "  ")
	fmt.Println(string(b))
	if *out != "" {
		os.WriteFile(*out, b, 0o644)
	}
}
