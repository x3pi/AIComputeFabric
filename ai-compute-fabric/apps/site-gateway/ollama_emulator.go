package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// OllamaClientEvent records a recent 3rd-party application request detected by the Ollama emulator.
type OllamaClientEvent struct {
	Time      string `json:"time"`
	ClientApp string `json:"client_app"`
	UserAgent string `json:"user_agent"`
	Method    string `json:"method"`
	Path      string `json:"path"`
	Model     string `json:"model,omitempty"`
	RemoteIP  string `json:"remote_ip"`
}

// OllamaModelDetails matches the native Ollama /api/tags and /api/show details structure.
type OllamaModelDetails struct {
	ParentModel       string   `json:"parent_model"`
	Format            string   `json:"format"`
	Family            string   `json:"family"`
	Families          []string `json:"families"`
	ParameterSize     string   `json:"parameter_size"`
	QuantizationLevel string   `json:"quantization_level"`
}

// OllamaModelTag matches one item in GET /api/tags (`{"models": [...]}`).
type OllamaModelTag struct {
	Name         string             `json:"name"`
	Model        string             `json:"model"`
	ModifiedAt   string             `json:"modified_at"`
	Size         uint64             `json:"size"`
	Digest       string             `json:"digest"`
	Details      OllamaModelDetails `json:"details"`
	Capabilities []string           `json:"capabilities,omitempty"`
}

// OllamaRunningModel matches one item in GET /api/ps (`{"models": [...]}`).
type OllamaRunningModel struct {
	Name      string             `json:"name"`
	Model     string             `json:"model"`
	Size      uint64             `json:"size"`
	SizeVRAM  uint64             `json:"size_vram"`
	Digest    string             `json:"digest"`
	Details   OllamaModelDetails `json:"details"`
	ExpiresAt string             `json:"expires_at"`
}

// OllamaEmulator implements Default Port Emulation (:11434) and full protocol translation
// for 3rd-party applications (Cursor, Continue.dev, Chatbox, Open WebUI, AnythingLLM, Cline, etc.)
// so they auto-discover and sync models with zero URL or API-key configuration.
type OllamaEmulator struct {
	mu              sync.RWMutex
	fleet           *Fleet
	ts              *TenantStore
	auth            *Auth
	audit           *AuditLog
	router          *Router
	client          *http.Client
	enabled         bool
	zeroConfigAuth  bool
	defaultTenantID string
	listenAddr      string
	srv             *http.Server
	lis             net.Listener
	lastBindErr     string
	servedRequests  atomic.Uint64
	recentClients   []OllamaClientEvent
}

// NewOllamaEmulator creates a new Ollama protocol & port 11434 emulator.
func NewOllamaEmulator(f *Fleet, ts *TenantStore, auth *Auth, audit *AuditLog, rt *Router) *OllamaEmulator {
	tr := &http.Transport{DisableKeepAlives: true, ResponseHeaderTimeout: 120 * time.Second}
	return &OllamaEmulator{
		fleet:          f,
		ts:             ts,
		auth:           auth,
		audit:          audit,
		router:         rt,
		client:         &http.Client{Transport: tr},
		enabled:        true,
		zeroConfigAuth: true,
		listenAddr:     "127.0.0.1:11434",
		recentClients:  make([]OllamaClientEvent, 0, 12),
	}
}

func setOllamaCORS(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Access-Control-Allow-Origin", "*")
	h.Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, HEAD, OPTIONS")
	h.Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Tenant-ID, X-AICF-Thinking, X-Requested-With, Accept, Origin")
	h.Set("Access-Control-Allow-Private-Network", "true")
	h.Set("Access-Control-Expose-Headers", "X-AICF-Ollama-Emulator, X-AICF-Worker, X-AICF-Agent, X-AICF-Thinking-Mode")
	h.Set("X-AICF-Ollama-Emulator", "true")
}

func classifyClientApp(ua string) string {
	l := strings.ToLower(ua)
	switch {
	case strings.Contains(l, "cursor"):
		return "Cursor IDE"
	case strings.Contains(l, "continue"):
		return "Continue.dev"
	case strings.Contains(l, "chatbox"):
		return "Chatbox AI"
	case strings.Contains(l, "open-webui") || strings.Contains(l, "openwebui"):
		return "Open WebUI"
	case strings.Contains(l, "anythingllm"):
		return "AnythingLLM"
	case strings.Contains(l, "cline") || strings.Contains(l, "roo"):
		return "Cline / Roo Code"
	case strings.Contains(l, "ollama"):
		return "Ollama CLI / SDK"
	case strings.Contains(l, "python") || strings.Contains(l, "httpx") || strings.Contains(l, "aiohttp"):
		return "Python AI SDK / LangChain"
	case strings.Contains(l, "node") || strings.Contains(l, "undici") || strings.Contains(l, "axios"):
		return "Node.js / VS Code Extension"
	case strings.Contains(l, "mozilla"):
		return "Web Browser / Extension"
	default:
		if ua == "" {
			return "3rd-Party Ollama Client"
		}
		if len(ua) > 36 {
			return ua[:36]
		}
		return ua
	}
}

func (oe *OllamaEmulator) recordClient(r *http.Request, model string) {
	oe.servedRequests.Add(1)
	ua := strings.TrimSpace(r.Header.Get("User-Agent"))
	ev := OllamaClientEvent{
		Time:      time.Now().UTC().Format(time.RFC3339),
		ClientApp: classifyClientApp(ua),
		UserAgent: ua,
		Method:    r.Method,
		Path:      r.URL.Path,
		Model:     model,
		RemoteIP:  r.RemoteAddr,
	}
	oe.mu.Lock()
	defer oe.mu.Unlock()
	oe.recentClients = append([]OllamaClientEvent{ev}, oe.recentClients...)
	if len(oe.recentClients) > 10 {
		oe.recentClients = oe.recentClients[:10]
	}
}

