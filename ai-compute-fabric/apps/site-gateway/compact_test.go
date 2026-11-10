package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeEngine imitates llama-server: it refuses chat requests whose text is longer than limitChars with the real
// exceed_context_size_error body, and answers summary requests (recognised by their system prompt).
type fakeEngine struct {
	mu          sync.Mutex
	limitChars  int
	failSummary bool
	summaries   []string // user text of each summary call
	chats       [][]byte // bodies of accepted chat calls
	refused     int
}

func (f *fakeEngine) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req struct {
			Messages []struct {
				Role    string `json:"role"`
				Content any    `json:"content"`
			} `json:"messages"`
		}
		_ = json.Unmarshal(body, &req)
		f.mu.Lock()
		defer f.mu.Unlock()
		if len(req.Messages) > 0 && strings.HasPrefix(fmt.Sprint(req.Messages[0].Content), "You compress the memory") {
			if f.failSummary {
				w.WriteHeader(500)
				return
			}
			f.summaries = append(f.summaries, fmt.Sprint(req.Messages[1].Content))
			fmt.Fprintf(w, `{"choices":[{"message":{"role":"assistant","content":"- SUMMARY%d: tên An, mã ZX-42"}}]}`, len(f.summaries))
			return
		}
		if bytes.Contains(body, []byte("BADREQUEST")) {
			w.WriteHeader(400)
			w.Write([]byte(`{"error":{"code":400,"message":"something else is wrong","type":"invalid_request_error"}}`))
			return
		}
		total := 0
		for _, m := range req.Messages {
			total += len(fmt.Sprint(m.Content))
		}
		if total > f.limitChars {
			f.refused++
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(400)
			fmt.Fprintf(w, `{"error":{"code":400,"message":"request (%d tokens) exceeds the available context size (8000 tokens)","type":"exceed_context_size_error","n_prompt_tokens":%d,"n_ctx":8000}}`, total/3, total/3)
			return
		}
		f.chats = append(f.chats, body)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
	}
}

func compactRig(t *testing.T, limit int) (*httptest.Server, *fakeEngine, *httptest.Server) {
	t.Helper()
	fe := &fakeEngine{limitChars: limit}
	up := httptest.NewServer(fe.handler())
	t.Cleanup(up.Close)
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second, Fair: true}, reg)
	addWorker(reg, "w1", up.URL, "m", 2)
	rt.Refresh()
	srv := httptest.NewServer(NewInference(rt, NewLimiter(0, 0)))
	t.Cleanup(srv.Close)
	sumCache = &summaryCache{m: map[string]cacheItem{}}
	return srv, fe, up
}

func convo(n int, withSystem bool) []map[string]any {
	var m []map[string]any
	if withSystem {
		m = append(m, map[string]any{"role": "system", "content": "Bạn là trợ lý."})
	}
	for i := 0; i < n; i++ {
		m = append(m, map[string]any{"role": "user", "content": fmt.Sprintf("câu hỏi số %d %s", i, strings.Repeat("x", 150))})
		m = append(m, map[string]any{"role": "assistant", "content": fmt.Sprintf("trả lời số %d %s", i, strings.Repeat("y", 150))})
	}
	m = append(m, map[string]any{"role": "user", "content": "câu hỏi cuối cùng"})
	return m
}

func cpost(t *testing.T, srv *httptest.Server, msgs []map[string]any, hdr map[string]string, extra map[string]any) (*http.Response, []byte) {
	t.Helper()
	body := map[string]any{"model": "m", "messages": msgs}
	for k, v := range extra {
		body[k] = v
	}
	b, _ := json.Marshal(body)
	req, _ := http.NewRequest("POST", srv.URL+"/v1/chat/completions", bytes.NewReader(b))
	req.Header.Set("X-Tenant-ID", "t1")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return resp, out
}

func lastChat(t *testing.T, fe *fakeEngine) (msgs []struct{ Role, Content string }) {
	t.Helper()
	fe.mu.Lock()
	defer fe.mu.Unlock()
	if len(fe.chats) == 0 {
		t.Fatal("engine accepted no chat request")
	}
	var req struct {
		Messages []struct {
			Role    string `json:"role"`
			Content string `json:"content"`
		} `json:"messages"`
	}
	json.Unmarshal(fe.chats[len(fe.chats)-1], &req)
	for _, m := range req.Messages {
		msgs = append(msgs, struct{ Role, Content string }{m.Role, m.Content})
	}
	return
}

