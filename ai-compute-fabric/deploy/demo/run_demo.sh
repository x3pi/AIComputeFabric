#!/usr/bin/env bash
# One-command local full-stack demo (Linux):
#   - site-gateway (:18080 HTTP + :17443 gRPC mTLS)
#   - worker-guard (:8099 Local UI + :18090 llama-server on GPU)
#   - core.vram_sidecar (:18095 GGUF header parser)
#   - agents.dag_api (:18096 Agent DAG viewer backed by PostgreSQL)
#   - studio (:18098 AI Studio: RAG, Prompts/Few-shot, QLoRA, Full-FT Estimate)
# Usage: deploy/demo/run_demo.sh [/path/to/llama-server] [/path/to/model.gguf]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK_DIR="$(cd "$ROOT/.." && pwd)/.aicf-work"
LLAMA="${1:-$WORK_DIR/bin/llama-server}"
MODEL="${2:-$WORK_DIR/Qwen3-4B-Q4_K_M.gguf}"
D="${DEMO_DIR:-$ROOT/.demo}"
PY="${PY:-$ROOT/.venv/bin/python}"
PG_ADMIN_DSN="${AICF_PG_ADMIN_DSN:-postgresql://postgres:aicf@127.0.0.1:55432/aicf}"
PG_APP_DSN="${AICF_PG_APP_DSN:-postgresql://aicf_app:aicf_app_pw@127.0.0.1:55432/aicf}"

mkdir -p "$D/bin" "$D/logs" "$D/studio"
(cd "$ROOT/apps/site-gateway" && go build -o "$D/bin/site-gateway" . && go build -o "$D/bin/pki" ./cmd/pki)
(cd "$ROOT/apps/worker-guard" && go build -o "$D/bin/worker-guard" .)
(cd "$ROOT/apps/global-control-plane" && go build -o "$D/bin/gcp-server" ./cmd/gcp-server)
[ -f "$D/pki/ca.crt" ] || "$D/bin/pki" -out "$D/pki" -workers pc-001

GRPC_PORT="${GRPC_PORT:-17443}"; HTTP_PORT="${HTTP_PORT:-18080}"
SIDECAR_PORT="${SIDECAR_PORT:-18095}"; DAG_PORT="${DAG_PORT:-18096}"; STUDIO_PORT="${STUDIO_PORT:-18098}"
EMBED_PORT="${EMBED_PORT:-18091}"; RERANK_PORT="${RERANK_PORT:-18092}"
export AICF_ADMIN_TOKEN="${AICF_ADMIN_TOKEN:-demo-admin-token}"
export PYTHONPATH="$ROOT"
LD="$(dirname "$LLAMA")"; export LD_LIBRARY_PATH="$LD:${LD_LIBRARY_PATH:-}"

pids=(); trap 'kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true' EXIT INT TERM

