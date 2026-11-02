#!/usr/bin/env bash
# usage: swap_ctx.sh <ctx> <parallel>   -- thay worker-guard voi ctx/parallel moi (giu nguyen moi co khac), roi gop lai Qwen3-Coder 2 GPU qua gateway
set -u
CTX="$1"; PAR="$2"
cd /mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric
ORIG=$(cat /tmp/claude-1000/guard.orig.cmd)
NEW=$(echo "$ORIG" | sed -E "s/--parallel [0-9]+ --ctx [0-9]+/--parallel $PAR --ctx $CTX/")
echo "guard cmd: $(echo "$NEW" | grep -o -- '--parallel [0-9]* --ctx [0-9]*')"
for p in $(pgrep -x worker-guard); do kill "$p"; done
for i in $(seq 1 30); do pgrep -x worker-guard >/dev/null || break; sleep 1; done
for p in $(pgrep -x llama-server); do kill "$p" 2>/dev/null; done; sleep 3
for p in $(pgrep -x llama-server); do kill -9 "$p" 2>/dev/null; done
# NOTE: dung setsid de guard song sau khi phien shell ket thuc
setsid nohup $NEW > .demo/logs/worker-guard-cmp.log 2>&1 < /dev/null &
for i in $(seq 1 90); do curl -sf -m 3 http://127.0.0.1:8099/api/state | grep -q '"state":"running"' && break; sleep 3; done
curl -s -m 5 http://127.0.0.1:8099/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('guard',d['state'],d['model'])"
socat TCP4-LISTEN:8099,bind=192.168.1.40,reuseaddr,fork,range=192.168.1.40/32 TCP4:127.0.0.1:8099 &
SP=$!; sleep 1
timeout 900 deploy/demo/apply_coder_merged.sh 2>&1 | tail -2
kill $SP 2>/dev/null
curl -s -m 5 http://10.42.0.1:18090/props | python3 -c "import sys,json;d=json.load(sys.stdin);print('n_ctx',d['default_generation_settings']['n_ctx'],'slots',d['total_slots'])"
