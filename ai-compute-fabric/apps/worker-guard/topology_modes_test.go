package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseBuffersWithRPCBuffers(t *testing.T) {
	b := Buffers{}
	ParseBuffers("CUDA0 model buffer size = 8600.50 MiB", &b)
	ParseBuffers("RPC0[192.168.1.49:50052] model buffer size = 8600.50 MiB", &b)
	ParseBuffers("CUDA0 KV buffer size = 512.00 MiB", &b)
	ParseBuffers("RPC0[192.168.1.49:50052] KV buffer size = 512.00 MiB", &b)
	ParseBuffers("CUDA0 compute buffer size = 42.00 MiB", &b)
	ParseBuffers("RPC0[192.168.1.49:50052] compute buffer size = 42.00 MiB", &b)
	ParseBuffers("CUDA_Host buffer size = 120.00 MiB", &b) // Should be excluded

	if b.ModelMiB != 17201.00 {
		t.Errorf("expected ModelMiB=17201.00, got %.2f", b.ModelMiB)
	}
	if b.KVMiB != 1024.00 {
		t.Errorf("expected KVMiB=1024.00, got %.2f", b.KVMiB)
	}
	if b.ComputeMiB != 84.00 {
		t.Errorf("expected ComputeMiB=84.00, got %.2f", b.ComputeMiB)
	}
}

func TestLlamaEngineRPCWorkerArgs(t *testing.T) {
	eng := &LlamaEngine{
		Bin:     "/path/to/llama-server",
		Mode:    "rpc-worker",
		RPCPort: 50052,
		Host:    "0.0.0.0",
	}
	args, err := eng.Args()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := []string{"-H", "0.0.0.0", "-p", "50052", "-c"}
	if strings.Join(args, " ") != strings.Join(expected, " ") {
		t.Errorf("expected args %v, got %v", expected, args)
	}
}

func TestLlamaEngineSmart1GPUArgs(t *testing.T) {
	eng := &LlamaEngine{
		Bin:      "/path/to/llama-server",
		Model:    "/models/Qwen3-30B-A3B-Q4_K_M.gguf",
		Mode:     "smart_1gpu",
		NCpuMoe:  26,
		KVType:   "q8_0",
		Ctx:      4096,
		Parallel: 1,
		Port:     18090,
	}
	args, err := eng.Args()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	str := strings.Join(args, " ")
	if !strings.Contains(str, "-ncmoe 26") {
		t.Errorf("expected the configured -ncmoe 26, got: %s", str)
	}
	if !strings.Contains(str, "-ctk q8_0") || !strings.Contains(str, "-ctv q8_0") {
		t.Errorf("expected the configured quantized KV cache, got: %s", str)
	}
	// without N the mode is refused instead of guessing one
	eng.NCpuMoe = 0
	if _, err := eng.Args(); err == nil {
		t.Errorf("smart_1gpu without n_cpu_moe must be refused")
	}
}

// A model whose name looks like a large MoE must not get -ncmoe / q8_0 by itself, in any mode.
func TestNoAutoFitByModelName(t *testing.T) {
	for _, mode := range []string{"", "standalone", "split"} {
		eng := &LlamaEngine{Bin: "x", Model: "/m/Qwen3-30B-A3B-Q4_K_M.gguf", Mode: mode, Ctx: 4096, Parallel: 1, Port: 1}
		args, err := eng.Args()
		if err != nil {
			t.Fatal(err)
		}
		str := strings.Join(args, " ")
		if strings.Contains(str, "-ncmoe") || strings.Contains(str, "q8_0") {
			t.Errorf("mode %q: unexpected auto-fit in %s", mode, str)
		}
	}
}

// A plain model switch (Studio, old clients) must leave rpc-worker mode and clear multi-GPU settings.
func TestConfigureOptionsEmptyModeResetsToStandalone(t *testing.T) {
	eng := &LlamaEngine{Bin: "x", Model: "a.gguf"}
	eng.ConfigureOptions(EngineOptions{Mode: "rpc-worker", RPCPort: 50052})
	if eng.Options().Mode != "rpc-worker" {
		t.Fatal("setup: expected rpc-worker")
	}
	eng.ConfigureOptions(EngineOptions{Model: "b.gguf"})
	o := eng.Options()
	if o.Mode != "standalone" || o.RPCServers != "" || o.NCpuMoe != 0 || o.Model != "b.gguf" {
		t.Errorf("not reset: %+v", o)
	}
	if m, _ := eng.Describe(); m != "b.gguf" {
		t.Errorf("Describe still reports %q", m)
	}
}

func TestRPCWorkerDefaultsToLoopback(t *testing.T) {
	eng := &LlamaEngine{Bin: "x", Mode: "rpc-worker"}
	args, _ := eng.Args()
	if strings.Join(args, " ") != "-H 127.0.0.1 -p 50052 -c" {
		t.Errorf("got %v", args)
	}
}

// The heartbeat model of an rpc-worker must keep its "rpc://" marker (the router keys on it), not be cut to a basename.
func TestBaseNameKeepsRPCMarker(t *testing.T) {
	if got := baseName("rpc://50052"); got != "rpc://50052" {
		t.Errorf("baseName(rpc://50052) = %q", got)
	}
	if got := baseName("/models/a.gguf"); got != "a.gguf" {
		t.Errorf("baseName path = %q", got)
	}
}

