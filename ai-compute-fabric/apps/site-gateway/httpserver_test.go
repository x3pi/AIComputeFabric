package main

import (
	"bufio"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func serveWith(t *testing.T, to serverTimeouts, h http.Handler) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := newHTTPServer("", h, to)
	go srv.Serve(lis)
	t.Cleanup(func() { srv.Close() })
	return lis.Addr().String()
}

// A client that drips its request header one byte at a time (slowloris) is cut off at ReadHeaderTimeout instead of
// holding a connection forever.
func TestSlowHeaderClientIsDisconnected(t *testing.T) {
	addr := serveWith(t, serverTimeouts{ReadHeader: 300 * time.Millisecond, Read: 5 * time.Second, Idle: 5 * time.Second}, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	start := time.Now()
	for _, b := range []byte("GET / HTTP/1.1\r\nHost: x\r\nX-Slow: ") {
		if _, err := c.Write([]byte{b}); err != nil {
			break
		}
		time.Sleep(60 * time.Millisecond)
		if time.Since(start) > 3*time.Second {
			t.Fatal("connection still open long after ReadHeaderTimeout")
		}
	}
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, err = io.Copy(io.Discard, c)
	if time.Since(start) > 2500*time.Millisecond {
		t.Fatalf("server held the slow connection for %v (err %v)", time.Since(start), err)
	}
}

// A normal request, and a long streaming reply that outlives every read-side timeout, still work: there is no WriteTimeout.
func TestStreamingReplyOutlivesTimeouts(t *testing.T) {
	addr := serveWith(t, serverTimeouts{ReadHeader: time.Second, Read: 400 * time.Millisecond, Idle: 400 * time.Millisecond}, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fl := w.(http.Flusher)
		for i := 0; i < 5; i++ {
			w.Write([]byte("data: tick\n\n"))
			fl.Flush()
			time.Sleep(200 * time.Millisecond)
		}
	}))
	resp, err := http.Get("http://" + addr + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	n := 0
	sc := bufio.NewScanner(resp.Body)
	for sc.Scan() {
		if strings.HasPrefix(sc.Text(), "data:") {
			n++
		}
	}
	if n != 5 {
		t.Fatalf("got %d of 5 stream events", n)
	}
}

func TestDefaultTimeoutsAreSetAndHeadersBounded(t *testing.T) {
	s := newHTTPServer(":0", http.NewServeMux(), defaultTimeouts)
	if s.ReadHeaderTimeout == 0 || s.ReadTimeout == 0 || s.IdleTimeout == 0 || s.MaxHeaderBytes == 0 || s.WriteTimeout != 0 {
		t.Fatalf("unexpected server limits: %+v", s)
	}
}
