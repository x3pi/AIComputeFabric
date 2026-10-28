package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	pb "github.com/aicf/ai-compute-fabric/contracts/gen/go"
)

// topoRig is a registry with fake worker-guard engines. A fake worker behaves like the real one: after a POST to
// /api/engine it reports the new model through a heartbeat (unless told to stay stopped or to reject the call).
type topoRig struct {
	t       *testing.T
	reg     *Registry
	tc      *TopologyCoordinator
	mu      sync.Mutex
	calls   []topoCall // global order of engine POSTs
	workers map[string]*fakeTopoWorker
}

type topoCall struct {
	id   string
	body map[string]any
}

type fakeTopoWorker struct {
	id      string
	srv     *httptest.Server
	reject  int           // non-zero: answer this HTTP code
	stuck   bool          // accept but never report running
	block   chan struct{} // if set, the handler waits for it
	started chan struct{} // closed on first call (when set)
}

func newTopoRig(t *testing.T, ids ...string) *topoRig {
	t.Helper()
	r := &topoRig{t: t, reg: NewRegistry(time.Minute), workers: map[string]*fakeTopoWorker{}}
	r.tc = NewTopologyCoordinator(r.reg, nil)
	r.tc.Poll, r.tc.RPCReadyTimeout, r.tc.LoadTimeout = 10*time.Millisecond, 300*time.Millisecond, 300*time.Millisecond
	for _, id := range ids {
		r.addWorker(id)
	}
	return r
}

func (r *topoRig) beat(id, model, state string) {
	r.reg.Heartbeat(&pb.HeartbeatRequest{
		Capacity: &pb.WorkerCapacity{WorkerId: id, VramFreeBytes: 11 << 30}, State: state, Model: model})
}

func (r *topoRig) addWorker(id string) *fakeTopoWorker {
	fw := &fakeTopoWorker{id: id}
	fw.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(req.Body).Decode(&body)
		r.mu.Lock()
		r.calls = append(r.calls, topoCall{id, body})
		r.mu.Unlock()
		if fw.started != nil {
			select {
			case <-fw.started:
			default:
				close(fw.started)
			}
		}
		if fw.block != nil {
			<-fw.block
		}
		if fw.reject != 0 {
			http.Error(w, "nope", fw.reject)
			return
		}
		if !fw.stuck {
			model, _ := body["model"].(string)
			if body["mode"] == TopologyModeRPCWrk {
				model = "rpc://50052"
			}
			// like a real worker: the directive is accepted now, the next heartbeat reports the new engine later
			go func() { time.Sleep(30 * time.Millisecond); r.beat(id, model, "running") }()
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	r.t.Cleanup(fw.srv.Close)
	r.reg.Register(&pb.RegisterRequest{WorkerId: id, Hostname: id + "-host", GpuName: "RTX", VramTotalBytes: 12 << 30,
		EngineUrl: fw.srv.URL, ParallelSlots: 1})
	r.beat(id, "Qwen3-4B-Q4_K_M.gguf", "running")
	r.workers[id] = fw
	return fw
}

func (r *topoRig) do(req TopologySwitchRequest) (*ClusterTopologyStatus, error) {
	return r.tc.SwitchTopology(context.Background(), req, Principal{Name: "t", Role: RoleAdmin}, "127.0.0.1")
}

func (r *topoRig) callIDs() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []string
	for _, c := range r.calls {
		out = append(out, c.id)
	}
	return out
}

func (r *topoRig) callsTo(id string) []map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []map[string]any
	for _, c := range r.calls {
		if c.id == id {
			out = append(out, c.body)
		}
	}
	return out
}

