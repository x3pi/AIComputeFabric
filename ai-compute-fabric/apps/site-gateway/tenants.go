package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/argon2"
)

const (
	MinPasswordLen          = 12
	argon2Time       uint32 = 2
	argon2MemoryKiB  uint32 = 19 * 1024 // OWASP minimum 19 MiB for Argon2id (t=2, p=1)
	argon2Threads    uint8  = 1
	argon2KeyLen     uint32 = 32
	defaultMaxTTL           = 24 * time.Hour
	defaultIdleTTL          = 2 * time.Hour
	defaultLockout          = 30 * time.Second
	maxLoginFailures        = 5
)

var commonWeakPasswords = map[string]bool{
	"password1234":  true,
	"password12345": true,
	"123456789012":  true,
	"1234567890123": true,
	"qwerty123456":  true,
	"qwertyuiop12":  true,
	"admin1234567":  true,
	"administrator": true,
	"changeme1234":  true,
	"welcome12345":  true,
	"letmein12345":  true,
	"iloveyou1234":  true,
}

// Tenant is one organisational user of the inference service (a department, an app).
// Only the SHA-256 of its API key and salted Argon2id password hash are stored: a lost tenants.json does not leak keys or passwords.
type Tenant struct {
	ID          string  `json:"id"`
	Name        string  `json:"name"`
	Email       string  `json:"email,omitempty"`
	PassSalt    string  `json:"pass_salt,omitempty"` // legacy v1 salt; cleared once migrated to argon2id
	PassHash    string  `json:"pass_hash,omitempty"`
	KeyHash     string  `json:"key_hash"`
	Weight      float64 `json:"weight"`       // share of the GPUs when contended (default 1)
	MaxInflight int     `json:"max_inflight"` // concurrent requests, 0 = gateway default
	Disabled    bool    `json:"disabled"`
	Pending     bool    `json:"pending,omitempty"` // true when created via public signup awaiting admin approval
}

// TenantView is the API shape: never contains the key hash or password hash.
type TenantView struct {
	ID          string  `json:"id"`
	Name        string  `json:"name"`
	Email       string  `json:"email,omitempty"`
	HasPassword bool    `json:"has_password,omitempty"`
	Weight      float64 `json:"weight"`
	MaxInflight int     `json:"max_inflight"`
	Disabled    bool    `json:"disabled"`
	Pending     bool    `json:"pending,omitempty"`
}

// SessionRecord tracks an active web session (`aicf_sess_...`) by its SHA-256 hash only.
type SessionRecord struct {
	ID          string        `json:"id"`
	TokenHash   string        `json:"token_hash"`
	TenantID    string        `json:"tenant_id"`
	CreatedAt   time.Time     `json:"created_at"`
	LastActive  time.Time     `json:"last_active"`
	ExpiresAt   time.Time     `json:"expires_at"`
	IdleTimeout time.Duration `json:"idle_timeout"`
	RemoteIP    string        `json:"remote_ip,omitempty"`
}

// SessionView is returned when listing a tenant's active sessions; never contains the token or its hash.
type SessionView struct {
	ID         string `json:"id"`
	TenantID   string `json:"tenant_id"`
	CreatedAt  string `json:"created_at"`
	LastActive string `json:"last_active"`
	ExpiresAt  string `json:"expires_at"`
	RemoteIP   string `json:"remote_ip,omitempty"`
}

type loginAttemptState struct {
	failures    int
	strikes     int
	firstFailed time.Time
	lockedUntil time.Time
}

type rateBucket struct {
	tokens float64
	last   time.Time
}

// SignupRateLimiter bounds public signup/register requests both globally and per client IP.
type SignupRateLimiter struct {
	mu         sync.Mutex
	perIPRate  float64
	perIPBurst float64
	globRate   float64
	globBurst  float64
	global     rateBucket
	byIP       map[string]*rateBucket
	now        func() time.Time
}

