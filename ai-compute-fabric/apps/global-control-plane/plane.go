package gcp

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// TLSConfig builds the server side of the federation transport: TLS 1.3, a client certificate that
// chains to the federation CA is mandatory, and a certificate whose serial is in `revoked` is refused
// during the handshake. `revoked` is a live function so the CRL can change without a restart.
// Limit: revocation is a serial-number set pushed to the plane, not a fetched, signed CRL/OCSP.
func TLSConfig(cert tls.Certificate, federationCA *x509.CertPool, revoked func(serial string) bool) *tls.Config {
	return &tls.Config{
		Certificates: []tls.Certificate{cert}, ClientCAs: federationCA, ClientAuth: tls.RequireAndVerifyClientCert, MinVersion: tls.VersionTLS13,
		VerifyPeerCertificate: func(raw [][]byte, chains [][]*x509.Certificate) error {
			if len(chains) == 0 || len(chains[0]) == 0 {
				return errors.New("no verified chain")
			}
			for _, c := range chains[0] {
				if revoked != nil && revoked(c.SerialNumber.String()) {
					return fmt.Errorf("certificate %s revoked", c.SerialNumber)
				}
			}
			return nil
		},
	}
}

type offer struct {
	Job      Job
	Site     string
	Accepted bool
}

type Plane struct {
	Policy Policy
	Ledger *Ledger
	Now    func() time.Time

	mu     sync.Mutex
	caps   map[string]Capacity
	offers map[string]*offer
	secret []byte
	TokenT time.Duration
}

func NewPlane(p Policy, secret []byte) *Plane {
	return &Plane{Policy: p, Ledger: NewLedger(), Now: time.Now, caps: map[string]Capacity{}, offers: map[string]*offer{}, secret: secret, TokenT: 2 * time.Minute}
}

// Token authorises exactly one site to accept exactly one job until it expires.
func (pl *Plane) token(job, site string) string {
	exp := strconv.FormatInt(pl.Now().Add(pl.TokenT).Unix(), 10)
	body := job + "|" + site + "|" + exp
	m := hmac.New(sha256.New, pl.secret)
	m.Write([]byte(body))
	return base64.RawURLEncoding.EncodeToString([]byte(body)) + "." + base64.RawURLEncoding.EncodeToString(m.Sum(nil))
}

func (pl *Plane) checkToken(tok, job, site string) error {
	parts := strings.SplitN(tok, ".", 2)
	if len(parts) != 2 {
		return errors.New("malformed token")
	}
	body, err1 := base64.RawURLEncoding.DecodeString(parts[0])
	sig, err2 := base64.RawURLEncoding.DecodeString(parts[1])
	if err1 != nil || err2 != nil {
		return errors.New("malformed token")
	}
	m := hmac.New(sha256.New, pl.secret)
	m.Write(body)
	if !hmac.Equal(sig, m.Sum(nil)) {
		return errors.New("bad token signature")
	}
	f := strings.Split(string(body), "|")
	if len(f) != 3 || f[0] != job || f[1] != site {
		return errors.New("token is for another job or site")
	}
	exp, _ := strconv.ParseInt(f[2], 10, 64)
	if pl.Now().Unix() > exp {
		return errors.New("token expired")
	}
	return nil
}

func siteOf(r *http.Request) (string, bool) {
	if r.TLS == nil || len(r.TLS.PeerCertificates) == 0 {
		return "", false
	}
	cn := r.TLS.PeerCertificates[0].Subject.CommonName
	return cn, cn != ""
}

func decode(r *http.Request, v any) error {
	d := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	d.DisallowUnknownFields() // a site that tries to send host lists/addresses is refused, not ignored
	return d.Decode(v)
}