func TestTopologyMergedTwoNodes(t *testing.T) {
	r := newTopoRig(t, "pc-001", "pc-win-01")
	st, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"},
		Model: "Qwen3-30B-A3B-Q4_K_M.gguf"})
	if err != nil {
		t.Fatal(err)
	}
	// members first, then the master
	if got := strings.Join(r.callIDs(), ","); got != "pc-win-01,pc-001" {
		t.Errorf("call order %s, want pc-win-01,pc-001", got)
	}
	m := r.callsTo("pc-001")[0]
	if m["mode"] != "merged" || m["rpc_servers"] != "pc-win-01-host:50052" || m["tensor_split"] != "1,1" || m["split_mode"] != "layer" {
		t.Errorf("master payload %+v", m)
	}
	if w := r.callsTo("pc-win-01")[0]; w["mode"] != "rpc-worker" {
		t.Errorf("member payload %+v", w)
	}
	if st.Mode != TopologyModeMerged || st.Status != "ready" {
		t.Errorf("status %+v", st)
	}
	if b := st.Workers["pc-001"].Badge; !strings.Contains(b, "MASTER") || !strings.Contains(b, "24 GB") {
		t.Errorf("master badge %q (should be computed from VRAM)", b)
	}
	if b := st.Workers["pc-win-01"].Badge; !strings.Contains(b, "RPC GPU MEMBER") {
		t.Errorf("member badge %q", b)
	}
	// a member must not be routable
	for _, rt := range r.reg.Routable() {
		if rt.ID == "pc-win-01" {
			t.Errorf("rpc member is routable")
		}
	}
}

func TestTopologyMergedThreeNodesAndDefaultSplit(t *testing.T) {
	r := newTopoRig(t, "a", "b", "c")
	_, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b", "c"},
		Model: "m.gguf", TensorSplit: "2,3,3"})
	if err != nil {
		t.Fatal(err)
	}
	m := r.callsTo("a")[0]
	if m["rpc_servers"] != "b-host:50052,c-host:50052" || m["tensor_split"] != "3,3,2" {
		t.Errorf("master payload %+v", m)
	}
	r2 := newTopoRig(t, "a", "b", "c")
	if _, err := r2.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b", "c"}, Model: "m.gguf"}); err != nil {
		t.Fatal(err)
	}
	if got := r2.callsTo("a")[0]["tensor_split"]; got != "1,1,1" {
		t.Errorf("default tensor_split %v, want 1,1,1", got)
	}
}

func TestTopologyMergedRollsBackWhenMasterNeverStarts(t *testing.T) {
	r := newTopoRig(t, "pc-001", "pc-win-01")
	r.workers["pc-001"].stuck = true
	_, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"}, Model: "big.gguf"})
	if err == nil || !strings.Contains(err.Error(), "did not start") || !strings.Contains(err.Error(), "restored") {
		t.Fatalf("want a timeout + restored error, got %v", err)
	}
	st := r.tc.GetStatus()
	if st.Mode != TopologyModeSplit || st.Status != "error" || st.LastError == "" {
		t.Errorf("state after failure: %+v", st)
	}
	// the member is put back on its previous model, the master too
	last := func(id string) map[string]any { c := r.callsTo(id); return c[len(c)-1] }
	if m := last("pc-win-01"); m["mode"] != "split" || m["model"] != "Qwen3-4B-Q4_K_M.gguf" {
		t.Errorf("member not restored: %+v", m)
	}
	if m := last("pc-001"); m["mode"] != "split" || m["model"] != "Qwen3-4B-Q4_K_M.gguf" {
		t.Errorf("master not restored: %+v", m)
	}
}

func TestTopologyMemberNotReadyNeverTouchesMaster(t *testing.T) {
	r := newTopoRig(t, "pc-001", "pc-win-01")
	r.workers["pc-win-01"].stuck = true
	_, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"}, Model: "big.gguf"})
	if err == nil || !strings.Contains(err.Error(), "not rpc-ready") {
		t.Fatalf("got %v", err)
	}
	for _, c := range r.callsTo("pc-001") {
		if c["mode"] == "merged" {
			t.Errorf("master was told to merge although the member never became ready")
		}
	}
}

func TestTopologyRejectedByWorkerFailsAndRestores(t *testing.T) {
	r := newTopoRig(t, "a", "b")
	r.workers["b"].reject = http.StatusInternalServerError
	_, err := r.do(TopologySwitchRequest{Mode: TopologyModeSmart1G, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "m.gguf", NCpuMoe: 26})
	if err == nil || !strings.Contains(err.Error(), "worker b") {
		t.Fatalf("got %v", err)
	}
	if got := r.callsTo("a"); len(got) != 2 || got[0]["n_cpu_moe"] != float64(26) || got[1]["mode"] != "split" {
		t.Errorf("a should get the new config then be restored: %+v", got)
	}
	if r.tc.GetStatus().Mode != TopologyModeSplit {
		t.Errorf("mode changed despite failure")
	}
}

