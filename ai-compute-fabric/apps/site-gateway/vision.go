package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"syscall"
	"time"
)

// Image input, OpenAI chat-completions style:
//
//	{"type":"image_url","image_url":{"url":"data:image/png;base64,..."|"https://...","detail":"low|high|auto"}}
//
// llama-server only understands data: URLs, so remote http(s) URLs are fetched here (SSRF-guarded) and inlined.
// Ollama clients send {"role":"user","content":"text","images":["<base64>"]}, which is converted to the same form.

const (
	maxRemoteImageBytes = 20 << 20
	maxImagesPerRequest = 8
)

// allowPrivateImageFetch is only flipped by tests (httptest servers listen on loopback).
var allowPrivateImageFetch = false

func blockedImageIP(ip net.IP) bool {
	if allowPrivateImageFetch {
		return false
	}
	if v4 := ip.To4(); v4 != nil {
		ip = v4
		if v4[0] == 100 && v4[1]&0xc0 == 64 { // 100.64.0.0/10 carrier-grade NAT
			return true
		}
		if v4[0] == 0 {
			return true
		}
	}
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified() || ip.IsInterfaceLocalMulticast()
}

// imageFetchClient dials only public addresses. The check runs on the resolved address at connect time, so DNS
// rebinding and redirects to internal hosts are refused too. Environment proxies are ignored on purpose.
func imageFetchClient() *http.Client {
	d := &net.Dialer{
		Timeout: 8 * time.Second,
		Control: func(network, address string, _ syscall.RawConn) error {
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				return err
			}
			ip := net.ParseIP(host)
			if ip == nil || blockedImageIP(ip) {
				return fmt.Errorf("address %s is not allowed (private, loopback or link-local)", host)
			}
			return nil
		},
	}
	return &http.Client{
		Timeout:   15 * time.Second,
		Transport: &http.Transport{Proxy: nil, DialContext: d.DialContext, DisableKeepAlives: true, ResponseHeaderTimeout: 10 * time.Second},
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 3 {
				return errors.New("too many redirects")
			}
			if req.URL.Scheme != "http" && req.URL.Scheme != "https" {
				return errors.New("redirect to a non-http(s) URL")
			}
			return nil
		},
	}
}

func imageMIME(b []byte) string {
	switch ct := http.DetectContentType(b); ct {
	case "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp":
		return ct
	}
	return ""
}

func fetchImageDataURL(ctx context.Context, raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", errors.New("only data:, http:// and https:// image URLs are supported")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "aicf-gateway-image-fetch/1")
	req.Header.Set("Accept", "image/*")
	resp, err := imageFetchClient().Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("image URL answered HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxRemoteImageBytes+1))
	if err != nil {
		return "", err
	}
	if len(data) > maxRemoteImageBytes {
		return "", fmt.Errorf("image is larger than %d MiB", maxRemoteImageBytes>>20)
	}
	mime := imageMIME(data)
	if mime == "" {
		return "", errors.New("the URL is not a PNG, JPEG, GIF, WebP or BMP image")
	}
	return "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data), nil
}

// NormalizeImageInputs rewrites image parts of an OpenAI chat request so the engine only ever sees data: URLs.
// It also accepts "image_url":"<url>" (plain string) and Responses-style {"type":"input_image","image_url":...}.
// Bodies without images are returned untouched.
func NormalizeImageInputs(ctx context.Context, body []byte) ([]byte, error) {
	if !bytes.Contains(body, []byte("image_url")) {
		return body, nil
	}
	var root map[string]json.RawMessage
	if json.Unmarshal(body, &root) != nil {
		return body, nil
	}
	var msgs []map[string]json.RawMessage
	if json.Unmarshal(root["messages"], &msgs) != nil {
		return body, nil
	}
	count, changed := 0, false
	for _, m := range msgs {
		var parts []map[string]any
		if json.Unmarshal(m["content"], &parts) != nil {
			continue
		}
		for _, p := range parts {
			t, _ := p["type"].(string)
			if t != "image_url" && t != "input_image" {
				continue
			}
			count++
			if count > maxImagesPerRequest {
				return nil, fmt.Errorf("at most %d images per request", maxImagesPerRequest)
			}
			var u string
			var detail any
			switch v := p["image_url"].(type) {
			case string:
				u = v
			case map[string]any:
				u, _ = v["url"].(string)
				detail = v["detail"]
			}
			if u == "" {
				return nil, errors.New("image_url.url is missing")
			}
			if !strings.HasPrefix(u, "data:") {
				du, err := fetchImageDataURL(ctx, u)
				if err != nil {
					return nil, fmt.Errorf("cannot use image URL: %w", err)
				}
				u = du
			}
			obj := map[string]any{"url": u}
			if detail != nil {
				obj["detail"] = detail
			}
			p["type"] = "image_url"
			p["image_url"] = obj
			changed = true
		}
		if changed {
			m["content"], _ = json.Marshal(parts)
		}
	}
	if !changed {
		return body, nil
	}
	root["messages"], _ = json.Marshal(msgs)
	return json.Marshal(root)
}

func base64ImageDataURL(b64 string) string {
	b64 = strings.TrimSpace(b64)
	if strings.HasPrefix(b64, "data:") {
		return b64
	}
	head := b64
	if len(head) > 64 {
		head = head[:64]
	}
	head = head[:len(head)/4*4]
	mime := "image/jpeg"
	if raw, err := base64.StdEncoding.DecodeString(head); err == nil {
		if m := imageMIME(raw); m != "" {
			mime = m
		}
	}
	return "data:" + mime + ";base64," + b64
}

// ollamaImagesToParts turns Ollama messages carrying "images":[base64...] into OpenAI content parts.
func ollamaImagesToParts(msgs []json.RawMessage) []json.RawMessage {
	out := make([]json.RawMessage, 0, len(msgs))
	for _, raw := range msgs {
		var m map[string]any
		if json.Unmarshal(raw, &m) != nil {
			out = append(out, raw)
			continue
		}
		imgs, _ := m["images"].([]any)
		if len(imgs) == 0 {
			out = append(out, raw)
			continue
		}
		parts := []any{}
		switch c := m["content"].(type) {
		case string:
			if c != "" {
				parts = append(parts, map[string]any{"type": "text", "text": c})
			}
		case []any:
			parts = append(parts, c...)
		}
		for _, im := range imgs {
			if s, ok := im.(string); ok && s != "" {
				parts = append(parts, map[string]any{"type": "image_url", "image_url": map[string]any{"url": base64ImageDataURL(s)}})
			}
		}
		m["content"] = parts
		delete(m, "images")
		b, _ := json.Marshal(m)
		out = append(out, b)
	}
	return out
}