var paramSizeRe = regexp.MustCompile(`(?i)(\d+(?:\.\d+)?b(?:-a\d+b)?)`)
var quantRe = regexp.MustCompile(`(?i)(Q[2-8]_[A-Z0-9_]+|IQ[1-4]_[A-Z0-9_]+|F16|BF16)`)

func inferOllamaDetails(modelName string) (OllamaModelDetails, uint64) {
	for _, cm := range modelCatalog {
		if strings.EqualFold(cm.GGUFFile, modelName) || strings.EqualFold(cm.ID, modelName) {
			fam := strings.ToLower(cm.Family)
			if fam == "" {
				fam = "llama"
			}
			param := "7B"
			if m := paramSizeRe.FindString(modelName); m != "" {
				param = strings.ToUpper(m)
			}
			return OllamaModelDetails{
				Format:            "gguf",
				Family:            fam,
				Families:          []string{fam},
				ParameterSize:     param,
				QuantizationLevel: cm.Quantization,
			}, cm.WeightsBytes
		}
	}
	low := strings.ToLower(modelName)
	family := "llama"
	switch {
	case strings.Contains(low, "qwen3"):
		family = "qwen3"
	case strings.Contains(low, "qwen"):
		family = "qwen2"
	case strings.Contains(low, "deepseek"):
		family = "deepseek2"
	case strings.Contains(low, "glm"):
		family = "glm"
	case strings.Contains(low, "kimi"):
		family = "kimi"
	}
	param := "7B"
	sizeBytes := uint64(4_700_000_000)
	if m := paramSizeRe.FindString(modelName); m != "" {
		param = strings.ToUpper(m)
		switch {
		case strings.HasPrefix(param, "30B"):
			sizeBytes = 18_556_686_336
		case strings.HasPrefix(param, "14B"):
			sizeBytes = 8_988_000_000
		case strings.HasPrefix(param, "9B"):
			sizeBytes = 5_780_000_000
		case strings.HasPrefix(param, "6.7B"), strings.HasPrefix(param, "7B"):
			sizeBytes = 4_100_000_000
		}
	}
	quant := "Q4_K_M"
	if q := quantRe.FindString(modelName); q != "" {
		quant = strings.ToUpper(q)
	}
	return OllamaModelDetails{
		Format:            "gguf",
		Family:            family,
		Families:          []string{family},
		ParameterSize:     param,
		QuantizationLevel: quant,
	}, sizeBytes
}

func modelDigest(name string) string {
	sum := sha256.Sum256([]byte("aicf-ollama:" + name))
	return hex.EncodeToString(sum[:])
}

func detectServerLANURLs(defaultPort string) []string {
	if defaultPort == "" {
		defaultPort = "18080"
	}
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil
	}
	var urls []string
	seen := map[string]bool{}
	for _, a := range addrs {
		ipnet, ok := a.(*net.IPNet)
		if !ok || ipnet.IP == nil || ipnet.IP.IsLoopback() {
			continue
		}
		ip4 := ipnet.IP.To4()
		if ip4 == nil {
			continue
		}
		s := ip4.String()
		if strings.HasPrefix(s, "172.17.") || strings.HasPrefix(s, "169.254.") {
			continue
		}
		u := "http://" + s + ":" + defaultPort
		if !seen[u] {
			seen[u] = true
			urls = append(urls, u)
		}
	}
	return urls
}

// collectModels returns the deduplicated list of active models on the cluster.
func (oe *OllamaEmulator) collectModels() []string {
	seen := map[string]bool{}
	var out []string
	if oe.router != nil && oe.router.reg != nil {
		for _, rt := range oe.router.reg.Routable() {
			m := strings.TrimSpace(rt.Model)
			if m != "" && !strings.HasPrefix(m, "rpc://") && !seen[m] {
				seen[m] = true
				out = append(out, m)
			}
		}
	}
	if len(out) == 0 && oe.fleet != nil {
		ws, _ := oe.fleet.Page(0, 100, "")
		for _, w := range ws {
			m := strings.TrimSpace(w.Model)
			if m != "" && !strings.HasPrefix(m, "rpc://") && !seen[m] {
				seen[m] = true
				out = append(out, m)
			}
		}
	}
	return out
}

// resolveModel maps an Ollama client model name (which may include `:latest`, omit `.gguf`, or use a short alias)
// to the actual routable model name on the cluster.
func (oe *OllamaEmulator) resolveModel(want string) string {
	want, _ = StripThinkingAlias(want)
	models := oe.collectModels()
	if len(models) == 0 {
		return strings.TrimSuffix(want, ":latest")
	}
	if want == "" {
		return models[0]
	}
	for _, m := range models {
		if m == want || strings.EqualFold(m, want) {
			return m
		}
	}
	trimmed := strings.TrimSuffix(want, ":latest")
	for _, m := range models {
		if strings.EqualFold(m, trimmed) || strings.EqualFold(strings.TrimSuffix(m, ".gguf"), trimmed) {
			return m
		}
	}
	lowWant := strings.ToLower(trimmed)
	for _, m := range models {
		lowM := strings.ToLower(m)
		if strings.Contains(lowM, lowWant) || strings.Contains(lowWant, strings.TrimSuffix(lowM, ".gguf")) {
			return m
		}
	}
	// If only 1 model is loaded on the cluster, route any 3rd-party default model tag directly to it so zero-config apps work immediately.
	if len(models) == 1 {
		return models[0]
	}
	return trimmed
}