func TestCompactLeavesRequestsThatFitUntouched(t *testing.T) {
	srv, fe, _ := compactRig(t, 100000)
	msgs := convo(3, true)
	resp, _ := cpost(t, srv, msgs, nil, nil)
	if resp.StatusCode != 200 || resp.Header.Get("X-AICF-Compacted") != "" || len(fe.summaries) != 0 {
		t.Fatalf("status %d header %q summaries %d", resp.StatusCode, resp.Header.Get("X-AICF-Compacted"), len(fe.summaries))
	}
	if got := lastChat(t, fe); len(got) != len(msgs) {
		t.Fatalf("history altered: %d -> %d messages", len(msgs), len(got))
	}
}

func TestCompactOnOverflowSummarisesOldMessagesAndRetries(t *testing.T) {
	srv, fe, _ := compactRig(t, 1500)
	msgs := convo(10, true) // ~ 3.6k chars: refused first
	resp, body := cpost(t, srv, msgs, nil, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("status %d: %s", resp.StatusCode, body)
	}
	if h := resp.Header.Get("X-AICF-Compacted"); !strings.Contains(h, "trigger=overflow") {
		t.Fatalf("header %q", h)
	}
	if fe.refused != 1 || len(fe.summaries) != 1 {
		t.Fatalf("refused %d summaries %d", fe.refused, len(fe.summaries))
	}
	got := lastChat(t, fe)
	if got[0].Role != "system" || !strings.Contains(got[0].Content, "Bạn là trợ lý.") || !strings.Contains(got[0].Content, "SUMMARY1") {
		t.Fatalf("memory note not merged into the system message: %+v", got[0])
	}
	if got[1].Role != "user" || len(got) != 1+5 {
		t.Fatalf("expected system + last 4 messages starting at a user turn, got %d messages, second role %s", len(got), got[1].Role)
	}
	if got[len(got)-1].Content != "câu hỏi cuối cùng" {
		t.Error("the latest user message must be kept verbatim")
	}
}

func TestCompactWithoutSystemMessageInsertsOne(t *testing.T) {
	srv, fe, _ := compactRig(t, 1500)
	resp, _ := cpost(t, srv, convo(10, false), nil, nil)
	got := lastChat(t, fe)
	if resp.StatusCode != 200 || got[0].Role != "system" || !strings.Contains(got[0].Content, "SUMMARY1") {
		t.Fatalf("status %d first %+v", resp.StatusCode, got[0])
	}
}

func TestCompactOffPassesTheEngineErrorThrough(t *testing.T) {
	srv, fe, _ := compactRig(t, 1500)
	for name, tc := range map[string]struct {
		hdr   map[string]string
		extra map[string]any
	}{"header": {map[string]string{"X-AICF-Compact": "off"}, nil}, "body field": {nil, map[string]any{"aicf_compact": "off"}}} {
		resp, body := cpost(t, srv, convo(10, true), tc.hdr, tc.extra)
		if resp.StatusCode != 400 || !strings.Contains(string(body), "exceed_context_size_error") || len(fe.summaries) != 0 {
			t.Fatalf("%s: status %d summaries %d body %s", name, resp.StatusCode, len(fe.summaries), body)
		}
	}
}

func TestCompactDoesNotTouchOtherBadRequests(t *testing.T) {
	srv, fe, _ := compactRig(t, 100000)
	msgs := convo(2, true)
	msgs[1]["content"] = "BADREQUEST"
	resp, body := cpost(t, srv, msgs, nil, nil)
	if resp.StatusCode != 400 || !strings.Contains(string(body), "something else is wrong") || len(fe.summaries) != 0 {
		t.Fatalf("status %d body %s", resp.StatusCode, body)
	}
}

func TestCompactFailureReturnsTheOriginalError(t *testing.T) {
	srv, fe, _ := compactRig(t, 1500)
	fe.failSummary = true
	resp, body := cpost(t, srv, convo(10, true), nil, nil)
	if resp.StatusCode != 400 || !strings.Contains(string(body), "exceed_context_size_error") || resp.Header.Get("X-AICF-Compact-Failed") == "" {
		t.Fatalf("status %d header %q body %s", resp.StatusCode, resp.Header.Get("X-AICF-Compact-Failed"), body)
	}
}