func reply(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// Handler serves the four federation calls. The caller's site is the CN of its verified client
// certificate; a body that names another site is refused.
func (pl *Plane) Handler() http.Handler {
	mux := http.NewServeMux()
	need := func(h func(http.ResponseWriter, *http.Request, string)) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			site, ok := siteOf(r)
			if !ok || r.Method != http.MethodPost {
				reply(w, http.StatusForbidden, map[string]string{"error": "forbidden"})
				return
			}
			h(w, r, site)
		}
	}
	mux.HandleFunc("/v1/advertise", need(func(w http.ResponseWriter, r *http.Request, site string) {
		var c Capacity
		if decode(r, &c) != nil || c.Site != site || c.FreeGPUs < 0 || c.FreeGPUs > c.TotalGPUs {
			reply(w, http.StatusBadRequest, map[string]string{"error": "bad capacity"})
			return
		}
		pl.mu.Lock()
		pl.caps[site] = c
		pl.mu.Unlock()
		reply(w, http.StatusOK, map[string]string{"ok": "1"})
	}))
	mux.HandleFunc("/v1/offer", need(func(w http.ResponseWriter, r *http.Request, site string) {
		var j Job
		if decode(r, &j) != nil || j.Origin != site {
			reply(w, http.StatusBadRequest, map[string]string{"error": "bad job or origin is not the caller"})
			return
		}
		pl.mu.Lock()
		defer pl.mu.Unlock()
		caps := map[string]Capacity{}
		for k, v := range pl.caps {
			caps[k] = v
		}
		d := Place(pl.Policy, j, caps)
		out := map[string]any{"decision": d}
		if d.Action == Offload {
			if o, dup := pl.offers[j.ID]; dup && o.Site != d.Site {
				reply(w, http.StatusConflict, map[string]string{"error": "job id already offered"})
				return
			}
			pl.offers[j.ID] = &offer{Job: j, Site: d.Site}
			c := caps[d.Site]
			c.FreeGPUs -= j.GPUs // reserve, so two offers cannot both take the last GPU
			pl.caps[d.Site] = c
			out["token"] = pl.token(j.ID, d.Site)
		}
		reply(w, http.StatusOK, out)
	}))
	mux.HandleFunc("/v1/accept", need(func(w http.ResponseWriter, r *http.Request, site string) {
		var a struct {
			Job   string `json:"job"`
			Token string `json:"token"`
		}
		if decode(r, &a) != nil {
			reply(w, http.StatusBadRequest, map[string]string{"error": "bad request"})
			return
		}
		pl.mu.Lock()
		defer pl.mu.Unlock()
		o := pl.offers[a.Job]
		if o == nil || o.Site != site || o.Accepted {
			reply(w, http.StatusForbidden, map[string]string{"error": "no such open offer for this site"})
			return
		}
		if err := pl.checkToken(a.Token, a.Job, site); err != nil {
			reply(w, http.StatusForbidden, map[string]string{"error": err.Error()})
			return
		}
		o.Accepted = true
		reply(w, http.StatusOK, map[string]any{"job": o.Job})
	}))
	mux.HandleFunc("/v1/usage", need(func(w http.ResponseWriter, r *http.Request, site string) {
		var batch []Usage
		if decode(r, &batch) != nil {
			reply(w, http.StatusBadRequest, map[string]string{"error": "bad batch"})
			return
		}
		for _, u := range batch {
			if u.Site != site {
				reply(w, http.StatusForbidden, map[string]string{"error": "a site can only report its own usage"})
				return
			}
		}
		added := 0
		for _, u := range batch {
			ok, err := pl.Ledger.Append(u)
			if err != nil {
				reply(w, http.StatusConflict, map[string]string{"error": err.Error()})
				return
			}
			if ok {
				added++
			}
		}
		reply(w, http.StatusOK, map[string]int{"added": added, "received": len(batch)})
	}))
	return mux
}

// Client is a site's handle on the plane.
type Client struct {
	Base string
	HTTP *http.Client
}

func (c *Client) post(path string, in, out any) (int, error) {
	b, _ := json.Marshal(in)
	resp, err := c.HTTP.Post(c.Base+path, "application/json", bytes.NewReader(b))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if out != nil {
		_ = json.NewDecoder(resp.Body).Decode(out)
	}
	return resp.StatusCode, nil
}

func (c *Client) Advertise(cap Capacity) (int, error) { return c.post("/v1/advertise", cap, nil) }
func (c *Client) ReportUsage(b []Usage) (int, error)  { return c.post("/v1/usage", b, nil) }
func (c *Client) OfferJob(j Job) (Decision, string, int, error) {
	var o struct {
		Decision Decision `json:"decision"`
		Token    string   `json:"token"`
	}
	code, err := c.post("/v1/offer", j, &o)
	return o.Decision, o.Token, code, err
}
func (c *Client) AcceptJob(job, token string) (int, error) {
	return c.post("/v1/accept", map[string]string{"job": job, "token": token}, nil)
}

// Site is the part that runs inside a company: it serves its own work without the plane, writes every
// usage record to its own ledger first, and ships the not-yet-confirmed ones whenever the plane is reachable.
type Site struct {
	ID    string
	Local *Ledger
	mu    sync.Mutex
	seq   uint64
	sent  int // number of Local records the plane has confirmed
}

func NewSite(id string) *Site { return &Site{ID: id, Local: NewLedger()} }

// Record is called after local work completes; it never touches the network.
func (s *Site) Record(u Usage) error {
	s.mu.Lock()
	s.seq++
	u.Site, u.Seq = s.ID, s.seq
	s.mu.Unlock()
	_, err := s.Local.Append(u)
	return err
}

// Sync sends everything the plane has not confirmed. On any failure nothing is marked sent, so the next
// call resends (idempotent on the plane side).
func (s *Site) Sync(c *Client, batch int) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	all := s.Local.Records()
	n := 0
	for s.sent < len(all) {
		end := min(s.sent+batch, len(all))
		code, err := c.ReportUsage(all[s.sent:end])
		if err != nil {
			return n, err
		}
		if code != http.StatusOK {
			return n, fmt.Errorf("plane answered %d", code)
		}
		n += end - s.sent
		s.sent = end
	}
	return n, nil
}
