"""P1.3: the committed real run (HF -> snapshot -> mirror -> converter -> GGUF -> llama-server) is complete and self-consistent."""
import json
import re
from pathlib import Path

R = json.loads((Path(__file__).resolve().parents[2] / "benchmarks/reports/p1_model_provenance_qwen3_0p6b.json").read_text())


def test_every_step_ran_and_links_agree():
    s = R["steps"]
    assert re.fullmatch(r"[0-9a-f]{40}", R["revision"])
    assert s["2_pull_through_lan_mirror"]["verified_against_pinned_digest"] is True
    assert s["4_verify_provenance"]["ok"] is True
    ident = s["4_verify_provenance"]["identity"]
    assert ident["gguf_sha256"] == s["3_convert"]["gguf_sha256"] and ident["source_revision"] == R["revision"] and ident["trust_remote_code"] is False
    assert s["3_convert"]["converter"]["llama_cpp_commit"] == "74bca3950054eed1795f525cb9cf06bfe796492d"      # the commit pinned in third_party/upstream-manifest.yaml


def test_llama_server_loaded_it_and_tokenizes_like_hugging_face():
    smoke = R["steps"]["6_llama_server_smoke"]
    assert smoke["loaded"] and smoke["sample_reply"].strip()
    assert smoke["sentences"] == len(smoke["tokenization_identical_to_hf_tokenizer"]) >= 4 and all(smoke["tokenization_identical_to_hf_tokenizer"])


def test_the_one_known_difference_from_the_published_gguf_is_recorded_not_hidden():
    same = R["steps"]["5_compare_with_published_gguf"]["same"]
    assert all(v for k, v in same.items() if k != "weights_bytes")
    assert same["weights_bytes"] is False                 # this converter writes a separate output.weight for tied embeddings (+165 MB)
