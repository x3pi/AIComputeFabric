package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

type rbacRig struct {
	h     http.Handler
	audit *AuditLog
	path  string
	reg   *Registry
}

func newRBACRig(t *testing.T) *rbacRig {
	t.Helper()
	dir := t.TempDir()
	roles := filepath.Join(dir, "roles.json")
	es := []map[string]string{
		{"name": "vera", "role": "viewer", "token_sha256": HashToken("tok-viewer")},
		{"name": "omar", "role": "operator", "token_sha256": HashToken("tok-operator")},
		{"name": "sam", "role": "operator", "site": "hanoi", "token_sha256": HashToken("tok-sam")},
		{"name": "ada", "role": "admin", "token_sha256": HashToken("tok-admin")},
	}
	b, _ := json.Marshal(es)
	os.WriteFile(roles, b, 0o600)
	auth := NewAuth("legacy-admin")
	if err := auth.LoadRoles(roles); err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(time.Hour)
	reg.Register(&pb.RegisterRequest{WorkerId: "w-hn", SiteId: "hanoi", EngineUrl: "http://x"})
	reg.Register(&pb.RegisterRequest{WorkerId: "w-hcm", SiteId: "hcm", EngineUrl: "http://x"})
	ts, _ := NewTenantStore("")
	path := filepath.Join(dir, "audit.jsonl")
	al, err := OpenAudit(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { al.Close() })
	return &rbacRig{h: NewAPIWith(NewFleet(reg), ts, auth, al), audit: al, path: path, reg: reg}
}

func (g *rbacRig) do(method, path, tok, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	rec := httptest.NewRecorder()
	g.h.ServeHTTP(rec, req)
	return rec
}

func TestRoleMatrix(t *testing.T) {
	g := newRBACRig(t)
	cases := []struct {
		name, tok, method, path, body string
		want                          int
	}{
		{"no token", "", "GET", "/api/workers", "", 401},
		{"unknown token", "nope", "GET", "/api/workers", "", 401},
		{"viewer reads workers", "tok-viewer", "GET", "/api/workers", "", 200},
		{"viewer reads tenants", "tok-viewer", "GET", "/api/tenants", "", 200},
		{"viewer cannot pause", "tok-viewer", "POST", "/api/workers/w-hn/pause", "", 403},
		{"viewer cannot create tenant", "tok-viewer", "POST", "/api/tenants", `{"name":"x"}`, 403},
		{"operator reads", "tok-operator", "GET", "/api/workers", "", 200},
		{"operator pauses any site", "tok-operator", "POST", "/api/workers/w-hcm/pause", "", 204},
		{"operator cannot create tenant", "tok-operator", "POST", "/api/tenants", `{"name":"x"}`, 403},
		{"operator cannot rotate key", "tok-operator", "POST", "/api/tenants/t1/rotate-key", "", 403},
		{"site operator pauses own site", "tok-sam", "POST", "/api/workers/w-hn/pause", "", 204},
		{"site operator blocked on other site", "tok-sam", "POST", "/api/workers/w-hcm/pause", "", 403},
		{"site operator cannot pause an unknown worker", "tok-sam", "POST", "/api/workers/ghost/pause", "", 403},
		{"admin creates tenant", "tok-admin", "POST", "/api/tenants", `{"name":"ops","weight":1}`, 201},
		{"legacy admin token still admin", "legacy-admin", "POST", "/api/tenants", `{"name":"ops2","weight":1}`, 201},
		{"static page stays public", "", "GET", "/", "", 200},
	}
	for _, c := range cases {
		if rec := g.do(c.method, c.path, c.tok, c.body); rec.Code != c.want {
			t.Errorf("%s: %s %s -> %d, want %d (%s)", c.name, c.method, c.path, rec.Code, c.want, strings.TrimSpace(rec.Body.String()))
		}
	}
}

