#!/usr/bin/env bash
cd "$(dirname "$0")"
{ WRAP=1 YARN_SCALE=2 ./swap_big.sh Qwen3-30B-A3B-Q4_K_M.gguf 65536 q8_0 "52,48" 2>&1 | grep -v "^\s\|Traceback\|JSONDecode"
  echo "VRAM truoc do: win $(ssh fios02@10.42.0.225 'nvidia-smi --query-gpu=memory.used --format=csv,noheader') linux $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
  python3 bigctx_ladder.py 16000 40000 60000
} > /tmp/claude-1000/cmp_out/ladder_64k.log 2>&1
echo done > /tmp/claude-1000/cmp_out/ladder_64k.done
