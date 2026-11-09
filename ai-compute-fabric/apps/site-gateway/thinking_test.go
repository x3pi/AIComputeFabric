package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestThinkingCapabilityDiscoveryAndToggleAcrossAPIs(t *testing.T) {
	var lastUpstream map[string]any
	workerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &lastUpstream)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"role":"assistant","reasoning_content":"step 1","content":"<think>step 1</think>\n42"},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":4}}`))
	}))
	defer workerSrv.Close()

	reg := NewRegistry(time.Hour)
	addWorker(reg, "pc-001", workerSrv.URL, "Qwen3-8B-Q4_K_M.gguf", 4)
	addWorker(reg, "pc-002", workerSrv.URL, "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", 4)
	f := NewFleet(reg)
	store, _ := NewTenantStore("")
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second}, reg)
	rt.Refresh()
	auth := NewAuth("strong-admin-token-for-thinking-test")

	inf := NewInference(rt, NewLimiter(0, 0))
	mux := http.NewServeMux()
	mux.Handle("/v1/", inf)
	mux.Handle("/", NewAPIServerless(f, store, auth, nil, nil, nil, nil, nil, rt))
	gw := httptest.NewServer(mux)
	defer gw.Close()

	// 1. GET /v1/models reports OpenAI standard fields + thinking capabilities + virtual :no_think/:think aliases for hybrid models
	mResp, err := http.Get(gw.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	defer mResp.Body.Close()
	var mList struct {
		Object string `json:"object"`
		Data   []struct {
			ID                  string         `json:"id"`
			Object              string         `json:"object"`
			Created             int64          `json:"created"`
			OwnedBy             string         `json:"owned_by"`
			Reasoning           bool           `json:"reasoning"`
			Thinking            bool           `json:"thinking"`
			ThinkingToggle      bool           `json:"thinking_toggle"`
			ThinkingMode        string         `json:"thinking_mode"`
			SupportedParameters []string       `json:"supported_parameters"`
			Capabilities        map[string]any `json:"capabilities"`
		} `json:"data"`
	}
	if err := json.NewDecoder(mResp.Body).Decode(&mList); err != nil {
		t.Fatal(err)
	}
	// Qwen3-8B (base + :no_think + :think = 3) + Qwen3-Coder-Instruct (1) = 4 entries
	if len(mList.Data) != 4 {
		t.Fatalf("expected 4 model entries (including :no_think/:think aliases), got %d", len(mList.Data))
	}
	byID := map[string]string{}
	for _, m := range mList.Data {
		if m.Object != "model" || m.Created == 0 {
			t.Fatalf("invalid OpenAI model object fields: %+v", m)
		}
		byID[m.ID] = m.ThinkingMode
	}
	if byID["Qwen3-8B-Q4_K_M.gguf"] != "hybrid" {
		t.Fatalf("Qwen3-8B must be hybrid thinking, got %q", byID["Qwen3-8B-Q4_K_M.gguf"])
	}
	if byID["Qwen3-8B-Q4_K_M.gguf:no_think"] != "disabled" {
		t.Fatalf("Qwen3-8B:no_think must be disabled, got %q", byID["Qwen3-8B-Q4_K_M.gguf:no_think"])
	}
	if byID["Qwen3-8B-Q4_K_M.gguf:think"] != "enabled" {
		t.Fatalf("Qwen3-8B:think must be enabled, got %q", byID["Qwen3-8B-Q4_K_M.gguf:think"])
	}
	if byID["Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"] != "none" {
		t.Fatalf("Qwen3-Coder-Instruct must be none, got %q", byID["Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"])
	}

	// 2. POST /api/show (Ollama discovery) includes "thinking" in capabilities for Qwen3-8B
	showResp, err := http.Post(gw.URL+"/api/show", "application/json", strings.NewReader(`{"name":"Qwen3-8B-Q4_K_M.gguf"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer showResp.Body.Close()
	var showData struct {
		Capabilities []string `json:"capabilities"`
	}
	_ = json.NewDecoder(showResp.Body).Decode(&showData)
	hasThinkCap := false
	for _, c := range showData.Capabilities {
		if c == "thinking" {
			hasThinkCap = true
		}
	}
	if !hasThinkCap {
		t.Fatalf("POST /api/show for Qwen3-8B must include 'thinking' capability, got %v", showData.Capabilities)
	}

	// 3. OpenAI POST /v1/chat/completions with :no_think alias or reasoning_effort="none" strips reasoning_content & <think>
	reqBody := `{"model":"Qwen3-8B-Q4_K_M.gguf:no_think","stream":false,"messages":[{"role":"user","content":"2+2?"}]}`
	cResp, err := http.Post(gw.URL+"/v1/chat/completions", "application/json", strings.NewReader(reqBody))
	if err != nil {
		t.Fatal(err)
	}
	var oaiDisabled struct {
		Choices []struct {
			Message struct {
				Content          string `json:"content"`
				ReasoningContent string `json:"reasoning_content"`
			} `json:"message"`
		} `json:"choices"`
	}
	_ = json.NewDecoder(cResp.Body).Decode(&oaiDisabled)
	cResp.Body.Close()
	if cResp.StatusCode != http.StatusOK {
		t.Fatalf("POST /v1/chat/completions status = %d", cResp.StatusCode)
	}
	if cResp.Header.Get("X-AICF-Thinking-Mode") != "off" {
		t.Fatalf("expected X-AICF-Thinking-Mode=off, got %q", cResp.Header.Get("X-AICF-Thinking-Mode"))
	}
	if gotModel, _ := lastUpstream["model"].(string); gotModel != "Qwen3-8B-Q4_K_M.gguf" {
		t.Fatalf("expected clean upstream model name, got %q", gotModel)
	}
	ctk, _ := lastUpstream["chat_template_kwargs"].(map[string]any)
	if en, ok := ctk["enable_thinking"].(bool); !ok || en != false {
		t.Fatalf("expected chat_template_kwargs.enable_thinking=false, got %v", ctk)
	}
	if len(oaiDisabled.Choices) == 0 || oaiDisabled.Choices[0].Message.Content != "42" || oaiDisabled.Choices[0].Message.ReasoningContent != "" {
		t.Fatalf("expected clean non-thinking OpenAI response when disabled, got %+v", oaiDisabled.Choices)
	}

	// 4. OpenAI standard reasoning_effort="none" and LiteLLM/Anthropic-style thinking={"type":"disabled"}
	for _, body := range []string{
		`{"model":"Qwen3-8B-Q4_K_M.gguf","reasoning_effort":"none","stream":false,"messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"Qwen3-8B-Q4_K_M.gguf","thinking":{"type":"disabled"},"stream":false,"messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"Qwen3-8B-Q4_K_M.gguf","include_reasoning":false,"stream":false,"messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"Qwen3-8B-Q4_K_M.gguf","reasoning":{"effort":"none"},"stream":false,"messages":[{"role":"user","content":"hi"}]}`,
	} {
		r, err := http.Post(gw.URL+"/v1/chat/completions", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		if r.Header.Get("X-AICF-Thinking-Mode") != "off" {
			t.Fatalf("payload %s expected X-AICF-Thinking-Mode=off, got %q", body, r.Header.Get("X-AICF-Thinking-Mode"))
		}
	}

	// 5. OpenAI standard reasoning_effort="high" / thinking={"type":"enabled"} enables thinking
	for _, body := range []string{
		`{"model":"Qwen3-8B-Q4_K_M.gguf","reasoning_effort":"high","stream":false,"messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"Qwen3-8B-Q4_K_M.gguf","thinking":{"type":"enabled","budget_tokens":2048},"stream":false,"messages":[{"role":"user","content":"hi"}]}`,
	} {
		r, err := http.Post(gw.URL+"/v1/chat/completions", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		if r.Header.Get("X-AICF-Thinking-Mode") != "on" {
			t.Fatalf("payload %s expected X-AICF-Thinking-Mode=on, got %q", body, r.Header.Get("X-AICF-Thinking-Mode"))
		}
	}

	// 6. Ollama POST /api/chat with think=false suppresses thinking field and strips <think> tags
	oResp, err := http.Post(gw.URL+"/api/chat", "application/json", strings.NewReader(`{"model":"Qwen3-8B-Q4_K_M.gguf","think":false,"stream":false,"messages":[{"role":"user","content":"2+2?"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	defer oResp.Body.Close()
	var oChat struct {
		Message struct {
			Content  string `json:"content"`
			Thinking string `json:"thinking"`
		} `json:"message"`
	}
	_ = json.NewDecoder(oResp.Body).Decode(&oChat)
	if oChat.Message.Thinking != "" {
		t.Fatalf("expected empty thinking when think=false, got %q", oChat.Message.Thinking)
	}
	if oChat.Message.Content != "42" {
		t.Fatalf("expected clean content '42' when think=false, got %q", oChat.Message.Content)
	}

	// 7. Ollama POST /api/chat with think=true preserves thinking field
	oRespOn, err := http.Post(gw.URL+"/api/chat", "application/json", strings.NewReader(`{"model":"Qwen3-8B-Q4_K_M.gguf","think":true,"stream":false,"messages":[{"role":"user","content":"2+2?"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	defer oRespOn.Body.Close()
	var oChatOn struct {
		Message struct {
			Content  string `json:"content"`
			Thinking string `json:"thinking"`
		} `json:"message"`
	}
	_ = json.NewDecoder(oRespOn.Body).Decode(&oChatOn)
	if oChatOn.Message.Thinking != "step 1" {
		t.Fatalf("expected thinking 'step 1' when think=true, got %q", oChatOn.Message.Thinking)
	}
}
