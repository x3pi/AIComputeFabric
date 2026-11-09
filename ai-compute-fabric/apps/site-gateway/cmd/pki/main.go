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
	rotate := flag.Bool("rotate", false, "rotate leaf certificates under existing CA if needed")
	renewWithinDays := flag.Int("renew-within-days", 30, "when -rotate is set, only rotate if gateway.crt expires within this many days (0 = force)")
	flag.Parse()
	ws := strings.Split(*workers, ",")
	valid := time.Duration(*days) * 24 * time.Hour
	if *rotate {
		gwCert := *dir + "/gateway.crt"
		if *renewWithinDays > 0 && !pki.NeedsRotation(gwCert, time.Duration(*renewWithinDays)*24*time.Hour) {
			return
		}
		if err := pki.RotateLeaves(*dir, strings.Split(*hosts, ","), ws, valid); err != nil {
			log.Fatal(err)
		}
		log.Printf("rotated gateway and %d worker leaf certificates under CA in %s", len(ws), *dir)
		return
	}
	if err := pki.Generate(*dir, strings.Split(*hosts, ","), ws, valid); err != nil {
		log.Fatal(err)
	}
	log.Printf("wrote CA, gateway and %d worker certificates to %s", len(ws), *dir)
}
