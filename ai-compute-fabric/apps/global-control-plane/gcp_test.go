package gcp

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var pol = Policy{TenantSites: map[string][]string{
	"acme": {"SITE_A", "SITE_B", "SITE_C"}, "beta": {"SITE_B"}, "gamma": {"SITE_A", "SITE_C"},
}}

func caps(a, b, c int) map[string]Capacity {
	return map[string]Capacity{
		"SITE_A": {Site: "SITE_A", Region: "VN", TotalGPUs: 50, FreeGPUs: a},
		"SITE_B": {Site: "SITE_B", Region: "VN", TotalGPUs: 50, FreeGPUs: b},
		"SITE_C": {Site: "SITE_C", Region: "EU", TotalGPUs: 50, FreeGPUs: c},
	}
}

// --- tests/security/test_wan_tenant_isolation_and_data_residency (20 leak scenarios) ---

func TestTwentyLeakScenariosNeverPlaceDataOnAForbiddenSite(t *testing.T) {
	type sc struct {
		name  string
		job   Job
		caps  map[string]Capacity
		never []string // sites that must not be chosen
		want  Action   // "" = any non-leaking outcome
	}
	j := func(id, tenant string, c DataClass, g int) Job {
		return Job{ID: id, Tenant: tenant, Origin: "SITE_A", Class: c, GPUs: g}
	}
	with := func(job Job, f func(*Job)) Job { f(&job); return job }
	full := caps(0, 50, 50)
	scs := []sc{
		{"restricted, origin full, B has 50 free", j("1", "acme", Restricted, 1), full, []string{"SITE_B", "SITE_C"}, QueueAtOrigin},
		{"restricted, origin full, all others free", j("2", "acme", Restricted, 4), caps(0, 50, 50), []string{"SITE_B", "SITE_C"}, QueueAtOrigin},
		{"restricted with allowlist naming other sites", with(j("3", "acme", Restricted, 1), func(x *Job) { x.SiteAllowlist = []string{"SITE_B"} }), full, []string{"SITE_A", "SITE_B", "SITE_C"}, Reject},
		{"allowlist [SITE_A] on public data", with(j("4", "acme", Public, 1), func(x *Job) { x.SiteAllowlist = []string{"SITE_A"} }), full, []string{"SITE_B", "SITE_C"}, QueueAtOrigin},
		{"allowlist [SITE_A] on internal data", with(j("5", "acme", Internal, 1), func(x *Job) { x.SiteAllowlist = []string{"SITE_A"} }), full, []string{"SITE_B", "SITE_C"}, QueueAtOrigin},
		{"tenant beta is enrolled only at B; origin A", j("6", "beta", Public, 1), full, []string{"SITE_A", "SITE_B", "SITE_C"}, Reject},
		{"tenant gamma never reaches B even if B is idle", j("7", "gamma", Public, 1), caps(0, 50, 0), []string{"SITE_B"}, QueueAtOrigin},
		{"gamma internal, C free: may go to C, never B", j("8", "gamma", Internal, 1), caps(0, 50, 9), []string{"SITE_B"}, Offload},
		{"region pin VN keeps data off the EU site", with(j("9", "acme", Internal, 1), func(x *Job) { x.Region = "VN" }), caps(0, 0, 50), []string{"SITE_C"}, QueueAtOrigin},
		{"region pin EU with origin in VN and restricted class", with(j("10", "acme", Restricted, 1), func(x *Job) { x.Region = "EU" }), caps(0, 0, 50), []string{"SITE_A", "SITE_B", "SITE_C"}, Reject},
		{"empty data class", j("11", "acme", "", 1), full, []string{"SITE_B", "SITE_C"}, Reject},
		{"unknown data class", j("12", "acme", "DATA_TOP_SECRET", 1), full, []string{"SITE_B", "SITE_C"}, Reject},
		{"lowercase class spelling is not accepted", j("13", "acme", "data_restricted", 1), full, []string{"SITE_B", "SITE_C"}, Reject},
		{"zero GPUs requested", j("14", "acme", Restricted, 0), full, []string{"SITE_B"}, Reject},
		{"unknown tenant", j("15", "ghost", Public, 1), full, []string{"SITE_B", "SITE_C"}, Reject},
		{"site B missing from capacity table", j("16", "acme", Restricted, 1), map[string]Capacity{"SITE_A": {Site: "SITE_A", FreeGPUs: 0}}, []string{"SITE_B"}, QueueAtOrigin},
		{"restricted fits at origin: stays local", j("17", "acme", Restricted, 1), caps(5, 50, 50), []string{"SITE_B", "SITE_C"}, RunLocal},
		{"restricted needs more GPUs than origin has", j("18", "acme", Restricted, 8), caps(7, 50, 50), []string{"SITE_B", "SITE_C"}, QueueAtOrigin},
		{"allowlist empty entry string", with(j("19", "acme", Restricted, 1), func(x *Job) { x.SiteAllowlist = []string{""} }), full, []string{"SITE_A", "SITE_B", "SITE_C"}, Reject},
		{"public data may use idle B (control: offload is possible)", j("20", "acme", Public, 1), caps(0, 50, 0), nil, Offload},
	}
	if len(scs) != 20 {
		t.Fatalf("expected 20 scenarios, have %d", len(scs))
	}
	for _, s := range scs {
		d := Place(pol, s.job, s.caps)
		for _, bad := range s.never {
			if d.Site == bad && (d.Action == Offload || d.Action == RunLocal || d.Action == QueueAtOrigin) {
				t.Errorf("%s: data placed on forbidden %s (%+v)", s.name, bad, d)
			}
		}
		if d.Action != s.want {
			t.Errorf("%s: action %s, want %s (%s)", s.name, d.Action, s.want, d.Reason)
		}
	}
}