func NewSignupRateLimiter(perIPPerMin, globalPerMin int) *SignupRateLimiter {
	if perIPPerMin <= 0 {
		perIPPerMin = 5
	}
	if globalPerMin <= 0 {
		globalPerMin = 20
	}
	now := time.Now
	return &SignupRateLimiter{
		perIPRate:  float64(perIPPerMin) / 60.0,
		perIPBurst: float64(perIPPerMin),
		globRate:   float64(globalPerMin) / 60.0,
		globBurst:  float64(globalPerMin),
		global:     rateBucket{tokens: float64(globalPerMin), last: now()},
		byIP:       map[string]*rateBucket{},
		now:        now,
	}
}

func (l *SignupRateLimiter) take(b *rateBucket, rate, burst float64, now time.Time) bool {
	elapsed := now.Sub(b.last).Seconds()
	if elapsed > 0 {
		b.tokens += elapsed * rate
		if b.tokens > burst {
			b.tokens = burst
		}
		b.last = now
	}
	if b.tokens < 1.0 {
		return false
	}
	b.tokens -= 1.0
	return true
}

func normalizeRemoteIP(remoteAddr string) string {
	host, _, err := net.SplitHostPort(strings.TrimSpace(remoteAddr))
	if err == nil && host != "" {
		return host
	}
	if remoteAddr == "" {
		return "unknown"
	}
	return remoteAddr
}

func (l *SignupRateLimiter) Allow(remoteAddr string) bool {
	if l == nil {
		return true
	}
	ip := normalizeRemoteIP(remoteAddr)
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	b, ok := l.byIP[ip]
	if !ok {
		if len(l.byIP) > 4096 {
			// Evict stale IP buckets to bound memory.
			for k, v := range l.byIP {
				if now.Sub(v.last) > 10*time.Minute {
					delete(l.byIP, k)
				}
			}
		}
		b = &rateBucket{tokens: l.perIPBurst, last: now}
		l.byIP[ip] = b
	}
	if !l.take(b, l.perIPRate, l.perIPBurst, now) {
		return false
	}
	if !l.take(&l.global, l.globRate, l.globBurst, now) {
		b.tokens += 1.0 // refund IP token if global bucket rejected
		return false
	}
	return true
}

var (
	ErrNoTenant      = errors.New("unknown tenant")
	ErrBadTenant     = errors.New("invalid tenant")
	ErrWeakPassword  = errors.New("password must be at least 12 characters and not a common password")
	ErrDuplicated    = errors.New("tenant name already exists")
	ErrBadCreds      = errors.New("invalid email/username or password")
	ErrAccountLocked = errors.New("too many failed login attempts; account temporarily locked")
	dummySalt        = []byte("aicf-constant-time-dummy-salt-32")
)

// TenantStore keeps tenants in memory and, when Path is set, in a JSON file written atomically with fsync.
type TenantStore struct {
	Path              string
	AllowSignup       bool
	SignupAutoApprove bool
	SessionMaxTTL     time.Duration
	SessionIdleTTL    time.Duration
	LockoutDuration   time.Duration
	SignupLimiter     *SignupRateLimiter
	Now               func() time.Time

	mu            sync.RWMutex
	byID          map[string]*Tenant
	byHash        map[string]*Tenant
	bySession     map[string]*SessionRecord
	loginAttempts map[string]*loginAttemptState
}

func NewTenantStore(path string) (*TenantStore, error) {
	s := &TenantStore{
		Path:            path,
		SessionMaxTTL:   defaultMaxTTL,
		SessionIdleTTL:  defaultIdleTTL,
		LockoutDuration: defaultLockout,
		SignupLimiter:   NewSignupRateLimiter(5, 20),
		Now:             time.Now,
		byID:            map[string]*Tenant{},
		byHash:          map[string]*Tenant{},
		bySession:       map[string]*SessionRecord{},
		loginAttempts:   map[string]*loginAttemptState{},
	}
	if path == "" {
		return s, nil
	}
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	var list []Tenant
	if err := json.Unmarshal(b, &list); err != nil {
		return nil, err
	}
	for i := range list {
		t := list[i]
		s.byID[t.ID] = &t
		s.byHash[t.KeyHash] = &t
	}
	if sb, serr := os.ReadFile(path + ".sessions"); serr == nil {
		var slist []SessionRecord
		if json.Unmarshal(sb, &slist) == nil {
			now := s.now()
			for i := range slist {
				rec := slist[i]
				if rec.TokenHash != "" && s.byID[rec.TenantID] != nil && now.Before(rec.ExpiresAt) {
					s.bySession[rec.TokenHash] = &rec
				}
			}
		}
	}
	return s, nil
}

