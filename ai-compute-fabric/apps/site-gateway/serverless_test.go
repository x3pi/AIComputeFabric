package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const slSecret = "test-shared-secret-0123456789"

type slSeen struct {
	mu   sync.Mutex
	last http.Header
	path string
	body string
	n    int
}

func (s *slSeen) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.last, s.path, s.body = r.Header.Clone(), r.URL.Path, string(b)
		s.n++
		s.mu.Unlock()
		w.Write([]byte("ok"))
	})
}

type slRig struct {
	api  http.Handler
	seen *slSeen
	key  string
}

func newSLRig(t *testing.T, enabled bool) *slRig {
	t.Helper()
	seen := &slSeen{}
	be := httptest.NewServer(seen.handler())
	t.Cleanup(be.Close)
	ts, err := NewTenantStore(filepath.Join(t.TempDir(), "tenants.json"))
	if err != nil {
		t.Fatal(err)
	}
	_, key, _ := ts.Create("acme", 1, 0)
	var h http.Handler
	if enabled {
		if h, err = ServerlessProxy(be.URL, slSecret, 1<<10); err != nil {
			t.Fatal(err)
		}
	}
	api := NewAPIServerless(NewFleet(NewRegistry(time.Hour)), ts, NewAuth("adm"), nil, nil, nil, nil, h)
	return &slRig{api: api, seen: seen, key: key}
}

func (r *slRig) do(method, path, bearer, body string, hdr ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	w := httptest.NewRecorder()
	r.api.ServeHTTP(w, req)
	return w
}

func TestServerlessRequiresCredential(t *testing.T) {
	r := newSLRig(t, true)
	for _, tok := range []string{"", "wrong-key", "aicf_nope"} {
		if w := r.do("POST", "/api/serverless/v1/invoke", tok, "{}"); w.Code != http.StatusUnauthorized {
			t.Fatalf("token %q: want 401 got %d", tok, w.Code)
		}
	}
	if r.seen.n != 0 {
		t.Fatalf("an unauthenticated request reached the backend (%d)", r.seen.n)
	}
}

func TestServerlessTenantKeyIsForwardedWithSecretAndIdentity(t *testing.T) {
	r := newSLRig(t, true)
	w := r.do("POST", "/api/serverless/v1/invoke", r.key, `{"x":1}`)
	if w.Code != 200 {
		t.Fatalf("got %d %s", w.Code, w.Body)
	}
	h := r.seen.last
	if r.seen.path != "/v1/invoke" || r.seen.body != `{"x":1}` {
		t.Fatalf("path/body: %q %q", r.seen.path, r.seen.body)
	}
	if h.Get("X-Aicf-Proxy-Secret") != slSecret || h.Get("X-Aicf-Tenant") == "" || h.Get("X-Aicf-Role") != "tenant" {
		t.Fatalf("headers not set by the gateway: %v", h)
	}
	if h.Get("Authorization") != "" {
		t.Fatal("the raw credential was forwarded downstream")
	}
}

func TestServerlessClientCannotForgeTenantOrSecret(t *testing.T) {
	r := newSLRig(t, true)
	good := r.do("POST", "/api/serverless/v1/invoke", r.key, "{}")
	realTenant := r.seen.last.Get("X-Aicf-Tenant")
	if good.Code != 200 || realTenant == "" {
		t.Fatal("setup")
	}
	r.do("POST", "/api/serverless/v1/invoke", r.key, "{}", "X-Aicf-Tenant", "victim", "X-Aicf-Proxy-Secret", "guess", "X-Aicf-Role", "admin", "X-Aicf-User", "root")
	h := r.seen.last
	if h.Get("X-Aicf-Tenant") != realTenant || h.Get("X-Aicf-Proxy-Secret") != slSecret || h.Get("X-Aicf-Role") != "tenant" || h.Get("X-Aicf-User") == "root" {
		t.Fatalf("client-supplied identity headers survived: %v", h)
	}
}

func TestServerlessAdminTokenHasNoTenantHeader(t *testing.T) {
	r := newSLRig(t, true)
	if w := r.do("GET", "/api/serverless/v1/containers", "adm", "", "X-Aicf-Tenant", "victim"); w.Code != 200 {
		t.Fatalf("admin: %d", w.Code)
	}
	if r.seen.last.Get("X-Aicf-Tenant") != "" || r.seen.last.Get("X-Aicf-Role") != RoleAdmin {
		t.Fatalf("admin principal must not carry a tenant: %v", r.seen.last)
	}
}

func TestServerlessDisabledTenantIsRejected(t *testing.T) {
	r := newSLRig(t, true)
	var id string
	{
		w := r.do("GET", "/api/tenants", "adm", "")
		id = between(w.Body.String(), `"id":"`, `"`)
	}
	if w := r.do("PATCH", "/api/tenants/"+id, "adm", `{"disabled":true}`); w.Code != 200 {
		t.Fatalf("disable: %d %s", w.Code, w.Body)
	}
	if w := r.do("POST", "/api/serverless/v1/invoke", r.key, "{}"); w.Code != http.StatusUnauthorized {
		t.Fatalf("disabled tenant key must be 401, got %d", w.Code)
	}
}

func TestServerlessOffByDefaultAnswers503ForAuthenticatedCallers(t *testing.T) {
	r := newSLRig(t, false)
	if w := r.do("POST", "/api/serverless/v1/invoke", r.key, "{}"); w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503 got %d", w.Code)
	}
	if w := r.do("POST", "/api/serverless/v1/invoke", "", "{}"); w.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated must still be 401 first, got %d", w.Code)
	}
}

func TestServerlessBodyLimit(t *testing.T) {
	r := newSLRig(t, true)
	if w := r.do("POST", "/api/serverless/v1/invoke", r.key, strings.Repeat("a", 4096)); w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413 got %d", w.Code)
	}
	if r.seen.n != 0 {
		t.Fatal("oversized body reached the backend")
	}
}

func TestServerlessProxyConfigValidation(t *testing.T) {
	if _, err := ServerlessProxy("ftp://x", slSecret, 1); err == nil {
		t.Fatal("non-http url accepted")
	}
	if _, err := ServerlessProxy("http://127.0.0.1:1", "short", 1); err == nil {
		t.Fatal("short secret accepted")
	}
	p := filepath.Join(t.TempDir(), "s")
	os.WriteFile(p, []byte("tiny\n"), 0o600)
	if _, err := ReadSecretFile(p); err == nil {
		t.Fatal("tiny secret file accepted")
	}
	os.WriteFile(p, []byte(slSecret+"\n"), 0o600)
	if s, err := ReadSecretFile(p); err != nil || s != slSecret {
		t.Fatalf("%q %v", s, err)
	}
}

func between(s, a, b string) string {
	i := strings.Index(s, a)
	if i < 0 {
		return ""
	}
	s = s[i+len(a):]
	return s[:strings.Index(s, b)]
}