// resolveTenant determines which tenant is billed/queued for an Ollama request.
func (oe *OllamaEmulator) resolveTenant(r *http.Request) (string, bool) {
	bearer := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if bearer != "" && !strings.EqualFold(bearer, "ollama") {
		if oe.ts != nil {
			if t, ok := oe.ts.Authenticate(bearer); ok {
				return t.ID, true
			}
		}
		if oe.auth != nil {
			if p, ok := oe.auth.Identify(bearer); ok {
				return p.Name, true
			}
		}
	}
	oe.mu.RLock()
	zc := oe.zeroConfigAuth
	defT := oe.defaultTenantID
	oe.mu.RUnlock()

	if !zc && oe.ts != nil {
		return "", false
	}
	if defT != "" {
		return defT, true
	}
	if oe.ts != nil {
		for _, t := range oe.ts.List() {
			if !t.Disabled && !t.Pending {
				return t.ID, true
			}
		}
	}
	if h := strings.TrimSpace(r.Header.Get("X-Tenant-ID")); h != "" {
		return h, true
	}
	return "ollama-local", true
}

// StartListener dynamically binds the dedicated Ollama port (e.g. 127.0.0.1:11434 or 0.0.0.0:11434).
func (oe *OllamaEmulator) StartListener(addr string) error {
	oe.mu.Lock()
	defer oe.mu.Unlock()
	if addr != "" {
		oe.listenAddr = addr
	}
	if oe.lis != nil {
		if oe.lis.Addr().String() == oe.listenAddr {
			return nil
		}
		_ = oe.srv.Close()
		oe.lis = nil
		oe.srv = nil
	}
	lis, err := net.Listen("tcp", oe.listenAddr)
	if err != nil {
		oe.lastBindErr = err.Error()
		return err
	}
	oe.lastBindErr = ""
	oe.lis = lis
	oe.enabled = true
	srv := &http.Server{
		Handler:           oe.DedicatedPortHandler(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       5 * time.Minute,
		IdleTimeout:       2 * time.Minute,
	}
	oe.srv = srv
	go func() {
		if serr := srv.Serve(lis); serr != nil && serr != http.ErrServerClosed {
			log.Printf("ollama-emulator listener (%s) stopped: %v", oe.listenAddr, serr)
		}
	}()
	log.Printf("ollama-emulator listening on http://%s (Zero-Config 3rd-party discovery active)", lis.Addr().String())
	return nil
}

// StopListener closes the dedicated :11434 listener if active.
func (oe *OllamaEmulator) StopListener() {
	oe.mu.Lock()
	defer oe.mu.Unlock()
	if oe.srv != nil {
		_ = oe.srv.Close()
		oe.srv = nil
		oe.lis = nil
	}
}

// ProbeLocalPort11434 checks what is currently listening on 127.0.0.1:11434 on the gateway host.
func (oe *OllamaEmulator) ProbeLocalPort11434() (state, detail string) {
	oe.mu.RLock()
	active := oe.lis != nil
	oe.mu.RUnlock()
	if active {
		return "aicf_listener", "site-gateway đang trực tiếp lắng nghe cổng 11434"
	}
	c := &http.Client{Timeout: 800 * time.Millisecond}
	resp, err := c.Get("http://127.0.0.1:11434/api/version")
	if err != nil {
		return "stopped", "Cổng 127.0.0.1:11434 đang trống (sẵn sàng kích hoạt)"
	}
	defer resp.Body.Close()
	if resp.Header.Get("X-AICF-Ollama-Emulator") == "true" {
		return "aicf_bridge", "Cầu nối AI Compute Fabric (aicf-ollama-bridge) đang hoạt động trên cổng 11434"
	}
	return "conflict_stock_ollama", "Cổng 127.0.0.1:11434 trên máy chủ đang bị dịch vụ ollama.service gốc chiếm giữ"
}

// Status returns the current state of the Ollama Emulator for the Web UI.
func (oe *OllamaEmulator) Status() map[string]any {
	portState, portDetail := oe.ProbeLocalPort11434()
	models := oe.collectModels()
	tags := make([]OllamaModelTag, 0, len(models))
	for _, m := range models {
		det, sz := inferOllamaDetails(m)
		tags = append(tags, OllamaModelTag{
			Name:       m,
			Model:      m,
			ModifiedAt: time.Now().UTC().Format(time.RFC3339),
			Size:       sz,
			Digest:     modelDigest(m),
			Details:    det,
		})
	}
	oe.mu.RLock()
	defer oe.mu.RUnlock()
	defTenant := oe.defaultTenantID
	if defTenant == "" && oe.ts != nil {
		for _, t := range oe.ts.List() {
			if !t.Disabled && !t.Pending {
				defTenant = t.ID
				break
			}
		}
	}
	if defTenant == "" {
		defTenant = "ollama-local"
	}
	return map[string]any{
		"enabled":             oe.enabled,
		"zero_config_auth":    oe.zeroConfigAuth,
		"default_tenant_id":   defTenant,
		"listen_addr":         oe.listenAddr,
		"listener_active":     oe.lis != nil,
		"port_11434_state":    portState,
		"port_11434_detail":   portDetail,
		"last_bind_error":     oe.lastBindErr,
		"routable_models":     models,
		"ollama_tags":         tags,
		"lan_gateway_urls":    detectServerLANURLs("18080"),
		"served_requests":     oe.servedRequests.Load(),
		"recent_clients":      oe.recentClients,
		"supported_endpoints": []string{"/api/tags", "/api/show", "/api/ps", "/api/version", "/api/chat", "/api/generate", "/v1/models", "/v1/chat/completions", "/v1/responses"},
		"compatible_apps":     []string{"OpenAI Codex CLI", "Cursor IDE", "Continue.dev", "Chatbox AI", "Open WebUI", "AnythingLLM", "Cline / Roo Code", "Page Assist"},
	}
}

// DedicatedPortHandler serves all Ollama + OpenAI routes on the dedicated :11434 port.
func (oe *OllamaEmulator) DedicatedPortHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		setOllamaCORS(w)
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if (r.URL.Path == "/" || r.URL.Path == "") && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			oe.recordClient(r, "")
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("Ollama is running"))
			return
		}
		if strings.HasPrefix(r.URL.Path, "/v1/") {
			oe.handleOpenAICompat(w, r)
			return
		}
		oe.ServeHTTP(w, r)
	})
}