# chat models the web UI may switch to (symlinks; embedding/reranker models are not chat models), and where exported adapters go
mkdir -p "$D/models" "$D/adapters"
for g in "$WORK_DIR"/*.gguf; do [ -f "$g" ] && ! basename "$g" | grep -qiE 'embed|rerank|bge' && ln -sf "$g" "$D/models/$(basename "$g")"; done

# 1. GGUF VRAM Sidecar
"$PY" -m core.vram_sidecar.server --models-root "$(dirname "$MODEL")" --host 127.0.0.1 --port "$SIDECAR_PORT" >"$D/logs/sidecar.log" 2>&1 & pids+=($!)

# 2. Agent DAG API & AI Studio (if PostgreSQL is reachable)
GW_EXTRA=(-vram-sidecar-url "http://127.0.0.1:$SIDECAR_PORT" -audit-log "$D/audit.jsonl")
if "$PY" -c "import psycopg; psycopg.connect('$PG_ADMIN_DSN', connect_timeout=2).close()" >/dev/null 2>&1; then
  "$PY" -m agents.dag_api --dsn "$PG_ADMIN_DSN" --listen "127.0.0.1:$DAG_PORT" >"$D/logs/dag_api.log" 2>&1 & pids+=($!)
  TRAIN_PY=""
  [ -x "$WORK_DIR/venv-train/bin/python" ] && TRAIN_PY="$WORK_DIR/venv-train/bin/python"
  # every Hugging Face directory under hf-train/ (config.json + weights) becomes a selectable LoRA base model
  BASE_MODELS="$("$PY" - "$WORK_DIR/hf-train" "$WORK_DIR/Qwen3-0.6B-hf" <<'PYEOF'
import json, sys
from pathlib import Path
found = {}
for root in map(Path, sys.argv[1:]):
    dirs = [root] if (root / "config.json").is_file() else sorted(p for p in root.glob("*") if (p / "config.json").is_file()) if root.is_dir() else []
    for d in dirs:
        found[d.name.lower()] = str(d)
print(json.dumps(found))
PYEOF
)"
  # RAG needs an embedding server (and optionally a reranker); small models, ~1 GB of VRAM together
  EMBED_GGUF="${EMBED_GGUF:-$WORK_DIR/Qwen3-Embedding-0.6B-Q8_0.gguf}"; RERANK_GGUF="${RERANK_GGUF:-$WORK_DIR/bge-reranker-v2-m3-Q8_0.gguf}"
  STUDIO_RAG=()
  if [ -f "$EMBED_GGUF" ]; then
    "$LLAMA" -m "$EMBED_GGUF" --embedding --pooling last -ub 2048 -c 4096 -ngl 99 --host 127.0.0.1 --port "$EMBED_PORT" >"$D/logs/embed.log" 2>&1 & pids+=($!)
    STUDIO_RAG+=(--embed-url "http://127.0.0.1:$EMBED_PORT")
    if [ -f "$RERANK_GGUF" ]; then
      "$LLAMA" -m "$RERANK_GGUF" --reranking -c 4096 -ngl 99 --host 127.0.0.1 --port "$RERANK_PORT" >"$D/logs/rerank.log" 2>&1 & pids+=($!)
      STUDIO_RAG+=(--rerank-url "http://127.0.0.1:$RERANK_PORT")
    fi
  else
    echo "WARNING: $EMBED_GGUF not found: AI Studio RAG will be disabled (set EMBED_GGUF=/path/to/embedding.gguf)" >&2
  fi
  LCPP="$WORK_DIR/llama.cpp-py"   # convert_lora_to_gguf.py + gguf-py: lets the Studio export trained adapters for llama-server
  "$PY" -m studio ${LCPP:+--llama-cpp-dir "$LCPP"} --data-dir "$D/studio" --dsn "$PG_APP_DSN" --admin-dsn "$PG_ADMIN_DSN" --setup \
    --port "$STUDIO_PORT" --worker-url "http://127.0.0.1:8099" --adapters-dir "$D/adapters" --llm-url "http://127.0.0.1:18090" "${STUDIO_RAG[@]}" \
    ${TRAIN_PY:+--train-python "$TRAIN_PY"} --base-models "$BASE_MODELS" >"$D/logs/studio.log" 2>&1 & pids+=($!)
  GW_EXTRA+=(-agents-url "http://127.0.0.1:$DAG_PORT" -studio-url "http://127.0.0.1:$STUDIO_PORT")
fi

# 3. Site Gateway
"$D/bin/site-gateway" -grpc "127.0.0.1:$GRPC_PORT" -http "127.0.0.1:$HTTP_PORT" -tenants-file "$D/tenants.json" \
  -tls-cert "$D/pki/gateway.crt" -tls-key "$D/pki/gateway.key" -tls-client-ca "$D/pki/ca.crt" \
  "${GW_EXTRA[@]}" >"$D/logs/gateway.log" 2>&1 & pids+=($!)
sleep 1

# 4. Worker Guard + llama-server
"$D/bin/worker-guard" --llama-server "$LLAMA" --model "$MODEL" --parallel 2 --ctx 4096 --headroom-mib 512 \
  --engine-port 18090 --models-dir "$D/models" --adapters-dir "$D/adapters" --gateway "127.0.0.1:$GRPC_PORT" --worker-id pc-001 \
  --tls-ca "$D/pki/ca.crt" --tls-cert "$D/pki/pc-001.crt" --tls-key "$D/pki/pc-001.key" \
  --tls-server-name localhost >"$D/logs/worker.log" 2>&1 & pids+=($!)

cat <<MSG
AI Compute Fabric Full-Stack Demo is starting (model loads in ~10-25 s):
  1. Cụm GPU & Dự báo VRAM : http://127.0.0.1:$HTTP_PORT/            (admin token: $AICF_ADMIN_TOKEN)
  2. Người dùng & Khóa API : http://127.0.0.1:$HTTP_PORT/tenants.html
  3. Playground & Đo tải   : http://127.0.0.1:$HTTP_PORT/chat.html
  4. AI Agent (Đồ thị DAG) : http://127.0.0.1:$HTTP_PORT/agents.html
  5. AI Studio             : http://127.0.0.1:$HTTP_PORT/studio.html
  6. Hướng dẫn Sử dụng     : http://127.0.0.1:$HTTP_PORT/guide.html
  7. Worker Guard Local UI : http://127.0.0.1:8099/
  Logs                     : $D/logs/
Ctrl-C to stop.
MSG
wait
