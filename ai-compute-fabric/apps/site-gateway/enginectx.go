package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"sync"
	"time"
)

// engineContextLength asks a llama-server for its per-slot context size (GET /props) so /v1/models can publish
// "context_length" (OpenRouter-style). Answers are cached for a minute; 0 means unknown.
var engineCtx = struct {
	mu sync.Mutex
	m  map[string]ctxEntry
}{m: map[string]ctxEntry{}}

type ctxEntry struct {
	n  int
	at time.Time
}

// cachedEngineContext never touches the network (listing models must not depend on a worker answering).
func cachedEngineContext(engineURL string) int {
	engineCtx.mu.Lock()
	defer engineCtx.mu.Unlock()
	return engineCtx.m[engineURL].n
}

// probeEngineContexts refreshes the cache for every routable worker; it runs in the background.
func probeEngineContexts(client *http.Client, reg *Registry) {
	for _, rt := range reg.Routable() {
		if rt.URL != "" {
			engineContextLength(client, rt.URL)
		}
	}
}

// StartEngineContextProbe keeps the cache fresh every 30 s (first probe after 2 s, when workers have registered).
func StartEngineContextProbe(client *http.Client, reg *Registry) {
	go func() {
		time.Sleep(2 * time.Second)
		for {
			probeEngineContexts(client, reg)
			time.Sleep(30 * time.Second)
		}
	}()
}

func engineContextLength(client *http.Client, engineURL string) int {
	n := 0
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	if req, err := http.NewRequestWithContext(ctx, http.MethodGet, engineURL+"/props", nil); err == nil {
		if resp, err := client.Do(req); err == nil {
			var p struct {
				Default struct {
					NCtx int `json:"n_ctx"`
				} `json:"default_generation_settings"`
			}
			if resp.StatusCode == http.StatusOK && json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&p) == nil {
				n = p.Default.NCtx
			}
			resp.Body.Close()
		}
	}
	engineCtx.mu.Lock()
	engineCtx.m[engineURL] = ctxEntry{n: n, at: time.Now()}
	engineCtx.mu.Unlock()
	return n
}

// withContextLength adds context_length / max_model_len to model entries when the size is known.
func withContextLength(entries []map[string]any, n int) []map[string]any {
	if n <= 0 {
		return entries
	}
	for _, e := range entries {
		e["context_length"] = n
		e["max_model_len"] = n
	}
	return entries
}
