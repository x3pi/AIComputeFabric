package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

func TestPhaD_DF3_StaticFileServerIgnoresUntrustedCWDWebFolder(t *testing.T) {
	origWD, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.Chdir(origWD) }()

	tmp := t.TempDir()
	evilWeb := filepath.Join(tmp, "web")
	if err := os.MkdirAll(evilWeb, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(evilWeb, "index.html"), []byte("PWNED_BY_CWD_WEB_DIR"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(tmp); err != nil {
		t.Fatal(err)
	}

	reg := NewRegistry(5 * time.Second)
	fleet := NewFleet(reg)
	h := HTTPHandler(fleet)

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 OK from embedded webFS, got %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "PWNED_BY_CWD_WEB_DIR") {
		t.Fatalf("D-F3 VULNERABILITY: HTTPHandler served untrusted CWD ./web/index.html instead of embedded webFS!")
	}
}

func TestPhaD_DF4_VerifyWorkerPeerRejectsEmptyCommonNameAndEmptyWorkerID(t *testing.T) {
	emptyCNCert := &x509.Certificate{Subject: pkix.Name{CommonName: ""}}
	ctxEmptyCN := peer.NewContext(context.Background(), &peer.Peer{
		AuthInfo: credentials.TLSInfo{
			State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{emptyCNCert}},
		},
	})
	if err := verifyWorkerPeer(ctxEmptyCN, "pc-001"); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("D-F4 VULNERABILITY: expected PermissionDenied for empty certificate CommonName, got %v", err)
	}

	validCert := &x509.Certificate{Subject: pkix.Name{CommonName: "pc-001"}}
	ctxValid := peer.NewContext(context.Background(), &peer.Peer{
		AuthInfo: credentials.TLSInfo{
			State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{validCert}},
		},
	})
	if err := verifyWorkerPeer(ctxValid, ""); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("D-F4 VULNERABILITY: expected PermissionDenied for empty workerID, got %v", err)
	}
	if err := verifyWorkerPeer(ctxValid, "pc-002"); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("expected PermissionDenied for mismatched workerID, got %v", err)
	}
	if err := verifyWorkerPeer(ctxValid, "pc-001"); err != nil {
		t.Fatalf("expected nil for matching CN=pc-001, got %v", err)
	}
}
