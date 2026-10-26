// site-gateway: the per-site control point workers register with (plan P2.2).
// Implemented: Register/Heartbeat over gRPC, in-memory registry, offline
// detection, operator pause directive, fleet page, optional mTLS. NOT implemented:
// NATS JetStream, PostgreSQL, leases.
package main

import (
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
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
	fair := flag.Bool("fair", true, "weighted fair queuing per tenant (false = one shared FIFO)")
	perTenantQueue := flag.Int("tenant-queue", 0, "queue slots one tenant may use (0 = max-queue/4)")
	perTenantInflight := flag.Int("tenant-inflight", 0, "concurrent requests one tenant may run (0 = unlimited)")
	weightsFlag := flag.String("tenant-weights", "", "comma list tenant=weight, e.g. sales=3,ops=1")
	tenantsFile := flag.String("tenants-file", "", "JSON file with tenants and hashed API keys (enables API-key auth)")
	adminToken := flag.String("admin-token", os.Getenv("AICF_ADMIN_TOKEN"), "bearer token for /api/* (admin); required when listening beyond loopback")
	affinity := flag.Duration("affinity-patience", 300*time.Millisecond, "wait this long for the worker holding a conversation's cache (0 = off)")
	maxLease := flag.Duration("max-lease-ttl", 10*time.Minute, "longest TTL a lease may have; clients extend while the job runs")
	tlsCert := flag.String("tls-cert", "", "gateway server certificate (enables mTLS on the gRPC port)")
	tlsKey := flag.String("tls-key", "", "gateway server private key")
	tlsCA := flag.String("tls-client-ca", "", "CA that must have signed every worker certificate")
	flag.Parse()
	reg := NewRegistry(*offline)
	lis, err := net.Listen("tcp", *grpcAddr)
	if err != nil {
		log.Fatal(err)
	}
	var opts []grpc.ServerOption
	if *tlsCert != "" {
		creds, err := ServerCreds(*tlsCert, *tlsKey, *tlsCA)
		if err != nil {
			log.Fatal("mTLS: ", err)
		}
		opts = append(opts, grpc.Creds(creds))
	} else if h, _, _ := net.SplitHostPort(*grpcAddr); h != "127.0.0.1" && h != "localhost" && h != "::1" {
		log.Print("WARNING: gRPC is listening beyond loopback without mTLS (--tls-cert/--tls-key/--tls-client-ca)")
	}
	s := grpc.NewServer(opts...)
	pb.RegisterFleetGatewayServer(s, &GRPC{Reg: reg})
	pb.RegisterLeaseServiceServer(s, NewLeaseManager(reg, *maxLease))
	go func() { log.Fatal(s.Serve(lis)) }()
	log.Printf("site-gateway gRPC %s, http://%s", *grpcAddr, *httpAddr)
	fleet := NewFleet(reg)
	go fleet.Run(time.Second, nil)
	weights := map[string]float64{}
	for _, kv := range strings.Split(*weightsFlag, ",") {
		if p := strings.SplitN(kv, "=", 2); len(p) == 2 {
			if w, err := strconv.ParseFloat(p[1], 64); err == nil && w > 0 {
				weights[strings.TrimSpace(p[0])] = w
			}
		}
	}
	store, err := NewTenantStore(*tenantsFile)
	if err != nil {
		log.Fatal(err)
	}
	var policy func(string) (float64, int)
	if *tenantsFile != "" {
		policy = store.Policy
	}
	router := NewRouter(RouterConfig{Policy: policy, MaxQueue: *maxQueue, MaxWait: *maxWait, Fair: *fair, MaxQueuePerTenant: *perTenantQueue,
		MaxInflightTenant: *perTenantInflight, Weights: weights, AffinityPatience: *affinity}, reg)
	go router.Run(nil)
	if h, _, _ := net.SplitHostPort(*httpAddr); *adminToken == "" && h != "127.0.0.1" && h != "localhost" && h != "::1" {
		log.Fatal("refusing to expose the admin API on ", *httpAddr, " without --admin-token (or AICF_ADMIN_TOKEN)")
	}
	inf := NewInference(router, NewLimiter(*rate, *burst))
	inf.Tenants, inf.AuthRequired = store, *tenantsFile != ""
	if !inf.AuthRequired {
		log.Print("WARNING: no --tenants-file: tenants are taken from the unauthenticated X-Tenant-ID header (dev mode)")
	}
	mux := http.NewServeMux()
	mux.Handle("/v1/", inf)
	mux.HandleFunc("/metrics", inf.Metrics)
	mux.Handle("/", NewAPI(fleet, store, *adminToken, router))
	log.Fatal(http.ListenAndServe(*httpAddr, mux))
}
