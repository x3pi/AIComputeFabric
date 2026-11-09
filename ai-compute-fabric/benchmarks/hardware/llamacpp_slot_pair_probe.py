#!/usr/bin/env python3
"""Force two concurrent requests onto chosen llama-server slots (id_slot) and measure aggregate decode tok/s.
Start llama-server first (port 18190); the pair list below is edited per run."""
import json, sys, threading, time, statistics, urllib.request
sys.path.insert(0, "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/benchmarks/hardware")
from openai_stream_bench import prompt
B = "http://127.0.0.1:18190"
def one(text, slot, mt=192):
    body = {"model": "x", "messages": [{"role": "user", "content": text}], "max_tokens": mt, "temperature": 0.0, "stream": True,
            "stream_options": {"include_usage": True}, "id_slot": slot}
    req = urllib.request.Request(B + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    first = last = None; n = 0
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            l = raw.decode().strip()
            if not l.startswith("data:") or l[5:].strip() == "[DONE]": continue
            try: ch = json.loads(l[5:])
            except ValueError: continue
            for c in ch.get("choices") or []:
                d = c.get("delta") or {}
                if d.get("content") or d.get("reasoning_content"):
                    now = time.perf_counter(); first = first or now; last = now; n += 1
    return n, (n - 1) / (last - first)
def pair(s0, s1, salt):
    out = [None, None]
    def w(i): out[i] = one(prompt(600, salt + i), (s0, s1)[i])
    ts = time.perf_counter(); th = [threading.Thread(target=w, args=(i,)) for i in (0, 1)]
    [t.start() for t in th]; [t.join() for t in th]; el = time.perf_counter() - ts
    return round(sum(o[0] for o in out) / el, 1), round(statistics.mean(o[1] for o in out), 1)
one(prompt(300, 1), 0, 8)
k = 300
for s0, s1 in [(0, 1), (0, 2), (2, 3)]:
    pair(s0, s1, k); k += 10
    print(f"slots ({s0},{s1}): agg/per-stream", pair(s0, s1, k), flush=True); k += 10
