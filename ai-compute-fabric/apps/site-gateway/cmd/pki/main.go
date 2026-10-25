// pki: create the internal CA and the gateway/worker certificates used for mTLS (plan P2.2).
package main

import (
	"flag"
	"log"
	"strings"
	"time"

	"github.com/aicf/ai-compute-fabric/apps/site-gateway/internal/pki"
)

func main() {
	dir := flag.String("out", "pki", "output directory")
	hosts := flag.String("gateway-hosts", "127.0.0.1,localhost", "comma list of gateway IPs/DNS names")
	workers := flag.String("workers", "pc-001", "comma list of worker ids to issue client certs for")
	days := flag.Int("days", 90, "validity of leaf certificates")
	flag.Parse()
	ws := strings.Split(*workers, ",")
	if err := pki.Generate(*dir, strings.Split(*hosts, ","), ws, time.Duration(*days)*24*time.Hour); err != nil {
		log.Fatal(err)
	}
	log.Printf("wrote CA, gateway and %d worker certificates to %s", len(ws), *dir)
}
