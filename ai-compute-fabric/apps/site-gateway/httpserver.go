package main

import (
	"net/http"
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

func newHTTPServer(addr string, h http.Handler, t serverTimeouts) *http.Server {
	return &http.Server{Addr: addr, Handler: h, ReadHeaderTimeout: t.ReadHeader, ReadTimeout: t.Read, IdleTimeout: t.Idle, MaxHeaderBytes: 64 << 10}
}
