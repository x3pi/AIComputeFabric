package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOllamaEmulatorProtocolAndDedicatedPort(t *testing.T) {
	// Fake llama-server upstream that speaks OpenAI /v1/chat/completions (streaming & non-streaming)
	workerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/responses" { // Codex CLI uses the Responses API (wire_api = "responses")
			w.Header().Set("Content-Type", "application/json")
			_, _ = fmt.Fprint(w, `{"object":"response","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"OK"}]}]}`)
			return
		}
		var req struct {
			Model  string `json:"model"`
			Stream bool   `json:"stream"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		if !req.Stream {
			w.Header().Set("Content-Type", "application/json")
			_, _ = fmt.Fprintf(w, `{"choices":[{"message":{"role":"assistant","content":"Hello from %s"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}`, req.Model)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fl, _ := w.(http.Flusher)
		for _, tok := range []string{"Xin ", "chào ", "Ollama!"} {
			_, _ = fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\n", tok)
			if fl != nil {
				fl.Flush()
			}
		}
		_, _ = fmt.Fprint(w, "data: [DONE]\n\n")
		if fl != nil {
			fl.Flush()
		}
	}))
	defer workerSrv.Close()

	reg := NewRegistry(time.Hour)
	addWorker(reg, "pc-001", workerSrv.URL, "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", 4)
	f := NewFleet(reg)
	store, _ := NewTenantStore("")
	_, tenantKey, _ := store.Create("dev-team", 1, 0)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second, Fair: true, Policy: store.Policy}, reg)
	rt.Refresh()
	auth := NewAuth("strong-admin-token-for-ollama-test")

	apiHandler := NewAPIServerless(f, store, auth, nil, nil, nil, nil, nil, rt)
	gwSrv := httptest.NewServer(apiHandler)
	defer gwSrv.Close()

	// 1. GET /api/tags works with Zero-Config (no Authorization header needed for 3rd-party Ollama discovery)
	resp, err := http.Get(gwSrv.URL + "/api/tags")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /api/tags status = %d, want 200", resp.StatusCode)
	}
	if resp.Header.Get("X-AICF-Ollama-Emulator") != "true" {
		t.Fatalf("expected X-AICF-Ollama-Emulator=true header")
	}
	var tagsReply struct {
		Models []OllamaModelTag `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tagsReply); err != nil {
		t.Fatal(err)
	}
	if len(tagsReply.Models) != 1 || tagsReply.Models[0].Name != "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf" {
		t.Fatalf("unexpected /api/tags models: %+v", tagsReply.Models)
	}

	// 1b. Codex: POST /api/ollama/v1/responses is forwarded to the engine's /v1/responses
	rreq, _ := http.NewRequest(http.MethodPost, gwSrv.URL+"/api/ollama/v1/responses", strings.NewReader(`{"model":"qwen3-coder:latest","input":"hi"}`))
	rreq.Header.Set("Content-Type", "application/json")
	rreq.Header.Set("Authorization", "Bearer "+tenantKey)
	rresp, err := http.DefaultClient.Do(rreq)
	if err != nil {
		t.Fatal(err)
	}
	rbody, _ := io.ReadAll(rresp.Body)
	rresp.Body.Close()
	if rresp.StatusCode != http.StatusOK || !strings.Contains(string(rbody), `"object":"response"`) {
		t.Fatalf("POST /api/ollama/v1/responses = %d %s", rresp.StatusCode, rbody)
	}

	// 2. POST /api/show with :latest suffix resolves to the active cluster model
	showResp, err := http.Post(gwSrv.URL+"/api/show", "application/json", strings.NewReader(`{"name":"Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf:latest"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer showResp.Body.Close()
	if showResp.StatusCode != http.StatusOK {
		t.Fatalf("POST /api/show status = %d", showResp.StatusCode)
	}

	// 3. POST /api/chat (streaming NDJSON)
	chatResp, err := http.Post(gwSrv.URL+"/api/chat", "application/json", strings.NewReader(`{"model":"qwen3-coder:latest","messages":[{"role":"user","content":"hi"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	defer chatResp.Body.Close()
	if chatResp.StatusCode != http.StatusOK {
		t.Fatalf("POST /api/chat status = %d", chatResp.StatusCode)
	}
	var fullText strings.Builder
	gotDone := false
	sc := bufio.NewScanner(chatResp.Body)
	for sc.Scan() {
		var frame struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
			Done bool `json:"done"`
		}
		if json.Unmarshal(sc.Bytes(), &frame) == nil {
			fullText.WriteString(frame.Message.Content)
			if frame.Done {
				gotDone = true
			}
		}
	}
	if fullText.String() != "Xin chào Ollama!" || !gotDone {
		t.Fatalf("unexpected streamed /api/chat output: %q (done=%v)", fullText.String(), gotDone)
	}

	// 4. POST /api/generate (non-streaming JSON)
	genResp, err := http.Post(gwSrv.URL+"/api/generate", "application/json", strings.NewReader(`{"model":"qwen3","prompt":"test","stream":false}`))
	if err != nil {
		t.Fatal(err)
	}
	defer genResp.Body.Close()
	var genOut struct {
		Response string `json:"response"`
		Done     bool   `json:"done"`
	}
	_ = json.NewDecoder(genResp.Body).Decode(&genOut)
	if !genOut.Done || !strings.Contains(genOut.Response, "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf") {
		t.Fatalf("unexpected /api/generate output: %+v", genOut)
	}

	// 5. Bridge script download for Linux, macOS, Windows embeds gateway URL & token
	for _, osName := range []string{"linux", "macos", "windows"} {
		bResp, err := http.Get(fmt.Sprintf("%s/api/ollama-emulator/bridge-script?os=%s&token=%s", gwSrv.URL, osName, tenantKey))
		if err != nil {
			t.Fatal(err)
		}
		bResp.Body.Close()
		if bResp.StatusCode != http.StatusOK {
			t.Fatalf("bridge-script os=%s status=%d", osName, bResp.StatusCode)
		}
	}

	// 6. Dedicated Port Listener (ephemeral 127.0.0.1:0)
	oe := NewOllamaEmulator(f, store, auth, nil, rt)
	if err := oe.StartListener("127.0.0.1:0"); err != nil {
		t.Fatal(err)
	}
	defer oe.StopListener()
	dedAddr := oe.lis.Addr().String()

	rootResp, err := http.Get("http://" + dedAddr + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer rootResp.Body.Close()
	if rootResp.StatusCode != http.StatusOK {
		t.Fatalf("dedicated port GET / status = %d", rootResp.StatusCode)
	}
}

func TestBridgeScriptsRenderAndRejectInjection(t *testing.T) {
	for name, build := range map[string]func(string, string) (string, error){"windows": buildWindowsBridgeScript, "unix": buildUnixBridgeScript} {
		out, err := build("http://192.168.1.40:18080", "aicf_abc123")
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if strings.Contains(out, "@@GW@@") || strings.Contains(out, "@@TOKEN@@") || !strings.Contains(out, "aicf_abc123") || !strings.Contains(out, "192.168.1.40:18080") {
			t.Fatalf("%s: placeholders not substituted", name)
		}
		for _, bad := range []string{"http://x'; Remove-Item C:\\ #", "http://x\" ; rm -rf ~ ; \"", "http://x$(id)", "http://x\n"} {
			if _, err := build(bad, "tok"); err == nil {
				t.Fatalf("%s: gateway %q must be rejected", name, bad)
			}
			if _, err := build("http://ok", bad); err == nil {
				t.Fatalf("%s: token %q must be rejected", name, bad)
			}
		}
	}
	win, _ := buildWindowsBridgeScript("http://h:1", "t")
	if !strings.Contains(win, "\r\n") || strings.Contains(strings.ReplaceAll(win, "\r\n", ""), "\n") {
		t.Fatal("windows bridge must use CRLF line endings only")
	}
}
