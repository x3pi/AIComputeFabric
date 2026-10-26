"""Runs lora_memory_one.py over a grid, one process per configuration, appending JSON lines.
usage: lora_memory_grid.py OUT.jsonl SET   (SET = calib | heldout | scale)"""
import itertools
import json
import subprocess
import sys
from pathlib import Path

W = Path(__file__).resolve().parents[3] / ".aicf-work/hf-train"
out, which = Path(sys.argv[1]), sys.argv[2]
if which == "calib":      # fit the estimator on these
    cfgs = [("Qwen3-0.6B", b, s, 16, c, o) for b, s, c, o in itertools.product([1, 2, 4], [256, 512, 1024], [0, 1], ["adamw", "adamw8bit"])]
elif which == "heldout":  # never used for fitting
    cfgs = [("Qwen3-0.6B", b, s, r, c, o) for b, s, r, c, o in [(3, 384, 8, 0, "adamw"), (6, 768, 32, 1, "adamw"), (8, 512, 64, 1, "adamw8bit"),
            (2, 1536, 16, 0, "adamw"), (1, 2048, 16, 1, "adamw"), (5, 640, 128, 0, "adamw8bit"), (12, 256, 16, 1, "adamw"), (16, 1024, 16, 1, "adamw8bit")]]
else:                     # a larger model: does the estimate scale?
    cfgs = [("Qwen3-1.7B", b, s, r, c, o) for b, s, r, c, o in [(1, 512, 16, 1, "adamw"), (2, 512, 16, 1, "adamw"), (4, 512, 16, 1, "adamw8bit"),
            (1, 1024, 32, 0, "adamw"), (2, 1024, 16, 0, "adamw8bit"), (4, 1024, 16, 1, "adamw"), (8, 512, 16, 1, "adamw"), (8, 1024, 16, 1, "adamw"), (1, 2048, 16, 1, "adamw")]]
done = {json.dumps(json.loads(l)["_cfg"]) for l in out.read_text().splitlines()} if out.exists() else set()
for cfg in cfgs:
    if json.dumps(list(cfg)) in done:
        continue
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("lora_memory_one.py")), str(W / cfg[0]), *map(str, cfg[1:4]), str(int(cfg[4])), cfg[5]],
                       capture_output=True, text=True, timeout=600)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
    res = json.loads(line[7:]) if line else {"ok": False, "error": (r.stderr or "no output")[-200:]}
    res["_cfg"] = list(cfg)
    res["set"] = which
    with open(out, "a") as f:
        f.write(json.dumps(res) + "\n")
    print(cfg, res.get("peak_used_mib"), res.get("error", ""), flush=True)