func TestEngineAPIValidatesMultiGPUFields(t *testing.T) {
	h, _, _ := engineRig(t)
	for _, body := range []string{
		`{"model":"a.gguf","mode":"bogus"}`,
		`{"model":"a.gguf","mode":"smart_1gpu"}`,
		`{"model":"a.gguf","mode":"merged"}`,
		`{"model":"a.gguf","mode":"merged","rpc_servers":"h:1; rm -rf /"}`,
		`{"model":"a.gguf","mode":"merged","rpc_servers":"h:1","tensor_split":"1,x"}`,
		`{"model":"a.gguf","mode":"merged","rpc_servers":"h:1","split_mode":"zzz"}`,
		`{"model":"a.gguf","kv_type":"q4"}`,
		`{"model":"a.gguf","n_cpu_moe":-1}`,
	} {
		if w := post(h, body); w.Code != 400 {
			t.Errorf("%s -> %d, want 400", body, w.Code)
		}
	}
	if w := post(h, `{"model":"a.gguf","mode":"merged","rpc_servers":"192.168.1.49:50052,10.0.0.2:50052","tensor_split":"1,1.5,2"}`); w.Code != http.StatusAccepted {
		t.Errorf("valid merged request -> %d: %s", w.Code, w.Body.String())
	}
}

func TestLlamaEngineMergedArgs(t *testing.T) {
	eng := &LlamaEngine{
		Bin:         "/path/to/llama-server",
		Model:       "/models/Qwen3-30B-A3B-Q4_K_M.gguf",
		Mode:        "merged",
		RPCServers:  "192.168.1.49:50052",
		SplitMode:   "layer",
		TensorSplit: "1,1",
		Ctx:         4096,
		Parallel:    1,
		Port:        18090,
	}
	args, err := eng.Args()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	str := strings.Join(args, " ")
	if !strings.Contains(str, "--rpc 192.168.1.49:50052") {
		t.Errorf("expected --rpc 192.168.1.49:50052 in merged args, got: %s", str)
	}
	if !strings.Contains(str, "--split-mode layer") {
		t.Errorf("expected --split-mode layer in merged args, got: %s", str)
	}
	if !strings.Contains(str, "--tensor-split 1,1") {
		t.Errorf("expected --tensor-split 1,1 in merged args, got: %s", str)
	}
}

func TestEngineAPIRPCWorkerMode(t *testing.T) {
	h, fake, dir := engineRig(t)
	_ = dir

	rec := post(h, `{"mode":"rpc-worker","rpc_port":50052}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("POST rpc-worker mode: got %d: %s", rec.Code, rec.Body.String())
	}
	if fake.model != "rpc://50052" {
		t.Errorf("expected model rpc://50052, got %q", fake.model)
	}

	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if resp["mode"] != "rpc-worker" {
		t.Errorf("expected mode rpc-worker, got %v", resp["mode"])
	}
}

func TestGuardPersistsAndRestoresStateAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	stateFile := filepath.Join(dir, "worker-state.json")
	modelPath := filepath.Join(dir, "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf")
	if err := os.WriteFile(modelPath, []byte("GGUF"), 0o600); err != nil {
		t.Fatal(err)
	}

	gpu := &fakeGPU{total: 12288}
	eng1 := &LlamaEngine{Bin: "/bin/llama-server", Model: filepath.Join(dir, "Qwen3-4B.gguf"), Ctx: 4096, Parallel: 1, Port: 18090}
	g1 := NewGuard(Config{HeadroomMiB: 512, StateFile: stateFile}, gpu, eng1)

	g1.SwitchWithOptions(EngineOptions{
		Model:       modelPath,
		Mode:        "merged",
		KVType:      "f16",
		RPCServers:  "10.42.0.225:50052",
		SplitMode:   "layer",
		TensorSplit: "52,48",
	})
	g1.Pause()

	// Restart worker-guard with default startup flags ("Qwen3-4B.gguf", "standalone") and the same state file.
	eng2 := &LlamaEngine{Bin: "/bin/llama-server", Model: filepath.Join(dir, "Qwen3-4B.gguf"), Mode: "standalone", Ctx: 4096, Parallel: 1, Port: 18090}
	g2 := NewGuard(Config{HeadroomMiB: 512, StateFile: stateFile}, gpu, eng2)

	snap := g2.Snapshot()
	if snap.State != StPaused {
		t.Fatalf("expected restored state paused, got %s", snap.State)
	}
	if snap.Mode != "merged" || snap.Model != "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf" || snap.RPCServers != "10.42.0.225:50052" || snap.TensorSplit != "52,48" {
		t.Fatalf("restored snapshot mismatch: %+v", snap)
	}

	g2.Resume()
	g2.SwitchWithOptions(EngineOptions{Mode: "rpc-worker", RPCPort: 50052})

	eng3 := &LlamaEngine{Bin: "/bin/llama-server", Model: filepath.Join(dir, "Qwen3-4B.gguf"), Mode: "standalone"}
	g3 := NewGuard(Config{HeadroomMiB: 512, StateFile: stateFile}, gpu, eng3)
	snap3 := g3.Snapshot()
	if snap3.State == StPaused || snap3.Mode != "rpc-worker" || snap3.Model != "rpc://50052" || snap3.RPCPort != 50052 {
		t.Fatalf("restored rpc-worker snapshot mismatch: %+v", snap3)
	}
}
