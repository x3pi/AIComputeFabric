#!/usr/bin/env bash
# usage: run_big_chain.sh  -- 64k q8_0 50:50 then 96k q8_0 47:53 (Qwen3-30B-A3B), log vao /tmp/claude-1000/cmp_out/big_<ctx>_q8.log
cd "$(dirname "$0")"
LOG=/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/.demo/logs/engine-pc001.log
for cfg in "65536 50,50 60000" "98304 47,53 92000"; do
  set -- $cfg
  { ./swap_big.sh Qwen3-30B-A3B-Q4_K_M.gguf $1 q8_0 "$2" 2>&1 | grep -v "^\s\|Traceback\|JSONDecode"
    grep -E "type_k|type_v|KV buffer" $LOG | tail -3 | cut -c1-160
    python3 bigctx_measure.py $3 2>&1 | tail -3
    echo "VRAM win: $(ssh fios02@10.42.0.225 'nvidia-smi --query-gpu=memory.used --format=csv,noheader')  linux: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
  } > /tmp/claude-1000/cmp_out/big_$1_q8.log 2>&1
done
echo CHAIN_DONE > /tmp/claude-1000/cmp_out/big_chain.done
