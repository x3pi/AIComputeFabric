package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestEngineContextLengthAndModelEntries(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/props" {
			w.Write([]byte(`{"default_generation_settings":{"n_ctx":65536}}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()
	if n := engineContextLength(http.DefaultClient, srv.URL); n != 65536 {
		t.Fatalf("n_ctx = %d", n)
	}
	if cachedEngineContext(srv.URL) != 65536 {
		t.Fatal("cache not filled")
	}
	e := withContextLength(BuildOpenAIModelEntries("Qwen3.8-27B-UD-Q4_K_M.gguf"), 65536)
	for _, m := range e {
		if m["context_length"] != 65536 {
			t.Errorf("%v has no context_length", m["id"])
		}
	}
	if n := engineContextLength(http.DefaultClient, "http://127.0.0.1:1"); n != 0 {
		t.Errorf("unreachable engine gave %d", n)
	}
	if got := withContextLength(BuildOpenAIModelEntries("x"), 0)[0]; got["context_length"] != nil {
		t.Error("unknown size must not be advertised")
	}
}
