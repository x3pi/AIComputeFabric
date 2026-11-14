package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestConnectPageWithdrawn(t *testing.T) {
	h := HTTPHandler(NewFleet(NewRegistry(time.Hour)))
	for _, p := range []string{"/connect.html", "/connect.js"} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, p, nil))
		if rr.Code != http.StatusNotFound {
			t.Fatalf("%s: want 404, got %d", p, rr.Code)
		}
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/common.js", nil))
	if rr.Code != http.StatusOK || strings.Contains(rr.Body.String(), "connect.html") {
		t.Fatalf("common.js must be served without the connect menu entry (code %d)", rr.Code)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/status.html", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("other pages must still be served, got %d", rr.Code)
	}
}