func TestPlacementPrefersMostFreeAndIsDeterministic(t *testing.T) {
	job := Job{ID: "x", Tenant: "acme", Origin: "SITE_A", Class: Public, GPUs: 2}
	for i := 0; i < 20; i++ {
		d := Place(pol, job, caps(0, 7, 7))
		if d.Action != Offload || d.Site != "SITE_B" {
			t.Fatalf("tie must break by site id: %+v", d)
		}
	}
	if d := Place(pol, job, caps(0, 7, 9)); d.Site != "SITE_C" {
		t.Fatalf("want most free: %+v", d)
	}
}

// --- over the real mTLS transport ---

func TestFederationCallsOverMTLS(t *testing.T) {
	e := startPlane(t, pol)
	a, b := e.siteClient(t, "SITE_A"), e.siteClient(t, "SITE_B")
	for _, c := range []struct {
		cl  *Client
		cap Capacity
	}{{a, Capacity{Site: "SITE_A", Region: "VN", TotalGPUs: 4, FreeGPUs: 0}}, {b, Capacity{Site: "SITE_B", Region: "VN", TotalGPUs: 8, FreeGPUs: 3}}} {
		if code, err := c.cl.Advertise(c.cap); err != nil || code != 200 {
			t.Fatalf("advertise: %v %d", err, code)
		}
	}
	d, tok, code, err := a.OfferJob(Job{ID: "j1", Tenant: "acme", Origin: "SITE_A", Class: Internal, GPUs: 2})
	if err != nil || code != 200 || d.Action != Offload || d.Site != "SITE_B" || tok == "" {
		t.Fatalf("offer: %+v %v %d", d, err, code)
	}
	if code, _ := a.AcceptJob("j1", tok); code != 403 {
		t.Fatalf("the origin must not be able to accept its own offload, got %d", code)
	}
	if code, _ := b.AcceptJob("j1", "garbage"); code != 403 {
		t.Fatalf("garbage token accepted: %d", code)
	}
	if code, _ := b.AcceptJob("j1", tok); code != 200 {
		t.Fatalf("legit accept refused: %d", code)
	}
	if code, _ := b.AcceptJob("j1", tok); code != 403 {
		t.Fatalf("an offer must be acceptable once, got %d", code)
	}
}

