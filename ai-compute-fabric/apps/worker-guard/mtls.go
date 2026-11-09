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

// ClientCreds builds mutual-TLS credentials: it trusts only the site CA, presents this
// worker's certificate to the gateway, and hot-reloads certFile/keyFile when rotated on disk.
func ClientCreds(caFile, certFile, keyFile, serverName string) (credentials.TransportCredentials, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, err
	}
	var modTime time.Time
	if st, err := os.Stat(certFile); err == nil {
		modTime = st.ModTime()
	}
	var mu sync.Mutex
	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, errors.New("no certificate in CA file")
	}
	return credentials.NewTLS(&tls.Config{
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			mu.Lock()
			defer mu.Unlock()
			if st, err := os.Stat(certFile); err == nil && !st.ModTime().Equal(modTime) {
				if c, err := tls.LoadX509KeyPair(certFile, keyFile); err == nil {
					cert = c
					modTime = st.ModTime()
				}
			}
			return &cert, nil
		},
		RootCAs:    pool,
		ServerName: serverName,
		MinVersion: tls.VersionTLS13,
	}), nil
}
