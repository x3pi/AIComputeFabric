package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

var knownDefaultAdminTokens = map[string]bool{
	"demo-admin-token":    true,
	"default-admin-token": true,
	"admin":               true,
	"admin-token":         true,
	"changeme":            true,
	"secret":              true,
	"password":            true,
	"aicf-demo-token":     true,
}

// IsKnownDefaultToken reports whether tok is a well-known demo or placeholder token.
func IsKnownDefaultToken(tok string) bool {
	t := strings.ToLower(strings.TrimSpace(tok))
	return knownDefaultAdminTokens[t]
}

func isLoopbackHost(host string) bool {
	return host == "127.0.0.1" || host == "localhost" || host == "::1"
}

// ValidateListenAuth refuses to expose the admin API beyond loopback when auth is open or uses a known default/weak token (B1.6).
func ValidateListenAuth(httpAddr string, auth *Auth) error {
	h, _, err := net.SplitHostPort(httpAddr)
	if err != nil {
		h = httpAddr
	}
	if isLoopbackHost(h) {
		return nil
	}
	if auth.Open() {
		return fmt.Errorf("refusing to expose the admin API on %s without --admin-token (or AICF_ADMIN_TOKEN), --admin-password, or --roles-file", httpAddr)
	}
	if auth.adminToken != "" && (IsKnownDefaultToken(auth.adminToken) || len(auth.adminToken) < 16) {
		return fmt.Errorf("refusing to expose the admin API on %s with a known default or short (<16 chars) --admin-token", httpAddr)
	}
	return nil
}

// WrapRequireViewer protects a read-only handler (such as /metrics) behind RoleViewer/RoleOperator/RoleAdmin when auth is configured (B4b).
func WrapRequireViewer(auth *Auth, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		if auth != nil && !auth.Open() {
			tok := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			p, ok := auth.Identify(tok)
			if !ok {
				w.Header().Set("WWW-Authenticate", `Bearer realm="aicf-admin"`)
				http.Error(w, "viewer token required", http.StatusUnauthorized)
				return
			}
			if !Allowed(p, r, func(string) string { return p.Site }) {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
		}
		next(w, r)
	}
}

// Roles for the admin API (plan P2.4): viewer reads, operator also pauses/resumes workers
// (optionally only in one site), admin does everything including tenants and keys.
const (
	RoleViewer   = "viewer"
	RoleOperator = "operator"
	RoleAdmin    = "admin"
)

type Principal struct {
	Name  string `json:"name"`
	Email string `json:"email,omitempty"`
	Role  string `json:"role"`
	Site  string `json:"site,omitempty"` // operator only: restrict to workers of this site
}

// UserAccount represents an Admin/Operator/Viewer user account with an Argon2id password hash.
type UserAccount struct {
	Username string `json:"username"`
	Email    string `json:"email,omitempty"`
	Role     string `json:"role"`
	Site     string `json:"site,omitempty"`
	PassHash string `json:"pass_hash,omitempty"`
}

// AdminSession tracks an active web console session (`aicf_adm_sess_...`) by its SHA-256 hash.
type AdminSession struct {
	ID          string
	TokenHash   string
	Principal   Principal
	CreatedAt   time.Time
	LastActive  time.Time
	ExpiresAt   time.Time
	IdleTimeout time.Duration
	RemoteIP    string
}

// Auth maps bearer tokens, revocable admin sessions, and username/password accounts to principals.
// Tokens from a file are stored as SHA-256 hex, and passwords are stored as salted Argon2id hashes.
type Auth struct {
	mu              sync.RWMutex
	adminToken      string
	byHash          map[string]Principal
	byUser          map[string]*UserAccount
	bySession       map[string]*AdminSession
	loginAttempts   map[string]*loginAttemptState
	SessionMaxTTL   time.Duration
	SessionIdleTTL  time.Duration
	LockoutDuration time.Duration
	Now             func() time.Time
}

// Open reports whether no credential is configured at all (loopback dev mode).
func (a *Auth) Open() bool {
	if a == nil {
		return true
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.adminToken == "" && len(a.byHash) == 0 && len(a.byUser) == 0
}

func NewAuth(adminToken string) *Auth {
	return &Auth{
		adminToken:      adminToken,
		byHash:          map[string]Principal{},
		byUser:          map[string]*UserAccount{},
		bySession:       map[string]*AdminSession{},
		loginAttempts:   map[string]*loginAttemptState{},
		SessionMaxTTL:   12 * time.Hour,
		SessionIdleTTL:  2 * time.Hour,
		LockoutDuration: defaultLockout,
		Now:             time.Now,
	}
}

func (a *Auth) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}