// IsOllamaEndpoint reports whether r.URL.Path is one of the native Ollama API routes handled on the main gateway port.
func IsOllamaEndpoint(path string) bool {
	p := strings.TrimSuffix(path, "/")
	switch p {
	case "/api/tags", "/api/version", "/api/ps", "/api/show", "/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/api/ollama/ping":
		return true
	}
	return strings.HasPrefix(path, "/api/ollama/v1/")
}

// ServeHTTP handles Ollama Native API requests (/api/tags, /api/show, /api/version, /api/ps, /api/chat, /api/generate, etc.).
func (oe *OllamaEmulator) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	setOllamaCORS(w)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	oe.mu.RLock()
	en := oe.enabled
	oe.mu.RUnlock()
	if !en {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "Ollama emulation is currently disabled on this gateway"})
		return
	}

	if strings.HasPrefix(r.URL.Path, "/api/ollama/v1/") {
		r2 := r.Clone(r.Context())
		r2.URL.Path = "/v1/" + strings.TrimPrefix(r.URL.Path, "/api/ollama/v1/")
		oe.handleOpenAICompat(w, r2)
		return
	}

	p := strings.TrimSuffix(r.URL.Path, "/")
	switch p {
	case "/api/ollama/ping":
		oe.recordClient(r, "")
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("Ollama is running"))
	case "/api/version":
		oe.recordClient(r, "")
		writeJSON(w, http.StatusOK, map[string]any{"version": "0.14.2"})
	case "/api/tags":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		oe.recordClient(r, "")
		models := oe.collectModels()
		now := time.Now().UTC().Format(time.RFC3339)
		tags := make([]OllamaModelTag, 0, len(models))
		for _, m := range models {
			det, sz := inferOllamaDetails(m)
			tags = append(tags, OllamaModelTag{
				Name:         m,
				Model:        m,
				ModifiedAt:   now,
				Size:         sz,
				Digest:       modelDigest(m),
				Details:      det,
				Capabilities: OllamaModelCapabilities(m),
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"models": tags})
	case "/api/ps":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		oe.recordClient(r, "")
		models := oe.collectModels()
		exp := time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339)
		running := make([]OllamaRunningModel, 0, len(models))
		for _, m := range models {
			det, sz := inferOllamaDetails(m)
			running = append(running, OllamaRunningModel{
				Name:      m,
				Model:     m,
				Size:      sz,
				SizeVRAM:  sz,
				Digest:    modelDigest(m),
				Details:   det,
				ExpiresAt: exp,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"models": running})
	case "/api/show":
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			Name  string `json:"name"`
			Model string `json:"model"`
		}
		_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req)
		want := req.Name
		if want == "" {
			want = req.Model
		}
		resolved := oe.resolveModel(want)
		oe.recordClient(r, resolved)
		det, _ := inferOllamaDetails(resolved)
		supportsThink, supportsToggle, thinkMode := ModelThinkingProfile(resolved)
		ctxLen := 16384
		writeJSON(w, http.StatusOK, map[string]any{
			"modelfile":    fmt.Sprintf("# Modelfile managed by AI Compute Fabric\nFROM %s\nPARAMETER num_ctx %d\n", resolved, ctxLen),
			"parameters":   fmt.Sprintf("num_ctx %d", ctxLen),
			"template":     "{{ .Prompt }}",
			"details":      det,
			"capabilities": OllamaModelCapabilities(resolved),
			"model_info": map[string]any{
				"general.architecture": det.Family,
				"llama.context_length": ctxLen,
				"aicf.thinking":        supportsThink,
				"aicf.thinking_toggle": supportsToggle,
				"aicf.thinking_mode":   thinkMode,
			},
		})
	case "/api/chat":
		oe.handleOllamaChat(w, r)
	case "/api/generate":
		oe.handleOllamaGenerate(w, r)
	case "/api/embed", "/api/embeddings":
		oe.handleOllamaEmbeddings(w, r)
	default:
		http.NotFound(w, r)
	}
}

type ollamaChatReq struct {
	Model          string            `json:"model"`
	Messages       []json.RawMessage `json:"messages"`
	Stream         *bool             `json:"stream"`
	Think          any               `json:"think,omitempty"`
	EnableThinking *bool             `json:"enable_thinking,omitempty"`
	Tools          json.RawMessage   `json:"tools,omitempty"`
	Options        map[string]any    `json:"options,omitempty"`
}

