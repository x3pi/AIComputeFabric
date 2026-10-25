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
	log.Fatal(http.ListenAndServe(*httpAddr, HTTPHandler(reg)))
}
