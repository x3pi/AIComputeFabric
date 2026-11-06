package main

import (
	"context"
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

type discardWriter struct{ h http.Header }

func (d *discardWriter) Header() http.Header {
	if d.h == nil {
		d.h = make(http.Header)
	}
	return d.h
}
func (d *discardWriter) Write(b []byte) (int, error) { return len(b), nil }
func (d *discardWriter) WriteHeader(int)             {}

func provisionServerlessPolicy(mux http.Handler, tid string, pending bool) {
	body := `{"serverless_enabled":true,"gpu_allowed":true,"max_vram_mb":8192,"max_concurrency":8}`
	if pending {
		body = `{"serverless_enabled":false,"gpu_allowed":false,"max_vram_mb":0,"max_concurrency":1}`
	}
	req, err := http.NewRequest(http.MethodPut, "/api/serverless/v1/policies/"+tid, strings.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Aicf-User", "cloud-iam")
	req.Header.Set("X-Aicf-Role", RoleAdmin)
	mux.ServeHTTP(&discardWriter{}, req)
}

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
	return NewAPIBestOfN(f, ts, auth, audit, agents, studio, nil, routers...)
}

// NewAPIBestOfN is NewAPIStudio plus an optional handler mounted at /api/bestofn/.
func NewAPIBestOfN(f *Fleet, ts *TenantStore, auth *Auth, audit *AuditLog, agents, studio, bestofn http.Handler, routers ...*Router) http.Handler {
	return NewAPIServerless(f, ts, auth, audit, agents, studio, bestofn, nil, routers...)
}

// NewAPIServerless is NewAPIBestOfN plus an optional handler mounted at /api/serverless/ (nil = answers 503: disabled).
func NewAPIServerless(f *Fleet, ts *TenantStore, auth *Auth, audit *AuditLog, agents, studio, bestofn, serverless http.Handler, routers ...*Router) http.Handler {
	inner := HTTPHandler(f)
	mux := http.NewServeMux()
	var rt *Router
	if len(routers) > 0 {
		rt = routers[0]
	}
	registerEstimatorAndStatsRoutes(mux, f, ts, rt)
	var topo *TopologyCoordinator
	if f != nil {
		topo = NewTopologyCoordinator(f.reg, audit)
		if rt != nil {
			rt.IsBusy = topo.IsWorkerBusy
		}
		if DefaultTopologyFile != "" {
			go topo.RunReconciler(nil)
		}
	}
	registerComputeAgentsRoutes(mux, f, topo, rt)
	mux.HandleFunc("/api/cluster/topology", func(w http.ResponseWriter, r *http.Request) {
		if topo == nil {
			http.Error(w, "topology coordinator not initialized", http.StatusServiceUnavailable)
			return
		}
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, http.StatusOK, topo.GetStatus())
		case http.MethodPost:
			var req TopologySwitchRequest
			if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
			caller := Principal{
				Name: r.Header.Get("X-Aicf-User"),
				Role: r.Header.Get("X-Aicf-Role"),
			}
			// a switch must finish (or roll back) even if the browser disconnects half-way
			res, err := topo.SwitchTopology(context.WithoutCancel(r.Context()), req, caller, r.RemoteAddr)
			if err != nil {
				code := http.StatusInternalServerError
				var reqErr *topologyRequestError
				switch {
				case errors.As(err, &reqErr):
					code = http.StatusBadRequest
				case errors.Is(err, ErrTopologyBusy):
					code = http.StatusConflict
				}
				writeJSON(w, code, map[string]any{"error": err.Error(), "topology": topo.GetStatus()})
				return
			}
			writeJSON(w, http.StatusOK, res)
		default:
			http.Error(w, "GET or POST", http.StatusMethodNotAllowed)
		}
	})
	if agents != nil {
		mux.Handle("/api/agents/", agents)
	}
	if studio != nil {
		mux.Handle("/api/studio/", studio)
	}
	if bestofn != nil {
		mux.Handle("/api/bestofn/", bestofn)
	}
	if serverless == nil {
		serverless = serverlessDisabled
	}
	mux.Handle(serverlessPrefix, serverless)
	mux.HandleFunc("/api/tenants", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, http.StatusOK, ts.List())
		case http.MethodPost:
			var in struct {
				Name        string  `json:"name"`
				Email       string  `json:"email"`
				Password    string  `json:"password"`
				Weight      float64 `json:"weight"`
				MaxInflight int     `json:"max_inflight"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
			if in.Password != "" {
				if err := ValidatePasswordStrength(in.Password); err != nil {
					http.Error(w, err.Error(), http.StatusBadRequest)
					return
				}
			}
			t, key, err := ts.Create(strings.TrimSpace(in.Name), in.Weight, in.MaxInflight)
			switch {
			case errors.Is(err, ErrDuplicated):
				http.Error(w, err.Error(), http.StatusConflict)
			case err != nil:
				http.Error(w, err.Error(), http.StatusBadRequest)
			default:
				if strings.TrimSpace(in.Email) != "" || in.Password != "" {
					if updated, cerr := ts.SetCredentials(t.ID, in.Email, in.Password); cerr == nil {
						t = updated
					} else {
						_ = ts.Delete(t.ID)
						http.Error(w, cerr.Error(), http.StatusBadRequest)
						return
					}
					provisionServerlessPolicy(mux, t.ID, false)
				}
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
				Email       *string  `json:"email"`
				Password    *string  `json:"password"`
				Weight      *float64 `json:"weight"`
				MaxInflight *int     `json:"max_inflight"`
				Disabled    *bool    `json:"disabled"`
				Pending     *bool    `json:"pending"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
			t, err := ts.UpdateWithPending(id, in.Weight, in.MaxInflight, in.Disabled, in.Pending)
			switch {
			case errors.Is(err, ErrNoTenant):
				http.Error(w, err.Error(), http.StatusNotFound)
			case err != nil:
				http.Error(w, err.Error(), http.StatusBadRequest)
			default:
				if (in.Email != nil && strings.TrimSpace(*in.Email) != "") || (in.Password != nil && *in.Password != "") {
					em := ""
					if in.Email != nil {
						em = *in.Email
					}
					pw := ""
					if in.Password != nil {
						pw = *in.Password
					}
					updated, cerr := ts.SetCredentials(id, em, pw)
					if cerr != nil {
						http.Error(w, cerr.Error(), http.StatusBadRequest)
						return
					}
					t = updated
				}
				if in.Pending != nil {
					provisionServerlessPolicy(mux, id, *in.Pending)
				}
				writeJSON(w, http.StatusOK, t)
			}
		case len(parts) == 1 && r.Method == http.MethodDelete:
			switch err := ts.Delete(id); {
			case errors.Is(err, ErrNoTenant):
				http.Error(w, err.Error(), http.StatusNotFound)
			case err != nil:
				http.Error(w, err.Error(), http.StatusInternalServerError)
			default:
				writeJSON(w, http.StatusOK, map[string]any{"deleted": id, "note": "the API key stopped working; the tenant's Studio data (documents, prompts, jobs) is kept until it is removed there"})
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
	return withSecurityHeaders(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "service": "site-gateway"})
			return
		}
		if r.URL.Path == "/readyz" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			workers := 0
			if f != nil && f.reg != nil {
				ws, _ := f.Page(0, 100, "running")
				workers = len(ws)
			}
			writeJSON(w, http.StatusOK, map[string]any{"status": "ready", "service": "site-gateway", "running_workers": workers})
			return
		}
		if r.URL.Path == "/api/whoami" && r.Method == http.MethodGet {
			who(w, r, auth, ts)
			return
		}
		if r.URL.Path == "/api/auth/login" && r.Method == http.MethodPost {
			var in struct {
				Identifier string `json:"identifier"`
				Username   string `json:"username"`
				Email      string `json:"email"`
				Password   string `json:"password"`
				APIKey     string `json:"api_key"`
				Token      string `json:"token"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "bad_request", "message": "Dữ liệu JSON không hợp lệ"}})
				return
			}
			ident := strings.TrimSpace(in.Identifier)
			if ident == "" {
				ident = strings.TrimSpace(in.Username)
			}
			if ident == "" {
				ident = strings.TrimSpace(in.Email)
			}
			rawTok := strings.TrimSpace(in.APIKey)
			if rawTok == "" {
				rawTok = strings.TrimSpace(in.Token)
			}
			if rawTok != "" {
				if auth != nil {
					if p, ok := auth.Identify(rawTok); ok {
						note(Principal{Name: "login-token:" + p.Name, Role: p.Role}, r, http.StatusOK)
						writeJSON(w, http.StatusOK, map[string]any{
							"authenticated": true,
							"role":          p.Role,
							"name":          p.Name,
							"email":         p.Email,
							"site":          p.Site,
							"token":         rawTok,
							"session_token": rawTok,
						})
						return
					}
				}
				if ts != nil {
					if t, ok := ts.Authenticate(rawTok); ok {
						note(Principal{Name: "login-key:" + t.Name, Role: "tenant"}, r, http.StatusOK)
						writeJSON(w, http.StatusOK, map[string]any{
							"authenticated": true,
							"role":          "tenant",
							"name":          t.Name,
							"email":         t.Email,
							"tenant":        t,
							"token":         rawTok,
							"session_token": rawTok,
							"api_key":       rawTok,
						})
						return
					}
				}
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "invalid_key", "message": "Khóa API hoặc Token không hợp lệ"}})
				return
			}
			if auth != nil {
				p, sessTok, err := auth.LoginUser(ident, in.Password, r.RemoteAddr)
				if errors.Is(err, ErrAccountLocked) {
					w.Header().Set("Retry-After", "30")
					note(Principal{Name: "login-locked:" + ident, Role: "-"}, r, http.StatusTooManyRequests)
					writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": map[string]any{"code": "account_locked", "message": "Tài khoản hoặc IP tạm thời bị khóa do nhập sai nhiều lần. Vui lòng thử lại sau."}})
					return
				}
				if err == nil {
					note(Principal{Name: "login:" + p.Name, Role: p.Role}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{
						"authenticated": true,
						"role":          p.Role,
						"name":          p.Name,
						"email":         p.Email,
						"site":          p.Site,
						"token":         sessTok,
						"session_token": sessTok,
					})
					return
				}
			}
			if ts != nil {
				t, sessTok, err := ts.LoginAccountFrom(ident, in.Password, r.RemoteAddr)
				if errors.Is(err, ErrAccountLocked) {
					w.Header().Set("Retry-After", "30")
					note(Principal{Name: "login-locked:" + ident, Role: "-"}, r, http.StatusTooManyRequests)
					writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": map[string]any{"code": "account_locked", "message": "Tài khoản hoặc IP tạm thời bị khóa do nhập sai nhiều lần. Vui lòng thử lại sau."}})
					return
				}
				if err == nil {
					note(Principal{Name: "login:" + t.Name, Role: "tenant"}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{
						"authenticated": true,
						"role":          "tenant",
						"name":          t.Name,
						"email":         t.Email,
						"tenant":        t,
						"token":         sessTok,
						"session_token": sessTok,
						"api_key":       sessTok,
					})
					return
				}
			}
			note(Principal{Name: "login-failed:" + ident, Role: "-"}, r, http.StatusUnauthorized)
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "invalid_credentials", "message": "Tên tài khoản/Email hoặc mật khẩu không đúng"}})
			return
		}
		if r.URL.Path == "/api/auth/logout" && r.Method == http.MethodPost {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			var in struct {
				SessionToken string `json:"session_token"`
				All          bool   `json:"all"`
			}
			_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<12)).Decode(&in)
			if bearerTok == "" {
				bearerTok = strings.TrimSpace(in.SessionToken)
			}
			revoked := false
			if auth != nil && bearerTok != "" {
				if auth.LogoutSession(bearerTok) {
					revoked = true
				}
			}
			if ts != nil && bearerTok != "" {
				if in.All {
					if t, ok := ts.Authenticate(bearerTok); ok {
						if ts.RevokeAllSessions(t.ID) > 0 {
							revoked = true
						}
					}
				} else if ts.LogoutSession(bearerTok) {
					revoked = true
				}
			}
			note(Principal{Name: "logout", Role: "-"}, r, http.StatusOK)
			writeJSON(w, http.StatusOK, map[string]any{"logged_out": true, "session_revoked": revoked})
			return
		}
		if r.URL.Path == "/api/auth/change-password" && r.Method == http.MethodPost {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			var in struct {
				CurrentPassword string `json:"current_password"`
				NewPassword     string `json:"new_password"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "bad_request", "message": "Dữ liệu JSON không hợp lệ"}})
				return
			}
			if auth != nil {
				if p, ok := auth.Identify(bearerTok); ok {
					targetUser := p.Name
					if targetUser == "admin-token" {
						targetUser = "admin"
					}
					if err := auth.ChangeUserPassword(targetUser, in.CurrentPassword, in.NewPassword); err != nil {
						code := http.StatusBadRequest
						errCode := "weak_password"
						if errors.Is(err, ErrBadCreds) {
							code = http.StatusUnauthorized
							errCode = "invalid_credentials"
						}
						writeJSON(w, code, map[string]any{"error": map[string]any{"code": errCode, "message": err.Error()}})
						return
					}
					_, newSess, _ := auth.LoginUser(targetUser, in.NewPassword, r.RemoteAddr)
					note(Principal{Name: "change-password:" + targetUser, Role: p.Role}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{"name": targetUser, "role": p.Role, "password_changed": true, "sessions_revoked": true, "session_token": newSess})
					return
				}
			}
			if ts != nil {
				if t, ok := ts.Authenticate(bearerTok); ok {
					if err := ts.VerifyAndChangePassword(t.ID, in.CurrentPassword, in.NewPassword); err != nil {
						code := http.StatusBadRequest
						errCode := "weak_password"
						if errors.Is(err, ErrBadCreds) {
							code = http.StatusUnauthorized
							errCode = "invalid_credentials"
						}
						writeJSON(w, code, map[string]any{"error": map[string]any{"code": errCode, "message": err.Error()}})
						return
					}
					_, newSess, _ := ts.LoginAccountFrom(t.ID, in.NewPassword, r.RemoteAddr)
					note(Principal{Name: "change-password:" + t.Name, Role: "tenant"}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{"tenant": t, "role": "tenant", "password_changed": true, "sessions_revoked": true, "session_token": newSess})
					return
				}
			}
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập để đổi mật khẩu"}})
			return
		}
		if r.URL.Path == "/api/auth/sessions" && r.Method == http.MethodGet {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			if auth != nil {
				if p, ok := auth.Identify(bearerTok); ok {
					targetUser := p.Name
					if targetUser == "admin-token" {
						targetUser = ""
					}
					writeJSON(w, http.StatusOK, map[string]any{"sessions": auth.ListSessions(targetUser)})
					return
				}
			}
			if ts != nil {
				if t, ok := ts.Authenticate(bearerTok); ok {
					writeJSON(w, http.StatusOK, map[string]any{"sessions": ts.ListSessions(t.ID)})
					return
				}
			}
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập để xem danh sách phiên"}})
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/auth/sessions/") && r.Method == http.MethodDelete {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			sessID := strings.TrimPrefix(r.URL.Path, "/api/auth/sessions/")
			if auth != nil {
				if p, ok := auth.Identify(bearerTok); ok {
					targetUser := p.Name
					if p.Role == RoleAdmin {
						targetUser = ""
					}
					revoked := auth.RevokeSessionByID(targetUser, sessID)
					note(Principal{Name: "revoke-session:" + p.Name, Role: p.Role}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{"revoked": revoked, "session_id": sessID})
					return
				}
			}
			if ts != nil {
				if t, ok := ts.Authenticate(bearerTok); ok {
					revoked := ts.RevokeSessionByID(t.ID, sessID)
					note(Principal{Name: "revoke-session:" + t.Name, Role: "tenant"}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{"revoked": revoked, "session_id": sessID})
					return
				}
			}
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập để thu hồi phiên"}})
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/serverless/v1/_lease/") {
			// Internal lease bridge (ADR-003 D-LEASE-HTTP): LeaseHTTPHandler verifies X-Aicf-Proxy-Secret with constant-time HMAC equality.
			mux.ServeHTTP(w, r)
			return
		}
		if r.URL.Path == "/api/serverless/v1/signup" && r.Method == http.MethodPost {
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			var in struct {
				Name string `json:"name"`
			}
			_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in)
			want := strings.TrimSpace(in.Name)
			if want == "" {
				want = "dev-workspace"
			}
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			// B1.1: Never grant implicit admin in open mode inside signup. Only an explicit RoleAdmin credential counts.
			isAdmin := false
			if !auth.Open() && bearerTok != "" {
				if p, ok := auth.Identify(bearerTok); ok && p.Role == RoleAdmin {
					isAdmin = true
				}
			}
			if !isAdmin && !ts.AllowSignup {
				note(Principal{Name: "signup-denied:" + want, Role: "-"}, r, http.StatusForbidden)
				writeJSON(w, http.StatusForbidden, map[string]any{"error": map[string]any{"code": "signup_disabled", "message": "Đăng ký công khai đang tắt trên gateway này (--allow-signup=false). Vui lòng liên hệ quản trị viên."}})
				return
			}
			if !isAdmin && ts.SignupLimiter != nil && !ts.SignupLimiter.Allow(r.RemoteAddr) {
				w.Header().Set("Retry-After", "12")
				note(Principal{Name: "signup-ratelimit:" + want, Role: "-"}, r, http.StatusTooManyRequests)
				writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": map[string]any{"code": "rate_limited", "message": "Quá nhiều yêu cầu đăng ký. Vui lòng thử lại sau."}})
				return
			}
			pending := !isAdmin && !ts.SignupAutoApprove
			maxInf := 4
			if pending {
				maxInf = 1
			}
			t, key, err := ts.CreateWithPending(want, 1, maxInf, pending)
			if errors.Is(err, ErrDuplicated) {
				// B1.1: Never rotate an existing tenant's key via signup. Key rotation is strictly via admin/rotate-key.
				note(Principal{Name: "signup-duplicate:" + want, Role: "-"}, r, http.StatusConflict)
				writeJSON(w, http.StatusConflict, map[string]any{"error": map[string]any{"code": "account_exists", "message": "Tên Workspace đã tồn tại. Không được phép xoay khóa qua signup; hãy dùng rotate-key kèm xác thực."}})
				return
			}
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			provisionServerlessPolicy(mux, t.ID, t.Pending)
			note(Principal{Name: "signup:" + t.Name, Role: "tenant"}, r, http.StatusCreated)
			writeJSON(w, http.StatusCreated, map[string]any{"tenant": t, "api_key": key})
			return
		}
		if r.URL.Path == "/api/serverless/v1/auth/register" && r.Method == http.MethodPost {
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			isAdmin := false
			if !auth.Open() && bearerTok != "" {
				if p, ok := auth.Identify(bearerTok); ok && p.Role == RoleAdmin {
					isAdmin = true
				}
			}
			if !isAdmin && !ts.AllowSignup {
				note(Principal{Name: "register-denied", Role: "-"}, r, http.StatusForbidden)
				writeJSON(w, http.StatusForbidden, map[string]any{"error": map[string]any{"code": "signup_disabled", "message": "Đăng ký công khai đang tắt trên gateway này (--allow-signup=false). Vui lòng liên hệ quản trị viên."}})
				return
			}
			if !isAdmin && ts.SignupLimiter != nil && !ts.SignupLimiter.Allow(r.RemoteAddr) {
				w.Header().Set("Retry-After", "12")
				note(Principal{Name: "register-ratelimit", Role: "-"}, r, http.StatusTooManyRequests)
				writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": map[string]any{"code": "rate_limited", "message": "Quá nhiều yêu cầu đăng ký. Vui lòng thử lại sau."}})
				return
			}
			var in struct {
				Name     string `json:"name"`
				Email    string `json:"email"`
				Password string `json:"password"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "bad_request", "message": "Dữ liệu JSON không hợp lệ"}})
				return
			}
			if err := ValidatePasswordStrength(in.Password); err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "weak_password", "message": "Mật khẩu phải có ít nhất 12 ký tự và không nằm trong danh sách mật khẩu phổ biến"}})
				return
			}
			pending := !isAdmin && !ts.SignupAutoApprove
			t, key, err := ts.RegisterAccountWithPending(in.Name, in.Email, in.Password, pending)
			if errors.Is(err, ErrDuplicated) {
				writeJSON(w, http.StatusConflict, map[string]any{"error": map[string]any{"code": "account_exists", "message": "Tên Workspace hoặc Email đã tồn tại. Hãy chuyển sang tab Đăng nhập."}})
				return
			}
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "bad_request", "message": err.Error()}})
				return
			}
			provisionServerlessPolicy(mux, t.ID, t.Pending)
			_, sessTok, _ := ts.LoginAccountFrom(t.ID, in.Password, r.RemoteAddr)
			note(Principal{Name: "register:" + t.Name, Role: "tenant"}, r, http.StatusCreated)
			writeJSON(w, http.StatusCreated, map[string]any{"tenant": t, "api_key": key, "session_token": sessTok})
			return
		}
		if r.URL.Path == "/api/serverless/v1/auth/login" && r.Method == http.MethodPost {
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			var in struct {
				Identifier string `json:"identifier"`
				Password   string `json:"password"`
				APIKey     string `json:"api_key"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "bad_request", "message": "Dữ liệu JSON không hợp lệ"}})
				return
			}
			if k := strings.TrimSpace(in.APIKey); k != "" {
				if t, ok := ts.Authenticate(k); ok {
					note(Principal{Name: "login-key:" + t.Name, Role: "tenant"}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{"role": "tenant", "tenant": t, "api_key": k, "session_token": k})
					return
				}
				if auth != nil {
					if p, ok := auth.Identify(k); ok {
						note(Principal{Name: "login-key:" + p.Name, Role: p.Role}, r, http.StatusOK)
						writeJSON(w, http.StatusOK, map[string]any{
							"role":          p.Role,
							"tenant":        map[string]any{"id": p.Name, "name": p.Name, "email": p.Email},
							"api_key":       k,
							"session_token": k,
						})
						return
					}
				}
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "invalid_key", "message": "Khóa API không hợp lệ hoặc đã bị thu hồi"}})
				return
			}
			t, sessTok, err := ts.LoginAccountFrom(in.Identifier, in.Password, r.RemoteAddr)
			if errors.Is(err, ErrBadCreds) && auth != nil {
				if p, admSess, aerr := auth.LoginUser(in.Identifier, in.Password, r.RemoteAddr); aerr == nil {
					note(Principal{Name: "login:" + p.Name, Role: p.Role}, r, http.StatusOK)
					writeJSON(w, http.StatusOK, map[string]any{
						"role":          p.Role,
						"tenant":        map[string]any{"id": p.Name, "name": p.Name, "email": p.Email},
						"api_key":       admSess,
						"session_token": admSess,
					})
					return
				}
			}
			if errors.Is(err, ErrAccountLocked) {
				w.Header().Set("Retry-After", "30")
				note(Principal{Name: "login-locked:" + in.Identifier, Role: "-"}, r, http.StatusTooManyRequests)
				writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": map[string]any{"code": "account_locked", "message": "Tài khoản hoặc IP tạm thời bị khóa do nhập sai nhiều lần. Vui lòng thử lại sau."}})
				return
			}
			if err != nil {
				note(Principal{Name: "login-failed:" + in.Identifier, Role: "-"}, r, http.StatusUnauthorized)
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "invalid_credentials", "message": "Email/Tên tài khoản hoặc mật khẩu không đúng"}})
				return
			}
			note(Principal{Name: "login:" + t.Name, Role: "tenant"}, r, http.StatusOK)
			writeJSON(w, http.StatusOK, map[string]any{"role": "tenant", "tenant": t, "api_key": sessTok, "session_token": sessTok})
			return
		}
		if r.URL.Path == "/api/serverless/v1/auth/logout" && r.Method == http.MethodPost {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			var in struct {
				SessionToken string `json:"session_token"`
				All          bool   `json:"all"`
			}
			_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<12)).Decode(&in)
			if bearerTok == "" {
				bearerTok = strings.TrimSpace(in.SessionToken)
			}
			revoked := false
			if ts != nil && bearerTok != "" {
				if in.All {
					if t, ok := ts.Authenticate(bearerTok); ok {
						revoked = ts.RevokeAllSessions(t.ID) > 0
					}
				} else {
					revoked = ts.LogoutSession(bearerTok)
				}
			}
			note(Principal{Name: "logout", Role: "tenant"}, r, http.StatusOK)
			writeJSON(w, http.StatusOK, map[string]any{"logged_out": true, "session_revoked": revoked})
			return
		}
		if r.URL.Path == "/api/serverless/v1/auth/sessions" && r.Method == http.MethodGet {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			t, ok := ts.Authenticate(bearerTok)
			if !ok {
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập để xem danh sách phiên"}})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"sessions": ts.ListSessions(t.ID)})
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/serverless/v1/auth/sessions/") && r.Method == http.MethodDelete {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			t, ok := ts.Authenticate(bearerTok)
			if !ok {
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập để thu hồi phiên"}})
				return
			}
			sessID := strings.TrimPrefix(r.URL.Path, "/api/serverless/v1/auth/sessions/")
			revoked := ts.RevokeSessionByID(t.ID, sessID)
			note(Principal{Name: "revoke-session:" + t.Name, Role: "tenant"}, r, http.StatusOK)
			writeJSON(w, http.StatusOK, map[string]any{"revoked": revoked, "session_id": sessID})
			return
		}
		if r.URL.Path == "/api/serverless/v1/auth/change-password" && r.Method == http.MethodPost {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			t, ok := ts.Authenticate(bearerTok)
			if !ok {
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập để đổi mật khẩu"}})
				return
			}
			var in struct {
				CurrentPassword string `json:"current_password"`
				NewPassword     string `json:"new_password"`
			}
			if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "bad_request", "message": "Dữ liệu JSON không hợp lệ"}})
				return
			}
			if err := ts.VerifyAndChangePassword(t.ID, in.CurrentPassword, in.NewPassword); err != nil {
				code := http.StatusBadRequest
				errCode := "weak_password"
				if errors.Is(err, ErrBadCreds) {
					code = http.StatusUnauthorized
					errCode = "invalid_credentials"
				}
				writeJSON(w, code, map[string]any{"error": map[string]any{"code": errCode, "message": err.Error()}})
				return
			}
			note(Principal{Name: "change-password:" + t.Name, Role: "tenant"}, r, http.StatusOK)
			writeJSON(w, http.StatusOK, map[string]any{"tenant": t, "password_changed": true, "sessions_revoked": true})
			return
		}
		if r.URL.Path == "/api/serverless/v1/auth/rotate-key" && r.Method == http.MethodPost {
			bearerTok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			if ts == nil {
				http.Error(w, "tenant store not configured", http.StatusServiceUnavailable)
				return
			}
			t, ok := ts.Authenticate(bearerTok)
			if !ok {
				writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "unauthorized", "message": "Cần đăng nhập trước khi cấp lại Khóa API"}})
				return
			}
			newKey, err := ts.RotateKey(t.ID)
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"code": "rotate_failed", "message": err.Error()}})
				return
			}
			note(Principal{Name: "rotate-key:" + t.Name, Role: "tenant"}, r, http.StatusOK)
			writeJSON(w, http.StatusOK, map[string]any{"tenant": t, "api_key": newKey})
			return
		}
		r.Header.Del("X-Aicf-User") // identity headers are set below from the credential, never taken from the client
		r.Header.Del("X-Aicf-Role")
		r.Header.Del("X-Aicf-Tenant")
		r.Header.Del("X-Aicf-Target-Tenant")
		r.Header.Del("X-Aicf-Proxy-Secret")
		bearer := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))

		// /api/bestofn/* and /api/serverless/* always require a valid tenant API key (or admin/operator token when configured).
		if strings.HasPrefix(r.URL.Path, "/api/bestofn/") || strings.HasPrefix(r.URL.Path, serverlessPrefix) {
			if ts != nil {
				if t, ok := ts.Authenticate(bearer); ok {
					r.Header.Set("X-Aicf-Tenant", t.ID)
					r.Header.Set("X-Aicf-User", t.Name)
					r.Header.Set("X-Aicf-Role", "tenant")
					sw := &statusWriter{ResponseWriter: w, code: http.StatusOK}
					mux.ServeHTTP(sw, r)
					note(Principal{Name: "tenant:" + t.Name, Role: "tenant"}, r, sw.code)
					return
				}
			}
			if !auth.Open() {
				if p, ok := auth.Identify(bearer); ok {
					if !Allowed(p, r, siteOf) {
						http.Error(w, "role "+p.Role+" may not "+r.Method+" "+r.URL.Path, http.StatusForbidden)
						note(p, r, http.StatusForbidden)
						return
					}
					r.Header.Set("X-Aicf-User", p.Name)
					r.Header.Set("X-Aicf-Role", p.Role)
					sw := &statusWriter{ResponseWriter: w, code: http.StatusOK}
					mux.ServeHTTP(sw, r)
					note(p, r, sw.code)
					return
				}
			}
			w.Header().Set("WWW-Authenticate", `Bearer realm="aicf"`)
			http.Error(w, "missing, invalid or disabled API key", http.StatusUnauthorized)
			note(Principal{Name: "-", Role: "-"}, r, http.StatusUnauthorized)
			return
		}

		// /api/compute-agents* accepts either an admin/operator/viewer token or a tenant API key (tenant keys never see members).
		if (r.URL.Path == "/api/compute-agents" || r.URL.Path == "/api/compute-agents/stream") && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			if p, ok := auth.Identify(bearer); ok {
				r.Header.Set("X-Aicf-User", p.Name)
				r.Header.Set("X-Aicf-Role", p.Role)
				mux.ServeHTTP(w, r)
				return
			}
			if ts != nil && bearer != "" {
				if t, ok := ts.Authenticate(bearer); ok {
					r.Header.Set("X-Aicf-Tenant", t.ID)
					r.Header.Set("X-Aicf-User", t.Name)
					r.Header.Set("X-Aicf-Role", "tenant")
					mux.ServeHTTP(w, r)
					return
				}
				w.Header().Set("WWW-Authenticate", `Bearer realm="aicf"`)
				http.Error(w, "invalid API key or admin token", http.StatusUnauthorized)
				return
			}
		}

		// /api/studio/* also allows authenticated Tenant sessions/keys to use AI Studio & Audio AI for their own tenant ID.
		if strings.HasPrefix(r.URL.Path, "/api/studio/") && ts != nil && bearer != "" {
			if _, staffOK := auth.Identify(bearer); !staffOK {
				if t, tokOK := ts.Authenticate(bearer); tokOK {
					if strings.HasPrefix(r.URL.Path, "/api/studio/v1/tenants/") {
						rest := strings.TrimPrefix(r.URL.Path, "/api/studio/v1/tenants/")
						targetTid := strings.SplitN(rest, "/", 2)[0]
						if targetTid != t.ID && targetTid != t.Name {
							http.Error(w, "tenant may only access its own Studio workspace", http.StatusForbidden)
							note(Principal{Name: "tenant:" + t.Name, Role: "tenant"}, r, http.StatusForbidden)
							return
						}
					}
					r.Header.Set("X-Aicf-Tenant", t.ID)
					r.Header.Set("X-Aicf-User", t.Name)
					r.Header.Set("X-Aicf-Role", "tenant")
					sw := &statusWriter{ResponseWriter: w, code: http.StatusOK}
					mux.ServeHTTP(sw, r)
					note(Principal{Name: "tenant:" + t.Name, Role: "tenant"}, r, sw.code)
					return
				}
			}
		}

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
	}))
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

