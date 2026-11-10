#!/usr/bin/env bash
# Boot helper: wait for the gateway and both workers; if the merged group is not already running the production model,
# apply it (same call as deploy/demo/apply_coder_merged.sh, with the temporary 192.168.1.40:8099 relay the gateway needs).
set -u
ROOT=/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric; GW=http://127.0.0.1:18080; TOKEN="${AICF_ADMIN_TOKEN:-demo-admin-token}"
MODEL="${MODEL:-Qwen3.8-27B-UD-Q4_K_M.gguf}"; KV="${KV:-q8_0}"; SPLIT="${SPLIT:-48,52}"
state() { curl -sf -m 5 -H "Authorization: Bearer $TOKEN" "$GW/api/workers" | python3 -c 'import json,sys
w={x["worker_id"]:x for x in json.load(sys.stdin)}
ok=all(i in w and w[i]["online"] and w[i]["state"]=="running" for i in ("pc-001","pc-win-01")) and w["pc-001"]["model"]=="'"$MODEL"'"
sys.exit(0 if ok else 1)' 2>/dev/null; }
for _ in $(seq 1 90); do   # up to 7.5 min for the guard to restore its own engine
  state && { echo "merged group already serving $MODEL"; exit 0; }
  sleep 5
done
echo "not serving $MODEL after 7.5 min: applying topology"
socat TCP4-LISTEN:8099,bind=192.168.1.40,reuseaddr,fork,range=192.168.1.40/32 TCP4:127.0.0.1:8099 & SP=$!
trap 'kill $SP 2>/dev/null' EXIT
sleep 1
cd "$ROOT" && MODEL="$MODEL" SPLIT="$SPLIT" KV="$KV" timeout 900 deploy/demo/apply_coder_merged.sh
