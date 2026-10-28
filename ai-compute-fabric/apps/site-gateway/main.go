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
	"path/filepath"
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
	rolesFile := flag.String("roles-file", "", "JSON roles file (viewer/operator/admin, SHA-256 token hashes) for the admin API")
	studioURL := flag.String("studio-url", "", "AI Studio service (python -m studio), served at /api/studio/")
	agentsURL := flag.String("agents-url", "", "agent workflow view service (agents/dag_api.py), served read-only at /api/agents/")
	flag.IntVar(&WorkerControlPort, "worker-control-port", 8099, "port of the worker-guard control API (worker-guard --listen) used by POST /api/cluster/topology; 0 = same as the worker engine_url")
	topologyFile := flag.String("topology-file", "", "JSON file to persist cluster topology state across restarts (default: <dir-of-tenants-file>/topology.json when -tenants-file is set)")
	auditPath := flag.String("audit-log", "", "append-only hash-chained audit log of admin actions")
	affinity := flag.Duration("affinity-patience", 300*time.Millisecond, "wait this long for the worker holding a conversation's cache (0 = off)")
	maxLease := flag.Duration("max-lease-ttl", 10*time.Minute, "longest TTL a lease may have; clients extend while the job runs")
	tlsCert := flag.String("tls-cert", "", "gateway server certificate (enables mTLS on the gRPC port)")
	tlsKey := flag.String("tls-key", "", "gateway server private key")
	tlsCA := flag.String("tls-client-ca", "", "CA that must have signed every worker certificate")
	vramSidecarFlag := flag.String("vram-sidecar-url", "", "GGUF facts service (python -m core.vram_sidecar), enables POST /api/v1/models/estimate-vram")
	flag.Parse()
	if *topologyFile != "" {
		DefaultTopologyFile = *topologyFile
	} else if *tenantsFile != "" {
		DefaultTopologyFile = filepath.Join(filepath.Dir(*tenantsFile), "topology.json")
	}
	vramSidecar = NewSidecar(*vramSidecarFlag)
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
	auth := NewAuth(*adminToken)
	if *rolesFile != "" {
		if err := auth.LoadRoles(*rolesFile); err != nil {
			log.Fatal(err)
		}
	}
	var audit *AuditLog
	if *auditPath != "" {
		if audit, err = OpenAudit(*auditPath); err != nil {
			log.Fatal(err)
		}
	}
	if h, _, _ := net.SplitHostPort(*httpAddr); auth.Open() && h != "127.0.0.1" && h != "localhost" && h != "::1" {
		log.Fatal("refusing to expose the admin API on ", *httpAddr, " without --admin-token (or AICF_ADMIN_TOKEN) or --roles-file")
	}
	inf := NewInference(router, NewLimiter(*rate, *burst))
	inf.Tenants, inf.AuthRequired = store, *tenantsFile != ""
	if !inf.AuthRequired {
		log.Print("WARNING: no --tenants-file: tenants are taken from the unauthenticated X-Tenant-ID header (dev mode)")
	}
	mux := http.NewServeMux()
	mux.Handle("/v1/", inf)
	mux.HandleFunc("/metrics", inf.Metrics)
	var agents http.Handler
	if *agentsURL != "" {
		if agents, err = AgentsProxy(*agentsURL); err != nil {
			log.Fatal(err)
		}
	}
	var studio http.Handler
	if *studioURL != "" {
		if studio, err = StudioProxy(*studioURL, 64<<20); err != nil {
			log.Fatal(err)
		}
	}
	mux.Handle("/", NewAPIStudio(fleet, store, auth, audit, agents, studio, router))
	log.Fatal(newHTTPServer(*httpAddr, mux, defaultTimeouts).ListenAndServe())
}