func TestTopologySplitFromMergedStopsMasterFirst(t *testing.T) {
	r := newTopoRig(t, "pc-001", "pc-win-01")
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"}, Model: "big.gguf"}); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	r.calls = nil
	r.mu.Unlock()
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"}, Model: "small.gguf"}); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(r.callIDs(), ","); got != "pc-001,pc-win-01" {
		t.Errorf("order %s, want master first", got)
	}
	if r.tc.GetStatus().Mode != TopologyModeSplit {
		t.Errorf("not split")
	}
}

func TestTopologyValidationRejectsBadRequests(t *testing.T) {
	r := newTopoRig(t, "a", "b")
	offline := r.addWorker("off")
	_ = offline
	// make "off" look offline
	r.reg.mu.Lock()
	r.reg.workers["off"].LastSeen = time.Now().Add(-time.Hour)
	r.reg.mu.Unlock()
	cases := map[string]TopologySwitchRequest{
		"bad mode":              {Mode: "x", PrimaryWorkerID: "a", Model: "m"},
		"no model":              {Mode: "split", PrimaryWorkerID: "a"},
		"no primary":            {Mode: "split", Model: "m"},
		"smart without n":       {Mode: "smart_1gpu", PrimaryWorkerID: "a", Model: "m"},
		"unknown worker":        {Mode: "split", PrimaryWorkerID: "zzz", Model: "m"},
		"duplicate":             {Mode: "merged", PrimaryWorkerID: "a", RPCWorkerIDs: []string{"a"}, Model: "m"},
		"merged without member": {Mode: "merged", PrimaryWorkerID: "a", Model: "m"},
		"split count":           {Mode: "merged", PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "m", TensorSplit: "1,1,1"},
		"split junk":            {Mode: "merged", PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "m", TensorSplit: "1;2"},
		"offline member":        {Mode: "merged", PrimaryWorkerID: "a", RPCWorkerIDs: []string{"off"}, Model: "m"},
		"bad kv":                {Mode: "split", PrimaryWorkerID: "a", Model: "m", KVType: "q4"},
	}
	for name, req := range cases {
		_, err := r.do(req)
		var re *topologyRequestError
		if err == nil || !asReqErr(err, &re) {
			t.Errorf("%s: want a request error, got %v", name, err)
		}
	}
	if n := len(r.callIDs()); n != 0 {
		t.Errorf("invalid requests reached workers (%d calls)", n)
	}
}

func asReqErr(err error, target **topologyRequestError) bool {
	e, ok := err.(*topologyRequestError)
	if ok {
		*target = e
	}
	return ok
}

