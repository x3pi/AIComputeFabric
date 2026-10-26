// Package gcp is the global control plane (plan P7): it never sees individual PCs, only per-site
// aggregate capacity, decides which site may run an offered job under the data-class policy, and
// keeps the append-only usage ledger. It is a logic + transport implementation exercised in-process;
// it has not run across two real sites.
package gcp

import (
	"errors"
	"sort"
)

type DataClass string

const (
	Public     DataClass = "DATA_PUBLIC"
	Internal   DataClass = "DATA_INTERNAL"
	Restricted DataClass = "DATA_RESTRICTED"
)

// Capacity is everything a site discloses: totals only, no host names or addresses.
type Capacity struct {
	Site        string `json:"site"`
	Region      string `json:"region"`
	TotalGPUs   int    `json:"total_gpus"`
	FreeGPUs    int    `json:"free_gpus"`
	FreeVRAMGiB int    `json:"free_vram_gib"`
}

type Job struct {
	ID            string    `json:"id"`
	Tenant        string    `json:"tenant"`
	Origin        string    `json:"origin"`
	Class         DataClass `json:"class"`
	SiteAllowlist []string  `json:"site_allowlist,omitempty"`
	Region        string    `json:"region,omitempty"` // data-residency: only sites in this region
	GPUs          int       `json:"gpus"`
}

type Action string

const (
	RunLocal      Action = "run_local"
	Offload       Action = "offload"
	QueueAtOrigin Action = "queue_at_origin"
	Reject        Action = "reject"
)

type Decision struct {
	Action Action `json:"action"`
	Site   string `json:"site,omitempty"`
	Reason string `json:"reason"`
}

// Policy says which sites each tenant is enrolled at. A tenant's job can only ever run where the
// tenant is enrolled, whatever its data class.
type Policy struct {
	TenantSites map[string][]string
}

func (p Policy) enrolled(tenant, site string) bool {
	for _, s := range p.TenantSites[tenant] {
		if s == site {
			return true
		}
	}
	return false
}

var ErrBadJob = errors.New("invalid job")

// Place decides where a job may run. Fail-closed: an unknown or empty data class, an origin the tenant
// is not enrolled at, or an empty set of permitted sites is a rejection, never a default to "anywhere".
// RESTRICTED data never leaves its origin site: when the origin is full the job waits there.
func Place(p Policy, job Job, caps map[string]Capacity) Decision {
	if job.ID == "" || job.Tenant == "" || job.Origin == "" || job.GPUs < 1 {
		return Decision{Reject, "", "incomplete job"}
	}
	switch job.Class {
	case Public, Internal, Restricted:
	default:
		return Decision{Reject, "", "unknown data class"}
	}
	if !p.enrolled(job.Tenant, job.Origin) {
		return Decision{Reject, "", "tenant is not enrolled at the origin site"}
	}
	allowed := map[string]bool{}
	switch job.Class {
	case Restricted:
		allowed[job.Origin] = true
	default:
		for _, s := range p.TenantSites[job.Tenant] {
			allowed[s] = true
		}
	}
	if len(job.SiteAllowlist) > 0 {
		in := map[string]bool{}
		for _, s := range job.SiteAllowlist {
			in[s] = true
		}
		for s := range allowed {
			if !in[s] {
				delete(allowed, s)
			}
		}
	}
	if job.Region != "" {
		for s := range allowed {
			if c, ok := caps[s]; !ok || c.Region != job.Region {
				delete(allowed, s)
			}
		}
	}
	if len(allowed) == 0 {
		return Decision{Reject, "", "no site satisfies the job's data policy"}
	}
	if allowed[job.Origin] && caps[job.Origin].FreeGPUs >= job.GPUs {
		return Decision{RunLocal, job.Origin, "origin has capacity"}
	}
	var cand []string
	for s := range allowed {
		if s != job.Origin && caps[s].FreeGPUs >= job.GPUs {
			cand = append(cand, s)
		}
	}
	if len(cand) > 0 {
		sort.Slice(cand, func(i, j int) bool {
			a, b := caps[cand[i]], caps[cand[j]]
			if a.FreeGPUs != b.FreeGPUs {
				return a.FreeGPUs > b.FreeGPUs
			}
			return cand[i] < cand[j]
		})
		return Decision{Offload, cand[0], "origin full; most free GPUs among permitted sites"}
	}
	if allowed[job.Origin] {
		return Decision{QueueAtOrigin, job.Origin, "no permitted site has capacity; waiting at origin"}
	}
	return Decision{Reject, "", "origin not permitted and no permitted site has capacity"}
}
