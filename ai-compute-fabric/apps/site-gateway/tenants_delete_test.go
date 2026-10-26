package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestTenantDeleteRevokesKeyPersistsAndRollsBack(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tenants.json")
	ts, err := NewTenantStore(path)
	if err != nil {
		t.Fatal(err)
	}
	a, keyA, _ := ts.Create("alpha", 1, 0)
	b, keyB, _ := ts.Create("beta", 1, 0)
	if _, ok := ts.Authenticate(keyA); !ok {
		t.Fatal("setup: key must work before deletion")
	}
	if err := ts.Delete(a.ID); err != nil {
		t.Fatal(err)
	}
	if _, ok := ts.Authenticate(keyA); ok {
		t.Fatal("the deleted tenant's API key still authenticates")
	}
	if _, ok := ts.Authenticate(keyB); !ok {
		t.Fatal("deleting one tenant must not affect another")
	}
	if l := ts.List(); len(l) != 1 || l[0].ID != b.ID {
		t.Fatalf("list after delete: %+v", l)
	}
	if w, _ := ts.Policy(a.ID); w != 1 {
		t.Fatal("a deleted tenant falls back to the default policy, not a stale one")
	}
	if err := ts.Delete(a.ID); err != ErrNoTenant {
		t.Fatalf("second delete must be ErrNoTenant, got %v", err)
	}
	re, err := NewTenantStore(path) // a restart must not bring it back
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := re.Authenticate(keyA); ok || len(re.List()) != 1 {
		t.Fatal("deleted tenant came back after reload")
	}
	// a failing write must leave the tenant in place (disk and memory agree)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path+".tmp", 0o700); err != nil { // the temp file cannot be created: the save fails
		t.Fatal(err)
	}
	if err := re.Delete(b.ID); err == nil {
		t.Fatal("expected the save error to be returned")
	}
	if _, ok := re.Authenticate(keyB); !ok || len(re.List()) != 1 {
		t.Fatal("tenant must still exist after a failed delete")
	}
}

func TestTenantDeleteEndpointRolesAndAudit(t *testing.T) {
	ts, _ := NewTenantStore("")
	v, key, _ := ts.Create("gamma", 1, 0)
	h := NewAPIFull(NewFleet(NewRegistry(time.Hour)), ts, NewAuth("adm"), nil, nil)
	do := func(tok, method, path string) int {
		r := httptest.NewRequest(method, path, nil)
		if tok != "" {
			r.Header.Set("Authorization", "Bearer "+tok)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w.Code
	}
	if c := do("", http.MethodDelete, "/api/tenants/"+v.ID); c != http.StatusUnauthorized {
		t.Fatalf("no token: %d", c)
	}
	if c := do(key, http.MethodDelete, "/api/tenants/"+v.ID); c != http.StatusUnauthorized && c != http.StatusForbidden {
		t.Fatalf("a tenant's own API key must not delete tenants: %d", c)
	}
	if c := do("adm", http.MethodDelete, "/api/tenants/t_nope"); c != http.StatusNotFound {
		t.Fatalf("unknown id: %d", c)
	}
	if c := do("adm", http.MethodDelete, "/api/tenants/"+v.ID); c != http.StatusOK {
		t.Fatalf("admin delete: %d", c)
	}
	if len(ts.List()) != 0 {
		t.Fatal("tenant still listed")
	}
}

func TestOnlyAdminMayDeleteTenants(t *testing.T) {
	r := httptest.NewRequest(http.MethodDelete, "/api/tenants/t_x", nil)
	for role, want := range map[string]bool{RoleViewer: false, RoleOperator: false, RoleAdmin: true} {
		if got := Allowed(Principal{Name: "x", Role: role}, r, func(string) string { return "" }); got != want {
			t.Errorf("role %s: allowed=%v want %v", role, got, want)
		}
	}
}
