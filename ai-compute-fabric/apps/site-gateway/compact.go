package main

// Conversation compaction for POST /v1/chat/completions.
//
// OpenAI-style APIs are stateless: the client resends the whole history, so a long conversation eventually exceeds the
// engine's context window and the engine answers HTTP 400 (exceed_context_size_error). The gateway can fix that by
// summarising the old part of the history and retrying, so no client change is needed.
//
// Modes (default "overflow", chosen with -compact-default; per request with the X-AICF-Compact header or a top-level
// "aicf_compact" field):
//
//	off       never touch the request
//	overflow  only when the engine has already refused the request as too long (such a request would fail anyway,
//	          so no working client sees a difference)
//	auto[:T]  also compact BEFORE sending when the estimated size passes T x context (default 0.7)
//
// What is kept: leading system messages, the last N messages (default 4, always starting at a user message so tool
// calls stay paired with their results). Everything in between becomes one summary that is appended to the system
// message (or becomes the system message). Summaries are cached by prefix, so a client that keeps resending a growing
// history only pays for the new part.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type CompactMode int

const (
	CompactOff CompactMode = iota
	CompactOverflow
	CompactAuto
)

// DefaultCompact is the gateway-wide default (-compact-default).
var DefaultCompact = CompactOverflow

func ParseCompactMode(v string) (CompactMode, error) {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "off", "false", "no", "0", "none":
		return CompactOff, nil
	case "", "overflow", "on", "true", "yes":
		return CompactOverflow, nil
	case "auto", "eager":
		return CompactAuto, nil
	}
	return CompactOverflow, fmt.Errorf("compaction mode must be off, overflow or auto, got %q", v)
}

type compactOpts struct {
	Mode      CompactMode
	Threshold float64 // fraction of the context window that triggers auto mode
	Keep      int     // trailing messages kept verbatim
}

func defaultCompactOpts() compactOpts {
	return compactOpts{Mode: DefaultCompact, Threshold: 0.7, Keep: 4}
}

func clampKeep(k int) int {
	if k < 2 {
		return 2
	}
	if k > 40 {
		return 40
	}
	return k
}

// compactOptionsFor reads the per-request settings and strips the non-standard body field before forwarding.
func compactOptionsFor(h http.Header, body []byte) (compactOpts, []byte) {
	o := defaultCompactOpts()
	apply := func(mode string) {
		m, thr, _ := strings.Cut(strings.TrimSpace(mode), ":")
		if pm, err := ParseCompactMode(m); err == nil {
			o.Mode = pm
		}
		if f, err := strconv.ParseFloat(thr, 64); err == nil && f >= 0.3 && f <= 0.95 {
			o.Threshold = f
		}
	}
	if v := h.Get("X-AICF-Compact"); v != "" {
		apply(v)
	}
	if v, err := strconv.Atoi(h.Get("X-AICF-Compact-Keep")); err == nil {
		o.Keep = clampKeep(v)
	}
	if !bytes.Contains(body, []byte("aicf_compact")) {
		return o, body
	}
	var root map[string]json.RawMessage
	if json.Unmarshal(body, &root) != nil {
		return o, body
	}
	raw, ok := root["aicf_compact"]
	if !ok {
		return o, body
	}
	delete(root, "aicf_compact")
	var s string
	var obj struct {
		Mode      string  `json:"mode"`
		Threshold float64 `json:"threshold"`
		Keep      int     `json:"keep"`
	}
	if json.Unmarshal(raw, &s) == nil {
		if h.Get("X-AICF-Compact") == "" {
			apply(s)
		}
	} else if json.Unmarshal(raw, &obj) == nil {
		if obj.Mode != "" && h.Get("X-AICF-Compact") == "" {
			apply(obj.Mode)
		}
		if obj.Threshold >= 0.3 && obj.Threshold <= 0.95 {
			o.Threshold = obj.Threshold
		}
		if obj.Keep > 0 {
			o.Keep = clampKeep(obj.Keep)
		}
	}
	out, err := json.Marshal(root)
	if err != nil {
		return o, body
	}
	return o, out
}

// ---- engine overflow detection ----

type overflowInfo struct {
	hit     bool
	status  int
	header  http.Header
	body    []byte
	nCtx    int
	nPrompt int
}

func (o *overflowInfo) replay(w http.ResponseWriter) {
	for k, v := range o.header {
		if k == "Content-Length" {
			continue
		}
		w.Header()[k] = v
	}
	w.WriteHeader(o.status)
	_, _ = w.Write(o.body)
}