func TestTopologyBusyReturns409AndStatusStaysReadable(t *testing.T) {
	r := newTopoRig(t, "a", "b")
	fw := r.workers["a"]
	fw.block, fw.started = make(chan struct{}), make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "m.gguf"})
		done <- err
	}()
	<-fw.started
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "a", Model: "m.gguf"}); !errors.Is(err, ErrTopologyBusy) {
		t.Errorf("second switch: got %v, want ErrTopologyBusy", err)
	}
	got := make(chan ClusterTopologyStatus, 1)
	go func() { got <- r.tc.GetStatus() }()
	select {
	case st := <-got:
		if st.Status != "switching" {
			t.Errorf("status %q during switch", st.Status)
		}
	case <-time.After(time.Second):
		t.Fatal("GetStatus blocked while a switch was running")
	}
	close(fw.block)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestTopologyHTTPStatusCodesAndRBAC(t *testing.T) {
	reg := NewRegistry(5 * time.Second)
	f := NewFleet(reg)
	ts, _ := NewTenantStore(t.TempDir() + "/tenants.json")
	auth := NewAuth("admin-secret")
	api := NewAPIStudio(f, ts, auth, nil, nil, nil)

	call := func(a http.Handler, method, token string, body any) *httptest.ResponseRecorder {
		var rd *bytes.Reader
		if body != nil {
			b, _ := json.Marshal(body)
			rd = bytes.NewReader(b)
		} else {
			rd = bytes.NewReader(nil)
		}
		req := httptest.NewRequest(method, "/api/cluster/topology", rd)
		req.Header.Set("Authorization", "Bearer "+token)
		rec := httptest.NewRecorder()
		a.ServeHTTP(rec, req)
		return rec
	}
	if rec := call(api, http.MethodGet, "admin-secret", nil); rec.Code != http.StatusOK {
		t.Fatalf("GET %d", rec.Code)
	}
	if rec := call(api, http.MethodPost, "admin-secret", TopologySwitchRequest{Mode: "split"}); rec.Code != http.StatusBadRequest {
		t.Errorf("invalid POST -> %d, want 400: %s", rec.Code, rec.Body.String())
	}
	if rec := call(api, http.MethodPost, "", TopologySwitchRequest{Mode: "split"}); rec.Code != http.StatusUnauthorized {
		t.Errorf("anonymous POST -> %d, want 401", rec.Code)
	}

	rolesPath := t.TempDir() + "/roles.json"
	viewerToken := "viewer-token-1234567890"
	rolesData, _ := json.Marshal([]map[string]string{{"name": "v", "role": RoleViewer, "token_sha256": HashToken(viewerToken)}})
	if err := os.WriteFile(rolesPath, rolesData, 0o600); err != nil {
		t.Fatal(err)
	}
	authR := NewAuth("admin-secret")
	if err := authR.LoadRoles(rolesPath); err != nil {
		t.Fatal(err)
	}
	apiR := NewAPIStudio(f, ts, authR, nil, nil, nil)
	if rec := call(apiR, http.MethodGet, viewerToken, nil); rec.Code != http.StatusOK {
		t.Errorf("viewer GET %d", rec.Code)
	}
	if rec := call(apiR, http.MethodPost, viewerToken, TopologySwitchRequest{Mode: "split", PrimaryWorkerID: "a", Model: "m"}); rec.Code != http.StatusForbidden {
		t.Errorf("viewer POST %d, want 403", rec.Code)
	}
}

// The router must not send inference to a worker that serves as an RPC member, and must keep doing so for the
// heartbeat model string the real worker-guard sends (the marker survives, see worker-guard baseName).
func TestRoutableSkipsRPCMembers(t *testing.T) {
	r := newTopoRig(t, "a", "b")
	r.beat("b", "rpc://50052", "running")
	routes := r.reg.Routable()
	if len(routes) != 1 || routes[0].ID != "a" {
		t.Fatalf("routes %+v", routes)
	}
}

// API order is "primary first"; llama.cpp wants the RPC devices first and the local GPU last (measured, see
// llamaTensorSplit), so a 40/60 request for primary/member must reach the master as 60,40.
func TestTopologyTensorSplitReorderedForLlama(t *testing.T) {
	r := newTopoRig(t, "pc-001", "pc-win-01")
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"},
		Model: "m.gguf", TensorSplit: "4,6"}); err != nil {
		t.Fatal(err)
	}
	if got := r.callsTo("pc-001")[0]["tensor_split"]; got != "6,4" {
		t.Errorf("tensor_split sent to master = %v, want 6,4", got)
	}
	if llamaTensorSplit("1") != "1" || llamaTensorSplit("1,2,3") != "2,3,1" {
		t.Errorf("llamaTensorSplit helper wrong")
	}
}

// Found on real hardware: changing only -ncmoe keeps the model name, so the heartbeat from BEFORE the change already
// looked "running that model" and the gateway reported success after 0.6 s while the engine was still reloading.
func TestSmartSameModelNewNWaitsForAFreshHeartbeat(t *testing.T) {
	r := newTopoRig(t, "pc-001")
	r.beat("pc-001", "m.gguf", "running") // already serving m.gguf (e.g. with another N)
	r.workers["pc-001"].stuck = true      // accepts the directive but never reports again
	_, err := r.do(TopologySwitchRequest{Mode: "smart_1gpu", PrimaryWorkerID: "pc-001", Model: "m.gguf", NCpuMoe: 20})
	if err == nil {
		t.Fatal("success was reported from a stale heartbeat (same model name, new N)")
	}
}

