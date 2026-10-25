package main

import (
	"fmt"
	"os/exec"
	"strconv"
	"strings"
)

// Proc is one process holding VRAM (Linux NVML per-process figures; on Windows
// WDDM these are unreliable, so the guard must not depend on them there: plan §B.2).
type Proc struct {
	PID int
	MiB uint64
}

// GPUSample is one reading of the device.
type GPUSample struct {
	TotalMiB, UsedMiB uint64
	TempC             int
	Procs             []Proc
}

// FreeMiB is total - used, saturating.
func (s GPUSample) FreeMiB() uint64 {
	if s.UsedMiB >= s.TotalMiB {
		return 0
	}
	return s.TotalMiB - s.UsedMiB
}

// OwnMiB sums the VRAM held by the given pids.
func (s GPUSample) OwnMiB(pids map[int]bool) uint64 {
	var n uint64
	for _, p := range s.Procs {
		if pids[p.PID] {
			n += p.MiB
		}
	}
	return n
}

// Sampler reads the GPU. The real one shells out to nvidia-smi (~30 ms/call).
type Sampler interface{ Sample() (GPUSample, error) }

type SMISampler struct{ Index int }

func (s SMISampler) Sample() (GPUSample, error) {
	out, err := exec.Command("nvidia-smi", "-i", strconv.Itoa(s.Index),
		"--query-gpu=memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits").Output()
	if err != nil {
		return GPUSample{}, fmt.Errorf("nvidia-smi: %w", err)
	}
	f := strings.Split(strings.TrimSpace(string(out)), ",")
	if len(f) != 3 {
		return GPUSample{}, fmt.Errorf("unexpected nvidia-smi output %q", out)
	}
	var g GPUSample
	u, e1 := strconv.ParseUint(strings.TrimSpace(f[0]), 10, 64)
	t, e2 := strconv.ParseUint(strings.TrimSpace(f[1]), 10, 64)
	c, e3 := strconv.Atoi(strings.TrimSpace(f[2]))
	if e1 != nil || e2 != nil || e3 != nil {
		return GPUSample{}, fmt.Errorf("parse %q", out)
	}
	g.UsedMiB, g.TotalMiB, g.TempC = u, t, c
	return g, nil
}

// ProcSampler is the optional, costlier per-process query. Each nvidia-smi spawn
// costs ~18 ms of CPU, so the guard calls it at 1 Hz (and during a reclaim), not
// on every fast poll: measured 0.38 CPU cores at 10 Hz x 2 spawns on an office PC.
type ProcSampler interface{ Procs() ([]Proc, error) }

func (s SMISampler) Procs() ([]Proc, error) {
	apps, err := exec.Command("nvidia-smi", "-i", strconv.Itoa(s.Index),
		"--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits").Output()
	if err != nil {
		return nil, err
	}
	var out []Proc
	for _, ln := range strings.Split(strings.TrimSpace(string(apps)), "\n") {
		p := strings.Split(ln, ",")
		if len(p) != 2 {
			continue
		}
		pid, e1 := strconv.Atoi(strings.TrimSpace(p[0]))
		m, e2 := strconv.ParseUint(strings.TrimSpace(p[1]), 10, 64)
		if e1 == nil && e2 == nil {
			out = append(out, Proc{pid, m})
		}
	}
	return out, nil
}