func (s *TenantStore) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func hashKey(key string) string {
	h := sha256.Sum256([]byte(key))
	return hex.EncodeToString(h[:])
}

// writeFileAtomic writes data to path via a temporary file (path + ".tmp"), fsyncs the file, renames it atomically, and fsyncs the directory.
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, perm)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if df, err := os.Open(dir); err == nil {
		_ = df.Sync()
		_ = df.Close()
	}
	return nil
}

func (s *TenantStore) saveSessionsLocked() error {
	if s.Path == "" {
		return nil
	}
	now := s.now()
	list := make([]SessionRecord, 0, len(s.bySession))
	for _, sess := range s.bySession {
		if now.Before(sess.ExpiresAt) {
			list = append(list, *sess)
		}
	}
	sort.Slice(list, func(i, j int) bool { return list[i].ID < list[j].ID })
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(s.Path+".sessions", b, 0o600)
}

func (s *TenantStore) saveLocked() error {
	if s.Path == "" {
		return nil
	}
	list := make([]Tenant, 0, len(s.byID))
	for _, t := range s.byID {
		list = append(list, *t)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].ID < list[j].ID })
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(s.Path, b, 0o600)
}

func newSecret(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err) // no entropy: nothing safe to do
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// Create adds a tenant and returns its API key. The key is shown exactly once.
func (s *TenantStore) Create(name string, weight float64, maxInflight int) (TenantView, string, error) {
	return s.CreateWithPending(name, weight, maxInflight, false)
}

// CreateWithPending adds a tenant with an explicit pending approval state.
func (s *TenantStore) CreateWithPending(name string, weight float64, maxInflight int, pending bool) (TenantView, string, error) {
	if name == "" || len(name) > 64 || weight < 0 || maxInflight < 0 {
		return TenantView{}, "", ErrBadTenant
	}
	if weight == 0 {
		weight = 1
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.byID {
		if t.Name == name {
			return TenantView{}, "", ErrDuplicated
		}
	}
	key := "aicf_" + newSecret(24)
	t := &Tenant{ID: "t_" + newSecret(6), Name: name, KeyHash: hashKey(key), Weight: weight, MaxInflight: maxInflight, Pending: pending}
	s.byID[t.ID], s.byHash[t.KeyHash] = t, t
	if err := s.saveLocked(); err != nil {
		delete(s.byID, t.ID)
		delete(s.byHash, t.KeyHash)
		return TenantView{}, "", err
	}
	return view(t), key, nil
}

func view(t *Tenant) TenantView {
	return TenantView{ID: t.ID, Name: t.Name, Email: t.Email, HasPassword: t.PassHash != "", Weight: t.Weight, MaxInflight: t.MaxInflight, Disabled: t.Disabled, Pending: t.Pending}
}

func ValidatePasswordStrength(password string) error {
	if len(password) < MinPasswordLen || len(password) > 256 {
		return ErrWeakPassword
	}
	lower := strings.ToLower(strings.TrimSpace(password))
	if commonWeakPasswords[lower] {
		return ErrWeakPassword
	}
	allSame := true
	for i := 1; i < len(password); i++ {
		if password[i] != password[0] {
			allSame = false
			break
		}
	}
	if allSame {
		return ErrWeakPassword
	}
	return nil
}

func hashPasswordArgon2id(password string) string {
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		panic(err)
	}
	dk := argon2.IDKey([]byte(password), salt, argon2Time, argon2MemoryKiB, argon2Threads, argon2KeyLen)
	return fmt.Sprintf("argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version,
		argon2MemoryKiB,
		argon2Time,
		argon2Threads,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(dk),
	)
}

