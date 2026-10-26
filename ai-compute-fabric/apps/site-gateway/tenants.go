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
	"sync"
)

// Tenant is one organisational user of the inference service (a department, an app).
// Only the SHA-256 of its API key is stored: a lost tenants.json does not leak keys.
type Tenant struct {
	ID          string  `json:"id"`
	Name        string  `json:"name"`
	KeyHash     string  `json:"key_hash"`
	Weight      float64 `json:"weight"`       // share of the GPUs when contended (default 1)
	MaxInflight int     `json:"max_inflight"` // concurrent requests, 0 = gateway default
	Disabled    bool    `json:"disabled"`
}

// TenantView is the API shape: never contains the key hash.
type TenantView struct {
	ID          string  `json:"id"`
	Name        string  `json:"name"`
	Weight      float64 `json:"weight"`
	MaxInflight int     `json:"max_inflight"`
	Disabled    bool    `json:"disabled"`
}

var (
	ErrNoTenant   = errors.New("unknown tenant")
	ErrBadTenant  = errors.New("invalid tenant")
	ErrDuplicated = errors.New("tenant name already exists")
)

// TenantStore keeps tenants in memory and, when Path is set, in a JSON file written atomically.
type TenantStore struct {
	Path string

	mu     sync.RWMutex
	byID   map[string]*Tenant
	byHash map[string]*Tenant
}

func NewTenantStore(path string) (*TenantStore, error) {
	s := &TenantStore{Path: path, byID: map[string]*Tenant{}, byHash: map[string]*Tenant{}}
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
	return TenantView{ID: t.ID, Name: t.Name, Weight: t.Weight, MaxInflight: t.MaxInflight, Disabled: t.Disabled}
}

// Authenticate maps an API key to its tenant; disabled tenants and unknown keys fail.
func (s *TenantStore) Authenticate(key string) (TenantView, bool) {
	if key == "" {
		return TenantView{}, false
	}
	h := hashKey(key)
	s.mu.RLock()
	defer s.mu.RUnlock()
	t, ok := s.byHash[h]
	if !ok || t.Disabled || subtle.ConstantTimeCompare([]byte(t.KeyHash), []byte(h)) != 1 {
		return TenantView{}, false
	}
	return view(t), true
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