func TestExpiredJobTokenIsRefused(t *testing.T) {
	e := startPlane(t, pol)
	a, b := e.siteClient(t, "SITE_A"), e.siteClient(t, "SITE_B")
	a.Advertise(Capacity{Site: "SITE_A", Region: "VN", TotalGPUs: 4, FreeGPUs: 0})
	b.Advertise(Capacity{Site: "SITE_B", Region: "VN", TotalGPUs: 8, FreeGPUs: 3})
	_, tok, _, _ := a.OfferJob(Job{ID: "j2", Tenant: "acme", Origin: "SITE_A", Class: Public, GPUs: 1})
	e.plane.Now = func() time.Time { return time.Now().Add(e.plane.TokenT + time.Second) }
	if code, _ := b.AcceptJob("j2", tok); code != 403 {
		t.Fatalf("expired token accepted: %d", code)
	}
}

func TestSiteCannotActAsAnotherSite(t *testing.T) {
	e := startPlane(t, pol)
	b := e.siteClient(t, "SITE_B")
	if code, _ := b.Advertise(Capacity{Site: "SITE_A", TotalGPUs: 99, FreeGPUs: 99}); code != 400 {
		t.Fatalf("B advertised capacity for A: %d", code)
	}
	if _, _, code, _ := b.OfferJob(Job{ID: "x", Tenant: "acme", Origin: "SITE_A", Class: Public, GPUs: 1}); code != 400 {
		t.Fatalf("B offered a job as origin A: %d", code)
	}
	if code, _ := b.ReportUsage([]Usage{{Site: "SITE_A", Tenant: "acme", Job: "j", Seq: 1, GPUMs: 1}}); code != 403 {
		t.Fatalf("B reported usage for A: %d", code)
	}
	if e.plane.Ledger.Len() != 0 {
		t.Fatal("a forged usage record reached the ledger")
	}
}

func TestAdvertiseRefusesHostLevelDetail(t *testing.T) {
	e := startPlane(t, pol)
	b := e.siteClient(t, "SITE_B")
	code, err := b.post("/v1/advertise", map[string]any{"site": "SITE_B", "total_gpus": 2, "free_gpus": 1, "workers": []string{"10.0.0.7", "10.0.0.8"}}, nil)
	if err != nil || code != 400 {
		t.Fatalf("a capacity report listing worker addresses must be refused: %d %v", code, err)
	}
}

func TestReservationStopsTwoOffersTakingTheLastGPU(t *testing.T) {
	e := startPlane(t, pol)
	a, b := e.siteClient(t, "SITE_A"), e.siteClient(t, "SITE_B")
	a.Advertise(Capacity{Site: "SITE_A", Region: "VN", TotalGPUs: 4, FreeGPUs: 0})
	b.Advertise(Capacity{Site: "SITE_B", Region: "VN", TotalGPUs: 4, FreeGPUs: 1})
	d1, _, _, _ := a.OfferJob(Job{ID: "r1", Tenant: "acme", Origin: "SITE_A", Class: Public, GPUs: 1})
	d2, _, _, _ := a.OfferJob(Job{ID: "r2", Tenant: "acme", Origin: "SITE_A", Class: Public, GPUs: 1})
	if d1.Action != Offload || d2.Action != QueueAtOrigin {
		t.Fatalf("second offer must wait: %+v %+v", d1, d2)
	}
}

// --- tests/security/test_wan_mtls_and_port_exposure (the TLS half) ---