func hashPasswordLegacy(salt, password string) string {
	h := sha256.Sum256([]byte("aicf-pw-v1:" + salt + ":" + password))
	for i := 0; i < 4096; i++ {
		h = sha256.Sum256(append(h[:], []byte(salt)...))
	}
	return hex.EncodeToString(h[:])
}

func verifyPasswordHash(storedHash, legacySalt, password string) (ok bool, needsUpgrade bool) {
	if strings.HasPrefix(storedHash, "argon2id$") {
		parts := strings.Split(storedHash, "$")
		if len(parts) != 5 {
			return false, false
		}
		var mem, t uint32
		var p uint8
		if _, err := fmt.Sscanf(parts[2], "m=%d,t=%d,p=%d", &mem, &t, &p); err != nil || mem == 0 || t == 0 || p == 0 {
			return false, false
		}
		salt, err1 := base64.RawStdEncoding.DecodeString(parts[3])
		want, err2 := base64.RawStdEncoding.DecodeString(parts[4])
		if err1 != nil || err2 != nil || len(want) == 0 {
			return false, false
		}
		got := argon2.IDKey([]byte(password), salt, t, mem, p, uint32(len(want)))
		return subtle.ConstantTimeCompare(got, want) == 1, false
	}
	if storedHash == "" {
		return false, false
	}
	expected := hashPasswordLegacy(legacySalt, password)
	if subtle.ConstantTimeCompare([]byte(storedHash), []byte(expected)) == 1 {
		return true, true
	}
	return false, false
}

func dummyVerifyPassword(password string) {
	got := argon2.IDKey([]byte(password), dummySalt[:16], argon2Time, argon2MemoryKiB, argon2Threads, argon2KeyLen)
	_ = subtle.ConstantTimeCompare(got, dummySalt[:32])
}

// RegisterAccount creates a Cloud Developer account with Email + Password and issues its initial API key.
func (s *TenantStore) RegisterAccount(name, email, password string) (TenantView, string, error) {
	return s.RegisterAccountWithPending(name, email, password, false)
}

// RegisterAccountWithPending creates a Cloud Developer account with an explicit pending state.
func (s *TenantStore) RegisterAccountWithPending(name, email, password string, pending bool) (TenantView, string, error) {
	name = strings.TrimSpace(name)
	email = strings.ToLower(strings.TrimSpace(email))
	if name == "" && email != "" {
		name = strings.Split(email, "@")[0]
	}
	if name == "" || len(name) > 64 {
		return TenantView{}, "", ErrBadTenant
	}
	if err := ValidatePasswordStrength(password); err != nil {
		return TenantView{}, "", err
	}
	pwHash := hashPasswordArgon2id(password)
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.byID {
		if strings.EqualFold(t.Name, name) || (email != "" && strings.EqualFold(t.Email, email)) {
			return TenantView{}, "", ErrDuplicated
		}
	}
	key := "aicf_" + newSecret(24)
	maxInflight := 8
	if pending {
		maxInflight = 1
	}
	t := &Tenant{
		ID:          "t_" + newSecret(6),
		Name:        name,
		Email:       email,
		PassHash:    pwHash,
		KeyHash:     hashKey(key),
		Weight:      1,
		MaxInflight: maxInflight,
		Pending:     pending,
	}
	s.byID[t.ID], s.byHash[t.KeyHash] = t, t
	if err := s.saveLocked(); err != nil {
		delete(s.byID, t.ID)
		delete(s.byHash, t.KeyHash)
		return TenantView{}, "", err
	}
	return view(t), key, nil
}

func (s *TenantStore) checkLockedOutLocked(key string, now time.Time) bool {
	st, ok := s.loginAttempts[key]
	if !ok {
		return false
	}
	if !st.lockedUntil.IsZero() && now.Before(st.lockedUntil) {
		return true
	}
	return false
}

func (s *TenantStore) recordLoginFailureLocked(key string, now time.Time) {
	st, ok := s.loginAttempts[key]
	if !ok {
		st = &loginAttemptState{firstFailed: now}
		s.loginAttempts[key] = st
	}
	if now.Sub(st.firstFailed) > 10*time.Minute {
		st.failures = 0
		st.firstFailed = now
	}
	st.failures++
	if st.failures >= maxLoginFailures {
		base := s.LockoutDuration
		if base <= 0 {
			base = defaultLockout
		}
		mult := 1 << min(st.strikes, 5)
		st.lockedUntil = now.Add(time.Duration(mult) * base)
		st.strikes++
		st.failures = 0
	}
}

