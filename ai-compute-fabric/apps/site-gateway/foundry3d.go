package main

import (
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
)

// Foundry3D reverse-proxy (ADR-005, Pha 5): tắt mặc định (-foundry3d-enabled=false).
// Gateway xác thực Bearer token / tenant API key, gắn X-Aicf-Proxy-Secret và X-Aicf-Tenant,
// giới hạn kích thước body và giới hạn số luồng đồng thời mỗi tenant.
const foundry3dPrefix = "/api/foundry3d/"

// MaxFoundry3DBody giới hạn kích thước JSON yêu cầu tạo job 3D (4 MiB).
const MaxFoundry3DBody = 4 << 20

// Foundry3DProxy chuyển tiếp /api/foundry3d/* sang dịch vụ Python foundry3d (:18120).
func Foundry3DProxy(target, secret string, maxBody int64, maxInflightPerTenant int) (http.Handler, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("-foundry3d-url must be an http(s) URL, got %q", target)
	}
	if len(secret) < 16 {
		return nil, fmt.Errorf("-foundry3d-secret-file is required when foundry3d is enabled (shared secret >= 16 chars)")
	}
	if maxBody <= 0 {
		maxBody = MaxFoundry3DBody
	}

	var mu sync.Mutex
	inflight := make(map[string]int)

	rp := &httputil.ReverseProxy{
		FlushInterval: -1, // hỗ trợ SSE (/events) không bị đệm
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(u)
			pr.Out.URL.Path = "/" + strings.TrimPrefix(pr.In.URL.Path, foundry3dPrefix)
			pr.Out.URL.RawPath = ""
			pr.Out.Header.Del("Authorization")
			pr.Out.Header.Del("X-Aicf-Proxy-Secret")
			for _, h := range []string{"X-Aicf-User", "X-Aicf-Role"} {
				if v := pr.In.Header.Get(h); v != "" {
					pr.Out.Header.Set(h, v)
				} else {
					pr.Out.Header.Del(h)
				}
			}
			tenant := strings.TrimSpace(pr.In.Header.Get("X-Aicf-Tenant"))
			if tenant == "" {
				tenant = "admin"
			}
			pr.Out.Header.Set("X-Aicf-Tenant", tenant)
			pr.Out.Header.Set("X-Aicf-Proxy-Secret", secret)
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			http.Error(w, "foundry3d service unavailable", http.StatusBadGateway)
		},
	}

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet, http.MethodHead, http.MethodPost, http.MethodDelete:
		default:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if r.ContentLength > maxBody {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
			return
		}
		tenant := strings.TrimSpace(r.Header.Get("X-Aicf-Tenant"))
		if tenant == "" {
			tenant = "admin"
		}
		if maxInflightPerTenant > 0 {
			mu.Lock()
			if inflight[tenant] >= maxInflightPerTenant {
				mu.Unlock()
				http.Error(w, "too many concurrent foundry3d requests for tenant", http.StatusTooManyRequests)
				return
			}
			inflight[tenant]++
			mu.Unlock()
			defer func() {
				mu.Lock()
				inflight[tenant]--
				if inflight[tenant] <= 0 {
					delete(inflight, tenant)
				}
				mu.Unlock()
			}()
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		rp.ServeHTTP(w, r)
	}), nil
}

// foundry3dDisabled trả về 503 khi -foundry3d-enabled=false (mặc định an toàn).
var foundry3dDisabled = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	http.Error(w, "foundry3d is disabled on this gateway (start with -foundry3d-enabled -foundry3d-url)", http.StatusServiceUnavailable)
})
