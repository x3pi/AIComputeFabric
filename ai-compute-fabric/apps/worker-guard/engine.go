package main

import (
	"bufio"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Buffers are the CUDA buffers llama.cpp logs at start-up (MiB). They feed the
// Weights / KV / Compute buckets, i.e. engine-reported numbers (plan §B.2).
type Buffers struct{ ModelMiB, KVMiB, ComputeMiB float64 }

var bufRe = regexp.MustCompile(`(\S+)\s+(model|KV|compute)\s+buffer size\s*=\s*([\d.]+)\s*MiB`)

// ParseBuffers sums device (non-host) buffers from llama.cpp log text.
func ParseBuffers(line string, b *Buffers) {
	m := bufRe.FindStringSubmatch(line)
	if m == nil || !strings.HasPrefix(m[1], "CUDA") || strings.HasPrefix(m[1], "CUDA_Host") {
		return
	}
	v, _ := strconv.ParseFloat(m[3], 64)
	switch m[2] {
	case "model":
		b.ModelMiB += v
	case "KV":
		b.KVMiB += v
	case "compute":
		b.ComputeMiB += v
	}
}

// StopInfo describes how an engine stop went.
type StopInfo struct {
	Signal  string
	Elapsed time.Duration
}

// Engine is the managed inference process.
type Engine interface {
	Start() error
	Stop() (StopInfo, error)
	Running() bool
	PID() int
	Buffers() Buffers
	Configure(model, lora string) // takes effect at the next Start
	Describe() (model, lora string)
}

// LlamaEngine manages one llama-server process.
type LlamaEngine struct {
	Bin, Model, LogPath string
	Lora                string // optional LoRA adapter in GGUF form (llama-server --lora); must come from the same base model as Model
	Port, Ctx, Parallel int
	NGL                 int
	FlashAttn           string        // "on" | "off" | "auto"
	KVType              string        // KV cache element type: f16 (default) or q8_0 (needs flash attention)
	Grace               time.Duration // SIGTERM grace before SIGKILL

	mu   sync.Mutex
	cmd  *exec.Cmd
	done chan struct{}
	bufs Buffers
}

func (e *LlamaEngine) Configure(model, lora string) {
	e.mu.Lock()
	e.Model, e.Lora = model, lora
	e.mu.Unlock()
}

func (e *LlamaEngine) Describe() (string, string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.Model, e.Lora
}

func (e *LlamaEngine) Running() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.cmd == nil {
		return false
	}
	select {
	case <-e.done:
		return false
	default:
		return true
	}
}

func (e *LlamaEngine) PID() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.cmd == nil || e.cmd.Process == nil {
		return 0
	}
	return e.cmd.Process.Pid
}

func (e *LlamaEngine) Buffers() Buffers {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.bufs
}

// Args builds the llama-server command line. Flash attention defaults to on: on the RTX 3060 it
// raised prompt processing 25-37 % and decode 6-9 % and used less VRAM (plan I.11). A quantised
// KV cache only works with flash attention, so that combination is refused here instead of
// failing inside llama-server.
func (e *LlamaEngine) Args() ([]string, error) {
	fa, kv := e.FlashAttn, e.KVType
	if fa == "" {
		fa = "on"
	}
	if kv == "" {
		kv = "f16"
	}
	if fa != "on" && fa != "off" && fa != "auto" {
		return nil, fmt.Errorf("flash attention must be on, off or auto, got %q", fa)
	}
	if kv != "f16" && kv != "q8_0" {
		return nil, fmt.Errorf("kv cache type must be f16 or q8_0, got %q", kv)
	}
	if kv != "f16" && fa == "off" {
		return nil, fmt.Errorf("kv cache type %s needs flash attention", kv)
	}
	if e.Lora != "" {
		st, err := os.Stat(e.Lora)
		if err != nil || st.IsDir() {
			return nil, fmt.Errorf("LoRA adapter %q is not a readable file", e.Lora)
		}
	}
	args := []string{"-m", e.Model, "-c", strconv.Itoa(e.Ctx * e.Parallel), "-np", strconv.Itoa(e.Parallel),
		"-ngl", strconv.Itoa(e.NGL), "--port", strconv.Itoa(e.Port), "--host", "127.0.0.1",
		"-fa", fa, "-ctk", kv, "-ctv", kv, "--no-warmup", "--fit", "off", "-lv", "4"}
	if e.Lora != "" {
		args = append(args, "--lora", e.Lora)
	}
	return args, nil
}

// Start launches llama-server and returns once /health is 200 (or on failure).
func (e *LlamaEngine) Start() error {
	e.mu.Lock()
	if e.cmd != nil {
		select {
		case <-e.done:
		default:
			e.mu.Unlock()
			return fmt.Errorf("engine already running")
		}
	}
	e.bufs = Buffers{}
	args, err := e.Args()
	if err != nil {
		e.mu.Unlock()
		return err
	}
	cmd := exec.Command(e.Bin, args...)
	prepareCmd(cmd)
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	if err := cmd.Start(); err != nil {
		e.mu.Unlock()
		return err
	}
	e.cmd, e.done = cmd, make(chan struct{})
	done := e.done
	e.mu.Unlock()

	go func() { // log pump + buffer parser
		var lf *os.File
		if e.LogPath != "" {
			lf, _ = os.Create(e.LogPath)
		}
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 1<<20), 1<<20)
		for sc.Scan() {
			if lf != nil {
				fmt.Fprintln(lf, sc.Text())
			}
			e.mu.Lock()
			ParseBuffers(sc.Text(), &e.bufs)
			e.mu.Unlock()
		}
		if lf != nil {
			lf.Close()
		}
	}()
	go func() { cmd.Wait(); pw.Close(); close(done) }()

	deadline := time.Now().Add(180 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-done:
			return fmt.Errorf("llama-server exited during start-up")
		default:
		}
		if r, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/health", e.Port)); err == nil {
			r.Body.Close()
			if r.StatusCode == 200 {
				return nil
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	e.Stop()
	return fmt.Errorf("llama-server not healthy after 180 s")
}

// Stop asks the engine to exit (SIGTERM to its process group on Unix), then forces it
// after Grace. Where no graceful stop exists (Windows) it terminates immediately.
func (e *LlamaEngine) Stop() (StopInfo, error) {
	e.mu.Lock()
	cmd, done := e.cmd, e.done
	e.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return StopInfo{}, nil
	}
	t0 := time.Now()
	info := StopInfo{}
	if name, ok := softStop(cmd); ok {
		info.Signal = name
		select {
		case <-done:
			info.Elapsed = time.Since(t0)
			return info, nil
		case <-time.After(e.Grace):
		}
	}
	info.Signal = hardStop(cmd)
	<-done
	info.Elapsed = time.Since(t0)
	return info, nil
}
