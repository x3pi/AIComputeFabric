package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSnapshotWithoutAdapterHasEmptyLora(t *testing.T) {
	g, _, _, _ := newRig(t)
	if s := g.Snapshot(); s.Lora != "" || s.Model != "" {
		t.Fatalf("model=%q lora=%q", s.Model, s.Lora)
	}
}

func TestSwitchRestartsWithNewModelOnlyWhenGPUIsFree(t *testing.T) {
	g, gpu, eng, clock := newRig(t)
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick() // running
	g.Switch("/m/b.gguf", "/a/x.gguf")
	if eng.running || g.st != StStopped {
		t.Fatalf("a switch must stop the engine first: running=%v state=%s", eng.running, g.st)
	}
	if m, l := eng.Describe(); m != "/m/b.gguf" || l != "/a/x.gguf" {
		t.Fatalf("engine not reconfigured: %q %q", m, l)
	}
	gpu.mu.Lock()
	gpu.external = 12288 - 1000 // the user is using the GPU: no restart allowed
	gpu.mu.Unlock()
	*clock = clock.Add(30 * time.Second)
	g.Tick()
	if eng.starts != 1 {
		t.Fatalf("switch must not take VRAM the user holds, starts=%d", eng.starts)
	}
	gpu.mu.Lock()
	gpu.external = 0
	gpu.mu.Unlock()
	g.Tick()
	*clock = clock.Add(11 * time.Second)
	g.Tick()
	if eng.starts != 2 || g.st != StRunning {
		t.Fatalf("expected restart with new model: starts=%d state=%s", eng.starts, g.st)
	}
	if s := g.Snapshot(); s.Model != "b.gguf" || s.Lora != "x.gguf" {
		t.Fatalf("snapshot shows %q+%q", s.Model, s.Lora)
	}
}

func engineRig(t *testing.T) (http.Handler, *fakeEngine, string) {
	t.Helper()
	g, _, eng, _ := newRig(t)
	root := t.TempDir()
	models, adapters := filepath.Join(root, "m"), filepath.Join(root, "a")
	os.MkdirAll(models, 0o755)
	os.MkdirAll(adapters, 0o755)
	os.MkdirAll(filepath.Join(models, "sub"), 0o755)
	for _, f := range []string{"m/sub/c.gguf", "m/a.gguf", "m/b.gguf", "a/x.gguf", "secret.gguf"} {
		os.WriteFile(filepath.Join(root, f), []byte("x"), 0o644)
	}
	return HandlerWith(g, EngineDirs{Models: models, Adapters: adapters}), eng, root
}

func post(h http.Handler, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest("POST", "/api/engine", strings.NewReader(body))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestEngineAPIAcceptsOnlyPlainNamesInsideConfiguredDirs(t *testing.T) {
	h, eng, root := engineRig(t)
	for _, body := range []string{
		`{"model":"../secret.gguf"}`, `{"model":"/etc/passwd"}`, `{"model":"m/a.gguf"}`, `{"model":"sub/c.gguf"}`, `{"model":"sub/../a.gguf"}`, `{"model":"a.txt"}`, `{"model":""}`,
		`{"model":"nope.gguf"}`, `{"model":"a.gguf","lora":"../secret.gguf"}`, `{"model":"a.gguf","lora":"missing.gguf"}`, `not json`,
	} {
		if w := post(h, body); w.Code != 400 {
			t.Errorf("%s -> %d, want 400", body, w.Code)
		}
	}
	if m, _ := eng.Describe(); m != "" {
		t.Fatalf("a refused request changed the engine: %q", m)
	}
	if w := post(h, `{"model":"b.gguf","lora":"x.gguf"}`); w.Code != 202 {
		t.Fatalf("valid request -> %d %s", w.Code, w.Body)
	}
	if m, l := eng.Describe(); m != filepath.Join(root, "m", "b.gguf") || l != filepath.Join(root, "a", "x.gguf") {
		t.Fatalf("engine got %q %q", m, l)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", "/api/engine", nil))
	if !strings.Contains(w.Body.String(), `"a.gguf"`) || !strings.Contains(w.Body.String(), `"x.gguf"`) || strings.Contains(w.Body.String(), "secret") {
		t.Fatalf("listing: %s", w.Body)
	}
}

func TestEngineAPIDisabledWithoutModelsDir(t *testing.T) {
	g, _, _, _ := newRig(t)
	h := Handler(g)
	if w := post(h, `{"model":"a.gguf"}`); w.Code != http.StatusForbidden {
		t.Fatalf("got %d, want 403", w.Code)
	}
}
