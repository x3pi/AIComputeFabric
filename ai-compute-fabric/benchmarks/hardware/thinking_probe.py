#!/usr/bin/env python3
"""Thinking ON vs the stored thinking-OFF result on the same HumanEval tasks (engine direct). Usage: thinking_probe.py STEP BUDGET OUT.json"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import full164_distributed_benchmark as F
import urllib.request
step, budget, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
probs = F.load_problems()[::step] if step else [p for p in F.load_problems() if p["task_id"] in set(open(sys.argv[4]).read().split(","))]
base = {t["task_id"]: t for t in json.load(open(F.OUT_DIR / "Qwen3.8-27B-UD-Q4_K_M__merged.json"))["tasks"]}
rows = []
for pr in probs:
    msgs = [{"role": "system", "content": F.SYS_PROMPT}, {"role": "user", "content": f"Complete this Python function:\n\n```python\n{pr['prompt']}\n```"}]
    body = {"model": "x", "messages": msgs, "max_tokens": budget, "temperature": 0.0, "seed": 1234, "cache_prompt": False}
    r = urllib.request.Request("http://127.0.0.1:18090/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time(); d = json.loads(urllib.request.urlopen(r, timeout=3600).read()); el = time.time() - t0
    m = d["choices"][0]["message"]; content = m.get("content") or ""
    ok, err = F.run_sandbox(F.build_program(pr, content))
    tm = d.get("timings", {})
    rows.append({"task_id": pr["task_id"], "passed_think": ok, "passed_nothink": base[pr["task_id"]]["passed"], "finish": d["choices"][0]["finish_reason"],
                 "out_tok": tm.get("predicted_n"), "think_chars": len(m.get("reasoning_content") or ""), "seconds": round(el, 1),
                 "decode_tok_s": round(tm.get("predicted_per_second", 0), 1), "nothink_out_tok": base[pr["task_id"]].get("out_tok")})
    print(json.dumps(rows[-1]), flush=True)
    json.dump(rows, open(out, "w"), indent=1)
print("DONE", sum(r["passed_think"] for r in rows), "/", len(rows), "nothink", sum(r["passed_nothink"] for r in rows))
