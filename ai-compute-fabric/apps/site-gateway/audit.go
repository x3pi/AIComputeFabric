package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"
)

// AuditLog is an append-only JSON-lines file in which every entry carries the hash of the
// previous one, so editing, deleting or reordering an earlier line is detected by Verify.
// This is tamper EVIDENCE, not tamper PREVENTION: anyone who can rewrite the whole file can
// recompute the chain. Ship the latest hash somewhere else to close that gap.
type AuditEntry struct {
	Seq    uint64 `json:"seq"`
	Time   string `json:"time"`
	Actor  string `json:"actor"`
	Role   string `json:"role"`
	Method string `json:"method"`
	Path   string `json:"path"`
	Status int    `json:"status"`
	Remote string `json:"remote,omitempty"`
	Prev   string `json:"prev"`
	Hash   string `json:"hash"`
}

type AuditLog struct {
	mu   sync.Mutex
	f    *os.File
	seq  uint64
	last string
}

const auditGenesis = "0000000000000000000000000000000000000000000000000000000000000000"

func entryHash(e AuditEntry) string {
	e.Hash = ""
	b, _ := json.Marshal(e)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// OpenAudit opens (creating if needed) the log and continues the chain; it refuses a log whose
// existing chain is already broken.
func OpenAudit(path string) (*AuditLog, error) {
	n, last, err := VerifyAudit(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("audit log %s is not intact: %w", path, err)
	}
	if os.IsNotExist(err) {
		n, last = 0, auditGenesis
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	return &AuditLog{f: f, seq: n, last: last}, nil
}

func (a *AuditLog) Close() error { return a.f.Close() }

// Record appends one entry and flushes it to disk before returning.
func (a *AuditLog) Record(actor, role, method, path string, status int, remote string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	e := AuditEntry{Seq: a.seq + 1, Time: time.Now().UTC().Format(time.RFC3339Nano), Actor: actor, Role: role,
		Method: method, Path: path, Status: status, Remote: remote, Prev: a.last}
	e.Hash = entryHash(e)
	b, _ := json.Marshal(e)
	if _, err := a.f.Write(append(b, '\n')); err != nil {
		return err
	}
	if err := a.f.Sync(); err != nil {
		return err
	}
	a.seq, a.last = e.Seq, e.Hash
	return nil
}

// VerifyAudit walks the chain and returns the entry count and last hash.
func VerifyAudit(path string) (uint64, string, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, "", err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	prev, n := auditGenesis, uint64(0)
	for sc.Scan() {
		var e AuditEntry
		if err := json.Unmarshal(sc.Bytes(), &e); err != nil {
			return n, prev, fmt.Errorf("line %d: %w", n+1, err)
		}
		if e.Seq != n+1 {
			return n, prev, fmt.Errorf("line %d: sequence %d, want %d (entry removed or reordered)", n+1, e.Seq, n+1)
		}
		if e.Prev != prev {
			return n, prev, fmt.Errorf("line %d: previous-hash mismatch (earlier entry changed or removed)", n+1)
		}
		if entryHash(e) != e.Hash {
			return n, prev, fmt.Errorf("line %d: entry was modified", n+1)
		}
		prev, n = e.Hash, n+1
	}
	return n, prev, sc.Err()
}