// SetUserPassword registers or updates an Admin/Operator/Viewer user account with an Argon2id password hash.
func (a *Auth) SetUserPassword(username, email, role, site, password string) error {
	username = strings.TrimSpace(username)
	email = strings.ToLower(strings.TrimSpace(email))
	if username == "" {
		return fmt.Errorf("username must not be empty")
	}
	switch role {
	case RoleViewer, RoleOperator, RoleAdmin:
	default:
		return fmt.Errorf("unknown role %q", role)
	}
	if site != "" && role != RoleOperator {
		return fmt.Errorf("site only applies to operators")
	}
	if err := ValidatePasswordStrength(password); err != nil {
		return err
	}
	pwHash := hashPasswordArgon2id(password)
	a.mu.Lock()
	defer a.mu.Unlock()
	a.byUser[strings.ToLower(username)] = &UserAccount{
		Username: username,
		Email:    email,
		Role:     role,
		Site:     site,
		PassHash: pwHash,
	}
	return nil
}

// SetUserPasswordHash registers an Admin/Operator/Viewer user account with a pre-computed Argon2id password hash.
func (a *Auth) SetUserPasswordHash(username, email, role, site, passHash string) error {
	username = strings.TrimSpace(username)
	email = strings.ToLower(strings.TrimSpace(email))
	passHash = strings.TrimSpace(passHash)
	if username == "" {
		return fmt.Errorf("username must not be empty")
	}
	if !strings.HasPrefix(passHash, "argon2id$") {
		return fmt.Errorf("password_hash must be in argon2id$ format")
	}
	switch role {
	case RoleViewer, RoleOperator, RoleAdmin:
	default:
		return fmt.Errorf("unknown role %q", role)
	}
	if site != "" && role != RoleOperator {
		return fmt.Errorf("site only applies to operators")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.byUser[strings.ToLower(username)] = &UserAccount{
		Username: username,
		Email:    email,
		Role:     role,
		Site:     site,
		PassHash: passHash,
	}
	return nil
}

type roleEntry struct {
	Principal
	TokenSHA256  string `json:"token_sha256,omitempty"`
	PasswordHash string `json:"password_hash,omitempty"`
	Password     string `json:"password,omitempty"`
}

// LoadRoles reads a JSON array of {name, email?, role, site?, token_sha256?, password_hash?, password?}.
func (a *Auth) LoadRoles(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var es []roleEntry
	if err := json.Unmarshal(raw, &es); err != nil {
		return fmt.Errorf("roles file: %w", err)
	}
	for i, e := range es {
		switch e.Role {
		case RoleViewer, RoleOperator, RoleAdmin:
		default:
			return fmt.Errorf("roles file: entry %d (%q): unknown role %q", i, e.Name, e.Role)
		}
		if e.Site != "" && e.Role != RoleOperator {
			return fmt.Errorf("roles file: entry %d (%q): site only applies to operators", i, e.Name)
		}
		hasToken := strings.TrimSpace(e.TokenSHA256) != ""
		hasPwHash := strings.TrimSpace(e.PasswordHash) != ""
		hasPw := e.Password != ""
		if !hasToken && !hasPwHash && !hasPw {
			return fmt.Errorf("roles file: entry %d (%q): token_sha256 or password_hash must be provided", i, e.Name)
		}
		if hasToken {
			h := strings.ToLower(strings.TrimSpace(e.TokenSHA256))
			if len(h) != 64 {
				return fmt.Errorf("roles file: entry %d (%q): token_sha256 must be 64 hex characters", i, e.Name)
			}
			if _, err := hex.DecodeString(h); err != nil {
				return fmt.Errorf("roles file: entry %d (%q): token_sha256 is not hex", i, e.Name)
			}
			a.mu.Lock()
			a.byHash[h] = e.Principal
			a.mu.Unlock()
		}
		if hasPwHash {
			if err := a.SetUserPasswordHash(e.Name, e.Email, e.Role, e.Site, e.PasswordHash); err != nil {
				return fmt.Errorf("roles file: entry %d (%q): %w", i, e.Name, err)
			}
		} else if hasPw {
			if err := a.SetUserPassword(e.Name, e.Email, e.Role, e.Site, e.Password); err != nil {
				return fmt.Errorf("roles file: entry %d (%q): %w", i, e.Name, err)
			}
		}
	}
	return nil
}

func (a *Auth) checkLockedOutLocked(key string, now time.Time) bool {
	st, ok := a.loginAttempts[key]
	if !ok {
		return false
	}
	return !st.lockedUntil.IsZero() && now.Before(st.lockedUntil)
}

func (a *Auth) recordLoginFailureLocked(key string, now time.Time) {
	st, ok := a.loginAttempts[key]
	if !ok {
		st = &loginAttemptState{firstFailed: now}
		a.loginAttempts[key] = st
	}
	if now.Sub(st.firstFailed) > 10*time.Minute {
		st.failures = 0
		st.firstFailed = now
	}
	st.failures++
	if st.failures >= maxLoginFailures {
		base := a.LockoutDuration
		if base <= 0 {
			base = defaultLockout
		}
		mult := 1 << min(st.strikes, 5)
		st.lockedUntil = now.Add(time.Duration(mult) * base)
		st.strikes++
		st.failures = 0
	}
}

func (a *Auth) clearLoginFailuresLocked(key string) {
	delete(a.loginAttempts, key)
}

// LoginUser verifies an Admin/Operator/Viewer Username/Email + Password using Argon2id
// and issues a revocable session token (`aicf_adm_sess_...`).
func (a *Auth) LoginUser(identifier, password, remoteAddr string) (Principal, string, error) {
	if a == nil {
		dummyVerifyPassword(password)
		return Principal{}, "", ErrBadCreds
	}
	ident := strings.TrimSpace(identifier)
	if ident == "" || password == "" {
		dummyVerifyPassword(password)
		return Principal{}, "", ErrBadCreds
	}
	acctKey := "adm_acct:" + strings.ToLower(ident)
	ipKey := "adm_ip:" + normalizeRemoteIP(remoteAddr)

	a.mu.Lock()
	now := a.now()
	if a.checkLockedOutLocked(acctKey, now) || (remoteAddr != "" && a.checkLockedOutLocked(ipKey, now)) {
		a.mu.Unlock()
		dummyVerifyPassword(password)
		return Principal{}, "", ErrAccountLocked
	}
	var found *UserAccount
	for _, u := range a.byUser {
		if strings.EqualFold(u.Username, ident) || (u.Email != "" && strings.EqualFold(u.Email, ident)) {
			found = u
			break
		}
	}
	if found == nil || found.PassHash == "" {
		a.recordLoginFailureLocked(acctKey, now)
		if remoteAddr != "" {
			a.recordLoginFailureLocked(ipKey, now)
		}
		a.mu.Unlock()
		dummyVerifyPassword(password)
		return Principal{}, "", ErrBadCreds
	}
	storedHash := found.PassHash
	a.mu.Unlock()

	ok, _ := verifyPasswordHash(storedHash, "", password)

	a.mu.Lock()
	defer a.mu.Unlock()
	now = a.now()
	if !ok {
		a.recordLoginFailureLocked(acctKey, now)
		if remoteAddr != "" {
			a.recordLoginFailureLocked(ipKey, now)
		}
		return Principal{}, "", ErrBadCreds
	}
	a.clearLoginFailuresLocked(acctKey)
	if remoteAddr != "" {
		a.clearLoginFailuresLocked(ipKey)
	}
	maxTTL := a.SessionMaxTTL
	if maxTTL <= 0 {
		maxTTL = 12 * time.Hour
	}
	idleTTL := a.SessionIdleTTL
	if idleTTL <= 0 {
		idleTTL = 2 * time.Hour
	}
	p := Principal{Name: found.Username, Email: found.Email, Role: found.Role, Site: found.Site}
	sessTok := "aicf_adm_sess_" + newSecret(24)
	tokHash := hashKey(sessTok)
	a.bySession[tokHash] = &AdminSession{
		ID:          "adm_sess_" + newSecret(6),
		TokenHash:   tokHash,
		Principal:   p,
		CreatedAt:   now,
		LastActive:  now,
		ExpiresAt:   now.Add(maxTTL),
		IdleTimeout: idleTTL,
		RemoteIP:    normalizeRemoteIP(remoteAddr),
	}
	return p, sessTok, nil
}

// ChangeUserPassword updates an Admin/Operator/Viewer user's password after verifying currentPassword (when non-empty)
// and revokes all active sessions for that user.
func (a *Auth) ChangeUserPassword(username, currentPassword, newPassword string) error {
	if a == nil {
		return ErrBadCreds
	}
	if err := ValidatePasswordStrength(newPassword); err != nil {
		return err
	}
	a.mu.RLock()
	u := a.byUser[strings.ToLower(strings.TrimSpace(username))]
	var storedHash string
	if u != nil {
		storedHash = u.PassHash
	}
	a.mu.RUnlock()
	if u == nil {
		dummyVerifyPassword(currentPassword)
		return ErrBadCreds
	}
	if currentPassword != "" {
		if ok, _ := verifyPasswordHash(storedHash, "", currentPassword); !ok {
			return ErrBadCreds
		}
	}
	newHash := hashPasswordArgon2id(newPassword)
	a.mu.Lock()
	defer a.mu.Unlock()
	u.PassHash = newHash
	for h, sess := range a.bySession {
		if strings.EqualFold(sess.Principal.Name, u.Username) {
			delete(a.bySession, h)
		}
	}
	return nil
}

// LogoutSession revokes an active Admin session token (`aicf_adm_sess_...`).
func (a *Auth) LogoutSession(token string) bool {
	if a == nil || token == "" {
		return false
	}
	h := hashKey(token)
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, ok := a.bySession[h]; ok {
		delete(a.bySession, h)
		return true
	}
	return false
}

// ListSessions returns active sessions for an Admin/Operator/Viewer user.
func (a *Auth) ListSessions(username string) []SessionView {
	if a == nil {
		return nil
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	now := a.now()
	out := []SessionView{}
	for h, sess := range a.bySession {
		if now.After(sess.ExpiresAt) || (sess.IdleTimeout > 0 && now.Sub(sess.LastActive) > sess.IdleTimeout) {
			delete(a.bySession, h)
			continue
		}
		if username == "" || strings.EqualFold(sess.Principal.Name, username) {
			out = append(out, SessionView{
				ID:         sess.ID,
				TenantID:   sess.Principal.Name,
				CreatedAt:  sess.CreatedAt.UTC().Format(time.RFC3339),
				LastActive: sess.LastActive.UTC().Format(time.RFC3339),
				ExpiresAt:  sess.ExpiresAt.UTC().Format(time.RFC3339),
				RemoteIP:   sess.RemoteIP,
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// RevokeSessionByID revokes an active Admin session by its public ID (`adm_sess_...`).
func (a *Auth) RevokeSessionByID(username, sessID string) bool {
	if a == nil {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for h, sess := range a.bySession {
		if sess.ID == sessID && (username == "" || strings.EqualFold(sess.Principal.Name, username)) {
			delete(a.bySession, h)
			return true
		}
	}
	return false
}

// Identify returns the principal for a bearer token or revocable admin session token.
func (a *Auth) Identify(token string) (Principal, bool) {
	if a == nil || token == "" {
		return Principal{}, false
	}
	if a.adminToken != "" && subtle.ConstantTimeCompare([]byte(token), []byte(a.adminToken)) == 1 {
		return Principal{Name: "admin-token", Role: RoleAdmin}, true
	}
	sum := sha256.Sum256([]byte(token))
	h := hex.EncodeToString(sum[:])
	a.mu.Lock()
	defer a.mu.Unlock()
	if p, ok := a.byHash[h]; ok {
		return p, true
	}
	if sess, ok := a.bySession[h]; ok {
		now := a.now()
		if now.After(sess.ExpiresAt) || (sess.IdleTimeout > 0 && now.Sub(sess.LastActive) > sess.IdleTimeout) {
			delete(a.bySession, h)
			return Principal{}, false
		}
		if subtle.ConstantTimeCompare([]byte(sess.TokenHash), []byte(h)) == 1 {
			sess.LastActive = now
			return sess.Principal, true
		}
		delete(a.bySession, h)
	}
	return Principal{}, false
}

// HashToken is what goes into the roles file.
func HashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// Allowed decides whether p may perform r. siteOf resolves a worker id to its site ("" if unknown).
func Allowed(p Principal, r *http.Request, siteOf func(string) string) bool {
	if p.Role == RoleAdmin {
		return true
	}
	if r.Method == http.MethodGet || r.Method == http.MethodHead {
		return p.Role == RoleViewer || p.Role == RoleOperator
	}
	// AI Studio: an operator may upload, create and run (POST/PUT); deleting data is admin-only (DELETE falls through to false).
	if p.Role == RoleOperator && (r.Method == http.MethodPost || r.Method == http.MethodPut) && strings.HasPrefix(r.URL.Path, "/api/studio/") {
		return true
	}
	// Serverless: an operator may force-evict warm containers and toggle drain/enable; policy changes remain admin-only.
	if p.Role == RoleOperator && ((r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/serverless/v1/containers/")) ||
		(r.Method == http.MethodPost && r.URL.Path == "/api/serverless/v1/admin/state")) {
		return true
	}
	if p.Role == RoleOperator && r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/api/workers/") {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/workers/"), "/")
		if len(parts) == 2 && (parts[1] == "pause" || parts[1] == "resume") {
			return p.Site == "" || siteOf(parts[0]) == p.Site
		}
	}
	return false
}
