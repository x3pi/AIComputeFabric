package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// vramSidecar is set by main from --vram-sidecar-url (nil = the endpoint answers 501). It is a package variable only
// because the route table is built in two places; tests set it directly.
var vramSidecar *Sidecar

// Sidecar reads architecture facts from a real GGUF file through core/vram_sidecar (Python), so a model that is not in
// the built-in catalogue can still be estimated. The sidecar only serves files under its own --models-root.
type Sidecar struct {
	base   string
	client *http.Client
}

func NewSidecar(base string) *Sidecar {
	if strings.TrimSpace(base) == "" {
		return nil
	}
	return &Sidecar{base: strings.TrimRight(base, "/"), client: &http.Client{Timeout: 20 * time.Second}}
}

type ggufFacts struct {
	Architecture      string `json:"architecture"`
	Name              string `json:"name"`
	Quantization      string `json:"quantization"`
	Layers            uint64 `json:"layers"`
	LayersAttn        uint64 `json:"layers_attn"`
	NKV               uint64 `json:"n_kv"`
	HeadDim           uint64 `json:"head_dim"`
	ContextLength     uint64 `json:"context_length"`
	WeightsBytes      uint64 `json:"weights_bytes"`
	NonRepeatingBytes uint64 `json:"non_repeating_bytes"`
	EmbeddingCPUBytes uint64 `json:"embedding_cpu_bytes"`
	Experts           uint64 `json:"experts"`
	Note              string `json:"note"`
}

func (s *Sidecar) Facts(file string) (ggufFacts, int, error) {
	var out ggufFacts
	resp, err := s.client.Get(s.base + "/facts?file=" + url.QueryEscape(file))
	if err != nil {
		return out, http.StatusBadGateway, fmt.Errorf("sidecar unreachable: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusOK {
		var e struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &e)
		code := resp.StatusCode
		if code >= 500 {
			code = http.StatusBadGateway
		}
		return out, code, fmt.Errorf("%s", e.Error)
	}
	if err := json.Unmarshal(body, &out); err != nil || out.Layers == 0 || out.NKV == 0 || out.HeadDim == 0 || out.WeightsBytes == 0 {
		return out, http.StatusBadGateway, fmt.Errorf("sidecar returned unusable facts")
	}
	return out, http.StatusOK, nil
}

func (g ggufFacts) catalogModel(file string) CatalogModel {
	arch := fmt.Sprintf("%s, %d layers, %d KV heads, head dim %d", g.Architecture, g.Layers, g.NKV, g.HeadDim)
	if g.Experts > 0 {
		arch += fmt.Sprintf(", %d experts (weights counted in full)", g.Experts)
	}
	note := "Read from the GGUF header; the 6-bucket rule is the one calibrated on Qwen3-4B/8B (Q4_K_M, Q8_0, F16) on an RTX 3060. Other architectures and quantisations are NOT calibrated."
	if g.Note != "" {
		note += " " + g.Note
	}
	layersAttn := g.LayersAttn
	if layersAttn == 0 {
		layersAttn = g.Layers
	}
	// llama.cpp keeps an untied token embedding in host memory even with -ngl 99 (seen as host_model in its log), so it
	// takes no VRAM; subtracting it removed a constant +334 MiB (8B Q4_K_M) / +652 MiB (8B Q8_0) overestimate.
	weights, nonRep := g.WeightsBytes, g.NonRepeatingBytes
	if g.EmbeddingCPUBytes > 0 && g.EmbeddingCPUBytes < weights && g.EmbeddingCPUBytes <= nonRep {
		weights, nonRep = weights-g.EmbeddingCPUBytes, nonRep-g.EmbeddingCPUBytes
		note += fmt.Sprintf(" Untied token embedding (%d MiB) is counted as host RAM, not VRAM.", g.EmbeddingCPUBytes/mib)
	}
	return CatalogModel{ID: "gguf:" + file, Name: g.Name, Family: g.Architecture, Architecture: arch, Quantization: g.Quantization, GGUFFile: file,
		Layers: g.Layers, LayersAttn: layersAttn, NKV: g.NKV, HeadDim: g.HeadDim, BytesPerKV: 2, WeightsBytes: weights,
		NonRepeatingBytes: nonRep, Confidence: "ESTIMATED", CalibrationNote: note, License: "unknown"}
}

// estimateFromGGUF serves POST /api/v1/models/estimate-vram: the EstimateRequest fields plus "gguf", a file name under the
// sidecar's models root. The model facts come from the file, not from the catalogue.
func estimateFromGGUF(f *Fleet) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		if vramSidecar == nil {
			http.Error(w, "no --vram-sidecar-url configured", http.StatusNotImplemented)
			return
		}
		var in struct {
			EstimateRequest
			GGUF string `json:"gguf"`
		}
		in.FlashAttn, in.KVBytesPerElem = true, 2
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&in) != nil || strings.TrimSpace(in.GGUF) == "" {
			http.Error(w, `bad JSON: "gguf" (file name) is required`, http.StatusBadRequest)
			return
		}
		facts, code, err := vramSidecar.Facts(in.GGUF)
		if err != nil {
			http.Error(w, err.Error(), code)
			return
		}
		writeJSON(w, http.StatusOK, computeEstimateFor(facts.catalogModel(in.GGUF), in.EstimateRequest, f))
	}
}
