package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// An admin can copy a tenant's key again, also after a restart; the file on disk never holds
// the key in the clear, and old files without a sealed key degrade to "rotate to get one".
func TestAdminCanCopyKeyAgainAndItIsSealedAtRest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tenants.json")
	s, _ := NewTenantStore(path)
	tv, key, _ := s.Create("sales", 1, 0)
	got, err := s.Reveal(tv.ID)
	if err != nil || got != key {
		t.Fatalf("reveal = %q, %v; want the original key", got, err)
	}
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), key) {
		t.Fatal("tenants.json contains the API key in clear text")
	}
	s2, _ := NewTenantStore(path) // restart
	if got, err := s2.Reveal(tv.ID); err != nil || got != key {
		t.Fatalf("after restart reveal = %q, %v", got, err)
	}
	if _, ok := s2.Authenticate(key); !ok {
		t.Fatal("key no longer authenticates after restart")
	}
	nk, _ := s2.RotateKey(tv.ID)
	if got, _ := s2.Reveal(tv.ID); got != nk || got == key {
		t.Fatal("reveal must return the rotated key, not the old one")
	}
	// A tenants.json written before this feature has no sealed key.
	s2.byID[tv.ID].KeyEnc = ""
	if _, err := s2.Reveal(tv.ID); err != ErrNoStoredKey {
		t.Fatalf("legacy tenant: err = %v, want ErrNoStoredKey", err)
	}
	if _, err := s2.Reveal("nope"); err != ErrNoTenant {
		t.Fatalf("unknown tenant: err = %v", err)
	}
	// The secret file alone is useless without tenants.json and vice versa; a wrong secret fails closed.
	os.WriteFile(path+".secret", make([]byte, 32), 0o600)
	s3, _ := NewTenantStore(path)
	if _, err := s3.Reveal(tv.ID); err == nil {
		t.Fatal("a different secret must not decrypt")
	}
}

func TestKeyEndpointNeedsAdminToken(t *testing.T) {
	store, _ := NewTenantStore("")
	h := NewAPI(NewFleet(NewRegistry(time.Hour)), store, "s3cret")
	tv, key, _ := store.Create("ops", 1, 0)
	req := httptest.NewRequest("GET", "/api/tenants/"+tv.ID+"/key", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 401 || strings.Contains(rec.Body.String(), key) {
		t.Fatalf("without token: %d %q", rec.Code, rec.Body.String())
	}
	req.Header.Set("Authorization", "Bearer s3cret")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]string
	json.Unmarshal(rec.Body.Bytes(), &out)
	if rec.Code != 200 || out["api_key"] != key || rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("with token: %d %v %v", rec.Code, out, rec.Header())
	}
	var list []TenantView
	rec = httptest.NewRecorder()
	r2 := httptest.NewRequest("GET", "/api/tenants", nil)
	r2.Header.Set("Authorization", "Bearer s3cret")
	h.ServeHTTP(rec, r2)
	json.Unmarshal(rec.Body.Bytes(), &list)
	if strings.Contains(rec.Body.String(), key) || len(list) != 1 || !list[0].KeyCopyable {
		t.Fatalf("list must flag key_copyable and never include the key: %s", rec.Body.String())
	}
}
