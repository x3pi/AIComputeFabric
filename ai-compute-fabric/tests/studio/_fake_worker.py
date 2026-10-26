"""Stand-in for studio.lora_run: same contract (job.json in, progress.jsonl / adapter/ / result.json out), no GPU."""
import json
import sys
import time
from pathlib import Path

d = Path(sys.argv[1])
job = json.loads((d / "job.json").read_text())
mode = job.get("fake", "ok")
(d / "progress.jsonl").write_text(json.dumps({"phase": "training", "step": 0}) + "\n")
if mode == "sleep":
    time.sleep(60)
if mode == "crash":
    print("boom", flush=True)
    sys.exit(3)
(d / "adapter").mkdir()
(d / "adapter" / "adapter_model.safetensors").write_bytes(b"x" * 100)
(d / "result.json").write_text(json.dumps({"steps": job["steps"], "last_loss": 1.0}))