func TestOperatorActionActuallyReachesTheWorkerAndForbiddenOneDoesNot(t *testing.T) {
	g := newRBACRig(t)
	g.do("POST", "/api/workers/w-hcm/pause", "tok-sam", "") // forbidden
	if wk, _ := NewFleet(g.reg).Get("w-hcm"); wk.PauseWanted {
		t.Fatal("a forbidden request must not change the worker")
	}
	g.do("POST", "/api/workers/w-hn/pause", "tok-sam", "") // allowed
	if wk, _ := NewFleet(g.reg).Get("w-hn"); !wk.PauseWanted {
		t.Fatal("an allowed request must take effect")
	}
}

func TestAuditRecordsMutationsDeniedAndFailedLoginsButNotReads(t *testing.T) {
	g := newRBACRig(t)
	g.do("GET", "/api/workers", "tok-viewer", "")
	g.do("POST", "/api/workers/w-hn/pause", "tok-omar-wrong", "")
	g.do("POST", "/api/workers/w-hn/pause", "tok-viewer", "")
	g.do("POST", "/api/workers/w-hn/pause", "tok-operator", "")
	g.do("POST", "/api/tenants", "tok-admin", `{"name":"secretteam","weight":1}`)
	n, _, err := VerifyAudit(g.path)
	if err != nil || n != 4 {
		t.Fatalf("entries %d err %v, want 4 (read not logged)", n, err)
	}
	raw, _ := os.ReadFile(g.path)
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	var e [4]AuditEntry
	for i := range e {
		json.Unmarshal([]byte(lines[i]), &e[i])
	}
	if e[0].Status != 401 || e[0].Actor != "-" {
		t.Errorf("failed login: %+v", e[0])
	}
	if e[1].Status != 403 || e[1].Actor != "vera" || e[1].Role != "viewer" {
		t.Errorf("denied: %+v", e[1])
	}
	if e[2].Status != 204 || e[2].Actor != "omar" {
		t.Errorf("allowed: %+v", e[2])
	}
	if e[3].Status != 201 || e[3].Actor != "ada" || e[3].Path != "/api/tenants" {
		t.Errorf("create: %+v", e[3])
	}
	if strings.Contains(string(raw), "secretteam") || strings.Contains(string(raw), "aicf_") || strings.Contains(string(raw), "tok-") {
		t.Fatal("the audit log must never contain request bodies, API keys or tokens")
	}
}

func TestAuditChainDetectsEditDeleteAndReorder(t *testing.T) {
	g := newRBACRig(t)
	for i := 0; i < 5; i++ {
		g.do("POST", "/api/workers/w-hn/pause", "tok-operator", "")
	}
	raw, _ := os.ReadFile(g.path)
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) != 5 {
		t.Fatal(len(lines))
	}
	write := func(ls []string) string {
		p := filepath.Join(t.TempDir(), "a.jsonl")
		os.WriteFile(p, []byte(strings.Join(ls, "\n")+"\n"), 0o600)
		return p
	}
	if _, _, err := VerifyAudit(write(lines)); err != nil {
		t.Fatalf("intact log rejected: %v", err)
	}
	edited := append([]string{}, lines...)
	edited[1] = strings.Replace(edited[1], `"actor":"omar"`, `"actor":"ada"`, 1)
	deleted := append(append([]string{}, lines[:2]...), lines[3:]...)
	reordered := append([]string{}, lines...)
	reordered[1], reordered[2] = reordered[2], reordered[1]
	for name, ls := range map[string][]string{"edited": edited, "deleted": deleted, "reordered": reordered} {
		if _, _, err := VerifyAudit(write(ls)); err == nil {
			t.Errorf("%s log was accepted", name)
		}
	}
	// truncating the tail leaves a valid chain: documented limit, caught only by an external copy of the last hash
	if n, _, err := VerifyAudit(write(lines[:3])); err != nil || n != 3 {
		t.Errorf("tail truncation: %d %v", n, err)
	}
}

