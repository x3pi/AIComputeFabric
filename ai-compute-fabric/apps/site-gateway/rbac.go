package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
)

// Roles for the admin API (plan P2.4): viewer reads, operator also pauses/resumes workers
// (optionally only in one site), admin does everything including tenants and keys.
const (
	RoleViewer   = "viewer"
	RoleOperator = "operator"
	RoleAdmin    = "admin"
)

type Principal struct {
	Name string `json:"name"`
	Role string `json:"role"`
	Site string `json:"site,omitempty"` // operator only: restrict to workers of this site
}

// Auth maps bearer tokens to principals. Tokens from a file are stored as SHA-256 hex, like
// tenant API keys. The legacy --admin-token is still honoured as the admin principal.
type Auth struct {
	adminToken string
	byHash     map[string]Principal
}

// Open reports whether no credential is configured at all (loopback dev mode).
func (a *Auth) Open() bool { return a == nil || (a.adminToken == "" && len(a.byHash) == 0) }

func NewAuth(adminToken string) *Auth {
	return &Auth{adminToken: adminToken, byHash: map[string]Principal{}}
}

type roleEntry struct {
	Principal
	TokenSHA256 string `json:"token_sha256"`
}

// LoadRoles reads a JSON array of {name, role, site?, token_sha256}.
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
		h := strings.ToLower(e.TokenSHA256)
		if len(h) != 64 {
			return fmt.Errorf("roles file: entry %d (%q): token_sha256 must be 64 hex characters", i, e.Name)
		}
		if _, err := hex.DecodeString(h); err != nil {
			return fmt.Errorf("roles file: entry %d (%q): token_sha256 is not hex", i, e.Name)
		}
		switch e.Role {
		case RoleViewer, RoleOperator, RoleAdmin:
		default:
			return fmt.Errorf("roles file: entry %d (%q): unknown role %q", i, e.Name, e.Role)
		}
		if e.Site != "" && e.Role != RoleOperator {
			return fmt.Errorf("roles file: entry %d (%q): site only applies to operators", i, e.Name)
		}
		a.byHash[h] = e.Principal
	}
	return nil
}

// Identify returns the principal for a bearer token.
func (a *Auth) Identify(token string) (Principal, bool) {
	if token == "" {
		return Principal{}, false
	}
	if a.adminToken != "" && subtle.ConstantTimeCompare([]byte(token), []byte(a.adminToken)) == 1 {
		return Principal{Name: "admin-token", Role: RoleAdmin}, true
	}
	sum := sha256.Sum256([]byte(token))
	p, ok := a.byHash[hex.EncodeToString(sum[:])]
	return p, ok
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
	if p.Role == RoleOperator && r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/api/workers/") {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/workers/"), "/")
		if len(parts) == 2 && (parts[1] == "pause" || parts[1] == "resume") {
			return p.Site == "" || siteOf(parts[0]) == p.Site
		}
	}
	return false
}