// who answers GET /api/whoami so the dashboard can hide what the caller may not do. It is a convenience for the UI
// only: every action is still checked server-side by Allowed, whatever the page shows.
func who(w http.ResponseWriter, r *http.Request, auth *Auth, ts *TenantStore) {
	bearer := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if ts != nil && bearer != "" {
		if _, staffOK := auth.Identify(bearer); !staffOK {
			if t, tokOK := ts.Authenticate(bearer); tokOK {
				active := !t.Disabled && !t.Pending
				writeJSON(w, http.StatusOK, map[string]any{
					"name": t.Name, "email": t.Email, "tenant_id": t.ID, "role": "tenant", "site": "", "open": false,
					"can": map[string]bool{
						"view": false, "pause": false, "tenants": false,
						"studio": active, "studio_delete": active, "serverless_evict": false,
						"serverless": active,
					},
				})
				return
			}
		}
	}
	p := Principal{Name: "open", Role: RoleAdmin}
	open := auth.Open()
	if !open {
		var ok bool
		p, ok = auth.Identify(bearer)
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
		"name": p.Name, "email": p.Email, "role": p.Role, "site": p.Site, "open": open,
		"can": map[string]bool{"view": probe("GET", "/api/stats"), "pause": probe("POST", "/api/workers/x/pause"), "tenants": probe("POST", "/api/tenants"),
			"studio": probe("POST", "/api/studio/x"), "studio_delete": probe("DELETE", "/api/studio/x"),
			"serverless_evict": probe("DELETE", "/api/serverless/v1/containers/x")},
	})
}
