#!/usr/bin/env bash
# One-command local demo (Linux): gateway + one worker-guard, mTLS on the gRPC link,
# API-key auth on the inference port, admin token on the management API.
# Usage: deploy/demo/run_demo.sh /path/to/llama-server /path/to/model.gguf
# Stop with Ctrl-C. State lives in ./.demo (certs, tenants.json, logs).
set -euo pipefail
LLAMA="${1:?llama-server path}"; MODEL="${2:?model .gguf path}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; D="${DEMO_DIR:-$ROOT/.demo}"
mkdir -p "$D/bin" "$D/logs"
(cd "$ROOT/apps/site-gateway" && go build -o "$D/bin/site-gateway" . && go build -o "$D/bin/pki" ./cmd/pki)
(cd "$ROOT/apps/worker-guard" && go build -o "$D/bin/worker-guard" .)
[ -f "$D/pki/ca.crt" ] || "$D/bin/pki" -out "$D/pki" -workers pc-001
GRPC_PORT="${GRPC_PORT:-17443}"; HTTP_PORT="${HTTP_PORT:-18080}"
export AICF_ADMIN_TOKEN="${AICF_ADMIN_TOKEN:-demo-admin-token}"
LD="$(dirname "$LLAMA")"; export LD_LIBRARY_PATH="$LD:${LD_LIBRARY_PATH:-}"
pids=(); trap 'kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null' EXIT INT TERM
"$D/bin/site-gateway" -grpc 127.0.0.1:$GRPC_PORT -http 127.0.0.1:$HTTP_PORT -tenants-file "$D/tenants.json" \
  -tls-cert "$D/pki/gateway.crt" -tls-key "$D/pki/gateway.key" -tls-client-ca "$D/pki/ca.crt" >"$D/logs/gateway.log" 2>&1 & pids+=($!)
sleep 1
"$D/bin/worker-guard" --llama-server "$LLAMA" --model "$MODEL" --parallel 2 --ctx 4096 --headroom-mib 512 \
  --gateway 127.0.0.1:$GRPC_PORT --worker-id pc-001 --tls-ca "$D/pki/ca.crt" --tls-cert "$D/pki/pc-001.crt" \
  --tls-key "$D/pki/pc-001.key" --tls-server-name localhost >"$D/logs/worker.log" 2>&1 & pids+=($!)
cat <<MSG
Demo is starting (the model takes ~10-30 s to load).
  Fleet page   : http://127.0.0.1:$HTTP_PORT/            (admin token: $AICF_ADMIN_TOKEN)
  Tenants page : http://127.0.0.1:$HTTP_PORT/tenants.html
  Chat page    : http://127.0.0.1:$HTTP_PORT/chat.html
  Worker page  : http://127.0.0.1:8099/  (worker-guard, cổng cố định)
  Logs         : $D/logs/
See docs/TESTING.md for the test steps. Ctrl-C to stop.
MSG
wait