func TestCompactCachesSummariesByPrefix(t *testing.T) {
	srv, fe, _ := compactRig(t, 1500)
	msgs := convo(10, true)
	cpost(t, srv, msgs, nil, nil)
	if len(fe.summaries) != 1 {
		t.Fatalf("first call: %d summaries", len(fe.summaries))
	}
	resp, _ := cpost(t, srv, msgs, nil, nil) // client resends the same history
	if len(fe.summaries) != 1 || resp.Header.Get("X-AICF-Compact-Cache") != "hit" {
		t.Fatalf("identical history re-summarised: %d summaries, cache header %q", len(fe.summaries), resp.Header.Get("X-AICF-Compact-Cache"))
	}
	grown := append(append([]map[string]any{}, msgs...),
		map[string]any{"role": "assistant", "content": "trả lời mới " + strings.Repeat("z", 150)},
		map[string]any{"role": "user", "content": "câu hỏi mới nữa"})
	cpost(t, srv, grown, nil, nil) // history grew by two messages
	if len(fe.summaries) != 2 {
		t.Fatalf("growing history should cost one more (incremental) summary, got %d", len(fe.summaries))
	}
	if !strings.Contains(fe.summaries[1], "Previous memory note") || !strings.Contains(fe.summaries[1], "SUMMARY1") {
		t.Error("incremental summary did not start from the cached note")
	}
}

func TestCompactKeepsToolCallsPairedWithTheirResults(t *testing.T) {
	srv, fe, _ := compactRig(t, 1500)
	msgs := convo(8, true)
	// the last 4 messages would start with a tool result: the cut must move back to a user message
	tail := []map[string]any{
		{"role": "user", "content": "chạy thử giúp"},
		{"role": "assistant", "content": "", "tool_calls": []any{map[string]any{"id": "c1", "type": "function", "function": map[string]any{"name": "run", "arguments": "{}"}}}},
		{"role": "tool", "tool_call_id": "c1", "content": "kết quả"},
		{"role": "assistant", "content": "xong"},
		{"role": "user", "content": "cảm ơn"},
	}
	msgs = append(msgs[:len(msgs)-1], tail...)
	resp, body := cpost(t, srv, msgs, nil, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("status %d: %s", resp.StatusCode, body)
	}
	fe.mu.Lock()
	var req struct {
		Messages []map[string]any `json:"messages"`
	}
	json.Unmarshal(fe.chats[len(fe.chats)-1], &req)
	fe.mu.Unlock()
	for i, m := range req.Messages {
		if m["role"] == "tool" && (i == 0 || req.Messages[i-1]["role"] != "assistant") {
			t.Fatalf("tool result without its assistant tool call at %d", i)
		}
	}
	if first := req.Messages[1]; first["role"] != "user" {
		t.Errorf("kept part starts with %v, want user", first["role"])
	}
}

func TestCompactAutoModeActsBeforeSending(t *testing.T) {
	srv, fe, up := compactRig(t, 1500)
	engineCtx.mu.Lock()
	engineCtx.m[up.URL] = ctxEntry{n: 1000, at: time.Now()}
	engineCtx.mu.Unlock()
	resp, _ := cpost(t, srv, convo(10, true), map[string]string{"X-AICF-Compact": "auto:0.5"}, nil)
	if resp.StatusCode != 200 || !strings.Contains(resp.Header.Get("X-AICF-Compacted"), "trigger=auto") || fe.refused != 0 {
		t.Fatalf("status %d header %q refused %d", resp.StatusCode, resp.Header.Get("X-AICF-Compacted"), fe.refused)
	}
}

func TestCompactOptionParsing(t *testing.T) {
	h := http.Header{}
	h.Set("X-AICF-Compact", "auto:0.6")
	h.Set("X-AICF-Compact-Keep", "6")
	o, body := compactOptionsFor(h, []byte(`{"model":"m","messages":[]}`))
	if o.Mode != CompactAuto || o.Threshold != 0.6 || o.Keep != 6 || string(body) != `{"model":"m","messages":[]}` {
		t.Fatalf("%+v %s", o, body)
	}
	o, body = compactOptionsFor(http.Header{}, []byte(`{"model":"m","aicf_compact":{"mode":"off","keep":9},"messages":[]}`))
	if o.Mode != CompactOff || o.Keep != 9 || strings.Contains(string(body), "aicf_compact") {
		t.Fatalf("%+v %s", o, body)
	}
	if o, _ := compactOptionsFor(http.Header{}, []byte(`{}`)); o.Mode != CompactOverflow || o.Keep != 4 {
		t.Fatalf("defaults wrong: %+v", o)
	}
	if _, err := ParseCompactMode("sometimes"); err == nil {
		t.Error("bad mode accepted")
	}
}
