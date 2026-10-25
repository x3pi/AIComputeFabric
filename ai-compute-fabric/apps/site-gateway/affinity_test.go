package main

import (
	"encoding/json"
	"testing"
	"time"
)

type msg = struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

func twoWorkers(t *testing.T, patience time.Duration, slots uint32) *Router {
	t.Helper()
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: 5 * time.Second, AffinityPatience: patience, AffinityEntries: 100}, reg)
	addWorker(reg, "w1", "http://a", "m", slots)
	addWorker(reg, "w2", "http://b", "m", slots)
	rt.Refresh()
	return rt
}

// steer the FIRST turn onto w2 by keeping w1 busy, then free both: least-loaded alone would now pick w1.
func firstTurnOnW2(t *testing.T, rt *Router, session string) {
	t.Helper()
	busy, _ := rt.Acquire(Req{Model: "m"}, nil)
	if busy.ID != "w1" {
		t.Fatalf("setup: expected w1, got %s", busy.ID)
	}
	first, _ := rt.Acquire(Req{Model: "m", Session: session}, nil)
	if first.ID != "w2" {
		t.Fatalf("setup: expected w2, got %s", first.ID)
	}
	rt.Release(first, "", false)
	rt.Release(busy, "", false)
}

func TestFollowUpTurnGoesToTheWorkerThatHoldsTheCache(t *testing.T) {
	rt := twoWorkers(t, 300*time.Millisecond, 2)
	firstTurnOnW2(t, rt, "conv-1")
	got, err := rt.Acquire(Req{Model: "m", Session: "conv-1"}, nil)
	if err != nil || got.ID != "w2" {
		t.Fatalf("follow-up must return to w2, got %v %v", got.ID, err)
	}
	other, _ := rt.Acquire(Req{Model: "m", Session: "conv-2"}, nil)
	if other.ID != "w1" {
		t.Fatalf("an unrelated conversation still balances by load, got %s", other.ID)
	}
}

func TestAffinityOffMeansLeastLoadedOnly(t *testing.T) {
	rt := twoWorkers(t, 0, 2)
	firstTurnOnW2(t, rt, "conv-1")
	if got, _ := rt.Acquire(Req{Model: "m", Session: "conv-1"}, nil); got.ID != "w1" {
		t.Fatalf("with affinity off the tie goes to w1, got %s", got.ID)
	}
}

func TestWaitsBrieflyForTheCacheHolderThenFallsBack(t *testing.T) {
	rt := twoWorkers(t, 150*time.Millisecond, 1)
	firstTurnOnW2(t, rt, "conv-1")
	hold, _ := rt.Acquire(Req{Model: "m", Session: "other"}, nil) // takes w1 or w2
	_ = hold
	// make w2 the busy one
	rt.Release(hold, "", false)
	w2, _ := rt.Acquire(Req{Model: "m", Session: "conv-1"}, nil) // follow-up lands on w2 and keeps it busy
	if w2.ID != "w2" {
		t.Fatalf("setup: %s", w2.ID)
	}
	start := time.Now()
	got, err := rt.Acquire(Req{Model: "m", Session: "conv-1"}, nil) // w2 busy, w1 free
	if err != nil || got.ID != "w1" {
		t.Fatalf("after the patience it must use the free worker, got %v %v", got.ID, err)
	}
	if waited := time.Since(start); waited < 120*time.Millisecond || waited > 700*time.Millisecond {
		t.Fatalf("should have waited about the patience (150 ms), waited %v", waited)
	}
}

func TestWaitingRequestGetsTheCacheHolderWhenItFreesInTime(t *testing.T) {
	rt := twoWorkers(t, 2*time.Second, 1)
	firstTurnOnW2(t, rt, "conv-1")
	w2, _ := rt.Acquire(Req{Model: "m", Session: "conv-1"}, nil)
	res := make(chan Route, 1)
	go func() { r, _ := rt.Acquire(Req{Model: "m", Session: "conv-1"}, nil); res <- r }()
	waitFor(t, "queued", func() bool { _, q, _ := rt.Snapshot(); return q == 1 })
	time.Sleep(50 * time.Millisecond)
	if len(res) != 0 {
		t.Fatal("must not take the idle w1 while the cache holder may free up soon")
	}
	rt.Release(w2, "", false)
	select {
	case r := <-res:
		if r.ID != "w2" {
			t.Fatalf("got %s, want the cache holder w2", r.ID)
		}
	case <-time.After(time.Second):
		t.Fatal("never dispatched")
	}
}

func TestAffinityTableIsBounded(t *testing.T) {
	reg := NewRegistry(time.Hour)
	rt := NewRouter(RouterConfig{MaxQueue: 8, MaxWait: time.Second, AffinityPatience: time.Second, AffinityEntries: 3}, reg)
	addWorker(reg, "w1", "http://a", "m", 100)
	rt.Refresh()
	for i := 0; i < 50; i++ {
		r, _ := rt.Acquire(Req{Model: "m", Session: string(rune('a' + i))}, nil)
		rt.Release(r, "", false)
	}
	rt.mu.Lock()
	n := len(rt.sessions)
	rt.mu.Unlock()
	if n > 3 {
		t.Fatalf("session table grew to %d entries, limit 3", n)
	}
}

func TestSessionKeyIsStableAcrossTurnsAndScopedToTheTenant(t *testing.T) {
	sys := msg{"system", json.RawMessage(`"Bạn là trợ lý"`)}
	u1 := msg{"user", json.RawMessage(`"Tóm tắt tài liệu"`)}
	a1 := msg{"assistant", json.RawMessage(`"Đây là tóm tắt"`)}
	u2 := msg{"user", json.RawMessage(`"Ý thứ hai?"`)}
	turn1 := sessionKey("t1", []msg{sys, u1})
	turn2 := sessionKey("t1", []msg{sys, u1, a1, u2})
	if turn1 == "" || turn1 != turn2 {
		t.Fatalf("later turns must map to the same conversation: %q vs %q", turn1, turn2)
	}
	if sessionKey("t2", []msg{sys, u1}) == turn1 {
		t.Fatal("two tenants with identical prompts must not share a key")
	}
	if sessionKey("t1", []msg{sys, msg{"user", json.RawMessage(`"Câu hỏi khác"`)}}) == turn1 {
		t.Fatal("a different first question is a different conversation")
	}
	if sessionKey("t1", []msg{sys}) != "" {
		t.Fatal("no user message yet: nothing to anchor on")
	}
	noSys1 := sessionKey("t1", []msg{u1})
	if noSys1 == "" || noSys1 != sessionKey("t1", []msg{u1, a1, u2}) {
		t.Fatal("conversations without a system prompt must also be stable")
	}
}
