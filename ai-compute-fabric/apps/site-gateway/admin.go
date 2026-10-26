package main

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"
)

// NewAPI wraps the fleet API with admin authentication and adds tenant management.
// Everything under /api/ needs "Authorization: Bearer <admin token>" when a token is
// configured; the static pages stay public (they hold no data until the token is entered).
func NewAPI(f *Fleet, ts *TenantStore, adminToken string, routers ...*Router) http.Handler {
	return NewAPIWith(f, ts, NewAuth(adminToken), nil, routers...)
}

// statusWriter remembers the status code so the audit entry can record the outcome.
type statusWriter struct {
	http.ResponseWriter
	code int
}

func (s *statusWriter) WriteHeader(c int) { s.code = c; s.ResponseWriter.WriteHeader(c) }

// NewAPIWith is NewAPI with role-based access and an optional audit log. With no credential
// configured the API is open (loopback development only; main refuses that beyond loopback).
func NewAPIWith(f *Fleet, ts *TenantStore, auth *Auth, audit *AuditLog, routers ...*Router) http.Handler {
	inner := HTTPHandler(f)
	mux := http.NewServeMux()
	var rt *Router
	if len(routers) > 0 {
		rt = routers[0]
	}
	registerEstimatorAndStatsRoutes(mux, f, ts, rt)
	mux.HandleFunc("/api/tenants", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, http.StatusOK, ts.List())
		case http.MethodPost:
			var in struct {
				Name        string  `json:"name"`
				Weight      float64 `json:"weight"`
				MaxInflight int     `json:"max_inflight"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
			t, key, err := ts.Create(strings.TrimSpace(in.Name), in.Weight, in.MaxInflight)
			switch {
			case errors.Is(err, ErrDuplicated):
				http.Error(w, err.Error(), http.StatusConflict)
			case err != nil:
				http.Error(w, err.Error(), http.StatusBadRequest)
			default:
				writeJSON(w, http.StatusCreated, map[string]any{"tenant": t, "api_key": key, "note": "shown once; store it now"})
			}
		default:
			http.Error(w, "GET or POST", http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/api/tenants/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/tenants/"), "/")
		id := parts[0]
		switch {
		case len(parts) == 2 && parts[1] == "rotate-key" && r.Method == http.MethodPost:
			key, err := ts.RotateKey(id)
			if err != nil {
				http.Error(w, err.Error(), http.StatusNotFound)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"api_key": key, "note": "the previous key no longer works"})
		case len(parts) == 1 && r.Method == http.MethodPatch:
			var in struct {
				Weight      *float64 `json:"weight"`
				MaxInflight *int     `json:"max_inflight"`
				Disabled    *bool    `json:"disabled"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
			t, err := ts.Update(id, in.Weight, in.MaxInflight, in.Disabled)
			switch {
			case errors.Is(err, ErrNoTenant):
				http.Error(w, err.Error(), http.StatusNotFound)
			case err != nil:
				http.Error(w, err.Error(), http.StatusBadRequest)
			default:
				writeJSON(w, http.StatusOK, t)
			}
		default:
			http.NotFound(w, r)
		}
	})
	mux.Handle("/", inner)
	siteOf := f.reg.SiteOf
	note := func(p Principal, r *http.Request, code int) {
		if audit != nil {
			if err := audit.Record(p.Name, p.Role, r.Method, r.URL.Path, code, r.RemoteAddr); err != nil {
				log.Printf("audit: %v", err)
			}
		}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/api/") || auth.Open() {
			mux.ServeHTTP(w, r)
			return
		}
		p, ok := auth.Identify(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if !ok {
			w.Header().Set("WWW-Authenticate", `Bearer realm="aicf-admin"`)
			http.Error(w, "admin token required", http.StatusUnauthorized)
			note(Principal{Name: "-", Role: "-"}, r, http.StatusUnauthorized)
			return
		}
		if !Allowed(p, r, siteOf) {
			http.Error(w, "role "+p.Role+" may not "+r.Method+" "+r.URL.Path, http.StatusForbidden)
			note(p, r, http.StatusForbidden)
			return
		}
		if r.Method == http.MethodGet || r.Method == http.MethodHead {
			mux.ServeHTTP(w, r)
			return
		}
		sw := &statusWriter{ResponseWriter: w, code: http.StatusOK}
		mux.ServeHTTP(sw, r)
		note(p, r, sw.code)
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}
