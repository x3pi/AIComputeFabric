package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestUsageCountsSlotsBusyAndRecentUsers(t *testing.T) {
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second, Fair: true}, reg)
	addWorker(reg, "w1", "http://x", "m", 2)
	rt.Refresh()
	now := time.Unix(1_000_000, 0)
	rt.now = func() time.Time { return now }

	u := rt.Usage(5 * time.Minute)
	if u.Slots != 2 || u.Inflight != 0 || u.UsersBusy != 0 || u.UsersRecent != 0 || u.LastRequest != 0 {
		t.Fatalf("idle gateway: %+v", u)
	}

	a, err := rt.Acquire(Req{Model: "m", Tenant: "alice"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := rt.Acquire(Req{Model: "m", Tenant: "bob"}, nil)
	u = rt.Usage(5 * time.Minute)
	if u.Inflight != 2 || u.UsersBusy != 2 || u.UsersRecent != 2 || u.RequestsRecent != 2 {
		t.Fatalf("two running: %+v", u)
	}
	rt.Release(a, "alice", false)
	rt.Release(b, "bob", false)

	now = now.Add(10 * time.Minute) // both fall out of the window, the last-request time stays
	u = rt.Usage(5 * time.Minute)
	if u.Inflight != 0 || u.UsersBusy != 0 || u.UsersRecent != 0 || u.RequestsRecent != 0 || u.LastRequest != 1_000_000 {
		t.Fatalf("after the window: %+v", u)
	}
}

func TestUsageEndpointIsPublicAndNamesNoTenant(t *testing.T) {
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second, Fair: true}, reg)
	addWorker(reg, "w1", "http://x", "m", 2)
	rt.Refresh()
	if r, err := rt.Acquire(Req{Model: "m", Tenant: "secret-tenant-name"}, nil); err == nil {
		defer rt.Release(r, "secret-tenant-name", false)
	}
	h := NewAPIServerless(NewFleet(reg), nil, NewAuth("tok"), nil, nil, nil, nil, nil, rt)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/usage", nil)) // no credentials
	if rec.Code != 200 {
		t.Fatalf("status page data must be public, got %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "secret-tenant-name") || !strings.Contains(rec.Body.String(), `"inflight":1`) {
		t.Fatalf("body: %s", rec.Body.String())
	}
	if rec := httptest.NewRecorder(); true {
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/stats", nil))
		if rec.Code != 401 {
			t.Fatalf("/api/stats must stay protected, got %d", rec.Code)
		}
	}
}