func (s *TenantStore) clearLoginFailuresLocked(key string) {
	delete(s.loginAttempts, key)
}

// LoginAccount verifies Email/Username + Password and returns a revocable session token (`aicf_sess_...`) without invalidating the CLI API key.
func (s *TenantStore) LoginAccount(identifier, password string) (TenantView, string, error) {
	return s.LoginAccountFrom(identifier, password, "")
}

// LoginAccountFrom verifies Email/Username + Password with per-(account, IP) brute-force lockout and constant-time non-existent account handling.
func (s *TenantStore) LoginAccountFrom(identifier, password, remoteAddr string) (TenantView, string, error) {
	ident := strings.TrimSpace(identifier)
	if ident == "" || password == "" {
		dummyVerifyPassword(password)
		return TenantView{}, "", ErrBadCreds
	}
	acctKey := "acct:" + strings.ToLower(ident)
	ipKey := "ip:" + normalizeRemoteIP(remoteAddr)

	s.mu.Lock()
	now := s.now()
	if s.checkLockedOutLocked(acctKey, now) || (remoteAddr != "" && s.checkLockedOutLocked(ipKey, now)) {
		s.mu.Unlock()
		dummyVerifyPassword(password)
		return TenantView{}, "", ErrAccountLocked
	}
	var found *Tenant
	for _, t := range s.byID {
		if strings.EqualFold(t.Email, ident) || strings.EqualFold(t.Name, ident) || t.ID == ident {
			found = t
			break
		}
	}
	if found == nil || found.Disabled || found.PassHash == "" {
		s.recordLoginFailureLocked(acctKey, now)
		if remoteAddr != "" {
			s.recordLoginFailureLocked(ipKey, now)
		}
		s.mu.Unlock()
		dummyVerifyPassword(password)
		return TenantView{}, "", ErrBadCreds
	}
	storedHash, legacySalt := found.PassHash, found.PassSalt
	s.mu.Unlock()

	ok, needsUpgrade := verifyPasswordHash(storedHash, legacySalt, password)

	s.mu.Lock()
	defer s.mu.Unlock()
	now = s.now()
	if !ok {
		s.recordLoginFailureLocked(acctKey, now)
		if remoteAddr != "" {
			s.recordLoginFailureLocked(ipKey, now)
		}
		return TenantView{}, "", ErrBadCreds
	}
	s.clearLoginFailuresLocked(acctKey)
	if remoteAddr != "" {
		s.clearLoginFailuresLocked(ipKey)
	}
	if needsUpgrade {
		found.PassHash = hashPasswordArgon2id(password)
		found.PassSalt = ""
		_ = s.saveLocked()
	}
	maxTTL := s.SessionMaxTTL
	if maxTTL <= 0 {
		maxTTL = defaultMaxTTL
	}
	idleTTL := s.SessionIdleTTL
	if idleTTL <= 0 {
		idleTTL = defaultIdleTTL
	}
	sessToken := "aicf_sess_" + newSecret(24)
	tokHash := hashKey(sessToken)
	s.bySession[tokHash] = &SessionRecord{
		ID:          "sess_" + newSecret(6),
		TokenHash:   tokHash,
		TenantID:    found.ID,
		CreatedAt:   now,
		LastActive:  now,
		ExpiresAt:   now.Add(maxTTL),
		IdleTimeout: idleTTL,
		RemoteIP:    normalizeRemoteIP(remoteAddr),
	}
	_ = s.saveSessionsLocked()
	return view(found), sessToken, nil
}

// ChangePassword updates a tenant's password to Argon2id and revokes all active web sessions.
func (s *TenantStore) ChangePassword(tenantID, newPassword string) error {
	return s.VerifyAndChangePassword(tenantID, "", newPassword)
}

