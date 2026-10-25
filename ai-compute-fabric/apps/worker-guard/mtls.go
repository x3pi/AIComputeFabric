package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"os"

	"google.golang.org/grpc/credentials"
)

// ClientCreds builds mutual-TLS credentials: it trusts only the site CA and presents this
// worker's certificate to the gateway.
func ClientCreds(caFile, certFile, keyFile, serverName string) (credentials.TransportCredentials, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, err
	}
	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, errors.New("no certificate in CA file")
	}
	return credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{cert}, RootCAs: pool,
		ServerName: serverName, MinVersion: tls.VersionTLS13}), nil
}