func parseOverflow(body []byte) (nCtx, nPrompt int, ok bool) {
	var e struct {
		Error struct {
			Type    string `json:"type"`
			NCtx    int    `json:"n_ctx"`
			NPrompt int    `json:"n_prompt_tokens"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &e) != nil || e.Error.Type != "exceed_context_size_error" {
		return 0, 0, false
	}
	return e.Error.NCtx, e.Error.NPrompt, true
}

// ---- message helpers ----

type chatMsg = map[string]any

func msgRole(m chatMsg) string { s, _ := m["role"].(string); return s }

const imageTokenAllowance = 1500

// estimateTokens is deliberately rough (3 chars per token + a flat allowance per image); it only decides when to try.
func estimateTokens(msgs []chatMsg) int {
	chars, imgs := 0, 0
	for _, m := range msgs {
		switch c := m["content"].(type) {
		case string:
			chars += len(c)
		case []any:
			for _, p := range c {
				if pm, ok := p.(map[string]any); ok {
					if t, _ := pm["type"].(string); t == "image_url" || t == "input_image" {
						imgs++
					} else if s, ok := pm["text"].(string); ok {
						chars += len(s)
					}
				}
			}
		}
		if tc, ok := m["tool_calls"]; ok {
			b, _ := json.Marshal(tc)
			chars += len(b)
		}
	}
	return chars/3 + imgs*imageTokenAllowance
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…[cắt bớt]"
}

// transcriptOf renders old messages as plain text for the summariser (images and long tool outputs are shortened).
func transcriptOf(msgs []chatMsg) []string {
	out := make([]string, 0, len(msgs))
	for _, m := range msgs {
		role := msgRole(m)
		var text strings.Builder
		switch c := m["content"].(type) {
		case string:
			text.WriteString(c)
		case []any:
			for _, p := range c {
				pm, _ := p.(map[string]any)
				if t, _ := pm["type"].(string); t == "image_url" || t == "input_image" {
					text.WriteString("[ảnh] ")
				} else if s, ok := pm["text"].(string); ok {
					text.WriteString(s)
					text.WriteString(" ")
				}
			}
		}
		if tcs, ok := m["tool_calls"].([]any); ok {
			for _, tc := range tcs {
				if tm, ok := tc.(map[string]any); ok {
					if fn, ok := tm["function"].(map[string]any); ok {
						a, _ := fn["arguments"].(string)
						fmt.Fprintf(&text, "\n[gọi công cụ %v(%s)]", fn["name"], clip(a, 500))
					}
				}
			}
		}
		label := map[string]string{"user": "Người dùng", "assistant": "Trợ lý", "tool": "Kết quả công cụ", "system": "Hệ thống", "developer": "Hệ thống"}[role]
		if label == "" {
			label = role
		}
		s := strings.TrimSpace(text.String())
		if role == "tool" {
			s = clip(s, 1500)
		}
		out = append(out, label+": "+s)
	}
	return out
}

// ---- summary cache (prefix-keyed) ----

type summaryCache struct {
	mu sync.Mutex
	m  map[string]cacheItem
}
type cacheItem struct {
	summary string
	at      time.Time
}

var sumCache = &summaryCache{m: map[string]cacheItem{}}

const (
	sumCacheTTL = time.Hour
	sumCacheMax = 512
)

func prefixKey(tenant string, old []chatMsg, k int) string {
	h := sha256.New()
	h.Write([]byte(tenant))
	for _, m := range old[:k] {
		b, _ := json.Marshal(m)
		h.Write([]byte{0})
		h.Write(b)
	}
	return hex.EncodeToString(h.Sum(nil)[:16])
}

func (c *summaryCache) get(key string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	it, ok := c.m[key]
	if !ok || time.Since(it.at) > sumCacheTTL {
		delete(c.m, key)
		return "", false
	}
	return it.summary, true
}

func (c *summaryCache) put(key, s string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.m) >= sumCacheMax {
		oldestK, oldestT := "", time.Now()
		for k, v := range c.m {
			if v.at.Before(oldestT) {
				oldestK, oldestT = k, v.at
			}
		}
		delete(c.m, oldestK)
	}
	c.m[key] = cacheItem{s, time.Now()}
}

// ---- counters (exported on /metrics) ----

var (
	compactOverflowN atomic.Uint64
	compactAutoN     atomic.Uint64
	compactFailedN   atomic.Uint64
	compactCacheHitN atomic.Uint64
)

// ---- the summariser ----

const summarySystemPrompt = "You compress the memory of a conversation. Write a SHORT but COMPLETE memory note from the conversation below: " +
	"the user's goals and requests, names, numbers, decisions already made, important conclusions, unfinished work. " +
	"Keep code, commands, paths and identifiers VERBATIM. Add nothing new and no commentary. " +
	"Write in the same language as the user, as bullet points. If a previous memory note is included, merge it into the new one."

func (in *Inference) summarize(ctx context.Context, rt Route, model, userText string) (string, error) {
	payload := map[string]any{
		"model": model, "stream": false, "max_tokens": 1200, "temperature": 0.2,
		"enable_thinking": false, "chat_template_kwargs": map[string]any{"enable_thinking": false},
		"messages": []any{
			map[string]any{"role": "system", "content": summarySystemPrompt},
			map[string]any{"role": "user", "content": userText},
		},
	}
	b, _ := json.Marshal(payload)
	cctx, cancel := context.WithTimeout(ctx, 150*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(cctx, http.MethodPost, rt.URL+"/v1/chat/completions", bytes.NewReader(b))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := in.Client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("summariser answered HTTP %d", resp.StatusCode)
	}
	var d struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if json.Unmarshal(raw, &d) != nil || len(d.Choices) == 0 || strings.TrimSpace(d.Choices[0].Message.Content) == "" {
		return "", errors.New("summariser returned no text")
	}
	return strings.TrimSpace(d.Choices[0].Message.Content), nil
}

// summarizeOld builds the memory note for the old messages: reuses the longest cached prefix, then folds the
// remaining messages in chunks that fit comfortably in the context window.
func (in *Inference) summarizeOld(ctx context.Context, rt Route, model, tenant string, old []chatMsg, nCtx int) (summary string, cacheHit bool, err error) {
	start := 0
	for k := len(old); k >= 1 && k >= len(old)-16; k-- {
		if s, ok := sumCache.get(prefixKey(tenant, old, k)); ok {
			summary, start, cacheHit = s, k, true
			break
		}
	}
	if start == len(old) {
		return summary, true, nil
	}
	if nCtx <= 0 {
		nCtx = 32768
	}
	chunkChars := nCtx * 3 / 2 // about half a window of text per summarising call
	lines := transcriptOf(old[start:])
	var chunk strings.Builder
	flush := func() error {
		if chunk.Len() == 0 {
			return nil
		}
		text := chunk.String()
		if summary != "" {
			text = "Previous memory note:\n" + summary + "\n\nNew part of the conversation:\n" + text
		}
		chunk.Reset()
		s, e := in.summarize(ctx, rt, model, text)
		if e != nil {
			return e
		}
		summary = s
		return nil
	}
	for _, ln := range lines {
		if len(ln) > chunkChars { // one gigantic message: keep head and tail
			ln = ln[:chunkChars/2] + "\n…[cắt bớt phần giữa]…\n" + ln[len(ln)-chunkChars/2:]
		}
		if chunk.Len()+len(ln) > chunkChars {
			if err = flush(); err != nil {
				return "", false, err
			}
		}
		chunk.WriteString(ln)
		chunk.WriteString("\n\n")
	}
	if err = flush(); err != nil {
		return "", false, err
	}
	sumCache.put(prefixKey(tenant, old, len(old)), summary)
	return summary, cacheHit && start > 0, nil
}

type compactResult struct {
	Body       []byte
	Compacted  int // messages replaced
	SummaryTok int
	CacheHit   bool
}

// compactBody rewrites an OpenAI chat request so its old history is replaced by a summary.
func (in *Inference) compactBody(ctx context.Context, rt Route, body []byte, o compactOpts, tenant string, nCtx int) (compactResult, error) {
	var root map[string]json.RawMessage
	if err := json.Unmarshal(body, &root); err != nil {
		return compactResult{}, err
	}
	var msgs []chatMsg
	if err := json.Unmarshal(root["messages"], &msgs); err != nil {
		return compactResult{}, errors.New("no messages to compact")
	}
	lead := 0
	for lead < len(msgs) && (msgRole(msgs[lead]) == "system" || msgRole(msgs[lead]) == "developer") {
		lead++
	}
	rest := msgs[lead:]
	cut := len(rest) - clampKeep(o.Keep)
	for cut > 0 && msgRole(rest[cut]) != "user" { // never split an assistant tool call from its results
		cut--
	}
	if cut <= 0 {
		return compactResult{}, errors.New("not enough old messages to compact")
	}
	old, kept := rest[:cut], rest[cut:]
	var model string
	_ = json.Unmarshal(root["model"], &model)
	summary, hit, err := in.summarizeOld(ctx, rt, model, tenant, old, nCtx)
	if err != nil {
		return compactResult{}, err
	}
	note := "Memory of the earlier part of this conversation (compressed automatically by the gateway):\n" + summary
	out := make([]chatMsg, 0, len(msgs))
	if lead > 0 {
		out = append(out, msgs[:lead]...)
		last := chatMsg{}
		for k, v := range out[lead-1] {
			last[k] = v
		}
		if s, ok := last["content"].(string); ok {
			last["content"] = s + "\n\n" + note
			out[lead-1] = last
		} else {
			out = append(out, chatMsg{"role": "system", "content": note})
		}
	} else {
		out = append(out, chatMsg{"role": "system", "content": note})
	}
	out = append(out, kept...)
	root["messages"], _ = json.Marshal(out)
	nb, err := json.Marshal(root)
	if err != nil {
		return compactResult{}, err
	}
	return compactResult{Body: nb, Compacted: len(old), SummaryTok: len(summary) / 3, CacheHit: hit}, nil
}

func setCompactHeaders(w http.ResponseWriter, r compactResult, trigger string) {
	w.Header().Set("X-AICF-Compacted", fmt.Sprintf("%d messages -> memory note (~%d tokens), trigger=%s", r.Compacted, r.SummaryTok, trigger))
	if r.CacheHit {
		w.Header().Set("X-AICF-Compact-Cache", "hit")
	}
}