// VerifyAndChangePassword verifies currentPassword (when non-empty and tenant already has a password)
// before updating to newPassword with Argon2id and revoking all active web sessions.
func (s *TenantStore) VerifyAndChangePassword(tenantID, currentPassword, newPassword string) error {
	if err := ValidatePasswordStrength(newPassword); err != nil {
		return err
	}
	s.mu.RLock()
	t := s.byID[tenantID]
	var storedHash, storedSalt string
	if t != nil {
		storedHash, storedSalt = t.PassHash, t.PassSalt
	}
	s.mu.RUnlock()
	if t == nil {
		dummyVerifyPassword(currentPassword)
		return ErrNoTenant
	}
	if currentPassword != "" && storedHash != "" {
		if ok, _ := verifyPasswordHash(storedHash, storedSalt, currentPassword); !ok {
			return ErrBadCreds
		}
	}
	newHash := hashPasswordArgon2id(newPassword)
	s.mu.Lock()
	defer s.mu.Unlock()
	t = s.byID[tenantID]
	if t == nil {
		return ErrNoTenant
	}
	oldHash, oldSalt := t.PassHash, t.PassSalt
	t.PassHash = newHash
	t.PassSalt = ""
	if err := s.saveLocked(); err != nil {
		t.PassHash, t.PassSalt = oldHash, oldSalt
		return err
	}
	s.revokeTenantSessionsLocked(tenantID)
	return nil
}

