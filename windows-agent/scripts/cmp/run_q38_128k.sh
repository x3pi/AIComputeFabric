#!/usr/bin/env bash
cd "$(dirname "$0")"
{ ./swap_big.sh Qwen3.8-27B-UD-Q4_K_M.gguf 131072 q8_0 "48,52" </dev/null 2>&1 | grep -E "ready|n_ctx|rror"
  LADDER_TIMEOUT=600 python3 bigctx_ladder.py 0 30000 70000 120000 </dev/null 2>&1
} > /tmp/claude-1000/cmp_out/q38_27b_128k.log 2>&1
echo done > /tmp/claude-1000/cmp_out/q38_27b_128k.done
