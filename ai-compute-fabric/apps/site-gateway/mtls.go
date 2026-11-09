package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"os"
	"sync"
	"time"

	"google.golang.org/grpc/credentials"
)

type reloadingKeyPair struct {
	certFile string
	keyFile  string
	mu       sync.Mutex
	cert     tls.Certificate
	modTime  time.Time
}

func newReloadingKeyPair(certFile, keyFile string) (*reloadingKeyPair, error) {
	c, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, err
	}
	var mt time.Time
	if st, err := os.Stat(certFile); err == nil {
		mt = st.ModTime()
	}
	return &reloadingKeyPair{certFile: certFile, keyFile: keyFile, cert: c, modTime: mt}, nil
}

func (r *reloadingKeyPair) get() (*tls.Certificate, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if st, err := os.Stat(r.certFile); err == nil && !st.ModTime().Equal(r.modTime) {
		if c, err := tls.LoadX509KeyPair(r.certFile, r.keyFile); err == nil {
			r.cert = c
			r.modTime = st.ModTime()
		}
	}
	return &r.cert, nil
}

// ServerCreds builds gRPC transport credentials that REQUIRE a client certificate signed by
// clientCAFile (mutual TLS) and hot-reload certFile/keyFile when rotated on disk.
func ServerCreds(certFile, keyFile, clientCAFile string) (credentials.TransportCredentials, error) {
	kp, err := newReloadingKeyPair(certFile, keyFile)
	if err != nil {
		return nil, err
	}
	pem, err := os.ReadFile(clientCAFile)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, errors.New("no certificate in client CA file")
	}
	return credentials.NewTLS(&tls.Config{
		GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) { return kp.get() },
		ClientCAs:      pool,
		ClientAuth:     tls.RequireAndVerifyClientCert,
		MinVersion:     tls.VersionTLS13,
	}), nil
}
