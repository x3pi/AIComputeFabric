package gcp

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

type ca struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pool *x509.CertPool
}

var serial int64 = 100

func newCA(t *testing.T, name string) *ca {
	t.Helper()
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: name}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, _ := x509.CreateCertificate(rand.Reader, tpl, tpl, &k.PublicKey, k)
	c, _ := x509.ParseCertificate(der)
	p := x509.NewCertPool()
	p.AddCert(c)
	return &ca{c, k, p}
}

// issue signs a leaf for cn; notAfter lets a test mint an already-expired one. A nil signer self-signs.
func issue(t *testing.T, signer *ca, cn string, notAfter time.Time) (tls.Certificate, int64) {
	t.Helper()
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	serial++
	tpl := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: cn}, NotBefore: time.Now().Add(-2 * time.Hour), NotAfter: notAfter,
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
	parent, pk := tpl, k
	if signer != nil {
		parent, pk = signer.cert, signer.key
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, parent, &k.PublicKey, pk)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: k}, serial
}

type env struct {
	ca      *ca
	plane   *Plane
	srv     *httptest.Server
	revoked map[string]bool
	hits    *int
}

func startPlane(t *testing.T, pol Policy) *env {
	t.Helper()
	root := newCA(t, "federation-ca")
	srvCert, _ := issue(t, root, "plane", time.Now().Add(time.Hour))
	e := &env{ca: root, plane: NewPlane(pol, []byte("test-secret")), revoked: map[string]bool{}}
	hits := 0
	e.hits = &hits
	h := e.plane.Handler()
	e.srv = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits++; h.ServeHTTP(w, r) }))
	e.srv.TLS = TLSConfig(srvCert, root.pool, func(s string) bool { return e.revoked[s] })
	e.srv.StartTLS()
	t.Cleanup(e.srv.Close)
	return e
}

func (e *env) client(t *testing.T, cert tls.Certificate) *Client {
	t.Helper()
	srvPool := x509.NewCertPool()
	srvPool.AddCert(e.srv.Certificate())
	return &Client{Base: e.srv.URL, HTTP: &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{Certificates: []tls.Certificate{cert}, RootCAs: srvPool, MinVersion: tls.VersionTLS13}}}}
}

func (e *env) siteClient(t *testing.T, site string) *Client {
	c, _ := issue(t, e.ca, site, time.Now().Add(time.Hour))
	return e.client(t, c)
}

// restart brings the plane back on a new port with the same state (ledger, offers) and the same CA.
func (e *env) restart(t *testing.T) {
	t.Helper()
	srvCert, _ := issue(t, e.ca, "plane", time.Now().Add(time.Hour))
	h := e.plane.Handler()
	e.srv = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { *e.hits++; h.ServeHTTP(w, r) }))
	e.srv.TLS = TLSConfig(srvCert, e.ca.pool, func(s string) bool { return e.revoked[s] })
	e.srv.StartTLS()
	t.Cleanup(e.srv.Close)
}
