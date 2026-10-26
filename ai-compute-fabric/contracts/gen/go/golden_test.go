package aicfv1

import (
	"bytes"
	"flag"
	"os"
	"path/filepath"
	"testing"

	"google.golang.org/protobuf/proto"
)

// Cross-language contract: Go serializes these messages, contracts/testdata/*.bin holds the bytes, and
// tests/unit/test_protobuf_contracts.py parses the same bytes with the Python bindings (and re-serializes
// them to the same bytes). Run `go test -update` after an intended schema change and commit the .bin files.
var update = flag.Bool("update", false, "rewrite contracts/testdata/*.bin")

func goldenMessages() map[string]proto.Message {
	b := &VramBuckets{TotalBytes: 12 << 30, OsUserBytes: 512 << 20, WeightsBytes: 4683 << 20, KvStateBytes: 1 << 30, ComputeBytes: 300 << 20, HeadroomBytes: 1536 << 20, AllocatableBytes: 4 << 30}
	return map[string]proto.Message{
		"lease_token": &LeaseToken{LeaseId: "lease-1", WorkerId: "w-1", DeploymentId: "dep-1", VramReservedBytes: 6 << 30, TtlMs: 30000, IssuedUnixMs: 1790000000000},
		"model_identity": &ModelIdentity{ModelId: "Qwen/Qwen3-8B", WeightSha256: "aa", TokenizerSha256: "bb", Quantization: "Q4_K_M",
			SourceRepo: "Qwen/Qwen3-8B", GgufSha256: "cc", LicenseSpdx: "Apache-2.0", TrustRemoteCode: false},
		"vram_estimate": &VramEstimate{DeploymentPlanId: "plan-1", SystemRamBytes: 8 << 30, Confidence: ConfidenceLevel_CONFIDENCE_CALIBRATED, ErrorMarginPct: 5,
			EstimatorVersion: "t", FitStatus: "fits", PerGpu: []*GpuLayerEstimate{{WorkerId: "w-1", GpuIndex: 0, LayerStart: 0, LayerEnd: 36, ProjectedBuckets: b}}},
		"worker_capacity": &WorkerCapacity{WorkerId: "w-1", SiteId: "s-1", VramFreeBytes: 9 << 30, RamFreeBytes: 20 << 30, CpuThreadsAvailable: 12,
			SupportedEngines: []string{"llama"}, UserIsActive: true, Vram: b},
	}
}

func TestGoldenBytesAreStable(t *testing.T) {
	dir := filepath.Join("..", "..", "testdata")
	for name, m := range goldenMessages() {
		got, err := proto.MarshalOptions{Deterministic: true}.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(dir, name+".bin")
		if *update {
			if err := os.WriteFile(path, got, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("%s: %v (run go test -update)", name, err)
		}
		if !bytes.Equal(got, want) {
			t.Errorf("%s: Go now serializes differently from the committed bytes", name)
		}
		back := m.ProtoReflect().New().Interface()
		if err := proto.Unmarshal(want, back); err != nil || !proto.Equal(back, m) {
			t.Errorf("%s: committed bytes do not decode to the expected message (%v)", name, err)
		}
	}
}
