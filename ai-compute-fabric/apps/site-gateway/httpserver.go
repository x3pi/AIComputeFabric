package main

import (
	"net/http"
	"strings"
	"time"
)

// Timeouts for the HTTP listener. There is deliberately NO WriteTimeout: /api/fleet/stream (SSE) and streamed inference
// replies are long-lived by design. What a slow or hostile client can hold open is bounded by the read side instead:
// the header must arrive quickly (slowloris), the whole request (up to the 64 MiB Studio upload) within ReadTimeout,
// and an idle keep-alive connection is closed.
type serverTimeouts struct {
	ReadHeader, Read, Idle time.Duration
}

var defaultTimeouts = serverTimeouts{ReadHeader: 10 * time.Second, Read: 5 * time.Minute, Idle: 2 * time.Minute}

const defaultCSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; connect-src 'self' http://127.0.0.1:11434 http://localhost:11434; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"

// withSecurityHeaders attaches baseline production security headers and Cache-Control policy to every HTTP response (B4e, B4f).
func withSecurityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
		h.Set("Content-Security-Policy", defaultCSP)
		if r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
			h.Set("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
		}
		p := r.URL.Path
		if strings.HasPrefix(p, "/api/") || strings.HasPrefix(p, "/v1/") || p == "/metrics" {
			if h.Get("Cache-Control") == "" {
				h.Set("Cache-Control", "no-store")
			}
		} else if strings.HasPrefix(p, "/vendor/") {
			h.Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		next.ServeHTTP(w, r)
	})
}

func newHTTPServer(addr string, h http.Handler, t serverTimeouts) *http.Server {
	return &http.Server{Addr: addr, Handler: withSecurityHeaders(h), ReadHeaderTimeout: t.ReadHeader, ReadTimeout: t.Read, IdleTimeout: t.Idle, MaxHeaderBytes: 64 << 10}
}
