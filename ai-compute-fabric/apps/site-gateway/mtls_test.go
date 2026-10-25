package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/aicf/ai-compute-fabric/apps/site-gateway/internal/pki"
	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

func mtlsServer(t *testing.T, dir string) (string, *Registry) {
	t.Helper()
	creds, err := ServerCreds(filepath.Join(dir, "gateway.crt"), filepath.Join(dir, "gateway.key"), filepath.Join(dir, "ca.crt"))
	if err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(3 * time.Second)
	lis, _ := net.Listen("tcp", "127.0.0.1:0")
	s := grpc.NewServer(grpc.Creds(creds))
	pb.RegisterFleetGatewayServer(s, &GRPC{Reg: reg})
	go s.Serve(lis)
	t.Cleanup(s.Stop)
	return lis.Addr().String(), reg
}

func register(addr string, creds credentials.TransportCredentials) error {
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(creds))
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, err = pb.NewFleetGatewayClient(conn).Register(ctx, &pb.RegisterRequest{WorkerId: "pc-001"})
	return err
}

func clientCreds(t *testing.T, ca, cert, key string) credentials.TransportCredentials {
	t.Helper()
	cfg := &tls.Config{ServerName: "localhost", MinVersion: tls.VersionTLS13}
	pem, _ := os.ReadFile(ca)
	cfg.RootCAs = x509.NewCertPool()
	cfg.RootCAs.AppendCertsFromPEM(pem)
	if cert != "" {
		c, err := tls.LoadX509KeyPair(cert, key)
		if err != nil {
			t.Fatal(err)
		}
		cfg.Certificates = []tls.Certificate{c}
	}
	return credentials.NewTLS(cfg)
}

// Only a worker holding a certificate signed by the site CA may register; a client without a
// certificate, one signed by another CA, and plaintext are all refused.
func TestMTLSAdmitsEnrolledWorkersOnly(t *testing.T) {
	dir, other := t.TempDir(), t.TempDir()
	if err := pki.Generate(dir, []string{"127.0.0.1", "localhost"}, []string{"pc-001"}, time.Hour); err != nil {
		t.Fatal(err)
	}
	if err := pki.Generate(other, []string{"127.0.0.1", "localhost"}, []string{"pc-001"}, time.Hour); err != nil {
		t.Fatal(err)
	}
	addr, _ := mtlsServer(t, dir)
	ca := filepath.Join(dir, "ca.crt")

	if err := register(addr, clientCreds(t, ca, filepath.Join(dir, "pc-001.crt"), filepath.Join(dir, "pc-001.key"))); err != nil {
		t.Fatalf("enrolled worker refused: %v", err)
	}
	if err := register(addr, clientCreds(t, ca, "", "")); err == nil {
		t.Fatal("client without certificate was admitted")
	}
	if err := register(addr, clientCreds(t, ca, filepath.Join(other, "pc-001.crt"), filepath.Join(other, "pc-001.key"))); err == nil {
		t.Fatal("certificate from a foreign CA was admitted")
	}
	if err := register(addr, insecure.NewCredentials()); err == nil {
		t.Fatal("plaintext client was admitted")
	}
}
