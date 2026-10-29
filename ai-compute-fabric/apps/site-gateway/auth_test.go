package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestTenantStoreKeysAreHashedPersistedAndRotatable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tenants.json")
	s, _ := NewTenantStore(path)
	v, key, err := s.Create("sales", 2, 3)
	if err != nil || !strings.HasPrefix(key, "aicf_") || v.Weight != 2 {
		t.Fatalf("%v %q %+v", err, key, v)
	}
	raw, _ := os.ReadFile(path)
	if bytes.Contains(raw, []byte(key)) {
		t.Fatal("the plaintext API key must never be written to disk")
	}
	if fi, _ := os.Stat(path); fi.Mode().Perm() != 0o600 {
		t.Fatalf("tenants file must be 0600, got %v", fi.Mode().Perm())
	}
	if _, ok := s.Authenticate(key); !ok {
		t.Fatal("valid key must authenticate")
	}
	if _, ok := s.Authenticate(key + "x"); ok {
		t.Fatal("wrong key must not authenticate")
	}
	if _, ok := s.Authenticate(""); ok {
		t.Fatal("empty key must not authenticate")
	}
	if _, _, err := s.Create("sales", 1, 0); err != ErrDuplicated {
		t.Fatalf("duplicate name: %v", err)
	}
	s2, _ := NewTenantStore(path) // restart
	if _, ok := s2.Authenticate(key); !ok {
		t.Fatal("tenants must survive a restart")
	}
	newKey, _ := s2.RotateKey(v.ID)
	if _, ok := s2.Authenticate(key); ok {
		t.Fatal("old key must stop working after rotation")
	}
	if _, ok := s2.Authenticate(newKey); !ok {
		t.Fatal("new key must work")
	}
	off := true
	s2.Update(v.ID, nil, nil, &off)
	if _, ok := s2.Authenticate(newKey); ok {
		t.Fatal("disabled tenant must not authenticate")
	}
	if w, m := s2.Policy(v.ID); w != 2 || m != 3 {
		t.Fatalf("policy %v %v", w, m)
	}
}

func authRig(t *testing.T) (*httptest.Server, *TenantStore, *upstream, *Router) {
	t.Helper()
	up := newUpstream()
	close(up.release)
	t.Cleanup(up.Close)
	store, _ := NewTenantStore("")
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second, Fair: true, Policy: store.Policy}, reg)
	addWorker(reg, "w1", up.URL, "m", 4)
	rt.Refresh()
	in := NewInference(rt, NewLimiter(0, 0))
	in.Tenants, in.AuthRequired = store, true
	srv := httptest.NewServer(in)
	t.Cleanup(srv.Close)
	return srv, store, up, rt
}

