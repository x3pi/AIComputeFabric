package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

func TestFoundry3DDisabledReturns503(t *testing.T) {
	f := NewFleet(NewRegistry(0))
	ts, err := NewTenantStore("")
	if err != nil {
		t.Fatal(err)
	}
	_, key, err := ts.Create("t-alpha", 1, 2)
	if err != nil {
		t.Fatal(err)
	}
	h := NewAPIServerless(f, ts, NewAuth("adm-secret-token-1234"), nil, nil, nil, nil, nil)
	req := httptest.NewRequest(http.MethodGet, "/api/foundry3d/v1/status", nil)
	req.Header.Set("Authorization", "Bearer "+key)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 when disabled by default, got %d", rec.Code)
	}
}

func TestFoundry3DProxyInjectsSecretAndStripsSpoofedHeaders(t *testing.T) {
	const proxySecret = "foundry3d-shared-secret-123456"
	var gotSecret, gotTenant, gotAuth string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotSecret = r.Header.Get("X-Aicf-Proxy-Secret")
		gotTenant = r.Header.Get("X-Aicf-Tenant")
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewEncoder(w).Encode(map[string]any{"service": "foundry3d", "path": r.URL.Path})
	}))
	defer backend.Close()

	f := NewFleet(NewRegistry(0))
	ts, err := NewTenantStore("")
	if err != nil {
		t.Fatal(err)
	}
	ten, key, err := ts.Create("studio-a", 1, 2)
	if err != nil {
		t.Fatal(err)
	}
	proxy, err := Foundry3DProxy(backend.URL, proxySecret, MaxFoundry3DBody, 2)
	if err != nil {
		t.Fatal(err)
	}
	h := NewAPIFoundry3D(f, ts, NewAuth("adm-secret-token-1234"), nil, nil, nil, nil, nil, proxy)

	req := httptest.NewRequest(http.MethodGet, "/api/foundry3d/v1/status", nil)
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("X-Aicf-Tenant", "evil-spoofed-tenant")
	req.Header.Set("X-Aicf-Proxy-Secret", "evil-spoofed-secret")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d (%s)", rec.Code, rec.Body.String())
	}
	if gotSecret != proxySecret {
		t.Fatalf("expected injected secret %q, got %q", proxySecret, gotSecret)
	}
	if gotTenant != ten.ID {
		t.Fatalf("expected authenticated tenant %q, got %q", ten.ID, gotTenant)
	}
	if gotAuth != "" {
		t.Fatalf("Authorization header must be stripped, got %q", gotAuth)
	}
}

func TestFoundry3DProxyPerTenantInflightCap(t *testing.T) {
	release := make(chan struct{})
	entered := make(chan struct{})
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		w.WriteHeader(http.StatusOK)
	}))
	defer backend.Close()

	proxy, err := Foundry3DProxy(backend.URL, "foundry3d-shared-secret-123456", MaxFoundry3DBody, 1)
	if err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		r1 := httptest.NewRequest(http.MethodPost, "/api/foundry3d/v1/jobs", nil)
		r1.Header.Set("X-Aicf-Tenant", "tenant-cap")
		w1 := httptest.NewRecorder()
		proxy.ServeHTTP(w1, r1)
		if w1.Code != http.StatusOK {
			t.Errorf("first request expected 200, got %d", w1.Code)
		}
	}()
	<-entered

	r2 := httptest.NewRequest(http.MethodPost, "/api/foundry3d/v1/jobs", nil)
	r2.Header.Set("X-Aicf-Tenant", "tenant-cap")
	w2 := httptest.NewRecorder()
	proxy.ServeHTTP(w2, r2)
	if w2.Code != http.StatusTooManyRequests {
		close(release)
		wg.Wait()
		t.Fatalf("expected 429 on 2nd concurrent request for same tenant, got %d", w2.Code)
	}

	close(release)
	wg.Wait()
}