func (oe *OllamaEmulator) handleOllamaChat(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	tenant, ok := oe.resolveTenant(r)
	if !ok {
		reject(w, http.StatusUnauthorized, 0, "missing or invalid API key (enable Zero-Config Auth in Ollama Emulator settings)")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 32<<20))
	if err != nil {
		reject(w, http.StatusRequestEntityTooLarge, 0, "request body too large")
		return
	}
	var in ollamaChatReq
	if json.Unmarshal(body, &in) != nil {
		reject(w, http.StatusBadRequest, 0, "body must be JSON")
		return
	}
	var rawMap map[string]any
	_ = json.Unmarshal(body, &rawMap)
	thinkState := ExtractThinkingDirective(rawMap, r.Header.Get("X-AICF-Thinking"))

	resolvedModel := oe.resolveModel(in.Model)
	replyModel := in.Model
	if replyModel == "" {
		replyModel = resolvedModel
	}
	oe.recordClient(r, resolvedModel)

	stream := true
	if in.Stream != nil {
		stream = *in.Stream
	}

	oaiPayload := map[string]any{
		"model":    resolvedModel,
		"messages": ollamaImagesToParts(in.Messages),
		"stream":   stream,
	}
	if thinkState != nil {
		oaiPayload["enable_thinking"] = *thinkState
	}
	if len(in.Tools) > 0 && string(in.Tools) != "null" {
		oaiPayload["tools"] = in.Tools
	}
	if in.Options != nil {
		if temp, ok := in.Options["temperature"]; ok {
			oaiPayload["temperature"] = temp
		}
		if topP, ok := in.Options["top_p"]; ok {
			oaiPayload["top_p"] = topP
		}
		if numPred, ok := in.Options["num_predict"]; ok {
			oaiPayload["max_tokens"] = numPred
		}
	}
	oaiBytes, _ := json.Marshal(oaiPayload)
	oaiBytes, _, thinkState = ApplyThinkingControl(oaiBytes, r.Header.Get("X-AICF-Thinking"))
	oe.dispatchAndTranslate(w, r, tenant, resolvedModel, replyModel, oaiBytes, stream, false, thinkState)
}

type ollamaGenerateReq struct {
	Model          string         `json:"model"`
	Prompt         string         `json:"prompt"`
	System         string         `json:"system"`
	Images         []string       `json:"images,omitempty"`
	Stream         *bool          `json:"stream"`
	Think          any            `json:"think,omitempty"`
	EnableThinking *bool          `json:"enable_thinking,omitempty"`
	Options        map[string]any `json:"options,omitempty"`
}

func (oe *OllamaEmulator) handleOllamaGenerate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	tenant, ok := oe.resolveTenant(r)
	if !ok {
		reject(w, http.StatusUnauthorized, 0, "missing or invalid API key")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 32<<20))
	if err != nil {
		reject(w, http.StatusRequestEntityTooLarge, 0, "request body too large")
		return
	}
	var in ollamaGenerateReq
	if json.Unmarshal(body, &in) != nil {
		reject(w, http.StatusBadRequest, 0, "body must be JSON")
		return
	}
	var rawMap map[string]any
	_ = json.Unmarshal(body, &rawMap)
	thinkState := ExtractThinkingDirective(rawMap, r.Header.Get("X-AICF-Thinking"))

	resolvedModel := oe.resolveModel(in.Model)
	replyModel := in.Model
	if replyModel == "" {
		replyModel = resolvedModel
	}
	oe.recordClient(r, resolvedModel)

	stream := true
	if in.Stream != nil {
		stream = *in.Stream
	}
	msgs := []any{}
	if strings.TrimSpace(in.System) != "" {
		msgs = append(msgs, map[string]string{"role": "system", "content": in.System})
	}
	if len(in.Images) > 0 {
		parts := []any{map[string]any{"type": "text", "text": in.Prompt}}
		for _, im := range in.Images {
			parts = append(parts, map[string]any{"type": "image_url", "image_url": map[string]any{"url": base64ImageDataURL(im)}})
		}
		msgs = append(msgs, map[string]any{"role": "user", "content": parts})
	} else {
		msgs = append(msgs, map[string]string{"role": "user", "content": in.Prompt})
	}

	oaiPayload := map[string]any{
		"model":    resolvedModel,
		"messages": msgs,
		"stream":   stream,
	}
	if thinkState != nil {
		oaiPayload["enable_thinking"] = *thinkState
	}
	if in.Options != nil {
		if temp, ok := in.Options["temperature"]; ok {
			oaiPayload["temperature"] = temp
		}
		if numPred, ok := in.Options["num_predict"]; ok {
			oaiPayload["max_tokens"] = numPred
		}
	}
	oaiBytes, _ := json.Marshal(oaiPayload)
	oaiBytes, _, thinkState = ApplyThinkingControl(oaiBytes, r.Header.Get("X-AICF-Thinking"))
	oe.dispatchAndTranslate(w, r, tenant, resolvedModel, replyModel, oaiBytes, stream, true, thinkState)
}

func (oe *OllamaEmulator) dispatchAndTranslate(w http.ResponseWriter, r *http.Request, tenant, resolvedModel, replyModel string, oaiBytes []byte, stream, isGenerate bool, thinkState *bool) {
	if oe.router == nil {
		reject(w, http.StatusServiceUnavailable, 5*time.Second, "router not initialized")
		return
	}
	rt, err := oe.router.Acquire(Req{
		Model:  resolvedModel,
		Tenant: tenant,
		Prio:   PrioInteractive,
	}, r.Context().Done())
	if err != nil {
		reject(w, http.StatusServiceUnavailable, 2*time.Second, err.Error())
		return
	}
	start := time.Now()
	done := oe.forwardTranslated(w, r, rt, replyModel, oaiBytes, stream, isGenerate, start, thinkState)
	oe.router.Release(rt, tenant, !done, time.Since(start))
}

