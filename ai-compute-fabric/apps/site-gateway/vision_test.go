package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

var tinyPNG = []byte("\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde")

func chatBody(urls ...string) []byte {
	parts := []any{map[string]any{"type": "text", "text": "what is this"}}
	for _, u := range urls {
		parts = append(parts, map[string]any{"type": "image_url", "image_url": map[string]any{"url": u, "detail": "low"}})
	}
	b, _ := json.Marshal(map[string]any{"model": "m", "messages": []any{map[string]any{"role": "user", "content": parts}}})
	return b
}

func firstImageURL(t *testing.T, body []byte) string {
	t.Helper()
	var d struct {
		Messages []struct {
			Content []struct {
				Type     string `json:"type"`
				ImageURL struct {
					URL    string `json:"url"`
					Detail string `json:"detail"`
				} `json:"image_url"`
			} `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(body, &d); err != nil {
		t.Fatal(err)
	}
	for _, p := range d.Messages[0].Content {
		if p.Type == "image_url" {
			return p.ImageURL.URL
		}
	}
	t.Fatal("no image part")
	return ""
}

func TestNormalizeImageInputsNoImageIsUntouched(t *testing.T) {
	in := []byte(`{"model":"m","messages":[{"role":"user","content":"hi"}]}`)
	out, err := NormalizeImageInputs(context.Background(), in)
	if err != nil || string(out) != string(in) {
		t.Fatalf("got %q, %v", out, err)
	}
}

func TestNormalizeImageInputsKeepsDataURL(t *testing.T) {
	u := "data:image/png;base64," + base64.StdEncoding.EncodeToString(tinyPNG)
	out, err := NormalizeImageInputs(context.Background(), chatBody(u))
	if err != nil || firstImageURL(t, out) != u {
		t.Fatalf("data URL changed: %v", err)
	}
}

func TestNormalizeImageInputsFetchesRemoteImage(t *testing.T) {
	allowPrivateImageFetch = true
	defer func() { allowPrivateImageFetch = false }()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/octet-stream") // type is sniffed, not trusted
		w.Write(tinyPNG)
	}))
	defer srv.Close()
	out, err := NormalizeImageInputs(context.Background(), chatBody(srv.URL+"/a.png"))
	if err != nil {
		t.Fatal(err)
	}
	got := firstImageURL(t, out)
	if !strings.HasPrefix(got, "data:image/png;base64,") {
		t.Fatalf("not inlined as PNG data URL: %.40s", got)
	}
	if !strings.Contains(string(out), `"detail":"low"`) {
		t.Error("detail was dropped")
	}
}

func TestNormalizeImageInputsRefusesPrivateAddresses(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(tinyPNG) }))
	defer srv.Close() // listens on 127.0.0.1
	for _, u := range []string{srv.URL, "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/x.png", "http://[::1]:80/x.png", "http://192.168.1.40:18080/x.png"} {
		if _, err := NormalizeImageInputs(context.Background(), chatBody(u)); err == nil {
			t.Errorf("%s was fetched, SSRF guard failed", u)
		}
	}
}

func TestNormalizeImageInputsRejectsNonImagesAndOtherSchemes(t *testing.T) {
	allowPrivateImageFetch = true
	defer func() { allowPrivateImageFetch = false }()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("<html>not an image</html>")) }))
	defer srv.Close()
	for _, u := range []string{srv.URL, "file:///etc/passwd", "ftp://example.com/a.png"} {
		if _, err := NormalizeImageInputs(context.Background(), chatBody(u)); err == nil {
			t.Errorf("%s accepted", u)
		}
	}
}

func TestNormalizeImageInputsLimitsImageCount(t *testing.T) {
	u := "data:image/png;base64," + base64.StdEncoding.EncodeToString(tinyPNG)
	urls := make([]string, maxImagesPerRequest+1)
	for i := range urls {
		urls[i] = u
	}
	if _, err := NormalizeImageInputs(context.Background(), chatBody(urls...)); err == nil {
		t.Fatal("too many images accepted")
	}
}

func TestNormalizeImageInputsAcceptsPlainStringImageURL(t *testing.T) {
	u := "data:image/png;base64," + base64.StdEncoding.EncodeToString(tinyPNG)
	body := []byte(`{"messages":[{"role":"user","content":[{"type":"image_url","image_url":"` + u + `"}]}]}`)
	out, err := NormalizeImageInputs(context.Background(), body)
	if err != nil || firstImageURL(t, out) != u {
		t.Fatalf("string image_url not normalised: %v %s", err, out)
	}
}

func TestOllamaImagesToParts(t *testing.T) {
	b64 := base64.StdEncoding.EncodeToString(tinyPNG)
	in := []json.RawMessage{json.RawMessage(`{"role":"user","content":"colour?","images":["` + b64 + `"]}`), json.RawMessage(`{"role":"assistant","content":"red"}`)}
	out := ollamaImagesToParts(in)
	if string(out[1]) != string(in[1]) {
		t.Error("message without images changed")
	}
	if strings.Contains(string(out[0]), `"images"`) {
		t.Error("images key kept")
	}
	if got := firstImageURL(t, []byte(`{"messages":[`+string(out[0])+`]}`)); !strings.HasPrefix(got, "data:image/png;base64,") {
		t.Fatalf("mime not detected: %.40s", got)
	}
}

func TestBlockedImageIP(t *testing.T) {
	for ip, want := range map[string]bool{"127.0.0.1": true, "10.1.2.3": true, "172.16.0.1": true, "192.168.1.40": true, "169.254.169.254": true,
		"100.64.0.1": true, "0.0.0.0": true, "::1": true, "fd00::1": true, "8.8.8.8": false, "1.1.1.1": false} {
		if got := blockedImageIP(net.ParseIP(ip)); got != want {
			t.Errorf("%s: blocked=%v want %v", ip, got, want)
		}
	}
}

func TestVisionModelAdvertisesInputModalities(t *testing.T) {
	e := BuildOpenAIModelEntry("Qwen3.8-27B-UD-Q4_K_M.gguf")
	mods, _ := e["input_modalities"].([]string)
	if len(mods) != 2 || mods[1] != "image" {
		t.Errorf("input_modalities = %v", e["input_modalities"])
	}
	if _, ok := BuildOpenAIModelEntry("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf")["architecture"]; ok {
		t.Error("text-only model advertises architecture")
	}
}