func TestTopologyTwoGroupsAndAStandaloneAreIndependent(t *testing.T) {
	r := newTopoRig(t, "a", "b", "c", "d", "e")
	for _, g := range [][]string{{"a", "b"}, {"c", "d"}} {
		if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: g[0], RPCWorkerIDs: g[1:], Model: "big.gguf"}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeSmart1G, PrimaryWorkerID: "e", Model: "m.gguf", NCpuMoe: 26}); err != nil {
		t.Fatal(err)
	}
	st := r.tc.GetStatus()
	if len(st.Groups) != 2 || st.Groups[0].ID != "a" || st.Groups[1].ID != "c" || st.Groups[1].RPCWorkerIDs[0] != "d" {
		t.Fatalf("groups %+v", st.Groups)
	}
	if st.Workers["b"].Group != "a" || st.Workers["d"].Group != "c" || st.Workers["c"].Role != "master" ||
		st.Workers["e"].Mode != TopologyModeSmart1G || st.Workers["e"].Group != "" {
		t.Errorf("worker roles %+v", st.Workers)
	}
	if got := r.callsTo("c"); len(got) != 1 {
		t.Errorf("building group a+b or e must not touch c: %v", got)
	}
}

func TestTopologyGroupIsChangedAsAUnit(t *testing.T) {
	r := newTopoRig(t, "a", "b", "c")
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "big.gguf"}); err != nil {
		t.Fatal(err)
	}
	n := len(r.callIDs())
	// split only "b" out of the group: refused, nothing is touched
	_, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "b", Model: "m.gguf"})
	var reqErr *topologyRequestError
	if !errors.As(err, &reqErr) || !strings.Contains(err.Error(), "whole group") {
		t.Fatalf("partial dissolve: got %v", err)
	}
	// stealing the member into another group is refused as well
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "c", RPCWorkerIDs: []string{"b"}, Model: "big.gguf"}); !errors.As(err, &reqErr) {
		t.Fatalf("steal member: got %v", err)
	}
	if len(r.callIDs()) != n || len(r.tc.GetStatus().Groups) != 1 {
		t.Fatalf("a refused request changed something: calls %v", r.callIDs())
	}
	// dissolving the whole group works, and c is then free to be merged with b
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "m.gguf"}); err != nil {
		t.Fatal(err)
	}
	if len(r.tc.GetStatus().Groups) != 0 {
		t.Fatal("group still listed after dissolve")
	}
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "c", RPCWorkerIDs: []string{"b"}, Model: "big.gguf"}); err != nil {
		t.Fatal(err)
	}
}

func TestTopologyDisjointSwitchesRunConcurrentlyOverlapGets409(t *testing.T) {
	r := newTopoRig(t, "a", "b", "c")
	fw := r.workers["a"]
	fw.block, fw.started = make(chan struct{}), make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "a", Model: "m.gguf"})
		done <- err
	}()
	<-fw.started
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeSplit, PrimaryWorkerID: "c", Model: "m.gguf"}); err != nil {
		t.Errorf("disjoint worker must not wait for a: %v", err)
	}
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "b", RPCWorkerIDs: []string{"a"}, Model: "m.gguf"}); !errors.Is(err, ErrTopologyBusy) {
		t.Errorf("overlap: got %v, want busy", err)
	}
	if st := r.tc.GetStatus(); st.Status != "switching" || len(st.Busy) != 1 || st.Busy[0] != "a" {
		t.Errorf("status %q busy %v", st.Status, st.Busy)
	}
	close(fw.block)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if st := r.tc.GetStatus(); st.Status != "ready" || len(st.Busy) != 0 {
		t.Errorf("after: %q %v", st.Status, st.Busy)
	}
}