func call(url string, hdr map[string]string) *http.Response {
	req, _ := http.NewRequest("POST", url+"/v1/chat/completions", strings.NewReader(`{"model":"m"}`))
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		panic(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	return resp
}

func TestInferenceRequiresAValidKeyAndIgnoresSpoofedTenantHeader(t *testing.T) {
	srv, store, up, rt := authRig(t)
	_, key, _ := store.Create("sales", 1, 0)
	if r := call(srv.URL, nil); r.StatusCode != 401 || r.Header.Get("WWW-Authenticate") == "" {
		t.Fatalf("no key -> %d", r.StatusCode)
	}
	if r := call(srv.URL, map[string]string{"Authorization": "Bearer nope"}); r.StatusCode != 401 {
		t.Fatalf("bad key -> %d", r.StatusCode)
	}
	if up.hits != 0 {
		t.Fatal("rejected requests must never reach a worker")
	}
	if r := call(srv.URL, map[string]string{"Authorization": "Bearer " + key, "X-Tenant-ID": "someone-else"}); r.StatusCode != 200 {
		t.Fatalf("valid key -> %d", r.StatusCode)
	}
	served := rt.ServedByTenant()
	if served["someone-else"] != 0 || len(served) != 1 {
		t.Fatalf("accounting must use the key's tenant, not the header: %v", served)
	}
	off := true
	id := store.List()[0].ID
	store.Update(id, nil, nil, &off)
	if r := call(srv.URL, map[string]string{"Authorization": "Bearer " + key}); r.StatusCode != 401 {
		t.Fatalf("disabled tenant -> %d", r.StatusCode)
	}
}

func TestAdminAPIGuardAndTenantManagement(t *testing.T) {
	store, _ := NewTenantStore("")
	f := NewFleet(NewRegistry(time.Hour))
	h := NewAPI(f, store, "s3cret")
	do := func(method, path, tok, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec
	}
	for _, p := range []string{"/api/workers", "/api/tenants", "/api/fleet/stream"} {
		if rec := do("GET", p, "", ""); rec.Code != 401 {
			t.Errorf("%s without token -> %d", p, rec.Code)
		}
		if rec := do("GET", p, "wrong", ""); rec.Code != 401 {
			t.Errorf("%s with wrong token -> %d", p, rec.Code)
		}
	}
	if rec := do("GET", "/index.html", "", ""); rec.Code == 401 {
		t.Error("static pages must stay reachable so the token can be typed in")
	}
	rec := do("POST", "/api/tenants", "s3cret", `{"name":"ops","weight":2}`)
	if rec.Code != 201 {
		t.Fatalf("create -> %d %s", rec.Code, rec.Body)
	}
	var created struct {
		Tenant TenantView `json:"tenant"`
		Key    string     `json:"api_key"`
	}
	json.Unmarshal(rec.Body.Bytes(), &created)
	if created.Key == "" || created.Tenant.Weight != 2 {
		t.Fatalf("%+v", created)
	}
	list := do("GET", "/api/tenants", "s3cret", "")
	if strings.Contains(list.Body.String(), created.Key) || strings.Contains(list.Body.String(), "key_hash") {
		t.Fatal("listing must not expose keys or hashes")
	}
	if rec := do("POST", "/api/tenants", "s3cret", `{"name":"ops"}`); rec.Code != 409 {
		t.Fatalf("duplicate -> %d", rec.Code)
	}
	if rec := do("PATCH", "/api/tenants/"+created.Tenant.ID, "s3cret", `{"weight":5,"max_inflight":2}`); rec.Code != 200 {
		t.Fatalf("patch -> %d", rec.Code)
	}
	if w, m := store.Policy(created.Tenant.ID); w != 5 || m != 2 {
		t.Fatalf("policy %v %v", w, m)
	}
	if rec := do("PATCH", "/api/tenants/"+created.Tenant.ID, "s3cret", `{"weight":-1}`); rec.Code != 400 {
		t.Fatalf("negative weight -> %d", rec.Code)
	}
	if rec := do("POST", "/api/tenants/"+created.Tenant.ID+"/rotate-key", "s3cret", ""); rec.Code != 200 {
		t.Fatalf("rotate -> %d", rec.Code)
	}
}

func TestDeadlineRejectsRequestsThatCannotBeServedInTime(t *testing.T) {
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 20, MaxWait: 30 * time.Second}, reg)
	addWorker(reg, "w1", "http://x", "m", 2)
	rt.Refresh()
	// teach the router that a request holds a slot for ~4 s
	a, _ := rt.Acquire(Req{Model: "m"}, nil)
	rt.Release(a, "", false, 4*time.Second)
	// occupy both slots and queue 4 requests behind them
	r1, _ := rt.Acquire(Req{Model: "m"}, nil)
	r2, _ := rt.Acquire(Req{Model: "m"}, nil)
	defer rt.Release(r1, "", false)
	defer rt.Release(r2, "", false)
	order := make(chan Req, 8)
	for i := 0; i < 4; i++ {
		enqueue(t, rt, order, Req{Model: "m"})
	}
	// estimate: (4 ahead + 1) / 2 slots * 4 s = 10 s
	_, err := rt.Acquire(Req{Model: "m", Deadline: 2 * time.Second}, nil)
	if err != ErrWouldMiss {
		t.Fatalf("a 2 s deadline against a ~10 s queue must be refused immediately, got %v", err)
	}
	if est := rt.LastEstimate(); est < 9*time.Second || est > 11*time.Second {
		t.Fatalf("estimate %v, want ~10 s", est)
	}
	cancel := make(chan struct{})
	time.AfterFunc(80*time.Millisecond, func() { close(cancel) })
	if _, err := rt.Acquire(Req{Model: "m", Deadline: 20 * time.Second}, cancel); err != ErrWaitTimeout {
		t.Fatalf("a generous deadline must be allowed to queue, got %v", err)
	}
}