func TestBadCertificatesAreRefusedAndNeverReachTheHandler(t *testing.T) {
	e := startPlane(t, pol)
	otherCA := newCA(t, "someone-else")
	expired, _ := issue(t, e.ca, "SITE_A", time.Now().Add(-time.Minute))
	selfSigned, _ := issue(t, nil, "SITE_A", time.Now().Add(time.Hour))
	foreign, _ := issue(t, otherCA, "SITE_A", time.Now().Add(time.Hour))
	revokedCert, rs := issue(t, e.ca, "SITE_A", time.Now().Add(time.Hour))
	e.revoked[fmt.Sprint(rs)] = true
	cases := []struct {
		name string
		cl   *Client
	}{{"expired", e.client(t, expired)}, {"self-signed", e.client(t, selfSigned)}, {"foreign CA", e.client(t, foreign)}, {"revoked", e.client(t, revokedCert)}}
	for _, c := range cases {
		code, err := c.cl.Advertise(Capacity{Site: "SITE_A", TotalGPUs: 1, FreeGPUs: 1})
		if err == nil || code != 0 {
			t.Errorf("%s: request went through (code %d)", c.name, code)
		}
	}
	if *e.hits != 0 {
		t.Fatalf("%d requests reached the handler through a bad certificate", *e.hits)
	}
	// no client certificate at all
	noCert := &Client{Base: e.srv.URL, HTTP: &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: cases[0].cl.HTTP.Transport.(*http.Transport).TLSClientConfig.Clone()}}}
	noCert.HTTP.Transport.(*http.Transport).TLSClientConfig.Certificates = nil
	if _, err := noCert.Advertise(Capacity{Site: "SITE_A"}); err == nil {
		t.Fatal("a client without a certificate was served")
	}
	// control: a good certificate works on the same server
	if code, err := e.siteClient(t, "SITE_A").Advertise(Capacity{Site: "SITE_A", TotalGPUs: 1, FreeGPUs: 1}); err != nil || code != 200 {
		t.Fatalf("control request failed: %v %d", err, code)
	}
}

func TestRevocationTakesEffectWithoutRestart(t *testing.T) {
	e := startPlane(t, pol)
	cert, s := issue(t, e.ca, "SITE_A", time.Now().Add(time.Hour))
	cl := e.client(t, cert)
	if code, err := cl.Advertise(Capacity{Site: "SITE_A", TotalGPUs: 1, FreeGPUs: 1}); err != nil || code != 200 {
		t.Fatalf("before revocation: %v %d", err, code)
	}
	e.revoked[fmt.Sprint(s)] = true
	cl.HTTP.Transport.(*http.Transport).CloseIdleConnections() // session resumption must not bypass the check
	cl.HTTP.Transport.(*http.Transport).TLSClientConfig.ClientSessionCache = nil
	if _, err := cl.Advertise(Capacity{Site: "SITE_A", TotalGPUs: 1, FreeGPUs: 1}); err == nil {
		t.Fatal("revoked certificate still accepted on a new connection")
	}
}

func TestPlainHTTPAndOldTLSAreRefused(t *testing.T) {
	e := startPlane(t, pol)
	resp, err := http.Get("http://" + e.srv.Listener.Addr().String() + "/v1/advertise")
	if err == nil {
		resp.Body.Close()
		if resp.StatusCode == 200 {
			t.Fatal("plain HTTP served")
		}
	}
	if e.srv.TLS.MinVersion < 0x0304 {
		t.Fatal("TLS below 1.3 allowed")
	}
}

// --- tests/resilience/test_wan_partition_and_autonomous_site (logic half; no tc netem) ---

