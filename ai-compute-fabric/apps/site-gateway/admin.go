package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
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
	return NewAPIFull(f, ts, auth, audit, nil, routers...)
}

// AgentsProxy forwards GET /api/agents/* to the agent-workflow view service (agents/dag_api.py).
// Only reads are forwarded; roles and audit are applied by the caller because it is mounted
// under /api/ like every other route.
func AgentsProxy(target string) (http.Handler, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("--agents-url must be an http(s) URL, got %q", target)
	}
	rp := &httputil.ReverseProxy{Rewrite: func(pr *httputil.ProxyRequest) {
		pr.SetURL(u)
		pr.Out.URL.Path = "/" + strings.TrimPrefix(pr.In.URL.Path, "/api/agents/")
		pr.Out.URL.RawPath = ""
		pr.Out.Header.Del("Authorization") // the view service has no use for, and must not see, the admin credential
	}}
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "agent view service unavailable", http.StatusBadGateway)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		rp.ServeHTTP(w, r)
	}), nil
}

// NewAPIFull is NewAPIWith plus an optional handler mounted at /api/agents/.
func NewAPIFull(f *Fleet, ts *TenantStore, auth *Auth, audit *AuditLog, agents http.Handler, routers ...*Router) http.Handler {
	return NewAPIStudio(f, ts, auth, audit, agents, nil, routers...)
}

// StudioProxy forwards /api/studio/* to the AI Studio service (studio/, Python). Unlike the agents view it carries
// writes (uploads, job creation), so the body size is capped here, the client's own identity headers are discarded and the
// caller's authenticated name and role are attached for the service to record. Roles and audit are applied by the caller.
func StudioProxy(target string, maxBody int64) (http.Handler, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("--studio-url must be an http(s) URL, got %q", target)
	}
	rp := &httputil.ReverseProxy{Rewrite: func(pr *httputil.ProxyRequest) {
		pr.SetURL(u)
		pr.Out.URL.Path = "/" + strings.TrimPrefix(pr.In.URL.Path, "/api/studio/")
		pr.Out.URL.RawPath = ""
		pr.Out.Header.Del("Authorization") // the service must not see the admin credential
	}}
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "studio service unavailable", http.StatusBadGateway)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodDelete:
		default:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		rp.ServeHTTP(w, r)
	}), nil
}

// NewAPIStudio is NewAPIFull plus an optional handler mounted at /api/studio/.
func NewAPIStudio(f *Fleet, ts *TenantStore, auth *Auth, audit *AuditLog, agents, studio http.Handler, routers ...*Router) http.Handler {
	inner := HTTPHandler(f)
	mux := http.NewServeMux()
	var rt *Router
	if len(routers) > 0 {
		rt = routers[0]
	}
	registerEstimatorAndStatsRoutes(mux, f, ts, rt)
	if agents != nil {
		mux.Handle("/api/agents/", agents)
	}
	if studio != nil {
		mux.Handle("/api/studio/", studio)
	}
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
		if r.URL.Path == "/api/whoami" && r.Method == http.MethodGet {
			who(w, r, auth)
			return
		}
		r.Header.Del("X-Aicf-User") // identity headers are set below from the credential, never taken from the client
		r.Header.Del("X-Aicf-Role")
		if !strings.HasPrefix(r.URL.Path, "/api/") || auth.Open() {
			if auth.Open() {
				r.Header.Set("X-Aicf-User", "open")
				r.Header.Set("X-Aicf-Role", RoleAdmin)
			}
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
		r.Header.Set("X-Aicf-User", p.Name)
		r.Header.Set("X-Aicf-Role", p.Role)
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

// who answers GET /api/whoami so the dashboard can hide what the caller may not do. It is a convenience for the UI
// only: every action is still checked server-side by Allowed, whatever the page shows.
func who(w http.ResponseWriter, r *http.Request, auth *Auth) {
	p := Principal{Name: "open", Role: RoleAdmin}
	open := auth.Open()
	if !open {
		var ok bool
		p, ok = auth.Identify(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if !ok {
			w.Header().Set("WWW-Authenticate", `Bearer realm="aicf-admin"`)
			http.Error(w, "admin token required", http.StatusUnauthorized)
			return
		}
	}
	probe := func(method, path string) bool {
		req, _ := http.NewRequest(method, path, nil)
		return Allowed(p, req, func(string) string { return p.Site })
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"name": p.Name, "role": p.Role, "site": p.Site, "open": open,
		"can": map[string]bool{"view": probe("GET", "/api/stats"), "pause": probe("POST", "/api/workers/x/pause"), "tenants": probe("POST", "/api/tenants"),
			"studio": probe("POST", "/api/studio/x"), "studio_delete": probe("DELETE", "/api/studio/x")},
	})
}
