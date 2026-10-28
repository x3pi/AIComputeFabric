package main

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Buffers are the CUDA / RPC buffers llama.cpp logs at start-up (MiB). They feed the
// Weights / KV / Compute buckets, i.e. engine-reported numbers (plan §B.2).
type Buffers struct{ ModelMiB, KVMiB, ComputeMiB float64 }

var bufRe = regexp.MustCompile(`(\S+)\s+(model|KV|compute)\s+buffer size\s*=\s*([\d.]+)\s*MiB`)

// ParseBuffers sums device (non-host) buffers from llama.cpp log text.
// Supports both CUDA device buffers (e.g. CUDA0) and RPC device buffers (e.g. RPC0[192.168.1.49:50052]).
func ParseBuffers(line string, b *Buffers) {
	m := bufRe.FindStringSubmatch(line)
	if m == nil {
		return
	}
	dev := m[1]
	if (!strings.HasPrefix(dev, "CUDA") && !strings.HasPrefix(dev, "RPC")) || strings.HasPrefix(dev, "CUDA_Host") {
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

// rpcModelPrefix marks a worker that serves as a GGML RPC member (it has no model of its own).
const rpcModelPrefix = "rpc://"

// EngineOptions configures the engine mode and multi-GPU / large model settings.
type EngineOptions struct {
	Model       string `json:"model"`
	Lora        string `json:"lora"`
	Mode        string `json:"mode"` // "" (= standalone) | "standalone" | "split" | "smart_1gpu" | "merged" | "rpc-worker"
	NCpuMoe     int    `json:"n_cpu_moe"`
	KVType      string `json:"kv_type"` // "" keeps the worker's startup --kv-cache-type
	RPCServers  string `json:"rpc_servers"`
	SplitMode   string `json:"split_mode"`
	TensorSplit string `json:"tensor_split"`
	RPCPort     int    `json:"rpc_port"`
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

// ConfigurableEngine allows advanced topology/mode switching.
type ConfigurableEngine interface {
	Engine
	ConfigureOptions(opts EngineOptions)
	Options() EngineOptions
}

// LlamaEngine manages one llama-server process or one ggml-rpc-server process.
type LlamaEngine struct {
	StartTimeout        time.Duration // how long Start() waits for the engine to become healthy (default 180 s; a big model through GGML RPC needs more)
	Bin, Model, LogPath string
	Lora                string // optional LoRA adapter in GGUF form (llama-server --lora); must come from the same base model as Model
	Host                string // address llama-server binds to (default: 127.0.0.1)
	Port, Ctx, Parallel int
	NGL                 int
	FlashAttn           string        // "on" | "off" | "auto"
	KVType              string        // KV cache element type: f16 (default) or q8_0 (needs flash attention)
	Grace               time.Duration // SIGTERM grace before SIGKILL

	// Mode and RPC settings
	Mode         string // "standalone" | "smart_1gpu" | "rpc-worker"
	NCpuMoe      int    // -ncmoe flag
	RPCServers   string // --rpc
	SplitMode    string // --split-mode ("layer" | "row")
	TensorSplit  string // --tensor-split (e.g. "1,1")
	RPCPort      int    // port for ggml-rpc-server
	RPCServerBin string // path to ggml-rpc-server executable

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

func (e *LlamaEngine) ConfigureOptions(opts EngineOptions) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if opts.Model != "" {
		e.Model = opts.Model
	}
	e.Lora = opts.Lora
	e.bufs = Buffers{} // buffers parsed from the previous model's log no longer describe what will be loaded
	// An empty mode means standalone: a plain model switch (Studio, old API clients) must leave rpc-worker / merged.
	e.Mode = opts.Mode
	if e.Mode == "" {
		e.Mode = "standalone"
	}
	if opts.KVType != "" {
		e.KVType = opts.KVType
	}
	e.NCpuMoe = opts.NCpuMoe
	e.RPCServers = opts.RPCServers
	e.SplitMode = opts.SplitMode
	e.TensorSplit = opts.TensorSplit
	if opts.RPCPort > 0 {
		e.RPCPort = opts.RPCPort
	}
}

func (e *LlamaEngine) Options() EngineOptions {
	e.mu.Lock()
	defer e.mu.Unlock()
	return EngineOptions{
		Model:       e.Model,
		Lora:        e.Lora,
		Mode:        e.Mode,
		NCpuMoe:     e.NCpuMoe,
		KVType:      e.KVType,
		RPCServers:  e.RPCServers,
		SplitMode:   e.SplitMode,
		TensorSplit: e.TensorSplit,
		RPCPort:     e.RPCPort,
	}
}

func (e *LlamaEngine) Describe() (string, string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.Mode == "rpc-worker" {
		port := e.RPCPort
		if port <= 0 {
			port = 50052
		}
		return fmt.Sprintf("%s%d", rpcModelPrefix, port), ""
	}
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

// Args builds the process command line.
func (e *LlamaEngine) Args() ([]string, error) {
	if e.Mode == "rpc-worker" {
		port := e.RPCPort
		if port <= 0 {
			port = 50052
		}
		host := e.Host
		if host == "" { // GGML RPC has no authentication: never listen on all interfaces unless the operator asked (--engine-host)
			host = "127.0.0.1"
		}
		return []string{"-H", host, "-p", strconv.Itoa(port), "-c"}, nil
	}

	fa, kv := e.FlashAttn, e.KVType
	if fa == "" {
		fa = "on"
	}
	if kv == "" {
		kv = "f16"
	}
	ncmoe := e.NCpuMoe
	// No value is guessed from the model name: how many expert layers fit depends on the card, context and KV type,
	// so smart_1gpu needs N from the operator (plan: sweep in phase 1). KV type stays whatever was configured.
	if e.Mode == "smart_1gpu" && ncmoe <= 0 {
		return nil, fmt.Errorf("smart_1gpu needs n_cpu_moe > 0 (number of MoE layers whose experts stay in RAM)")
	}
	if ncmoe < 0 {
		return nil, fmt.Errorf("n_cpu_moe must be >= 0, got %d", ncmoe)
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
	host := e.Host
	if host == "" {
		host = "127.0.0.1"
	}
	args := []string{"-m", e.Model, "-c", strconv.Itoa(e.Ctx * e.Parallel), "-np", strconv.Itoa(e.Parallel),
		"-ngl", strconv.Itoa(e.NGL), "--port", strconv.Itoa(e.Port), "--host", host,
		"-fa", fa, "-ctk", kv, "-ctv", kv, "--no-warmup", "--fit", "off", "-lv", "4"}
	if ncmoe > 0 {
		args = append(args, "-ncmoe", strconv.Itoa(ncmoe))
	}
	if e.RPCServers != "" {
		args = append(args, "--rpc", e.RPCServers)
		splitMode := e.SplitMode
		if splitMode == "" {
			splitMode = "layer"
		}
		args = append(args, "--split-mode", splitMode)
		if e.TensorSplit != "" {
			args = append(args, "--tensor-split", e.TensorSplit)
		}
	}
	if e.Lora != "" {
		args = append(args, "--lora", e.Lora)
	}
	return args, nil
}

// Start launches the engine (llama-server or ggml-rpc-server) and returns once healthy.
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

	binPath := e.Bin
	if e.Mode == "rpc-worker" {
		if e.RPCServerBin != "" {
			binPath = e.RPCServerBin
		} else {
			dir := filepath.Dir(e.Bin)
			name := "ggml-rpc-server"
			if strings.HasSuffix(strings.ToLower(e.Bin), ".exe") {
				name = "ggml-rpc-server.exe"
			}
			binPath = filepath.Join(dir, name)
		}
	}

	cmd := exec.Command(binPath, args...)
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

	startTimeout := e.StartTimeout
	if startTimeout <= 0 {
		startTimeout = 180 * time.Second
	}
	deadline := time.Now().Add(startTimeout)
	if e.Mode == "rpc-worker" {
		rpcPort := e.RPCPort
		if rpcPort <= 0 {
			rpcPort = 50052
		}
		for time.Now().Before(deadline) {
			select {
			case <-done:
				return fmt.Errorf("ggml-rpc-server exited during start-up")
			default:
			}
			conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", rpcPort), 500*time.Millisecond)
			if err == nil {
				conn.Close()
				return nil
			}
			time.Sleep(200 * time.Millisecond)
		}
		e.Stop()
		return fmt.Errorf("ggml-rpc-server not healthy after %s", startTimeout)
	}

	checkHost := e.Host
	if checkHost == "" || checkHost == "0.0.0.0" {
		checkHost = "127.0.0.1"
	}
	for time.Now().Before(deadline) {
		select {
		case <-done:
			return fmt.Errorf("llama-server exited during start-up")
		default:
		}
		if r, err := http.Get(fmt.Sprintf("http://%s:%d/health", checkHost, e.Port)); err == nil {
			r.Body.Close()
			if r.StatusCode == 200 {
				return nil
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	e.Stop()
	return fmt.Errorf("llama-server not healthy after %s", startTimeout)
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