func (oe *OllamaEmulator) forwardTranslated(w http.ResponseWriter, r *http.Request, rt Route, replyModel string, oaiBytes []byte, stream, isGenerate bool, start time.Time, thinkState *bool) bool {
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, rt.URL+"/v1/chat/completions", bytes.NewReader(oaiBytes))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := oe.client.Do(req)
	if err != nil {
		reject(w, http.StatusBadGateway, time.Second, "worker connection failed")
		return false
	}
	defer resp.Body.Close()

	w.Header().Set("X-AICF-Worker", rt.ID)
	w.Header().Set("X-AICF-Agent", rt.ID)
	if thinkState != nil {
		if *thinkState {
			w.Header().Set("X-AICF-Thinking-Mode", "on")
		} else {
			w.Header().Set("X-AICF-Thinking-Mode", "off")
		}
	}

	if !stream {
		raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		if err != nil {
			reject(w, http.StatusBadGateway, 0, "failed reading worker response")
			return false
		}
		var oaiResp struct {
			Choices []struct {
				Message struct {
					Role             string          `json:"role"`
					Content          string          `json:"content"`
					ReasoningContent string          `json:"reasoning_content"`
					ToolCalls        json.RawMessage `json:"tool_calls,omitempty"`
				} `json:"message"`
				FinishReason string `json:"finish_reason"`
			} `json:"choices"`
			Usage struct {
				PromptTokens     int `json:"prompt_tokens"`
				CompletionTokens int `json:"completion_tokens"`
			} `json:"usage"`
		}
		_ = json.Unmarshal(raw, &oaiResp)
		content := ""
		thinking := ""
		var toolCalls json.RawMessage
		finish := "stop"
		if len(oaiResp.Choices) > 0 {
			content = oaiResp.Choices[0].Message.Content
			thinking = oaiResp.Choices[0].Message.ReasoningContent
			toolCalls = oaiResp.Choices[0].Message.ToolCalls
			if oaiResp.Choices[0].FinishReason != "" {
				finish = oaiResp.Choices[0].FinishReason
			}
		}
		if thinkState != nil && !*thinkState {
			content = CleanDisabledThinkingContent(content)
			thinking = ""
		}
		dur := time.Since(start).Nanoseconds()
		nowStr := time.Now().UTC().Format(time.RFC3339Nano)
		if isGenerate {
			out := map[string]any{
				"model":             replyModel,
				"created_at":        nowStr,
				"response":          content,
				"done":              true,
				"done_reason":       finish,
				"total_duration":    dur,
				"prompt_eval_count": oaiResp.Usage.PromptTokens,
				"eval_count":        oaiResp.Usage.CompletionTokens,
			}
			if thinking != "" {
				out["thinking"] = thinking
			}
			writeJSON(w, http.StatusOK, out)
			return true
		}
		msgObj := map[string]any{"role": "assistant", "content": content}
		if thinking != "" {
			msgObj["thinking"] = thinking
		}
		if len(toolCalls) > 0 && string(toolCalls) != "null" {
			msgObj["tool_calls"] = toolCalls
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"model":             replyModel,
			"created_at":        nowStr,
			"message":           msgObj,
			"done":              true,
			"done_reason":       finish,
			"total_duration":    dur,
			"prompt_eval_count": oaiResp.Usage.PromptTokens,
			"eval_count":        oaiResp.Usage.CompletionTokens,
		})
		return true
	}

	// Streaming mode: convert OpenAI SSE (`data: {...}`) to Ollama NDJSON (`application/x-ndjson`)
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	fl, _ := w.(http.Flusher)

	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	evalCount := 0
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payload == "[DONE]" {
			break
		}
		var chunk struct {
			Choices []struct {
				Delta struct {
					Content          string          `json:"content"`
					ReasoningContent string          `json:"reasoning_content"`
					ToolCalls        json.RawMessage `json:"tool_calls,omitempty"`
				} `json:"delta"`
				FinishReason *string `json:"finish_reason"`
			} `json:"choices"`
		}
		if json.Unmarshal([]byte(payload), &chunk) != nil || len(chunk.Choices) == 0 {
			continue
		}
		delta := chunk.Choices[0].Delta.Content
		reasoning := chunk.Choices[0].Delta.ReasoningContent
		if thinkState != nil && !*thinkState {
			reasoning = ""
		}
		if delta == "" && reasoning == "" && len(chunk.Choices[0].Delta.ToolCalls) == 0 {
			continue
		}
		evalCount++
		nowStr := time.Now().UTC().Format(time.RFC3339Nano)
		var outLine []byte
		if isGenerate {
			genDelta := delta
			if genDelta == "" && reasoning != "" {
				genDelta = reasoning
			}
			frame := map[string]any{
				"model":      replyModel,
				"created_at": nowStr,
				"response":   genDelta,
				"done":       false,
			}
			if reasoning != "" {
				frame["thinking"] = reasoning
			}
			outLine, _ = json.Marshal(frame)
		} else {
			msgContent := delta
			if msgContent == "" && reasoning != "" {
				msgContent = reasoning
			}
			msg := map[string]any{"role": "assistant", "content": msgContent}
			if reasoning != "" {
				msg["thinking"] = reasoning
			}
			if len(chunk.Choices[0].Delta.ToolCalls) > 0 {
				msg["tool_calls"] = chunk.Choices[0].Delta.ToolCalls
			}
			outLine, _ = json.Marshal(map[string]any{
				"model":      replyModel,
				"created_at": nowStr,
				"message":    msg,
				"done":       false,
			})
		}
		_, _ = w.Write(append(outLine, '\n'))
		if fl != nil {
			fl.Flush()
		}
	}

	dur := time.Since(start).Nanoseconds()
	nowStr := time.Now().UTC().Format(time.RFC3339Nano)
	var finalLine []byte
	if isGenerate {
		finalLine, _ = json.Marshal(map[string]any{
			"model":          replyModel,
			"created_at":     nowStr,
			"response":       "",
			"done":           true,
			"done_reason":    "stop",
			"total_duration": dur,
			"eval_count":     evalCount,
		})
	} else {
		finalLine, _ = json.Marshal(map[string]any{
			"model":          replyModel,
			"created_at":     nowStr,
			"message":        map[string]any{"role": "assistant", "content": ""},
			"done":           true,
			"done_reason":    "stop",
			"total_duration": dur,
			"eval_count":     evalCount,
		})
	}
	_, _ = w.Write(append(finalLine, '\n'))
	if fl != nil {
		fl.Flush()
	}
	return true
}

