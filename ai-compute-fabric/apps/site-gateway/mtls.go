package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"os"

	"google.golang.org/grpc/credentials"
)

// ServerCreds builds gRPC transport credentials that REQUIRE a client certificate signed by
// clientCAFile (mutual TLS). Only workers enrolled with that CA can register.
func ServerCreds(certFile, keyFile, clientCAFile string) (credentials.TransportCredentials, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
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
	return credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{cert}, ClientCAs: pool,
		ClientAuth: tls.RequireAndVerifyClientCert, MinVersion: tls.VersionTLS13}), nil
}
