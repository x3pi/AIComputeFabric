// Package pki creates the internal CA and certificates used for gateway<->worker mTLS.
package pki

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"time"
)

// Generate writes an internal CA, a gateway server certificate and one client certificate
// per worker into dir (ca.crt/ca.key, gateway.crt/.key, <worker>.crt/.key). Intended for
// small sites; a production deployment can replace it with step-ca (plan P2.2).
func Generate(dir string, gatewayHosts []string, workers []string, valid time.Duration) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	now := time.Now().Add(-time.Minute)
	caTpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "AICF internal CA"},
		NotBefore: now, NotAfter: now.Add(valid * 4), IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageCRLSign}
	caDER, err := x509.CreateCertificate(rand.Reader, caTpl, caTpl, &caKey.PublicKey, caKey)
	if err != nil {
		return err
	}
	ca, _ := x509.ParseCertificate(caDER)
	if err := writePEM(filepath.Join(dir, "ca"), caDER, caKey); err != nil {
		return err
	}
	issue := func(name string, serial int64, usage x509.ExtKeyUsage, hosts []string) error {
		k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return err
		}
		tpl := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: name},
			NotBefore: now, NotAfter: now.Add(valid), KeyUsage: x509.KeyUsageDigitalSignature,
			ExtKeyUsage: []x509.ExtKeyUsage{usage}}
		for _, h := range hosts {
			if ip := net.ParseIP(h); ip != nil {
				tpl.IPAddresses = append(tpl.IPAddresses, ip)
			} else {
				tpl.DNSNames = append(tpl.DNSNames, h)
			}
		}
		der, err := x509.CreateCertificate(rand.Reader, tpl, ca, &k.PublicKey, caKey)
		if err != nil {
			return err
		}
		return writePEM(filepath.Join(dir, name), der, k)
	}
	if err := issue("gateway", 2, x509.ExtKeyUsageServerAuth, gatewayHosts); err != nil {
		return err
	}
	for i, w := range workers {
		if err := issue(w, int64(10+i), x509.ExtKeyUsageClientAuth, nil); err != nil {
			return err
		}
	}
	return nil
}

func writePEM(base string, certDER []byte, key *ecdsa.PrivateKey) error {
	if err := os.WriteFile(base+".crt", pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}), 0o644); err != nil {
		return err
	}
	kd, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return err
	}
	return os.WriteFile(base+".key", pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kd}), 0o600)
}
