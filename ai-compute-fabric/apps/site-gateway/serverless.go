package main

import (
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
)

// Serverless GPU/Python gateway (ADR-003 D1/D2). The gateway authenticates, audits and size-limits; the Python service
// (serverless/, :18110) does scheduling and talks NATS. The gateway never sees or unpickles user code.
//
// Trust: the service accepts X-Aicf-Tenant only together with X-Aicf-Proxy-Secret, which this proxy sets itself and which
// is stripped from every inbound request, so a client can never choose its own tenant.
const serverlessPrefix = "/api/serverless/"

// MaxServerlessBody bounds one request: an 8 MiB blob is ~11 MiB as base64 JSON.
const MaxServerlessBody = 16 << 20

// ReadSecretFile returns the trimmed content of a secret file; it must be at least 16 bytes.
func ReadSecretFile(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	s := strings.TrimSpace(string(b))
	if len(s) < 16 {
		return "", fmt.Errorf("%s: secret must be at least 16 characters", path)
	}
	return s, nil
}

// ServerlessProxy forwards /api/serverless/* to the Python service.
func ServerlessProxy(target, secret string, maxBody int64, leases ...*LeaseManager) (http.Handler, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("--serverless-url must be an http(s) URL, got %q", target)
	}
	if len(secret) < 16 {
		return nil, fmt.Errorf("--serverless-secret-file is required with --serverless-url (shared secret, >= 16 chars)")
	}
	var leaseHandler http.Handler
	if len(leases) > 0 && leases[0] != nil {
		leaseHandler = LeaseHTTPHandler(leases[0], secret)
	}
	rp := &httputil.ReverseProxy{
		FlushInterval: -1, // log streams (SSE) must not be buffered
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(u)
			pr.Out.URL.Path = "/" + strings.TrimPrefix(pr.In.URL.Path, serverlessPrefix)
			pr.Out.URL.RawPath = ""
			pr.Out.Header.Del("Authorization") // never forward raw credentials downstream
			pr.Out.Header.Del("X-Aicf-Proxy-Secret")
			for _, h := range []string{"X-Aicf-Tenant", "X-Aicf-User", "X-Aicf-Role"} {
				if v := pr.In.Header.Get(h); v != "" { // set by the auth layer above, never by the client
					pr.Out.Header.Set(h, v)
				} else {
					pr.Out.Header.Del(h)
				}
			}
			pr.Out.Header.Set("X-Aicf-Proxy-Secret", secret)
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			http.Error(w, "serverless service unavailable", http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/serverless/v1/_lease/") {
			if leaseHandler == nil {
				http.Error(w, "lease bridge not configured", http.StatusServiceUnavailable)
				return
			}
			leaseHandler.ServeHTTP(w, r)
			return
		}
		if r.ContentLength > maxBody {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		rp.ServeHTTP(w, r)
	}), nil
}

// serverlessDisabled answers when --serverless-url is not configured (the default).
var serverlessDisabled = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	http.Error(w, "serverless is disabled on this gateway (start it with --serverless-url)", http.StatusServiceUnavailable)
})
