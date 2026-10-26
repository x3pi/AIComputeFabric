#!/usr/bin/env bash
# Choose the model a demo worker serves, and (re)start that worker.
#   deploy/demo/worker.sh list                      models available + estimated VRAM (needs the demo gateway + sidecar running)
#   deploy/demo/worker.sh status                    worker state as the gateway sees it
#   deploy/demo/worker.sh start <name|file.gguf>    stop the current worker, start one serving that model
#   deploy/demo/worker.sh stop
# Options for start: --ctx N (per slot, default 4096)  --parallel N (default 2)  --worker-id ID (default pc-001)
#   --lora JOB_ID|file.gguf   serve a LoRA adapter trained in the AI Studio on top of the model (JOB_ID is downloaded as GGUF from the Studio)
#   --tenant NAME             tenant of that job (default demo)
#   --force                   skip the check that the model matches the adapter's base model
# Needs deploy/demo/run_demo.sh to be running (gateway, certificates, sidecar). The Studio's RAG/prompt box uses this worker's engine (port 18090).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK_DIR="$(cd "$ROOT/.." && pwd)/.aicf-work"
D="${DEMO_DIR:-$ROOT/.demo}"
LLAMA="${LLAMA:-$WORK_DIR/bin/llama-server}"
GW="http://127.0.0.1:${HTTP_PORT:-18080}"; TOKEN="${AICF_ADMIN_TOKEN:-demo-admin-token}"; GRPC_PORT="${GRPC_PORT:-17443}"
cmd="${1:-}"; shift || true
usable() { ls "$WORK_DIR"/*.gguf 2>/dev/null | grep -vi -e embedding -e reranker; }
worker_pids() { ps -eo pid,args | awk -v b="$D/bin/worker-guard" '$2==b {print $1}'; }
stop_all() {   # worker-guard first (it stops its engine), then make sure nothing still holds the engine port
  for p in $(worker_pids); do kill "$p"; done
  for _ in $(seq 1 20); do [ -z "$(worker_pids)" ] && [ -z "$(engine_pids)" ] && return 0; sleep 1; done
  for p in $(worker_pids) $(engine_pids); do kill -9 "$p" 2>/dev/null || true; done; sleep 1
}
engine_pids() { ps -eo pid,args | awk -v m="$LLAMA" '$2==m && / --port 18090 / {print $1}'; }
api() { curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$@"; }

case "$cmd" in
list)
  printf "%-34s %8s  %s\n" MODEL SIZE "ESTIMATED VRAM (ctx 4096 x 2 slots, ESTIMATE not measured)"
  for f in $(usable); do
    n="$(basename "$f")"; sz="$(du -h --apparent-size "$f" | cut -f1)"
    est="$(api -X POST "$GW/api/v1/models/estimate-vram" -H 'Content-Type: application/json' -d "{\"gguf\":\"$n\",\"ctx_per_slot\":4096,\"parallel\":2,\"flash_attn\":true,\"gpu_total_mib\":12288}" \
      | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print("%5d MiB  %-7s %s" % (d["required_per_gpu_bytes"]/2**20, d["fit_status"], d["execution_mode"]) if "required_per_gpu_bytes" in d else (d.get("error") or str(d)[:70]))
except Exception as e: print("n/a (%s)" % e)')"
    printf "%-34s %8s  %s\n" "$n" "$sz" "$est"
  done ;;
status)
  echo "worker-guard (local, live): $(curl -s --max-time 3 http://127.0.0.1:8099/api/state | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["state"]+", engine "+str(d["engine_mib"])+" MiB")' 2>/dev/null || echo not running)"
  echo "gateway view (updates every few seconds):"
  api "$GW/api/workers" | python3 -c 'import json,sys
for w in json.load(sys.stdin): print(w["worker_id"], "state="+w["state"], "online=%s"%w["online"], "model="+w["model"], "free_vram_MiB=%d" % (w["vram_free_bytes"]/2**20))' ;;
stop)
  stop_all; echo "worker stopped" ;;
start)
  [ $# -ge 1 ] || { echo "usage: worker.sh start <model name or .gguf path> [--ctx N] [--parallel N] [--worker-id ID]"; exit 2; }
  m="$1"; want="$1"; shift; ctx=4096; par=2; wid=pc-001; lora=""; tenant=demo; force=0
  while [ $# -gt 0 ]; do case "$1" in --ctx) ctx="$2";; --parallel) par="$2";; --worker-id) wid="$2";; --lora) lora="$2";; --tenant) tenant="$2";;
    --force) force=1; shift; continue;; *) echo "unknown option $1"; exit 2;; esac; shift 2; done
  [ -f "$m" ] || m="$WORK_DIR/$m"; [ -f "$m" ] || m="$(ls "$WORK_DIR"/*"$want"*.gguf 2>/dev/null | head -1)"
  [ -f "$m" ] || { echo "model not found; run: worker.sh list"; exit 1; }
  lora_arg=()
  if [ -n "$lora" ]; then
    if [[ "$lora" =~ ^[0-9a-f]{32}$ ]]; then          # a Studio job: check it succeeded and which base model it was trained from, then fetch the GGUF
      job="$(api "$GW/api/studio/v1/tenants/$tenant/lora/jobs/$lora")"
      base="$(printf '%s' "$job" | python3 -c 'import json,sys;j=json.load(sys.stdin);print(j["params"]["base_model"] if j.get("status")=="succeeded" else "")' 2>/dev/null || true)"
      [ -n "$base" ] || { echo "job $lora (tenant $tenant) not found or not succeeded"; exit 1; }
      if [ "$force" = 0 ] && ! basename "$m" | tr 'A-Z' 'a-z' | grep -qF "$base"; then
        echo "the adapter was trained from '$base' but you chose $(basename "$m"). An adapter only works on the SAME base model; pick a $base GGUF (or --force)."; exit 1
      fi
      mkdir -p "$D/adapters"; f="$D/adapters/$lora.gguf"
      api -o "$f" "$GW/api/studio/v1/tenants/$tenant/lora/jobs/$lora/adapter?format=gguf"
      [ "$(head -c4 "$f")" = "GGUF" ] || { echo "could not export the adapter as GGUF: $(head -c 300 "$f")"; rm -f "$f"; exit 1; }
      lora="$f"
    fi
    [ -f "$lora" ] || { echo "adapter file not found: $lora"; exit 1; }
    lora_arg=(--lora "$lora")
  fi
  [ -x "$D/bin/worker-guard" ] && [ -f "$D/pki/$wid.crt" ] || { echo "demo not built or no certificate for $wid under $D/pki (start deploy/demo/run_demo.sh first)"; exit 1; }
  stop_all
  export LD_LIBRARY_PATH="$(dirname "$LLAMA"):${LD_LIBRARY_PATH:-}"
  setsid nohup "$D/bin/worker-guard" --llama-server "$LLAMA" --model "$m" --parallel "$par" --ctx "$ctx" --headroom-mib 512 --engine-port 18090 \
    --gateway "127.0.0.1:$GRPC_PORT" --worker-id "$wid" --tls-ca "$D/pki/ca.crt" --tls-cert "$D/pki/$wid.crt" --tls-key "$D/pki/$wid.key" --tls-server-name localhost "${lora_arg[@]}" \
    >"$D/logs/worker.log" 2>&1 < /dev/null &
  echo "starting $(basename "$m")${lora_arg:+ + LoRA adapter $(basename "$lora")} as $wid (ctx $ctx x $par slots); waiting for the engine..."
  for _ in $(seq 1 90); do
    curl -s --max-time 2 http://127.0.0.1:18090/health | grep -q '"ok"' && {
      echo "ready: engine on :18090"
      for _ in $(seq 1 20); do api "$GW/api/workers" | grep -q "\"worker_id\":\"$wid\"[^}]*\"state\":\"running\"" && break; sleep 1; done
      "$0" status; exit 0; }
    sleep 1
  done
  echo "not ready after 90 s; see $D/logs/worker.log (not enough free VRAM? the worker yields the GPU to other programs)"; exit 1 ;;
*) sed -n 2,9p "$0"; exit 2 ;;
esac
