package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestB1SignupDisabledByDefaultAndNeverRotatesExistingKey(t *testing.T) {
	ts, err := NewTenantStore(filepath.Join(t.TempDir(), "tenants.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := NewAuth("strong-production-admin-token-2026")
	h := NewAPIServerless(NewFleet(NewRegistry(time.Hour)), ts, auth, nil, nil, nil, nil, nil)

	postJSON := func(path, tok string, body map[string]any, remote string) *httptest.ResponseRecorder {
		b, _ := json.Marshal(body)
		req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(b))
		req.Header.Set("Content-Type", "application/json")
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		if remote != "" {
			req.RemoteAddr = remote
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec
	}

	// 1. Default AllowSignup=false -> unauthenticated signup and auth/register are rejected (403)
	if rec := postJSON("/api/serverless/v1/signup", "", map[string]any{"name": "alpha"}, "10.0.0.1:1234"); rec.Code != http.StatusForbidden {
		t.Fatalf("signup with AllowSignup=false must be 403, got %d: %s", rec.Code, rec.Body.String())
	}
	if rec := postJSON("/api/serverless/v1/auth/register", "", map[string]any{"name": "alpha", "email": "a@b.c", "password": " StrongPass#2026!"}, "10.0.0.1:1234"); rec.Code != http.StatusForbidden {
		t.Fatalf("auth/register with AllowSignup=false must be 403, got %d", rec.Code)
	}

	// 2. Open mode (no admin token configured) STILL does not grant implicit admin in signup when AllowSignup=false
	openH := NewAPIServerless(NewFleet(NewRegistry(time.Hour)), ts, NewAuth(""), nil, nil, nil, nil, nil)
	reqOpen := httptest.NewRequest(http.MethodPost, "/api/serverless/v1/signup", strings.NewReader(`{"name":"open-try"}`))
	recOpen := httptest.NewRecorder()
	openH.ServeHTTP(recOpen, reqOpen)
	if recOpen.Code != http.StatusForbidden {
		t.Fatalf("open mode must not grant implicit admin in signup when AllowSignup=false, got %d", recOpen.Code)
	}

	// 3. Enable AllowSignup -> public signup creates a Pending tenant, rate-limited per IP, and duplicate NEVER rotates key
	ts.AllowSignup = true
	ts.SignupLimiter = NewSignupRateLimiter(3, 10)
	rec1 := postJSON("/api/serverless/v1/signup", "", map[string]any{"name": "team-one"}, "10.0.0.9:4000")
	if rec1.Code != http.StatusCreated {
		t.Fatalf("public signup with AllowSignup=true failed: %d %s", rec1.Code, rec1.Body.String())
	}
	var out1 struct {
		Tenant TenantView `json:"tenant"`
		APIKey string     `json:"api_key"`
	}
	_ = json.Unmarshal(rec1.Body.Bytes(), &out1)
	if !out1.Tenant.Pending {
		t.Fatalf("public signup without SignupAutoApprove must create tenant with Pending=true: %+v", out1.Tenant)
	}
	origKey := out1.APIKey

	// Duplicate signup with the same name must return 409 and MUST NOT invalidate origKey
	recDup := postJSON("/api/serverless/v1/signup", "strong-production-admin-token-2026", map[string]any{"name": "team-one"}, "10.0.0.9:4000")
	if recDup.Code != http.StatusConflict {
		t.Fatalf("duplicate signup must be 409 Conflict (never rotate key), got %d: %s", recDup.Code, recDup.Body.String())
	}
	if _, ok := ts.Authenticate(origKey); !ok {
		t.Fatal("duplicate signup rotated or invalidated the existing tenant's API key!")
	}

	// Exhaust per-IP rate limit (burst=3: 1 used above, 2 more allowed, 4th from same IP must be 429)
	_ = postJSON("/api/serverless/v1/signup", "", map[string]any{"name": "team-two"}, "10.0.0.9:4000")
	_ = postJSON("/api/serverless/v1/signup", "", map[string]any{"name": "team-three"}, "10.0.0.9:4000")
	recRL := postJSON("/api/serverless/v1/signup", "", map[string]any{"name": "team-four"}, "10.0.0.9:4000")
	if recRL.Code != http.StatusTooManyRequests || recRL.Header().Get("Retry-After") == "" {
		t.Fatalf("expected 429 Too Many Requests with Retry-After, got %d (Retry-After=%q)", recRL.Code, recRL.Header().Get("Retry-After"))
	}

	// Admin approval via PATCH /api/tenants/{id} clears Pending
	patchReq := httptest.NewRequest(http.MethodPatch, "/api/tenants/"+out1.Tenant.ID, strings.NewReader(`{"pending":false}`))
	patchReq.Header.Set("Authorization", "Bearer strong-production-admin-token-2026")
	patchRec := httptest.NewRecorder()
	h.ServeHTTP(patchRec, patchReq)
	if patchRec.Code != http.StatusOK {
		t.Fatalf("admin PATCH pending=false failed: %d %s", patchRec.Code, patchRec.Body.String())
	}
	tv, ok := ts.Authenticate(origKey)
	if !ok || tv.Pending {
		t.Fatalf("tenant should be approved (Pending=false), got %+v", tv)
	}
}

func TestB1Argon2idPasswordPolicyLegacyMigrationAndBruteForceLockout(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tenants.json")
	ts, err := NewTenantStore(path)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 11, 5, 12, 0, 0, 0, time.UTC)
	ts.Now = func() time.Time { return now }
	ts.LockoutDuration = 30 * time.Second

	// 1. Weak passwords (< 12 chars, common passwords, repeated chars) are rejected
	for _, weak := range []string{"short123", "password1234", "123456789012", "qwerty123456", "aaaaaaaaaaaa"} {
		if _, _, err := ts.RegisterAccount("u-"+weak[:4], weak[:4]+"@aicf.local", weak); err != ErrWeakPassword {
			t.Fatalf("weak password %q must fail with ErrWeakPassword, got %v", weak, err)
		}
	}

	// 2. Valid registration stores argon2id$v=19$ hash
	tv, cliKey, err := ts.RegisterAccount("sec-team", "sec@aicf.local", "V3ryStr0ng!Pass#2026")
	if err != nil {
		t.Fatal(err)
	}
	rawTenants := ts.byID[tv.ID]
	if !strings.HasPrefix(rawTenants.PassHash, "argon2id$v=19$m=19456,t=2,p=1$") || rawTenants.PassSalt != "" {
		t.Fatalf("expected argon2id hash and empty legacy salt, got hash=%q salt=%q", rawTenants.PassHash, rawTenants.PassSalt)
	}

	// 3. One-time migration from legacy v1 hash on successful login
	legacySalt := "legacySalt12"
	legacyPw := "LegacyAccount#2026!"
	ts.byID["t_legacy"] = &Tenant{
		ID:       "t_legacy",
		Name:     "legacy-user",
		Email:    "legacy@aicf.local",
		PassSalt: legacySalt,
		PassHash: hashPasswordLegacy(legacySalt, legacyPw),
		KeyHash:  hashKey("aicf_legacy_key_000000000000"),
		Weight:   1,
	}
	if _, _, err := ts.LoginAccountFrom("legacy@aicf.local", legacyPw, "10.1.1.1:5000"); err != nil {
		t.Fatalf("legacy login must succeed and migrate: %v", err)
	}
	if !strings.HasPrefix(ts.byID["t_legacy"].PassHash, "argon2id$v=19$") || ts.byID["t_legacy"].PassSalt != "" {
		t.Fatalf("legacy account was not migrated to argon2id: hash=%q salt=%q", ts.byID["t_legacy"].PassHash, ts.byID["t_legacy"].PassSalt)
	}

	// 4. Brute-force lockout after 5 failed attempts
	for i := 0; i < 5; i++ {
		if _, _, err := ts.LoginAccountFrom("sec@aicf.local", "WrongPassword#000", "10.2.2.2:6000"); err != ErrBadCreds {
			t.Fatalf("attempt %d: expected ErrBadCreds, got %v", i+1, err)
		}
	}
	// 6th attempt even with the CORRECT password must be locked out!
	if _, _, err := ts.LoginAccountFrom("sec@aicf.local", "V3ryStr0ng!Pass#2026", "10.2.2.2:6000"); err != ErrAccountLocked {
		t.Fatalf("expected ErrAccountLocked on 6th attempt even with valid password, got %v", err)
	}
	// Advance clock by 31s -> lockout expires and login succeeds
	now = now.Add(31 * time.Second)
	_, sess1, err := ts.LoginAccountFrom("sec@aicf.local", "V3ryStr0ng!Pass#2026", "10.2.2.2:6000")
	if err != nil || !strings.HasPrefix(sess1, "aicf_sess_") {
		t.Fatalf("login after lockout expiry failed: err=%v sess=%q", err, sess1)
	}

	// 5. Session idle timeout, absolute expiry, listing, and revocation on RotateKey / ChangePassword
	ts.SessionIdleTTL = 10 * time.Minute
	ts.SessionMaxTTL = 1 * time.Hour
	_, sess2, err := ts.LoginAccountFrom("sec@aicf.local", "V3ryStr0ng!Pass#2026", "10.2.2.3:6000")
	if err != nil {
		t.Fatal(err)
	}
	if sessions := ts.ListSessions(tv.ID); len(sessions) != 2 {
		t.Fatalf("expected 2 active sessions, got %d", len(sessions))
	}
	// Advance 11 minutes -> idle timeout (10m) expires both sessions
	now = now.Add(11 * time.Minute)
	if _, ok := ts.Authenticate(sess2); ok {
		t.Fatal("session must expire after SessionIdleTTL")
	}
	// CLI API key still works even when web sessions expire
	if _, ok := ts.Authenticate(cliKey); !ok {
		t.Fatal("permanent CLI API key must remain valid when web session expires")
	}
	// Create a new session and verify RotateKey revokes it immediately
	_, sess3, _ := ts.LoginAccountFrom("sec@aicf.local", "V3ryStr0ng!Pass#2026", "10.2.2.3:6000")
	if _, ok := ts.Authenticate(sess3); !ok {
		t.Fatal("fresh session must authenticate")
	}
	if _, err := ts.RotateKey(tv.ID); err != nil {
		t.Fatal(err)
	}
	if _, ok := ts.Authenticate(sess3); ok {
		t.Fatal("RotateKey must revoke all active web sessions for the tenant")
	}
}

func TestB1ValidateListenAuthRejectsDefaultAndWeakTokensBeyondLoopback(t *testing.T) {
	if err := ValidateListenAuth("127.0.0.1:18080", NewAuth("demo-admin-token")); err != nil {
		t.Fatalf("loopback may use demo token: %v", err)
	}
	for _, bad := range []string{"", "demo-admin-token", "admin", "changeme", "short-token"} {
		if err := ValidateListenAuth("0.0.0.0:18080", NewAuth(bad)); err == nil {
			t.Fatalf("0.0.0.0:18080 with token %q must be refused at startup", bad)
		}
	}
	if err := ValidateListenAuth("0.0.0.0:18080", NewAuth("prod_adm_9f8e7d6c5b4a3210fedcba98")); err != nil {
		t.Fatalf("0.0.0.0:18080 with strong token must be accepted: %v", err)
	}
}

func TestB4HealthzReadyzMetricsAuthAndSecurityHeaders(t *testing.T) {
	ts, _ := NewTenantStore("")
	auth := NewAuth("strong-admin-token-for-b4-tests")
	viewerTok := "viewer-secret-token-for-b4"
	auth.byHash[HashToken(viewerTok)] = Principal{Name: "ops-viewer", Role: RoleViewer}

	h := NewAPIServerless(NewFleet(NewRegistry(time.Hour)), ts, auth, nil, nil, nil, nil, nil)
	mux := http.NewServeMux()
	inf := NewInference(NewRouter(RouterConfig{}, NewRegistry(time.Hour)), nil)
	mux.HandleFunc("/metrics", WrapRequireViewer(auth, inf.Metrics))
	mux.Handle("/", h)
	srvHandler := withSecurityHeaders(mux)

	for _, path := range []string{"/healthz", "/readyz", "/index.html", "/api/nonexistent"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.Header.Set("X-Forwarded-Proto", "https")
		rec := httptest.NewRecorder()
		srvHandler.ServeHTTP(rec, req)
		for hdr, want := range map[string]string{
			"X-Content-Type-Options": "nosniff",
			"X-Frame-Options":        "DENY",
			"Referrer-Policy":        "no-referrer",
		} {
			if got := rec.Header().Get(hdr); got != want {
				t.Fatalf("%s: header %s = %q, want %q", path, hdr, got, want)
			}
		}
		if csp := rec.Header().Get("Content-Security-Policy"); !strings.Contains(csp, "script-src 'self'") || strings.Contains(csp, "script-src 'self' 'unsafe-inline'") {
			t.Fatalf("%s: CSP must enforce strict script-src 'self', got %q", path, csp)
		}
		if hsts := rec.Header().Get("Strict-Transport-Security"); !strings.Contains(hsts, "max-age=") {
			t.Fatalf("%s: expected HSTS header when X-Forwarded-Proto=https, got %q", path, hsts)
		}
	}

	// /metrics must reject unauthenticated callers when auth is configured, and allow RoleViewer
	recNoAuth := httptest.NewRecorder()
	srvHandler.ServeHTTP(recNoAuth, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if recNoAuth.Code != http.StatusUnauthorized {
		t.Fatalf("/metrics without token must be 401, got %d", recNoAuth.Code)
	}
	reqViewer := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	reqViewer.Header.Set("Authorization", "Bearer "+viewerTok)
	recViewer := httptest.NewRecorder()
	srvHandler.ServeHTTP(recViewer, reqViewer)
	if recViewer.Code != http.StatusOK {
		t.Fatalf("/metrics with RoleViewer token must be 200, got %d", recViewer.Code)
	}
}

func TestB1ConstantTimeComparisonEnforcedInSource(t *testing.T) {
	b, err := filepath.Glob("tenants.go")
	if err != nil || len(b) == 0 {
		t.Fatal("tenants.go not found")
	}
	srcBytes, err := http.Dir(".").Open("tenants.go")
	if err != nil {
		t.Fatal(err)
	}
	defer srcBytes.Close()
	buf := new(bytes.Buffer)
	_, _ = buf.ReadFrom(srcBytes)
	src := buf.String()
	if strings.Count(src, "subtle.ConstantTimeCompare(") < 5 {
		t.Fatalf("expected at least 5 subtle.ConstantTimeCompare calls in tenants.go (argon2id, legacy, dummy, key, session), got %d",
			strings.Count(src, "subtle.ConstantTimeCompare("))
	}
}
