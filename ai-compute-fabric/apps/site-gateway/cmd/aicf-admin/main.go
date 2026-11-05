// aicf-admin: Backup, Restore & Cryptographic Integrity Verification CLI (Pha B3).
//
// Commands:
//
//	aicf-admin verify  -data-dir /var/lib/aicf
//	aicf-admin backup  -data-dir /var/lib/aicf -out /var/backups/aicf-backup.tar.gz
//	aicf-admin restore -in /var/backups/aicf-backup.tar.gz -data-dir /var/lib/aicf
package main

import (
	"archive/tar"
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const genesisHash = "0000000000000000000000000000000000000000000000000000000000000000"

type auditEntry struct {
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

type ledgerEntry struct {
	Site             string `json:"site"`
	Tenant           string `json:"tenant"`
	Job              string `json:"job"`
	Seq              uint64 `json:"seq"`
	PromptTokens     uint64 `json:"prompt_tokens"`
	CompletionTokens uint64 `json:"completion_tokens"`
	GPUMs            uint64 `json:"gpu_ms"`
	VRAMMiBSec       uint64 `json:"vram_mib_s"`
	AudioMs          uint64 `json:"audio_ms"`
	MicroKWh         uint64 `json:"micro_kwh"`
	Prev             string `json:"prev"`
	Hash             string `json:"hash"`
}

type tenantRec struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	KeyHash string `json:"key_sha256"`
}

type backupManifest struct {
	CreatedAt string            `json:"created_at"`
	Files     map[string]string `json:"files_sha256"`
}

func sha256Bytes(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func verifyAuditChain(path string) (int, error) {
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	prev := genesisHash
	n := 0
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var e auditEntry
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			return n, fmt.Errorf("audit %s line %d: %w", path, n+1, err)
		}
		n++
		if e.Seq != uint64(n) {
			return n, fmt.Errorf("audit %s line %d: seq=%d, want %d", path, n, e.Seq, n)
		}
		if e.Prev != prev {
			return n, fmt.Errorf("audit %s line %d: prev hash mismatch", path, n)
		}
		got := e.Hash
		e.Hash = ""
		raw, _ := json.Marshal(e)
		if sha256Bytes(raw) != got {
			return n, fmt.Errorf("audit %s line %d: hash mismatch (tampered)", path, n)
		}
		prev = got
	}
	return n, sc.Err()
}

func verifyLedgerChain(path string) (int, error) {
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	prev := genesisHash
	n := 0
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var e ledgerEntry
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			return n, fmt.Errorf("ledger %s line %d: %w", path, n+1, err)
		}
		n++
		if e.Seq != uint64(n) {
			return n, fmt.Errorf("ledger %s line %d: seq=%d, want %d", path, n, e.Seq, n)
		}
		if e.Prev != prev {
			return n, fmt.Errorf("ledger %s line %d: prev hash mismatch", path, n)
		}
		got := e.Hash
		e.Hash = ""
		raw, _ := json.Marshal(e)
		if sha256Bytes(raw) != got {
			return n, fmt.Errorf("ledger %s line %d: hash mismatch (tampered)", path, n)
		}
		prev = got
	}
	return n, sc.Err()
}

func verifyTenantsFile(path string) (int, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	var list []tenantRec
	if err := json.Unmarshal(b, &list); err != nil {
		return 0, fmt.Errorf("tenants %s: invalid JSON: %w", path, err)
	}
	seenID := map[string]bool{}
	for i, t := range list {
		if t.ID == "" || t.Name == "" || len(t.KeyHash) != 64 {
			return i, fmt.Errorf("tenants %s entry %d: invalid id/name/key_sha256", path, i)
		}
		if seenID[t.ID] {
			return i, fmt.Errorf("tenants %s entry %d: duplicate id %s", path, i, t.ID)
		}
		seenID[t.ID] = true
	}
	return len(list), nil
}

func verifyJSONFile(path string) error {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		return fmt.Errorf("%s: invalid JSON: %w", path, err)
	}
	return nil
}

func verifyDataDir(dataDir string) (map[string]any, error) {
	if st, err := os.Stat(dataDir); err != nil || !st.IsDir() {
		return nil, fmt.Errorf("data-dir %s does not exist or is not a directory", dataDir)
	}
	tenantsCount, err := verifyTenantsFile(filepath.Join(dataDir, "tenants.json"))
	if err != nil {
		return nil, err
	}
	if err := verifyJSONFile(filepath.Join(dataDir, "topology.json")); err != nil {
		return nil, err
	}
	if err := verifyJSONFile(filepath.Join(dataDir, "deployments.json")); err != nil {
		return nil, err
	}
	if err := verifyJSONFile(filepath.Join(dataDir, "quotas.json")); err != nil {
		return nil, err
	}
	auditCount, err := verifyAuditChain(filepath.Join(dataDir, "audit.jsonl"))
	if err != nil {
		return nil, err
	}
	ledgerCount, err := verifyLedgerChain(filepath.Join(dataDir, "usage_ledger.jsonl"))
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok":             true,
		"data_dir":       dataDir,
		"tenants_count":  tenantsCount,
		"audit_entries":  auditCount,
		"ledger_entries": ledgerCount,
	}, nil
}

func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
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
	return os.Rename(tmp, path)
}

