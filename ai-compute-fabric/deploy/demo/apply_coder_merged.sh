#!/usr/bin/env bash
# Put Qwen3-Coder-30B-A3B on pc-001 (master) + pc-win-01 (RPC member) as one merged group via the gateway.
# Needs: Windows AICFWorkerGuard service with --headroom-mib 300 (1536 makes the guard kill rpc-server, measured 2026-10-28).
# tensor_split is primary-first: pc-001 48 / pc-win-01 52 (the 5070 holds 52% = best at 32k; > 53% collapses).
set -euo pipefail
GW="${GW:-http://127.0.0.1:18080}"; TOKEN="${AICF_ADMIN_TOKEN:-demo-admin-token}"
MODEL="${MODEL:-Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf}"; SPLIT="${SPLIT:-48,52}"
for _ in $(seq 1 60); do   # both workers online and running
  curl -sf -H "Authorization: Bearer $TOKEN" "$GW/api/cluster/topology" | python3 -c 'import json,sys
w=json.load(sys.stdin)["workers"]; sys.exit(0 if {"pc-001","pc-win-01"} <= set(w) and all(v["online"] and v["state"]=="running" for v in w.values()) else 1)' && break
  sleep 5
done
curl -sf -m 900 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$GW/api/cluster/topology" \
  -d "{\"mode\":\"merged\",\"model\":\"$MODEL\",\"primary_worker_id\":\"pc-001\",\"rpc_worker_ids\":[\"pc-win-01\"],\"tensor_split\":\"$SPLIT\",\"rpc_port\":50052}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("error") or ("ready", [(g["id"], g["model"], g["tensor_split"]) for g in d["groups"]]))'
