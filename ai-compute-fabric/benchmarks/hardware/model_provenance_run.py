#!/usr/bin/env python3
"""P1.3 evidence run: Hugging Face -> pinned snapshot -> LAN mirror -> llama.cpp converter -> GGUF -> llama-server.

usage: model_provenance_run.py WORKDIR LLAMA_CPP_DIR LLAMA_SERVER OUT.json [--repo Qwen/Qwen3-0.6B --revision <sha40>] [--compare REFERENCE.gguf]
Needs network access to huggingface.co for the first step, and a Python with torch + transformers for the converter."""
import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.model_registry.provenance import convert_with_record, model_identity, verify_provenance  # noqa: E402
from core.model_registry.snapshot import manifest_digest, pull_from_mirror, serve_mirror, snapshot_dir, take_snapshot, verify_snapshot  # noqa: E402
from core.vram_sidecar.gguf import read_facts  # noqa: E402

SENTENCES = ["Xin chào, hôm nay trời đẹp quá.", "Hệ thống phân phối tính toán AI trên nhiều máy tính.", "The quick brown fox jumps over 13 lazy dogs!", "def f(x):\n    return x**2  # bình phương"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("llama_cpp")
    ap.add_argument("llama_server")
    ap.add_argument("out")
    ap.add_argument("--repo", default="Qwen/Qwen3-0.6B")
    ap.add_argument("--revision", required=True)
    ap.add_argument("--compare")
    ap.add_argument("--license", default="Apache-2.0")
    a = ap.parse_args()
    work = Path(a.workdir)
    reg_a, reg_b = work / "registry-hub", work / "registry-worker"
    steps = {}

    t = time.time()
    m = take_snapshot(reg_a, a.repo, a.revision)
    steps["1_snapshot_from_hf"] = {"files": len(m["files"]), "bytes": sum(m["sizes"].values()), "manifest_sha256": manifest_digest(m), "seconds": round(time.time() - t, 1)}

    t = time.time()
    srv = serve_mirror(reg_a)
    try:
        mb = pull_from_mirror(f"http://127.0.0.1:{srv.server_port}", reg_b, a.repo, a.revision, manifest_digest(m))
    finally:
        srv.shutdown()
    assert mb == m and verify_snapshot(snapshot_dir(reg_b, a.repo, a.revision)) == m
    steps["2_pull_through_lan_mirror"] = {"verified_against_pinned_digest": True, "seconds": round(time.time() - t, 1)}

    gguf = work / "out" / f"{a.repo.split('/')[1]}-q8_0.gguf"
    rec = convert_with_record(snapshot_dir(reg_b, a.repo, a.revision), gguf, Path(a.llama_cpp) / "convert_hf_to_gguf.py",
                              subprocess.run(["git", "-C", a.llama_cpp, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
                              outtype="q8_0", extra_pythonpath=str(Path(a.llama_cpp) / "gguf-py"))
    steps["3_convert"] = {"gguf_bytes": rec["gguf"]["bytes"], "gguf_sha256": rec["gguf"]["sha256"], "converter": rec["converter"], "facts": rec["gguf"]["facts"]}
    verify_provenance(gguf, lambda r, v: snapshot_dir(reg_b, r, v))
    steps["4_verify_provenance"] = {"ok": True, "identity": model_identity(rec, a.license)}

    if a.compare:
        ref, mine = read_facts(a.compare), read_facts(gguf)
        keys = ("architecture", "quantization", "layers", "n_kv", "head_dim", "context_length", "weights_bytes")
        steps["5_compare_with_published_gguf"] = {"reference": a.compare, "same": {k: getattr(ref, k) == getattr(mine, k) for k in keys},
                                                  "reference_facts": {k: getattr(ref, k) for k in keys}, "converted_facts": {k: getattr(mine, k) for k in keys}}

    port = free_port()
    p = subprocess.Popen([a.llama_server, "-m", str(gguf), "--port", str(port), "-ngl", "99", "-c", "512"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(str(snapshot_dir(reg_b, a.repo, a.revision) / "files"))
        same = []
        for s in SENTENCES:
            req = urllib.request.Request(base + "/tokenize", data=json.dumps({"content": s, "add_special": False}).encode(), headers={"Content-Type": "application/json"})
            theirs = json.load(urllib.request.urlopen(req, timeout=20))["tokens"]
            same.append(theirs == tok(s, add_special_tokens=False)["input_ids"])
        req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps({"messages": [{"role": "user", "content": "Say hi in one word."}], "max_tokens": 8, "temperature": 0,
                                                                                    "chat_template_kwargs": {"enable_thinking": False}}).encode(), headers={"Content-Type": "application/json"})
        reply = json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]
    finally:
        p.terminate()
        p.wait(15)
    steps["6_llama_server_smoke"] = {"loaded": True, "tokenization_identical_to_hf_tokenizer": same, "sentences": len(SENTENCES), "sample_reply": reply}
    rep = {"schema_version": 1, "date": time.strftime("%Y-%m-%d"), "repo": a.repo, "revision": a.revision, "steps": steps,
           "not_covered": ["a single model, a single quantisation (q8_0)", "the provenance record is not signed", "conversion is not bit-reproducible across converter/torch versions",
                           "no gated/private repo", "mirror has no authentication or TLS"]}
    Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: (v if k.startswith("6") else "ok") for k, v in steps.items()}, indent=1))


if __name__ == "__main__":
    main()
