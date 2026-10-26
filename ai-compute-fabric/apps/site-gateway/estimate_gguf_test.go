package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func fakeSidecar(t *testing.T, status int, body string, seen *string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if seen != nil {
			*seen = r.URL.Query().Get("file")
		}
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
}

func postGGUF(h http.HandlerFunc, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(http.MethodPost, "/api/v1/models/estimate-vram", bytes.NewBufferString(body)))
	return rec
}

const factsJSON = `{"architecture":"qwen3","name":"X","quantization":"Q4_K_M","layers":36,"layers_attn":36,"n_kv":8,"head_dim":128,"weights_bytes":5000000000,"non_repeating_bytes":700000000,"embedding_cpu_bytes":350000000}`

func TestEstimateFromGGUFUsesFileFactsAndKeepsEmbeddingOffTheGPU(t *testing.T) {
	var seen string
	s := fakeSidecar(t, 200, factsJSON, &seen)
	defer s.Close()
	vramSidecar = NewSidecar(s.URL)
	defer func() { vramSidecar = nil }()
	rec := postGGUF(estimateFromGGUF(nil), `{"gguf":"sub/x.gguf","ctx_per_slot":4096,"parallel":1,"flash_attn":true}`)
	if rec.Code != 200 || seen != "sub/x.gguf" {
		t.Fatalf("code %d, sidecar asked for %q: %s", rec.Code, seen, rec.Body)
	}
	var out EstimateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.Model.WeightsBytes != 5000000000-350000000 || out.Model.NonRepeatingBytes != 700000000-350000000 {
		t.Errorf("untied embedding must come off the GPU weights: %+v", out.Model)
	}
	if out.Model.Confidence != "ESTIMATED" || out.Model.ID != "gguf:sub/x.gguf" {
		t.Errorf("a model outside the calibrated set must say ESTIMATED: %+v", out.Model)
	}
}

func TestEstimateFromGGUFErrors(t *testing.T) {
	vramSidecar = nil
	if c := postGGUF(estimateFromGGUF(nil), `{"gguf":"x.gguf"}`).Code; c != http.StatusNotImplemented {
		t.Errorf("without a sidecar: %d", c)
	}
	for _, c := range []struct {
		name   string
		status int
		body   string
		reqs   string
		want   int
	}{
		{"outside root", 403, `{"error":"file is outside the models root"}`, `{"gguf":"../x.gguf"}`, 403},
		{"missing", 404, `{"error":"no such model file"}`, `{"gguf":"x.gguf"}`, 404},
		{"sidecar crashed", 500, `{"error":"boom"}`, `{"gguf":"x.gguf"}`, 502},
		{"garbage facts", 200, `{"layers":0}`, `{"gguf":"x.gguf"}`, 502},
		{"no file name", 200, factsJSON, `{"ctx_per_slot":4096}`, 400},
		{"bad json", 200, factsJSON, `{`, 400},
	} {
		s := fakeSidecar(t, c.status, c.body, nil)
		vramSidecar = NewSidecar(s.URL)
		if got := postGGUF(estimateFromGGUF(nil), c.reqs).Code; got != c.want {
			t.Errorf("%s: got %d want %d", c.name, got, c.want)
		}
		s.Close()
	}
	vramSidecar = NewSidecar("http://127.0.0.1:1")
	defer func() { vramSidecar = nil }()
	if c := postGGUF(estimateFromGGUF(nil), `{"gguf":"x.gguf"}`).Code; c != http.StatusBadGateway {
		t.Errorf("unreachable sidecar: %d", c)
	}
	rec := httptest.NewRecorder()
	estimateFromGGUF(nil)(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET: %d", rec.Code)
	}
}
