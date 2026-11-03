#!/usr/bin/env bash
cd "$(dirname "$0")"
LOG=/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/.demo/logs/engine-pc001.log
{ WRAP=1 ./swap_big.sh Qwen3-30B-A3B-Q4_K_M.gguf 98304 q8_0 "47,53" 2>&1 | grep -v "^\s\|Traceback\|JSONDecode"
  grep -E "n_ctx_slot|n_ctx_seq|rope|yarn|KV buffer|exceeds|error" $LOG | tail -8 | cut -c1-170
  python3 bigctx_measure.py 92000 2>&1 | tail -3
  echo "VRAM win: $(ssh fios02@10.42.0.225 'nvidia-smi --query-gpu=memory.used --format=csv,noheader')  linux: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
} > /tmp/claude-1000/cmp_out/big_96k_yarn.log 2>&1
echo done > /tmp/claude-1000/cmp_out/big_96k_yarn.done
