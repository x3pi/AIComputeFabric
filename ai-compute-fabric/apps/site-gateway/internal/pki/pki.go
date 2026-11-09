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
	return issueLeaves(dir, ca, caKey, gatewayHosts, workers, valid)
}

// NeedsRotation reports whether certFile is missing, unreadable, or expires within renewWithin from now.
func NeedsRotation(certFile string, renewWithin time.Duration) bool {
	pemBytes, err := os.ReadFile(certFile)
	if err != nil {
		return true
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return true
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return true
	}
	return time.Now().Add(renewWithin).After(cert.NotAfter)
}

// RotateLeaves re-issues the gateway and worker leaf certificates signed by the existing CA in dir
// (ca.crt + ca.key). If the CA does not exist yet, it falls back to Generate.
func RotateLeaves(dir string, gatewayHosts []string, workers []string, valid time.Duration) error {
	caCertPEM, err1 := os.ReadFile(filepath.Join(dir, "ca.crt"))
	caKeyPEM, err2 := os.ReadFile(filepath.Join(dir, "ca.key"))
	if err1 != nil || err2 != nil {
		return Generate(dir, gatewayHosts, workers, valid)
	}
	cBlock, _ := pem.Decode(caCertPEM)
	kBlock, _ := pem.Decode(caKeyPEM)
	if cBlock == nil || kBlock == nil {
		return Generate(dir, gatewayHosts, workers, valid)
	}
	ca, err := x509.ParseCertificate(cBlock.Bytes)
	if err != nil {
		return err
	}
	caKey, err := x509.ParseECPrivateKey(kBlock.Bytes)
	if err != nil {
		return err
	}
	return issueLeaves(dir, ca, caKey, gatewayHosts, workers, valid)
}

func issueLeaves(dir string, ca *x509.Certificate, caKey *ecdsa.PrivateKey, gatewayHosts []string, workers []string, valid time.Duration) error {
	now := time.Now().Add(-time.Minute)
	baseSerial := time.Now().UnixNano()
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
			} else if h != "" {
				tpl.DNSNames = append(tpl.DNSNames, h)
			}
		}
		der, err := x509.CreateCertificate(rand.Reader, tpl, ca, &k.PublicKey, caKey)
		if err != nil {
			return err
		}
		return writePEM(filepath.Join(dir, name), der, k)
	}
	if err := issue("gateway", baseSerial+2, x509.ExtKeyUsageServerAuth, gatewayHosts); err != nil {
		return err
	}
	for i, w := range workers {
		if err := issue(w, baseSerial+int64(10+i), x509.ExtKeyUsageClientAuth, nil); err != nil {
			return err
		}
	}
	return nil
}

func writePEM(base string, certDER []byte, key *ecdsa.PrivateKey) error {
	tmpCrt := base + ".crt.tmp"
	if err := os.WriteFile(tmpCrt, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}), 0o644); err != nil {
		return err
	}
	kd, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return err
	}
	tmpKey := base + ".key.tmp"
	if err := os.WriteFile(tmpKey, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kd}), 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmpKey, base+".key"); err != nil {
		return err
	}
	return os.Rename(tmpCrt, base+".crt")
}