func TestPartitionThenReconcileGivesZeroDifference(t *testing.T) {
	e := startPlane(t, pol)
	siteA, siteB := NewSite("SITE_A"), NewSite("SITE_B")
	ca := e.siteClient(t, "SITE_A")

	serve := func(s *Site, n int, tenant string) (ok int) { // purely local: never touches a Client
		for i := 0; i < n; i++ {
			if s.Record(Usage{Tenant: tenant, Job: fmt.Sprintf("%s-%d", s.ID, i), PromptTokens: int64(10 + i), CompletionTokens: int64(100 + i), GPUMs: int64(250 + i), VRAMMiBSec: 4000, MicroKWh: 90}) == nil {
				ok++
			}
		}
		return
	}
	// healthy WAN for a while
	serve(siteA, 5, "acme")
	serve(siteB, 5, "beta")
	if _, err := siteA.Sync(ca, 3); err != nil {
		t.Fatal(err)
	}
	// WAN goes away: the plane stops answering
	e.srv.CloseClientConnections()
	e.srv.Close()
	if got := serve(siteA, 40, "acme") + serve(siteB, 40, "beta"); got != 80 {
		t.Fatalf("local service during the partition: %d/80", got)
	}
	if _, err := siteA.Sync(ca, 10); err == nil {
		t.Fatal("sync claimed success with no plane")
	}
	if e.plane.Ledger.Len() != 5 {
		t.Fatalf("plane ledger should still hold only what arrived before the cut, has %d", e.plane.Ledger.Len())
	}
	// WAN back: a new plane server over the same ledger and CA
	e.restart(t)
	ca, cb := e.siteClient(t, "SITE_A"), e.siteClient(t, "SITE_B")
	if _, err := siteA.Sync(ca, 7); err != nil {
		t.Fatal(err)
	}
	if _, err := siteB.Sync(cb, 7); err != nil {
		t.Fatal(err)
	}
	// resend everything once more: duplicates must change nothing
	siteA.mu.Lock()
	siteA.sent = 0
	siteA.mu.Unlock()
	if _, err := siteA.Sync(ca, 50); err != nil {
		t.Fatal(err)
	}
	for _, s := range []*Site{siteA, siteB} {
		miss, diff := s.Local.Diff(e.plane.Ledger)
		if len(miss)+len(diff) != 0 {
			t.Fatalf("%s: missing %v different %v", s.ID, miss, diff)
		}
	}
	if e.plane.Ledger.Len() != siteA.Local.Len()+siteB.Local.Len() {
		t.Fatalf("plane has %d records, sites have %d", e.plane.Ledger.Len(), siteA.Local.Len()+siteB.Local.Len())
	}
	if err := e.plane.Ledger.Verify(); err != nil {
		t.Fatal(err)
	}
	// totals match exactly
	var want int64
	for _, s := range []*Site{siteA, siteB} {
		for _, r := range s.Local.Records() {
			want += r.GPUMs
		}
	}
	var got int64
	for _, tot := range e.plane.Ledger.Totals() {
		got += tot.Sum.GPUMs
	}
	if got != want {
		t.Fatalf("gpu_ms total: plane %d, sites %d", got, want)
	}
}

func TestLedgerRejectsConflictingResendAndDetectsTampering(t *testing.T) {
	l := NewLedger()
	u := Usage{Site: "S", Tenant: "t", Job: "j", Seq: 1, GPUMs: 5}
	if ok, err := l.Append(u); !ok || err != nil {
		t.Fatal("first append")
	}
	if ok, err := l.Append(u); ok || err != nil {
		t.Fatal("exact duplicate must be a silent no-op")
	}
	u2 := u
	u2.GPUMs = 6
	if _, err := l.Append(u2); err != ErrConflict {
		t.Fatalf("same key with other numbers must conflict, got %v", err)
	}
	if _, err := l.Append(Usage{Site: "S", Tenant: "t", Job: "j", Seq: 2, GPUMs: -1}); err == nil {
		t.Fatal("negative usage accepted")
	}
	l.Append(Usage{Site: "S", Tenant: "t", Job: "k", Seq: 2, GPUMs: 7})
	if err := l.Verify(); err != nil {
		t.Fatal(err)
	}
	l.entries[0].GPUMs = 1 // someone edits history
	if l.Verify() == nil {
		t.Fatal("edit not detected")
	}
}

func TestTotalsAreGroupedByTenantAndSite(t *testing.T) {
	l := NewLedger()
	l.Append(Usage{Site: "A", Tenant: "x", Job: "1", Seq: 1, PromptTokens: 1, CompletionTokens: 2, AudioMs: 3})
	l.Append(Usage{Site: "A", Tenant: "x", Job: "2", Seq: 2, PromptTokens: 10})
	l.Append(Usage{Site: "B", Tenant: "x", Job: "3", Seq: 1, PromptTokens: 100})
	l.Append(Usage{Site: "A", Tenant: "y", Job: "4", Seq: 3, PromptTokens: 1000})
	tt := l.Totals()
	if len(tt) != 3 || tt[0].Sum.PromptTokens != 11 || tt[0].Sum.CompletionTokens != 2 || tt[0].Sum.AudioMs != 3 || tt[1].Sum.PromptTokens != 100 || tt[2].Sum.PromptTokens != 1000 {
		t.Fatalf("%+v", tt)
	}
}

