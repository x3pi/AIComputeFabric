// inferload sends C concurrent streaming chat requests (closed loop) for a fixed
// time and reports TTFT, per-request and aggregate decode speed, and rejections.
// It works against llama-server directly or through site-gateway, so the proxy's
// own overhead can be measured by comparing the two.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type result struct {
	status  int
	ttft    time.Duration
	total   time.Duration
	tokens  int
	retryAf string
	err     string
}

func main() {
	url := flag.String("url", "http://127.0.0.1:8080/v1/chat/completions", "")
	model := flag.String("model", "", "")
	c := flag.Int("c", 8, "concurrent clients")
	dur := flag.Duration("duration", 30*time.Second, "")
	maxTok := flag.Int("max-tokens", 64, "")
	out := flag.String("out", "", "")
	tenant := flag.String("tenant", "", "X-Tenant-ID header")
	prio := flag.String("priority", "", "X-Priority header (batch)")
	noKA := flag.Bool("no-keepalive", false, "disable HTTP keep-alive on the client")
	flag.Parse()
	if *noKA {
		http.DefaultTransport.(*http.Transport).DisableKeepAlives = true
	}

	body, _ := json.Marshal(map[string]any{"model": *model, "stream": true, "max_tokens": *maxTok, "temperature": 0,
		"messages": []map[string]string{{"role": "user", "content": "Viết một đoạn ngắn về lợi ích của việc dùng chung GPU trong văn phòng."}}})
	var mu sync.Mutex
	var res []result
	deadline := time.Now().Add(*dur)
	start := time.Now()
	var wg sync.WaitGroup
	for i := 0; i < *c; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for time.Now().Before(deadline) {
				r := one(*url, body, *tenant, *prio)
				mu.Lock()
				res = append(res, r)
				mu.Unlock()
				if r.status == 429 || r.status == 503 { // honour back-pressure like a polite client
					time.Sleep(200 * time.Millisecond)
				}
			}
		}()
	}
	wg.Wait()
	wall := time.Since(start).Seconds()

	var ok []result
	codes := map[int]int{}
	errs := map[string]int{}
	toks := 0
	for _, r := range res {
		codes[r.status]++
		if r.err != "" {
			errs[r.err]++
		}
		if r.status == 200 && r.tokens > 0 {
			ok = append(ok, r)
			toks += r.tokens
		}
	}
	q := func(v []float64, p float64) float64 {
		if len(v) == 0 {
			return 0
		}
		sort.Float64s(v)
		return v[int(p*float64(len(v)-1))]
	}
	var ttft, per []float64
	for _, r := range ok {
		ttft = append(ttft, float64(r.ttft.Milliseconds()))
		if d := (r.total - r.ttft).Seconds(); d > 0 && r.tokens > 1 {
			per = append(per, float64(r.tokens-1)/d)
		}
	}
	m := map[string]any{"url": *url, "concurrency": *c, "duration_s": wall, "requests": len(res), "ok": len(ok), "status_counts": codes, "client_errors": errs,
		"aggregate_tok_s": float64(toks) / wall, "ttft_ms_p50": q(ttft, 0.5), "ttft_ms_p95": q(ttft, 0.95),
		"per_request_tok_s_p50": q(per, 0.5), "per_request_tok_s_p5": q(per, 0.05)}
	b, _ := json.MarshalIndent(m, "", "  ")
	fmt.Println(string(b))
	if *out != "" {
		if err := os.WriteFile(*out, b, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, "cannot write report:", err)
			os.Exit(1)
		}
	}
}

func one(url string, body []byte, tenant, prio string) result {
	t0 := time.Now()
	req, _ := http.NewRequest("POST", url, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if tenant != "" {
		req.Header.Set("X-Tenant-ID", tenant)
	}
	if prio != "" {
		req.Header.Set("X-Priority", prio)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return result{status: -1, err: err.Error()}
	}
	defer resp.Body.Close()
	r := result{status: resp.StatusCode, retryAf: resp.Header.Get("Retry-After")}
	if resp.StatusCode != 200 {
		return r
	}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		l := sc.Text()
		if !strings.HasPrefix(l, "data: ") || strings.HasPrefix(l, "data: [DONE]") {
			continue
		}
		var ch struct {
			Choices []struct {
				Delta struct {
					Content   string `json:"content"`
					Reasoning string `json:"reasoning_content"` // Qwen3 streams its thinking here
				} `json:"delta"`
			} `json:"choices"`
		}
		if json.Unmarshal([]byte(l[6:]), &ch) != nil || len(ch.Choices) == 0 || ch.Choices[0].Delta.Content == "" && ch.Choices[0].Delta.Reasoning == "" {
			continue
		}
		if r.tokens == 0 {
			r.ttft = time.Since(t0)
		}
		r.tokens++ // llama-server streams one token per chunk (content or reasoning)
	}
	r.total = time.Since(t0)
	return r
}
