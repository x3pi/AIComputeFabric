package gcp

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
)

// Usage is integer-valued on purpose: reconciliation must match exactly, which floats cannot promise.
// Units: tokens, GPU milliseconds, VRAM MiB-seconds, audio milliseconds, micro-kWh.
type Usage struct {
	Site             string `json:"site"`
	Tenant           string `json:"tenant"`
	Job              string `json:"job"`
	Seq              uint64 `json:"seq"` // per-site sequence number: the idempotency key together with Site
	PromptTokens     int64  `json:"prompt_tokens"`
	CompletionTokens int64  `json:"completion_tokens"`
	GPUMs            int64  `json:"gpu_ms"`
	VRAMMiBSec       int64  `json:"vram_mib_s"`
	AudioMs          int64  `json:"audio_ms"`
	MicroKWh         int64  `json:"micro_kwh"`
}

type entry struct {
	Usage
	Prev string `json:"prev"`
	Hash string `json:"hash"`
}

const genesis = "0000000000000000000000000000000000000000000000000000000000000000"

var ErrConflict = errors.New("same (site, seq) already recorded with different content")

// Ledger is append-only and hash-chained (tamper evidence, like the gateway audit log). Appending the
// same record twice is a no-op, so a site may safely resend after a partition.
type Ledger struct {
	mu      sync.Mutex
	entries []entry
	index   map[string]int
}

func NewLedger() *Ledger { return &Ledger{index: map[string]int{}} }

func key(site string, seq uint64) string { return fmt.Sprintf("%s#%d", site, seq) }

func hashOf(e entry) string {
	e.Hash = ""
	b, _ := json.Marshal(e)
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// Append returns (true, nil) when the record was new, (false, nil) for an exact duplicate.
func (l *Ledger) Append(u Usage) (bool, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if u.Site == "" || u.Tenant == "" || u.Job == "" || u.PromptTokens < 0 || u.CompletionTokens < 0 || u.GPUMs < 0 || u.VRAMMiBSec < 0 || u.AudioMs < 0 || u.MicroKWh < 0 {
		return false, errors.New("invalid usage record")
	}
	if i, ok := l.index[key(u.Site, u.Seq)]; ok {
		if l.entries[i].Usage != u {
			return false, ErrConflict
		}
		return false, nil
	}
	prev := genesis
	if n := len(l.entries); n > 0 {
		prev = l.entries[n-1].Hash
	}
	e := entry{Usage: u, Prev: prev}
	e.Hash = hashOf(e)
	l.index[key(u.Site, u.Seq)] = len(l.entries)
	l.entries = append(l.entries, e)
	return true, nil
}

func (l *Ledger) Len() int { l.mu.Lock(); defer l.mu.Unlock(); return len(l.entries) }

// Verify walks the chain and reports the first broken link.
func (l *Ledger) Verify() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	prev := genesis
	for i, e := range l.entries {
		if e.Prev != prev || hashOf(e) != e.Hash {
			return fmt.Errorf("ledger broken at entry %d", i)
		}
		prev = e.Hash
	}
	return nil
}

// Total sums usage per (tenant, site).
type Total struct {
	Tenant, Site string
	Sum          Usage
}

func (l *Ledger) Totals() []Total {
	l.mu.Lock()
	defer l.mu.Unlock()
	m := map[[2]string]*Usage{}
	for _, e := range l.entries {
		k := [2]string{e.Tenant, e.Site}
		if m[k] == nil {
			m[k] = &Usage{}
		}
		s := m[k]
		s.PromptTokens += e.PromptTokens
		s.CompletionTokens += e.CompletionTokens
		s.GPUMs += e.GPUMs
		s.VRAMMiBSec += e.VRAMMiBSec
		s.AudioMs += e.AudioMs
		s.MicroKWh += e.MicroKWh
	}
	var out []Total
	for k, v := range m {
		out = append(out, Total{k[0], k[1], *v})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Tenant != out[j].Tenant {
			return out[i].Tenant < out[j].Tenant
		}
		return out[i].Site < out[j].Site
	})
	return out
}

// Records returns a copy of the usage records (without chain fields), in append order.
func (l *Ledger) Records() []Usage {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Usage, len(l.entries))
	for i, e := range l.entries {
		out[i] = e.Usage
	}
	return out
}

// Diff compares two ledgers record by record and returns the keys missing from or different in `other`.
func (l *Ledger) Diff(other *Ledger) (missing, different []string) {
	for _, u := range l.Records() {
		other.mu.Lock()
		i, ok := other.index[key(u.Site, u.Seq)]
		var got Usage
		if ok {
			got = other.entries[i].Usage
		}
		other.mu.Unlock()
		if !ok {
			missing = append(missing, key(u.Site, u.Seq))
		} else if got != u {
			different = append(different, key(u.Site, u.Seq))
		}
	}
	return
}
