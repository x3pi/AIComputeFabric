// Command gcp-server runs the Global Control Plane (plan P7) over TLS 1.3 mTLS with optional
// durable hash-chained ledger persistence on disk and live certificate-serial revocation.
package main

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	gcp "github.com/aicf/ai-compute-fabric/apps/global-control-plane"
)

func loadPolicy(path string) (gcp.Policy, error) {
	if path == "" {
		return gcp.Policy{TenantSites: map[string][]string{}}, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return gcp.Policy{}, err
	}
	var wrapped struct {
		TenantSites map[string][]string `json:"tenant_sites"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && len(wrapped.TenantSites) > 0 {
		return gcp.Policy{TenantSites: wrapped.TenantSites}, nil
	}
	var direct map[string][]string
	if err := json.Unmarshal(raw, &direct); err != nil {
		return gcp.Policy{}, err
	}
	return gcp.Policy{TenantSites: direct}, nil
}

func isRevokedFromFile(path, serial string) bool {
	if path == "" {
		return false
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(raw), "\n") {
		s := strings.TrimSpace(line)
		if s != "" && !strings.HasPrefix(s, "#") && s == serial {
			return true
		}
	}
	return false
}

func main() {
	var (
		listen       = flag.String("listen", "127.0.0.1:8443", "HTTPS mTLS listen address")
		certFile     = flag.String("cert", "", "server TLS certificate PEM")
		keyFile      = flag.String("key", "", "server TLS private key PEM")
		caFile       = flag.String("ca", "", "federation CA certificate PEM for verifying site client certificates")
		policyFile   = flag.String("policy-file", "", "JSON file mapping tenants to enrolled site IDs")
		ledgerFile   = flag.String("ledger-file", "", "append-only hash-chained usage ledger JSONL file")
		revokedFile  = flag.String("revoked-file", "", "file of revoked client certificate serial numbers (one per line)")
		secretEnv    = flag.String("secret-env", "AICF_GCP_SECRET", "environment variable holding the offer-token HMAC secret")
		verifyLedger = flag.Bool("verify-ledger", false, "verify --ledger-file hash chain and exit")
	)
	flag.Parse()

	if *verifyLedger {
		if *ledgerFile == "" {
			log.Fatal("--verify-ledger requires --ledger-file")
		}
		n, last, err := gcp.VerifyLedgerFile(*ledgerFile)
		if err != nil {
			log.Fatalf("FAIL ledger verification (%s): %v", *ledgerFile, err)
		}
		fmt.Printf("OK entries=%d head=%s\n", n, last)
		return
	}

	if *certFile == "" || *keyFile == "" || *caFile == "" {
		log.Fatal("--cert, --key and --ca are required for mTLS 1.3 federation transport")
	}

	srvCert, err := tls.LoadX509KeyPair(*certFile, *keyFile)
	if err != nil {
		log.Fatalf("load server keypair: %v", err)
	}
	caPEM, err := os.ReadFile(*caFile)
	if err != nil {
		log.Fatalf("read CA file: %v", err)
	}
	caPool := x509.NewCertPool()
	if !caPool.AppendCertsFromPEM(caPEM) {
		log.Fatal("no certificates parsed from --ca")
	}

	pol, err := loadPolicy(*policyFile)
	if err != nil {
		log.Fatalf("load policy: %v", err)
	}

	secret := []byte(os.Getenv(*secretEnv))
	if len(secret) == 0 {
		secret = make([]byte, 32)
		if _, err := rand.Read(secret); err != nil {
			log.Fatalf("rand secret: %v", err)
		}
	}

	plane := gcp.NewPlane(pol, secret)
	if *ledgerFile != "" {
		ledger, err := gcp.OpenLedger(*ledgerFile)
		if err != nil {
			log.Fatalf("open ledger %s: %v", *ledgerFile, err)
		}
		defer func() { _ = ledger.Close() }()
		plane.Ledger = ledger
	}

	tlsCfg := gcp.TLSConfig(srvCert, caPool, func(serial string) bool {
		return isRevokedFromFile(*revokedFile, serial)
	})
	srv := &http.Server{
		Addr:              *listen,
		Handler:           plane.Handler(),
		TLSConfig:         tlsCfg,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    64 << 10,
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()

	log.Printf("global-control-plane listening on https://%s (mTLS 1.3, ledger=%q)", *listen, *ledgerFile)
	if err := srv.ListenAndServeTLS("", ""); err != nil && err != http.ErrServerClosed {
		log.Fatalf("ListenAndServeTLS: %v", err)
	}
}
