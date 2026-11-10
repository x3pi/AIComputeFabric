#!/usr/bin/env bash
# Starts AI Studio with the same arguments run_demo.sh uses (base models and audio models discovered at start).
set -eu
ROOT=/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric; WORK=/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work; D=$ROOT/.demo; PY=$ROOT/.venv/bin/python
BASE=$($PY - "$WORK/hf-train" "$WORK/Qwen3-0.6B-hf" <<'PYEOF'
import json, sys
from pathlib import Path
found = {}
for root in map(Path, sys.argv[1:]):
    dirs = [root] if (root / "config.json").is_file() else sorted(p for p in root.glob("*") if (p / "config.json").is_file()) if root.is_dir() else []
    for d in dirs:
        found[d.name.lower()] = str(d)
print(json.dumps(found))
PYEOF
)
AUD=(); [ -d "$WORK/models-audio/Qwen3-ASR-0.6B-hf" ] && AUD+=(--asr-model "$WORK/models-audio/Qwen3-ASR-0.6B-hf")
[ -d "$WORK/models-audio/mms-tts-vie" ] && AUD+=(--tts-model "$WORK/models-audio/mms-tts-vie")
exec $PY -m studio --llama-cpp-dir "$WORK/llama.cpp-py" --data-dir "$D/studio" --dsn "postgresql://aicf_app:aicf_app_pw@127.0.0.1:55432/aicf" --admin-dsn "postgresql://postgres:aicf@127.0.0.1:55432/aicf" --setup \
  --port 18098 --worker-url http://127.0.0.1:8099 --worker-id pc-001 --adapters-dir "$D/adapters" --llm-url http://127.0.0.1:18090 \
  --embed-url http://127.0.0.1:18091 --rerank-url http://127.0.0.1:18092 --train-python "$WORK/venv-train/bin/python" \
  --base-models "$BASE" "${AUD[@]}"