func TestRegionPinFailsClosedWhenTheSiteHasNotAdvertisedItsRegion(t *testing.T) {
	job := Job{ID: "r", Tenant: "acme", Origin: "SITE_A", Class: Internal, Region: "VN", GPUs: 1}
	d := Place(pol, job, map[string]Capacity{}) // no site has advertised anything yet
	if d.Action != Reject {
		t.Fatalf("an unknown region must not satisfy a residency pin: %+v", d)
	}
}

func TestOpenLedgerPersistsAcrossRestartAndRejectsTamperedFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.jsonl")
	l1, err := OpenLedger(path)
	if err != nil {
		t.Fatal(err)
	}
	u1 := Usage{Site: "SITE_A", Tenant: "acme", Job: "j1", Seq: 1, PromptTokens: 120, CompletionTokens: 340, GPUMs: 500, VRAMMiBSec: 2048, MicroKWh: 42}
	u2 := Usage{Site: "SITE_B", Tenant: "beta", Job: "j2", Seq: 1, PromptTokens: 80, CompletionTokens: 160, GPUMs: 300, VRAMMiBSec: 1024, MicroKWh: 21}
	if ok, err := l1.Append(u1); !ok || err != nil {
		t.Fatalf("append u1: %v %v", ok, err)
	}
	if ok, err := l1.Append(u2); !ok || err != nil {
		t.Fatalf("append u2: %v %v", ok, err)
	}
	if err := l1.Close(); err != nil {
		t.Fatal(err)
	}

	n, head, err := VerifyLedgerFile(path)
	if err != nil || n != 2 || len(head) != 64 {
		t.Fatalf("verify: n=%d head=%s err=%v", n, head, err)
	}

	// Reopen and verify idempotency + chain continuation
	l2, err := OpenLedger(path)
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := l2.Append(u1); ok || err != nil {
		t.Fatalf("duplicate after reopen must be no-op: %v %v", ok, err)
	}
	u3 := Usage{Site: "SITE_A", Tenant: "acme", Job: "j3", Seq: 2, PromptTokens: 50, CompletionTokens: 50, GPUMs: 100}
	if ok, err := l2.Append(u3); !ok || err != nil {
		t.Fatalf("append u3 after reopen: %v %v", ok, err)
	}
	if l2.Len() != 3 || l2.Verify() != nil {
		t.Fatalf("expected 3 verified entries, got %d", l2.Len())
	}
	_ = l2.Close()

	// Tamper with the on-disk file and verify OpenLedger refuses to load it
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	tampered := strings.Replace(string(raw), `"gpu_ms":500`, `"gpu_ms":999`, 1)
	if err := os.WriteFile(path, []byte(tampered), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenLedger(path); err == nil {
		t.Fatal("expected OpenLedger to reject tampered file")
	}
}

func TestPlaneStatusAndLedgerTotalsEndpointsOverMTLS(t *testing.T) {
	e := startPlane(t, pol)
	a, b := e.siteClient(t, "SITE_A"), e.siteClient(t, "SITE_B")
	_, _ = a.Advertise(Capacity{Site: "SITE_A", Region: "VN", TotalGPUs: 10, FreeGPUs: 4, FreeVRAMGiB: 48})
	_, _ = b.Advertise(Capacity{Site: "SITE_B", Region: "VN", TotalGPUs: 20, FreeGPUs: 12, FreeVRAMGiB: 144})
	_, _ = a.ReportUsage([]Usage{{Site: "SITE_A", Tenant: "acme", Job: "j1", Seq: 1, PromptTokens: 100, CompletionTokens: 200, GPUMs: 1000}})
	_, _ = b.ReportUsage([]Usage{{Site: "SITE_B", Tenant: "acme", Job: "j2", Seq: 1, PromptTokens: 50, CompletionTokens: 100, GPUMs: 500}})

	sites, recs, code, err := a.Status()
	if err != nil || code != 200 || len(sites) != 2 || recs != 2 {
		t.Fatalf("status: code=%d recs=%d sites=%+v err=%v", code, recs, sites, err)
	}
	totals, code, err := a.LedgerTotals()
	if err != nil || code != 200 || len(totals) != 2 {
		t.Fatalf("ledger totals: code=%d totals=%+v err=%v", code, totals, err)
	}
}