func TestTopologyFailedGroupRollbackLeavesOtherGroupAlone(t *testing.T) {
	r := newTopoRig(t, "a", "b", "c", "d")
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "a", RPCWorkerIDs: []string{"b"}, Model: "big.gguf"}); err != nil {
		t.Fatal(err)
	}
	r.workers["c"].stuck = true // master c never reports running
	if _, err := r.do(TopologySwitchRequest{Mode: TopologyModeMerged, PrimaryWorkerID: "c", RPCWorkerIDs: []string{"d"}, Model: "big.gguf"}); err == nil {
		t.Fatal("expected failure")
	}
	st := r.tc.GetStatus()
	if len(st.Groups) != 1 || st.Groups[0].ID != "a" || st.Status != "error" {
		t.Errorf("after failed second group: %+v", st)
	}
	if len(r.callsTo("a")) != 1 || len(r.callsTo("b")) != 1 {
		t.Errorf("group a+b was touched by group c+d's rollback: a=%v b=%v", r.callsTo("a"), r.callsTo("b"))
	}
}

func TestTopologyPersistsStateAcrossGatewayRestartAndReconcilesWorkers(t *testing.T) {
	r := newTopoRig(t, "pc-001", "pc-win-01", "pc-003")
	stateFile := t.TempDir() + "/topology.json"
	if err := r.tc.SetStateFile(stateFile); err != nil {
		t.Fatal(err)
	}
	if _, err := r.do(TopologySwitchRequest{
		Mode: TopologyModeMerged, PrimaryWorkerID: "pc-001", RPCWorkerIDs: []string{"pc-win-01"},
		Model: "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", TensorSplit: "48,52",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := r.do(TopologySwitchRequest{
		Mode: TopologyModeSmart1G, PrimaryWorkerID: "pc-003",
		Model: "Qwen3-30B-A3B-Q4_K_M.gguf", NCpuMoe: 26, KVType: "q8_0",
	}); err != nil {
		t.Fatal(err)
	}

	// Simulate a gateway restart: create a brand-new TopologyCoordinator pointing at the same state file.
	tc2 := NewTopologyCoordinator(r.reg, nil)
	tc2.Poll, tc2.RPCReadyTimeout, tc2.LoadTimeout, tc2.ReconcileCooldown = 10*time.Millisecond, 300*time.Millisecond, 300*time.Millisecond, time.Millisecond
	if err := tc2.SetStateFile(stateFile); err != nil {
		t.Fatal(err)
	}
	st := tc2.GetStatus()
	if st.Mode != TopologyModeMerged || len(st.Groups) != 1 || st.Groups[0].ID != "pc-001" ||
		st.Groups[0].Model != "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf" || st.Groups[0].TensorSplit != "48,52" {
		t.Fatalf("restored groups mismatch: %+v", st.Groups)
	}
	if st.Workers["pc-003"].Mode != TopologyModeSmart1G {
		t.Fatalf("restored indep worker mode mismatch: %+v", st.Workers["pc-003"])
	}

	// Simulate pc-win-01 and pc-003 restarting without local state files (reporting default 4B model).
	r.mu.Lock()
	r.calls = nil
	r.mu.Unlock()
	r.beat("pc-win-01", "Qwen3-4B-Q4_K_M.gguf", "running")
	r.beat("pc-003", "Qwen3-4B-Q4_K_M.gguf", "running")

	tc2.ReconcileOnce(context.Background())

	if got := r.callsTo("pc-win-01"); len(got) == 0 || got[len(got)-1]["mode"] != TopologyModeRPCWrk {
		t.Fatalf("expected pc-win-01 to be reconciled back to rpc-worker, calls=%+v", got)
	}
	if got := r.callsTo("pc-001"); len(got) == 0 || got[len(got)-1]["mode"] != TopologyModeMerged || got[len(got)-1]["tensor_split"] != "52,48" {
		t.Fatalf("expected pc-001 to be reconciled back to merged 52,48, calls=%+v", got)
	}
	if got := r.callsTo("pc-003"); len(got) == 0 || got[len(got)-1]["mode"] != TopologyModeSmart1G || got[len(got)-1]["n_cpu_moe"] != float64(26) {
		t.Fatalf("expected pc-003 to be reconciled back to smart_1gpu N=26, calls=%+v", got)
	}
}