func (oe *OllamaEmulator) handleOllamaEmbeddings(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	tenant, ok := oe.resolveTenant(r)
	if !ok {
		reject(w, http.StatusUnauthorized, 0, "missing or invalid API key")
		return
	}
	var in struct {
		Model  string `json:"model"`
		Prompt string `json:"prompt"`
		Input  any    `json:"input"`
	}
	_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&in)
	resolved := oe.resolveModel(in.Model)
	oe.recordClient(r, resolved)
	input := in.Input
	if input == nil {
		input = in.Prompt
	}
	rt, err := oe.router.Acquire(Req{Model: resolved, Tenant: tenant, Prio: PrioInteractive}, r.Context().Done())
	if err != nil {
		reject(w, http.StatusServiceUnavailable, 2*time.Second, err.Error())
		return
	}
	defer oe.router.Release(rt, tenant, false)
	oaiBody, _ := json.Marshal(map[string]any{"model": resolved, "input": input})
	req, _ := http.NewRequestWithContext(r.Context(), http.MethodPost, rt.URL+"/v1/embeddings", bytes.NewReader(oaiBody))
	req.Header.Set("Content-Type", "application/json")
	resp, err := oe.client.Do(req)
	if err != nil {
		reject(w, http.StatusBadGateway, time.Second, "worker connection failed")
		return
	}
	defer resp.Body.Close()
	var oaiEmb struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&oaiEmb)
	var embs [][]float64
	var first []float64
	for _, d := range oaiEmb.Data {
		embs = append(embs, d.Embedding)
		if first == nil {
			first = d.Embedding
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"model":      resolved,
		"embeddings": embs,
		"embedding":  first,
	})
}

// handleOpenAICompat handles /v1/models and /v1/chat/completions on the :11434 listener (and /api/ollama/v1/*)
// with Zero-Config model normalization and fallback tenant authentication.
func (oe *OllamaEmulator) handleOpenAICompat(w http.ResponseWriter, r *http.Request) {
	setOllamaCORS(w)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	tenant, ok := oe.resolveTenant(r)
	if !ok {
		reject(w, http.StatusUnauthorized, 0, "missing or invalid API key")
		return
	}
	p := strings.TrimSuffix(r.URL.Path, "/")
	if r.Method == http.MethodGet && p == "/v1/models" {
		oe.recordClient(r, "")
		models := oe.collectModels()
		data := make([]map[string]any, 0, len(models)*3)
		for _, m := range models {
			data = append(data, BuildOpenAIModelEntries(m)...)
		}
		writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": data})
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	switch p {
	case "/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses":
	default:
		http.NotFound(w, r)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 32<<20))
	if err != nil {
		reject(w, http.StatusRequestEntityTooLarge, 0, "request body too large")
		return
	}
	var payload map[string]any
	if json.Unmarshal(body, &payload) != nil {
		reject(w, http.StatusBadRequest, 0, "body must be JSON")
		return
	}
	wantModel, _ := payload["model"].(string)
	resolved := oe.resolveModel(wantModel)
	payload["model"] = resolved
	oe.recordClient(r, resolved)
	rewritten, _ := json.Marshal(payload)
	if p == "/v1/chat/completions" {
		var imgErr error
		if rewritten, imgErr = NormalizeImageInputs(r.Context(), rewritten); imgErr != nil {
			reject(w, http.StatusBadRequest, 0, imgErr.Error())
			return
		}
	}
	var thinkState *bool
	if p == "/v1/chat/completions" || p == "/v1/responses" {
		// Preserve model suffix directive (:no_think / :think) from the original wantModel before resolveModel stripped it
		if _, aliasThink := StripThinkingAlias(wantModel); aliasThink != nil && ExtractThinkingDirective(payload, r.Header.Get("X-AICF-Thinking")) == nil {
			if *aliasThink {
				rewritten, _, thinkState = ApplyThinkingControl(rewritten, "true")
			} else {
				rewritten, _, thinkState = ApplyThinkingControl(rewritten, "false")
			}
		} else {
			rewritten, _, thinkState = ApplyThinkingControl(rewritten, r.Header.Get("X-AICF-Thinking"))
		}
	}
	if thinkState != nil {
		if *thinkState {
			w.Header().Set("X-AICF-Thinking-Mode", "on")
		} else {
			w.Header().Set("X-AICF-Thinking-Mode", "off")
		}
	}

	rt, err := oe.router.Acquire(Req{Model: resolved, Tenant: tenant, Prio: PrioInteractive}, r.Context().Done())
	if err != nil {
		reject(w, http.StatusServiceUnavailable, 2*time.Second, err.Error())
		return
	}
	start := time.Now()
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, rt.URL+p, bytes.NewReader(rewritten))
	if err != nil {
		oe.router.Release(rt, tenant, true)
		reject(w, http.StatusBadGateway, time.Second, "bad upstream request")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := oe.client.Do(req)
	if err != nil {
		oe.router.Release(rt, tenant, true, time.Since(start))
		reject(w, http.StatusBadGateway, time.Second, "worker connection failed")
		return
	}
	defer resp.Body.Close()
	for k, v := range resp.Header {
		if k == "Content-Length" {
			continue
		}
		w.Header()[k] = v
	}
	setOllamaCORS(w)
	w.Header().Set("X-AICF-Worker", rt.ID)
	w.Header().Set("X-AICF-Agent", rt.ID)
	ct := strings.ToLower(resp.Header.Get("Content-Type"))
	if strings.Contains(ct, "application/json") && p == "/v1/chat/completions" {
		rawResp, _ := io.ReadAll(resp.Body)
		normResp := NormalizeOpenAIChatResponse(rawResp, thinkState)
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(normResp)
		oe.router.Release(rt, tenant, false, time.Since(start))
		return
	}
	w.WriteHeader(resp.StatusCode)
	fl, _ := w.(http.Flusher)
	buf := make([]byte, 8192)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			_, _ = w.Write(buf[:n])
			if fl != nil {
				fl.Flush()
			}
		}
		if rerr != nil {
			break
		}
	}
	oe.router.Release(rt, tenant, false, time.Since(start))
}

