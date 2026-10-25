// site-gateway: the per-site control point workers register with (plan P2.2).
// Implemented: Register/Heartbeat over gRPC, in-memory registry, offline
// detection, operator pause directive, fleet page. NOT implemented: mTLS,
// NATS JetStream, PostgreSQL, leases.
package main

import (
	"flag"
	"log"
	"net"
	"net/http"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
)

func main() {
	grpcAddr := flag.String("grpc", "127.0.0.1:7443", "gRPC listen address")
	httpAddr := flag.String("http", "127.0.0.1:8080", "HTTP/dashboard listen address")
	offline := flag.Duration("offline-after", 3*time.Second, "mark a worker offline after this long without heartbeat")
	maxQueue := flag.Int("max-queue", 64, "requests allowed to wait for a slot")
	maxWait := flag.Duration("max-wait", 10*time.Second, "longest a request waits for a slot")
	rate := flag.Float64("tenant-rate", 0, "requests/s per tenant (0 = unlimited)")
	burst := flag.Int("tenant-burst", 20, "token bucket burst per tenant")
	flag.Parse()
	reg := NewRegistry(*offline)
	lis, err := net.Listen("tcp", *grpcAddr)
	if err != nil {
		log.Fatal(err)
	}
	s := grpc.NewServer()
	pb.RegisterFleetGatewayServer(s, &GRPC{Reg: reg})
	go func() { log.Fatal(s.Serve(lis)) }()
	log.Printf("site-gateway gRPC %s, http://%s", *grpcAddr, *httpAddr)
	fleet := NewFleet(reg)
	go fleet.Run(time.Second, nil)
	router := NewRouter(RouterConfig{MaxQueue: *maxQueue, MaxWait: *maxWait}, reg)
	go router.Run(nil)
	inf := NewInference(router, NewLimiter(*rate, *burst))
	mux := http.NewServeMux()
	mux.Handle("/v1/", inf)
	mux.HandleFunc("/metrics", inf.Metrics)
	mux.Handle("/", HTTPHandler(fleet))
	log.Fatal(http.ListenAndServe(*httpAddr, mux))
}