func TestAuditContinuesAcrossRestartAndRefusesABrokenLog(t *testing.T) {
	g := newRBACRig(t)
	g.do("POST", "/api/workers/w-hn/pause", "tok-operator", "")
	g.audit.Close()
	a2, err := OpenAudit(g.path)
	if err != nil {
		t.Fatal(err)
	}
	a2.Record("x", "admin", "POST", "/api/y", 200, "")
	a2.Close()
	if n, _, err := VerifyAudit(g.path); err != nil || n != 2 {
		t.Fatalf("after restart: %d %v", n, err)
	}
	raw, _ := os.ReadFile(g.path)
	os.WriteFile(g.path, []byte(strings.Replace(string(raw), `"path":"/api/y"`, `"path":"/api/z"`, 1)), 0o600)
	if _, err := OpenAudit(g.path); err == nil {
		t.Fatal("a log that fails verification must not be appended to")
	}
}

func TestRolesFileValidation(t *testing.T) {
	bad := map[string]string{
		"short hash":    `[{"name":"a","role":"admin","token_sha256":"abc"}]`,
		"non-hex":       `[{"name":"a","role":"admin","token_sha256":"` + strings.Repeat("z", 64) + `"}]`,
		"unknown role":  `[{"name":"a","role":"root","token_sha256":"` + HashToken("x") + `"}]`,
		"site on admin": `[{"name":"a","role":"admin","site":"hn","token_sha256":"` + HashToken("x") + `"}]`,
		"not json":      `nope`,
	}
	for name, body := range bad {
		p := filepath.Join(t.TempDir(), "r.json")
		os.WriteFile(p, []byte(body), 0o600)
		if err := NewAuth("").LoadRoles(p); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestNoCredentialsConfiguredMeansOpenAPI(t *testing.T) {
	ts, _ := NewTenantStore("")
	h := NewAPI(NewFleet(NewRegistry(time.Hour)), ts, "")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/workers", nil))
	if rec.Code != 200 {
		t.Fatalf("%d", rec.Code)
	}
}

// An attacker who deletes an entry can renumber or re-hash the entries after it; each check must
// still fail on its own, so these craft logs that satisfy all checks but one.
func TestAuditEachCheckCatchesItsOwnForgery(t *testing.T) {
	mk := func(seq uint64, prev string) AuditEntry {
		e := AuditEntry{Seq: seq, Time: "t", Actor: "a", Role: "admin", Method: "POST", Path: "/api/x", Status: 200, Prev: prev}
		e.Hash = entryHash(e)
		return e
	}
	write := func(es ...AuditEntry) string {
		var sb strings.Builder
		for _, e := range es {
			b, _ := json.Marshal(e)
			sb.Write(b)
			sb.WriteByte('\n')
		}
		p := filepath.Join(t.TempDir(), "a.jsonl")
		os.WriteFile(p, []byte(sb.String()), 0o600)
		return p
	}
	e1 := mk(1, auditGenesis)
	if _, _, err := VerifyAudit(write(e1, mk(2, e1.Hash))); err != nil {
		t.Fatalf("honest log rejected: %v", err)
	}
	if _, _, err := VerifyAudit(write(e1, mk(2, "deadbeef"))); err == nil {
		t.Error("right sequence and valid own hash but wrong previous hash was accepted")
	}
	if _, _, err := VerifyAudit(write(e1, mk(5, e1.Hash))); err == nil {
		t.Error("right previous hash and valid own hash but wrong sequence was accepted")
	}
}

func TestViewerAndOperatorHaveNoWriteBeyondTheirRole(t *testing.T) {
	for _, role := range []string{RoleViewer, RoleOperator} {
		for _, path := range []string{"/api/tenants", "/api/tenants/t1", "/api/tenants/t1/rotate-key", "/api/workers", "/api/workers/w/other"} {
			for _, m := range []string{"POST", "PATCH", "PUT", "DELETE"} {
				if Allowed(Principal{Role: role}, httptest.NewRequest(m, path, nil), func(string) string { return "" }) {
					t.Errorf("%s allowed %s %s", role, m, path)
				}
			}
		}
	}
	if Allowed(Principal{Role: "mystery"}, httptest.NewRequest("GET", "/api/workers", nil), nil) {
		t.Error("an unknown role must get nothing")
	}
}

func TestAgentsProxyIsReadOnlyRoleCheckedAndHidesTheCredential(t *testing.T) {
	var gotPath, gotAuth string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotAuth = r.URL.Path, r.Header.Get("Authorization")
		w.Write([]byte(`{"ok":true}`))
	}))
	defer backend.Close()
	px, err := AgentsProxy(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	auth := NewAuth("")
	auth.byHash[HashToken("v")] = Principal{Name: "vera", Role: RoleViewer}
	auth.byHash[HashToken("a")] = Principal{Name: "ada", Role: RoleAdmin}
	ts, _ := NewTenantStore("")
	h := NewAPIFull(NewFleet(NewRegistry(time.Hour)), ts, auth, nil, px)
	do := func(method, tok string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, "/api/agents/workflows/wf-1", nil)
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec
	}
	if rec := do("GET", ""); rec.Code != 401 || gotPath != "" {
		t.Fatalf("no token: %d, backend saw %q", rec.Code, gotPath)
	}
	if rec := do("GET", "v"); rec.Code != 200 || gotPath != "/workflows/wf-1" {
		t.Fatalf("viewer read: %d path %q", rec.Code, gotPath)
	}
	if gotAuth != "" {
		t.Fatalf("the admin credential reached the backend: %q", gotAuth)
	}
	gotPath = ""
	if rec := do("POST", "v"); rec.Code != 403 || gotPath != "" {
		t.Fatalf("viewer write: %d", rec.Code)
	}
	if rec := do("POST", "a"); rec.Code != 405 || gotPath != "" {
		t.Fatalf("even an admin may only read through the proxy: %d path %q", rec.Code, gotPath)
	}
	backend.Close()
	if rec := do("GET", "v"); rec.Code != 502 {
		t.Fatalf("dead backend: %d", rec.Code)
	}
	for _, bad := range []string{"", "ftp://x", "localhost:1", "http://"} {
		if _, err := AgentsProxy(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestWhoamiTellsTheUIWhatEachRoleMayDoAndAgreesWithTheServer(t *testing.T) {
	g := newRBACRig(t)
	type who struct {
		Name, Role, Site string
		Open             bool
		Can              map[string]bool
	}
	get := func(tok string) (int, who) {
		rec := g.do("GET", "/api/whoami", tok, "")
		var w who
		json.Unmarshal(rec.Body.Bytes(), &w)
		return rec.Code, w
	}
	if code, _ := get(""); code != http.StatusUnauthorized {
		t.Fatalf("whoami without a token: %d", code)
	}
	if code, _ := get("wrong"); code != http.StatusUnauthorized {
		t.Fatalf("whoami with a bad token: %d", code)
	}
	want := map[string]map[string]bool{
		"tok-viewer": {"view": true, "pause": false, "tenants": false}, "tok-operator": {"view": true, "pause": true, "tenants": false},
		"tok-sam": {"view": true, "pause": true, "tenants": false}, "tok-admin": {"view": true, "pause": true, "tenants": true},
	}
	for tok, caps := range want {
		code, w := get(tok)
		if code != 200 {
			t.Fatalf("%s: %d", tok, code)
		}
		for k, v := range caps {
			if w.Can[k] != v {
				t.Errorf("%s: can[%s]=%v want %v", tok, k, w.Can[k], v)
			}
		}
		// the advertised capability must match what the server really does
		if got := g.do("POST", "/api/workers/w-hn/pause", tok, "").Code; (got/100 == 2) != (caps["pause"] && (w.Site == "" || w.Site == "hanoi")) {
			t.Errorf("%s: whoami says pause=%v but POST pause on w-hn answered %d", tok, caps["pause"], got)
		}
		if got := g.do("POST", "/api/tenants", tok, `{"id":"zz","name":"zz"}`).Code; (got/100 == 2) != caps["tenants"] {
			t.Errorf("%s: whoami says tenants=%v but POST /api/tenants answered %d", tok, caps["tenants"], got)
		}
	}
	if _, w := get("tok-sam"); w.Site != "hanoi" {
		t.Errorf("site-restricted operator should report its site, got %q", w.Site)
	}
	if rec := g.do("POST", "/api/whoami", "tok-admin", ""); rec.Code == 200 {
		t.Error("whoami must be GET only")
	}
}

func TestWhoamiInOpenModeReportsNoLogin(t *testing.T) {
	reg := NewRegistry(time.Hour)
	ts, _ := NewTenantStore("")
	h := NewAPIWith(NewFleet(reg), ts, NewAuth(""), nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/whoami", nil))
	var w struct {
		Open bool
		Can  map[string]bool
	}
	json.Unmarshal(rec.Body.Bytes(), &w)
	if rec.Code != 200 || !w.Open || !w.Can["tenants"] {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
}

func TestStudioProxyRolesIdentityHeadersAndBodyCap(t *testing.T) {
	type seen struct {
		path, auth, user, role string
		body                   int
	}
	var mu sync.Mutex
	var last seen
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		last = seen{r.Method + " " + r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("X-Aicf-User"), r.Header.Get("X-Aicf-Role"), len(b)}
		mu.Unlock()
		w.Write([]byte(`{"ok":true}`))
	}))
	defer backend.Close()
	px, err := StudioProxy(backend.URL, 1024)
	if err != nil {
		t.Fatal(err)
	}
	auth := NewAuth("")
	auth.byHash[HashToken("v")] = Principal{Name: "vera", Role: RoleViewer}
	auth.byHash[HashToken("o")] = Principal{Name: "omar", Role: RoleOperator}
	auth.byHash[HashToken("a")] = Principal{Name: "ada", Role: RoleAdmin}
	ts, _ := NewTenantStore("")
	h := NewAPIStudio(NewFleet(NewRegistry(time.Hour)), ts, auth, nil, nil, px)
	do := func(method, tok, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, "/api/studio/v1/tenants/acme/rag/documents/x", strings.NewReader(body))
		req.Header.Set("X-Aicf-User", "mallory") // a client must not be able to name itself
		req.Header.Set("X-Aicf-Role", "admin")
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec
	}
	reset := func() { mu.Lock(); last = seen{}; mu.Unlock() }
	get := func() seen { mu.Lock(); defer mu.Unlock(); return last }
	if rec := do("GET", "", ""); rec.Code != 401 || get().path != "" {
		t.Fatalf("no token: %d", rec.Code)
	}
	cases := []struct {
		tok, method string
		want        int
	}{{"v", "GET", 200}, {"v", "POST", 403}, {"v", "PUT", 403}, {"v", "DELETE", 403},
		{"o", "GET", 200}, {"o", "POST", 200}, {"o", "PUT", 200}, {"o", "DELETE", 403},
		{"a", "GET", 200}, {"a", "POST", 200}, {"a", "DELETE", 200}, {"a", "PATCH", 405}}
	for _, c := range cases {
		reset()
		rec := do(c.method, c.tok, "")
		if rec.Code != c.want {
			t.Errorf("%s %s: got %d want %d", c.tok, c.method, rec.Code, c.want)
		}
		g := get()
		if c.want == 200 {
			if g.path != c.method+" /v1/tenants/acme/rag/documents/x" || g.auth != "" {
				t.Errorf("%s %s: backend saw %q auth %q", c.tok, c.method, g.path, g.auth)
			}
			want := map[string]string{"v": "vera", "o": "omar", "a": "ada"}[c.tok]
			if g.user != want || g.role == "admin" && c.tok != "a" {
				t.Errorf("%s: identity headers were %q/%q, a client-supplied value must be replaced by %q", c.tok, g.user, g.role, want)
			}
		} else if g.path != "" {
			t.Errorf("%s %s reached the backend", c.tok, c.method)
		}
	}
	reset()
	if rec := do("POST", "o", strings.Repeat("x", 4096)); rec.Code == 200 || get().body > 1024 {
		t.Errorf("oversized body passed: %d, backend read %d bytes", rec.Code, get().body)
	}
	for _, bad := range []string{"", "ftp://x", "localhost:1", "http://"} {
		if _, err := StudioProxy(bad, 1); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}
