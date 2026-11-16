package main

import (
	"context"
	"testing"
	"time"
)

func TestOfflineAfterFollowsTheHeartbeatInterval(t *testing.T) {
	s := time.Second
	for _, c := range []struct {
		interval, explicit, want time.Duration
		wantErr                  bool
	}{
		{s, 0, 3 * s, false},                  // defaults: exactly the old behaviour (1 s / 3 s)
		{10 * s, 0, 30 * s, false},            // big fleet: one number to change
		{s, 5 * s, 5 * s, false},              // explicit override still honoured
		{s, 2 * s, 2 * s, false},              // exactly 2 intervals is the floor
		{s, 1500 * time.Millisecond, 0, true}, // below 2 intervals would flap
		{10 * s, 3 * s, 0, true},              // the stale-flag trap: interval raised, offline-after left behind
		{0, 0, 0, true},
		{-s, 0, 0, true},
	} {
		got, err := resolveOffline(c.interval, c.explicit)
		if (err != nil) != c.wantErr || got != c.want {
			t.Errorf("interval=%v explicit=%v: got %v err=%v, want %v wantErr=%v", c.interval, c.explicit, got, err, c.want, c.wantErr)
		}
	}
}

func TestRegisterTellsWorkersTheConfiguredHeartbeatInterval(t *testing.T) {
	reg, _ := regRig()
	for _, c := range []struct {
		set  time.Duration
		want uint32
	}{{0, 1000}, {time.Second, 1000}, {10 * time.Second, 10000}, {250 * time.Millisecond, 250}} {
		g := &GRPC{Reg: reg, HeartbeatEvery: c.set}
		rep, err := g.Register(context.Background(), req("pc-a", "http://a:1"))
		if err != nil || rep.HeartbeatIntervalMs != c.want {
			t.Errorf("set=%v: got %v err=%v, want %d ms", c.set, rep, err, c.want)
		}
	}
}
