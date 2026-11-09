#!/usr/bin/env python3
"""Latency of the embedding (18091) and reranker (18092) llama-servers: embeddings for ~60/~500/~2000-token texts (5 reps, median),
rerank of 20 documents (~200 tokens each). Measures only. Usage: embed_rerank_probe.py LABEL OUT.json"""
import json, statistics, sys, time, urllib.request

def post(port, path, body):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.perf_counter(); out = json.loads(urllib.request.urlopen(r, timeout=300).read()); return (time.perf_counter() - t) * 1000, out

W = "Distributed inference splits a neural network across several accelerators and passes small activations over the network. "
res = {"label": sys.argv[1]}
post(18091, "/v1/embeddings", {"input": W}); post(18092, "/rerank", {"query": "q", "documents": [W]})
for name, rep in (("embed_60tok", 3), ("embed_500tok", 25), ("embed_2000tok", 100)):
    ms = [post(18091, "/v1/embeddings", {"input": f"[{i}] " + W * rep})[0] for i in range(5)]
    res[name + "_ms_median"] = round(statistics.median(ms), 1)
docs = [f"[{i}] " + W * 8 for i in range(20)]
ms = [post(18092, "/rerank", {"query": "How is a model split across devices?", "documents": docs})[0] for _ in range(5)]
res["rerank_20docs_ms_median"] = round(statistics.median(ms), 1)
json.dump(res, open(sys.argv[2], "w"), indent=1); print(json.dumps(res))