// HandleConfigAPI handles GET /api/ollama-emulator/status, POST /api/ollama-emulator/config, and GET /api/ollama-emulator/bridge-script.
func (oe *OllamaEmulator) HandleConfigAPI(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/api/ollama-emulator/status":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		writeJSON(w, http.StatusOK, oe.Status())
	case "/api/ollama-emulator/config":
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		var in struct {
			Enabled         *bool   `json:"enabled"`
			ZeroConfigAuth  *bool   `json:"zero_config_auth"`
			DefaultTenantID *string `json:"default_tenant_id"`
			ListenAddr      *string `json:"listen_addr"`
			ListenerAction  string  `json:"listener_action"` // "start" | "stop" | ""
		}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<14)).Decode(&in) != nil {
			http.Error(w, "bad JSON", http.StatusBadRequest)
			return
		}
		oe.mu.Lock()
		if in.Enabled != nil {
			oe.enabled = *in.Enabled
		}
		if in.ZeroConfigAuth != nil {
			oe.zeroConfigAuth = *in.ZeroConfigAuth
		}
		if in.DefaultTenantID != nil {
			oe.defaultTenantID = strings.TrimSpace(*in.DefaultTenantID)
		}
		if in.ListenAddr != nil && strings.TrimSpace(*in.ListenAddr) != "" {
			oe.listenAddr = strings.TrimSpace(*in.ListenAddr)
		}
		addr := oe.listenAddr
		oe.mu.Unlock()

		var actionErr string
		switch in.ListenerAction {
		case "start":
			if err := oe.StartListener(addr); err != nil {
				actionErr = err.Error()
			}
		case "stop":
			oe.StopListener()
		}
		st := oe.Status()
		if actionErr != "" {
			st["action_error"] = actionErr
		}
		writeJSON(w, http.StatusOK, st)
	case "/api/ollama-emulator/bridge-script":
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		oe.serveBridgeScript(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (oe *OllamaEmulator) serveBridgeScript(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	osType := strings.ToLower(strings.TrimSpace(q.Get("os")))
	if osType == "" {
		ua := strings.ToLower(r.Header.Get("User-Agent"))
		switch {
		case strings.Contains(ua, "win"):
			osType = "windows"
		case strings.Contains(ua, "mac"):
			osType = "macos"
		default:
			osType = "linux"
		}
	}
	gwURL := strings.TrimSpace(q.Get("gateway"))
	if gwURL == "" {
		scheme := "http"
		if r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
			scheme = "https"
		}
		host := r.Host
		if host == "" {
			host = "127.0.0.1:18080"
		}
		gwURL = scheme + "://" + host
	}
	gwURL = strings.TrimRight(gwURL, "/")

	token := strings.TrimSpace(q.Get("token"))
	if token == "" {
		token = strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	}

	if osType == "windows" {
		w.Header().Set("Content-Type", "application/bat; charset=utf-8")
		w.Header().Set("Content-Disposition", `attachment; filename="aicf-ollama-bridge.bat"`)
		script, err := buildWindowsBridgeScript(gwURL, token)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		_, _ = w.Write([]byte(script))
		return
	}

	filename := "aicf-ollama-bridge.sh"
	if osType == "macos" {
		filename = "aicf-ollama-bridge.command"
	}
	w.Header().Set("Content-Type", "text/x-shellscript; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="%s"`, filename))
	script, err := buildUnixBridgeScript(gwURL, token)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	_, _ = w.Write([]byte(script))
}

//go:embed bridge/windows.bat.tmpl
var windowsBridgeTmpl string

//go:embed bridge/unix.sh.tmpl
var unixBridgeTmpl string

// bridgeValueRe limits what may be substituted into the generated scripts (Host header and token are
// client-controlled): URL/token characters only, so nothing can break out of the quoted literal.
var bridgeValueRe = regexp.MustCompile(`^[A-Za-z0-9._~:/@+=-]*$`)

func renderBridge(tmpl, gatewayURL, token string) (string, error) {
	if !bridgeValueRe.MatchString(gatewayURL) || !bridgeValueRe.MatchString(token) {
		return "", fmt.Errorf("gateway URL or token contains characters that are not allowed in the bridge script")
	}
	return strings.NewReplacer("@@GW@@", gatewayURL, "@@TOKEN@@", token).Replace(tmpl), nil
}

func buildUnixBridgeScript(gatewayURL, token string) (string, error) {
	return renderBridge(unixBridgeTmpl, gatewayURL, token)
}

func buildWindowsBridgeScript(gatewayURL, token string) (string, error) {
	out, err := renderBridge(windowsBridgeTmpl, gatewayURL, token)
	return strings.ReplaceAll(strings.ReplaceAll(out, "\r\n", "\n"), "\n", "\r\n"), err
}

func (oe *OllamaEmulator) Shutdown(ctx context.Context) {
	oe.mu.Lock()
	srv := oe.srv
	oe.mu.Unlock()
	if srv != nil {
		_ = srv.Shutdown(ctx)
	}
}