func runBackup(dataDir, outArchive string) error {
	if _, err := verifyDataDir(dataDir); err != nil {
		return fmt.Errorf("pre-backup verification failed: %w", err)
	}
	files := map[string][]byte{}
	sums := map[string]string{}
	err := filepath.Walk(dataDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(dataDir, path)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if strings.HasSuffix(rel, ".tmp") || rel == "manifest.json" {
			return nil
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		files[rel] = b
		sums[rel] = sha256Bytes(b)
		return nil
	})
	if err != nil {
		return err
	}
	manifest := backupManifest{
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
		Files:     sums,
	}
	mBytes, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	files["manifest.json"] = mBytes

	if err := os.MkdirAll(filepath.Dir(outArchive), 0o750); err != nil {
		return err
	}
	tmpArchive := outArchive + ".tmp"
	f, err := os.OpenFile(tmpArchive, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)

	keys := make([]string, 0, len(files))
	for k := range files {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, rel := range keys {
		data := files[rel]
		hdr := &tar.Header{
			Name:    rel,
			Mode:    0o600,
			Size:    int64(len(data)),
			ModTime: time.Now().UTC(),
		}
		if err := tw.WriteHeader(hdr); err != nil {
			_ = f.Close()
			return err
		}
		if _, err := tw.Write(data); err != nil {
			_ = f.Close()
			return err
		}
	}
	if err := tw.Close(); err != nil {
		_ = f.Close()
		return err
	}
	if err := gz.Close(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmpArchive, outArchive)
}

func runRestore(inArchive, dataDir string) error {
	f, err := os.Open(inArchive)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	defer func() { _ = gz.Close() }()
	tr := tar.NewReader(gz)

	extracted := map[string][]byte{}
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		clean := filepath.ToSlash(filepath.Clean(hdr.Name))
		if strings.HasPrefix(clean, "../") || strings.HasPrefix(clean, "/") || clean == ".." {
			return fmt.Errorf("unsafe path in archive: %s", hdr.Name)
		}
		b, err := io.ReadAll(io.LimitReader(tr, 256<<20))
		if err != nil {
			return err
		}
		extracted[clean] = b
	}
	mRaw, ok := extracted["manifest.json"]
	if !ok {
		return errors.New("backup archive missing manifest.json")
	}
	var manifest backupManifest
	if err := json.Unmarshal(mRaw, &manifest); err != nil {
		return fmt.Errorf("invalid manifest.json: %w", err)
	}
	for rel, expectedHash := range manifest.Files {
		b, ok := extracted[rel]
		if !ok {
			return fmt.Errorf("archive missing file listed in manifest: %s", rel)
		}
		if sha256Bytes(b) != expectedHash {
			return fmt.Errorf("checksum mismatch for %s in archive", rel)
		}
	}

	stagingDir, err := os.MkdirTemp("", "aicf-restore-stage-*")
	if err != nil {
		return err
	}
	defer func() { _ = os.RemoveAll(stagingDir) }()

	for rel, b := range extracted {
		if rel == "manifest.json" {
			continue
		}
		if err := writeFileAtomic(filepath.Join(stagingDir, filepath.FromSlash(rel)), b, 0o600); err != nil {
			return err
		}
	}
	if _, err := verifyDataDir(stagingDir); err != nil {
		return fmt.Errorf("staged restore failed cryptographic verification: %w", err)
	}

	if err := os.MkdirAll(dataDir, 0o750); err != nil {
		return err
	}
	for rel, b := range extracted {
		if rel == "manifest.json" {
			continue
		}
		if err := writeFileAtomic(filepath.Join(dataDir, filepath.FromSlash(rel)), b, 0o600); err != nil {
			return err
		}
	}
	return nil
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: aicf-admin <verify|backup|restore> [flags]")
		os.Exit(2)
	}
	cmd := os.Args[1]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	dataDir := fs.String("data-dir", "/var/lib/aicf", "AICF data directory")
	outFile := fs.String("out", "", "Output .tar.gz path (for backup)")
	inFile := fs.String("in", "", "Input .tar.gz path (for restore)")
	_ = fs.Parse(os.Args[2:])

	switch cmd {
	case "verify":
		res, err := verifyDataDir(*dataDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "VERIFY_FAILED: %v\n", err)
			os.Exit(1)
		}
		b, _ := json.Marshal(res)
		fmt.Println(string(b))
	case "backup":
		if *outFile == "" {
			fmt.Fprintln(os.Stderr, "backup requires -out <path.tar.gz>")
			os.Exit(2)
		}
		if err := runBackup(*dataDir, *outFile); err != nil {
			fmt.Fprintf(os.Stderr, "BACKUP_FAILED: %v\n", err)
			os.Exit(1)
		}
		res, _ := verifyDataDir(*dataDir)
		res["backup_archive"] = *outFile
		b, _ := json.Marshal(res)
		fmt.Println(string(b))
	case "restore":
		if *inFile == "" {
			fmt.Fprintln(os.Stderr, "restore requires -in <path.tar.gz>")
			os.Exit(2)
		}
		if err := runRestore(*inFile, *dataDir); err != nil {
			fmt.Fprintf(os.Stderr, "RESTORE_FAILED: %v\n", err)
			os.Exit(1)
		}
		res, err := verifyDataDir(*dataDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "RESTORE_VERIFY_FAILED: %v\n", err)
			os.Exit(1)
		}
		b, _ := json.Marshal(res)
		fmt.Println(string(b))
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q (expected verify, backup, or restore)\n", cmd)
		os.Exit(2)
	}
}