// SetCredentials updates a tenant's email and/or password (used by Admin provisioning/reset).
func (s *TenantStore) SetCredentials(id, email, password string) (TenantView, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	var pwHash string
	if password != "" {
		if err := ValidatePasswordStrength(password); err != nil {
			return TenantView{}, err
		}
		pwHash = hashPasswordArgon2id(password)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	t := s.byID[id]
	if t == nil {
		return TenantView{}, ErrNoTenant
	}
	if email != "" {
		for _, other := range s.byID {
			if other.ID != id && strings.EqualFold(other.Email, email) {
				return TenantView{}, ErrDuplicated
			}
		}
	}
	old := *t
	if email != "" {
		t.Email = email
	}
	if pwHash != "" {
		t.PassHash = pwHash
		t.PassSalt = ""
	}
	if err := s.saveLocked(); err != nil {
		*t = old
		return TenantView{}, err
	}
	if pwHash != "" {
		s.revokeTenantSessionsLocked(id)
	}
	return view(t), nil
}

func (s *TenantStore) revokeTenantSessionsLocked(tenantID string) int {
	n := 0
	for h, sess := range s.bySession {
		if sess.TenantID == tenantID {
			delete(s.bySession, h)
			n++
		}
	}
	if n > 0 {
		_ = s.saveSessionsLocked()
	}
	return n
}

// RevokeAllSessions revokes every active web session belonging to tenantID.
func (s *TenantStore) RevokeAllSessions(tenantID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.revokeTenantSessionsLocked(tenantID)
}

// ListSessions returns active non-expired sessions for tenantID.
func (s *TenantStore) ListSessions(tenantID string) []SessionView {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	out := []SessionView{}
	for h, sess := range s.bySession {
		if now.After(sess.ExpiresAt) || (sess.IdleTimeout > 0 && now.Sub(sess.LastActive) > sess.IdleTimeout) {
			delete(s.bySession, h)
			continue
		}
		if tenantID == "" || sess.TenantID == tenantID {
			out = append(out, SessionView{
				ID:         sess.ID,
				TenantID:   sess.TenantID,
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

// RevokeSessionByID revokes a specific session by its public ID (`sess_...`).
func (s *TenantStore) RevokeSessionByID(tenantID, sessionID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for h, sess := range s.bySession {
		if sess.ID == sessionID && (tenantID == "" || sess.TenantID == tenantID) {
			delete(s.bySession, h)
			_ = s.saveSessionsLocked()
			return true
		}
	}
	return false
}

// LogoutSession revokes a web session token (`aicf_sess_...`) immediately.
func (s *TenantStore) LogoutSession(token string) bool {
	if token == "" {
		return false
	}
	h := hashKey(token)
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.bySession[h]; ok {
		delete(s.bySession, h)
		_ = s.saveSessionsLocked()
		return true
	}
	return false
}

// Authenticate maps an API key or web session token to its tenant; disabled tenants, expired sessions and unknown keys fail.
func (s *TenantStore) Authenticate(key string) (TenantView, bool) {
	if key == "" {
		return TenantView{}, false
	}
	h := hashKey(key)
	s.mu.Lock()
	defer s.mu.Unlock()
	if t, ok := s.byHash[h]; ok {
		if !t.Disabled && subtle.ConstantTimeCompare([]byte(t.KeyHash), []byte(h)) == 1 {
			return view(t), true
		}
	}
	if sess, ok := s.bySession[h]; ok {
		now := s.now()
		if now.After(sess.ExpiresAt) || (sess.IdleTimeout > 0 && now.Sub(sess.LastActive) > sess.IdleTimeout) {
			delete(s.bySession, h)
			return TenantView{}, false
		}
		t := s.byID[sess.TenantID]
		if t != nil && !t.Disabled && subtle.ConstantTimeCompare([]byte(sess.TokenHash), []byte(h)) == 1 {
			sess.LastActive = now
			return view(t), true
		}
		delete(s.bySession, h)
	}
	return TenantView{}, false
}

func (s *TenantStore) List() []TenantView {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]TenantView, 0, len(s.byID))
	for _, t := range s.byID {
		out = append(out, view(t))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Update changes weight / cap / disabled; nil fields are left alone.
func (s *TenantStore) Update(id string, weight *float64, maxInflight *int, disabled *bool) (TenantView, error) {
	return s.UpdateWithPending(id, weight, maxInflight, disabled, nil)
}

// UpdateWithPending changes weight / cap / disabled / pending; nil fields are left alone.
func (s *TenantStore) UpdateWithPending(id string, weight *float64, maxInflight *int, disabled, pending *bool) (TenantView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := s.byID[id]
	if t == nil {
		return TenantView{}, ErrNoTenant
	}
	if (weight != nil && *weight <= 0) || (maxInflight != nil && *maxInflight < 0) {
		return TenantView{}, ErrBadTenant
	}
	old := *t
	if weight != nil {
		t.Weight = *weight
	}
	if maxInflight != nil {
		t.MaxInflight = *maxInflight
	}
	if disabled != nil {
		t.Disabled = *disabled
	}
	if pending != nil {
		t.Pending = *pending
	}
	if err := s.saveLocked(); err != nil {
		*t = old
		return TenantView{}, err
	}
	if t.Disabled {
		s.revokeTenantSessionsLocked(id)
	}
	return view(t), nil
}

// RotateKey issues a new key, invalidates the old one, and revokes all active web sessions for the tenant.
func (s *TenantStore) RotateKey(id string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := s.byID[id]
	if t == nil {
		return "", ErrNoTenant
	}
	key := "aicf_" + newSecret(24)
	delete(s.byHash, t.KeyHash)
	oldHash := t.KeyHash
	t.KeyHash = hashKey(key)
	s.byHash[t.KeyHash] = t
	if err := s.saveLocked(); err != nil {
		delete(s.byHash, t.KeyHash)
		t.KeyHash = oldHash
		s.byHash[oldHash] = t
		return "", err
	}
	s.revokeTenantSessionsLocked(id)
	return key, nil
}

// Delete removes a tenant for good: its API key and sessions stop authenticating at once and it is gone from the file.
func (s *TenantStore) Delete(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := s.byID[id]
	if t == nil {
		return ErrNoTenant
	}
	delete(s.byID, id)
	delete(s.byHash, t.KeyHash)
	if err := s.saveLocked(); err != nil {
		s.byID[id], s.byHash[t.KeyHash] = t, t
		return err
	}
	s.revokeTenantSessionsLocked(id)
	return nil
}

// Policy gives the router a tenant's weight and concurrency cap.
func (s *TenantStore) Policy(id string) (weight float64, maxInflight int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if t := s.byID[id]; t != nil {
		return t.Weight, t.MaxInflight
	}
	return 1, 0
}