func TestDeadlineHeaderGives429WithRetryAfter(t *testing.T) {
	srv, store, _, rt := authRig(t)
	_, key, _ := store.Create("sales", 1, 0)
	rt.mu.Lock()
	rt.svcEWMA = 60 // pretend every request takes a minute
	rt.mu.Unlock()
	// fill the 4 slots so the next request must queue
	var held []Route
	for i := 0; i < 4; i++ {
		r, _ := rt.Acquire(Req{Model: "m", Tenant: "x"}, nil)
		held = append(held, r)
	}
	r := call(srv.URL, map[string]string{"Authorization": "Bearer " + key, "X-Deadline-Ms": "1000"})
	if r.StatusCode != 429 || r.Header.Get("Retry-After") == "" {
		t.Fatalf("status %d retry-after %q", r.StatusCode, r.Header.Get("Retry-After"))
	}
	for _, h := range held {
		rt.Release(h, "x", false)
	}
}

func TestModelsListNeedsAKeyAndListsRunningModels(t *testing.T) {
	srv, store, up, _ := authRig(t)
	_, key, _ := store.Create("ide", 1, 0)
	get := func(hdr map[string]string) *http.Response {
		req, _ := http.NewRequest("GET", srv.URL+"/v1/models", nil)
		for k, v := range hdr {
			req.Header.Set(k, v)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	if r := get(nil); r.StatusCode != 401 {
		t.Fatalf("no key -> %d", r.StatusCode)
	}
	r := get(map[string]string{"Authorization": "Bearer " + key})
	var body struct {
		Object string `json:"object"`
		Data   []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || r.StatusCode != 200 || body.Object != "list" || len(body.Data) != 1 || body.Data[0].ID != "m" {
		t.Fatalf("models -> %d %+v %v", r.StatusCode, body, err)
	}
	if up.hits != 0 {
		t.Fatal("listing models must not touch a worker")
	}
	if r, _ := http.Get(srv.URL + "/v1/chat/completions"); r.StatusCode != 405 {
		t.Fatalf("GET on other /v1 paths must stay 405, got %d", r.StatusCode)
	}
}

func TestInferenceRejectsUnsupportedV1Subpaths(t *testing.T) {
	srv, store, up, _ := authRig(t)
	_, key, _ := store.Create("ide", 1, 0)
	post := func(path string) int {
		req, _ := http.NewRequest("POST", srv.URL+path, strings.NewReader(`{"model":"m"}`))
		req.Header.Set("Authorization", "Bearer "+key)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		return resp.StatusCode
	}
	for _, blocked := range []string{"/v1/tokenize", "/v1/props", "/v1/slots"} {
		if code := post(blocked); code != http.StatusNotFound {
			t.Fatalf("POST %s -> %d, want 404", blocked, code)
		}
	}
	if up.hits != 0 {
		t.Fatalf("unsupported /v1 subpaths must not reach upstream worker, got %d hits", up.hits)
	}
	for _, allowed := range []string{"/v1/chat/completions", "/v1/completions", "/v1/embeddings"} {
		if code := post(allowed); code != http.StatusOK {
			t.Fatalf("POST %s -> %d, want 200", allowed, code)
		}
	}
}
