package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"sort"
	"strings"
	"sync"
)

// Tenant is one organisational user of the inference service (a department, an app).
// Only the SHA-256 of its API key and salted password hash are stored: a lost tenants.json does not leak keys or passwords.
type Tenant struct {
	ID          string  `json:"id"`
	Name        string  `json:"name"`
	Email       string  `json:"email,omitempty"`
	PassSalt    string  `json:"pass_salt,omitempty"`
	PassHash    string  `json:"pass_hash,omitempty"`
	KeyHash     string  `json:"key_hash"`
	Weight      float64 `json:"weight"`       // share of the GPUs when contended (default 1)
	MaxInflight int     `json:"max_inflight"` // concurrent requests, 0 = gateway default
	Disabled    bool    `json:"disabled"`
}

// TenantView is the API shape: never contains the key hash or password hash.
type TenantView struct {
	ID          string  `json:"id"`
	Name        string  `json:"name"`
	Email       string  `json:"email,omitempty"`
	Weight      float64 `json:"weight"`
	MaxInflight int     `json:"max_inflight"`
	Disabled    bool    `json:"disabled"`
}

var (
	ErrNoTenant   = errors.New("unknown tenant")
	ErrBadTenant  = errors.New("invalid tenant")
	ErrDuplicated = errors.New("tenant name already exists")
	ErrBadCreds   = errors.New("invalid email/username or password")
)

// TenantStore keeps tenants in memory and, when Path is set, in a JSON file written atomically.
type TenantStore struct {
	Path string

	mu        sync.RWMutex
	byID      map[string]*Tenant
	byHash    map[string]*Tenant
	bySession map[string]*Tenant // revocable web session tokens (aicf_sess_...)
}

func NewTenantStore(path string) (*TenantStore, error) {
	s := &TenantStore{
		Path:      path,
		byID:      map[string]*Tenant{},
		byHash:    map[string]*Tenant{},
		bySession: map[string]*Tenant{},
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
	return s, nil
}

func hashKey(key string) string {
	h := sha256.Sum256([]byte(key))
	return hex.EncodeToString(h[:])
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
	b, _ := json.MarshalIndent(list, "", "  ")
	tmp := s.Path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.Path)
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
	t := &Tenant{ID: "t_" + newSecret(6), Name: name, KeyHash: hashKey(key), Weight: weight, MaxInflight: maxInflight}
	s.byID[t.ID], s.byHash[t.KeyHash] = t, t
	if err := s.saveLocked(); err != nil {
		delete(s.byID, t.ID)
		delete(s.byHash, t.KeyHash)
		return TenantView{}, "", err
	}
	return view(t), key, nil
}

func view(t *Tenant) TenantView {
	return TenantView{ID: t.ID, Name: t.Name, Email: t.Email, Weight: t.Weight, MaxInflight: t.MaxInflight, Disabled: t.Disabled}
}

func hashPassword(salt, password string) string {
	h := sha256.Sum256([]byte("aicf-pw-v1:" + salt + ":" + password))
	for i := 0; i < 4096; i++ {
		h = sha256.Sum256(append(h[:], []byte(salt)...))
	}
	return hex.EncodeToString(h[:])
}

// RegisterAccount creates a Cloud Developer account with Email + Password and issues its initial API key.
func (s *TenantStore) RegisterAccount(name, email, password string) (TenantView, string, error) {
	name = strings.TrimSpace(name)
	email = strings.ToLower(strings.TrimSpace(email))
	if name == "" && email != "" {
		name = strings.Split(email, "@")[0]
	}
	if name == "" || len(name) > 64 || len(password) < 6 {
		return TenantView{}, "", ErrBadTenant
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.byID {
		if strings.EqualFold(t.Name, name) || (email != "" && strings.EqualFold(t.Email, email)) {
			return TenantView{}, "", ErrDuplicated
		}
	}
	salt := newSecret(12)
	pwHash := hashPassword(salt, password)
	key := "aicf_" + newSecret(24)
	t := &Tenant{
		ID:          "t_" + newSecret(6),
		Name:        name,
		Email:       email,
		PassSalt:    salt,
		PassHash:    pwHash,
		KeyHash:     hashKey(key),
		Weight:      1,
		MaxInflight: 8,
	}
	s.byID[t.ID], s.byHash[t.KeyHash] = t, t
	if err := s.saveLocked(); err != nil {
		delete(s.byID, t.ID)
		delete(s.byHash, t.KeyHash)
		return TenantView{}, "", err
	}
	return view(t), key, nil
}

// LoginAccount verifies Email/Username + Password and returns a revocable session token (`aicf_sess_...`) without invalidating the CLI API key.
func (s *TenantStore) LoginAccount(identifier, password string) (TenantView, string, error) {
	ident := strings.TrimSpace(identifier)
	if ident == "" || password == "" {
		return TenantView{}, "", ErrBadCreds
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var found *Tenant
	for _, t := range s.byID {
		if strings.EqualFold(t.Email, ident) || strings.EqualFold(t.Name, ident) || t.ID == ident {
			found = t
			break
		}
	}
	if found == nil || found.Disabled || found.PassHash == "" {
		return TenantView{}, "", ErrBadCreds
	}
	expected := hashPassword(found.PassSalt, password)
	if subtle.ConstantTimeCompare([]byte(found.PassHash), []byte(expected)) != 1 {
		return TenantView{}, "", ErrBadCreds
	}
	sessToken := "aicf_sess_" + newSecret(24)
	s.bySession[hashKey(sessToken)] = found
	return view(found), sessToken, nil
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
		return true
	}
	return false
}

// Authenticate maps an API key or web session token to its tenant; disabled tenants and unknown keys fail.
func (s *TenantStore) Authenticate(key string) (TenantView, bool) {
	if key == "" {
		return TenantView{}, false
	}
	h := hashKey(key)
	s.mu.RLock()
	defer s.mu.RUnlock()
	if t, ok := s.byHash[h]; ok {
		if !t.Disabled && subtle.ConstantTimeCompare([]byte(t.KeyHash), []byte(h)) == 1 {
			return view(t), true
		}
	}
	if t, ok := s.bySession[h]; ok && !t.Disabled {
		return view(t), true
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
	if err := s.saveLocked(); err != nil {
		*t = old
		return TenantView{}, err
	}
	return view(t), nil
}

// RotateKey issues a new key and invalidates the old one.
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
	return key, nil
}

// Delete removes a tenant for good: its API key stops authenticating at once (requests already in flight finish) and it is
// gone from the file. Use Update(disabled) when the tenant may come back.
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
