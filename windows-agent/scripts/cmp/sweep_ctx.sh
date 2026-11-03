#!/usr/bin/env bash
# Quet ctx cho Qwen3-30B-A3B gop 2 card: moi cau hinh = swap + decode prompt ngan + prompt day ~25/60/90%
cd "$(dirname "$0")"; OUT=/tmp/claude-1000/cmp_out/sweep; mkdir -p $OUT; rm -f $OUT/all.done
# ten ctx kv wrap yarn split fills...
while read -r name ctx kv wrap yarn split f1 f2 f3; do
  [ -z "$name" ] && continue
  { echo "=== $name ctx=$ctx kv=$kv wrap=$wrap yarn=$yarn split=$split"
    WRAP=$wrap YARN_SCALE=$yarn ./swap_big.sh Qwen3-30B-A3B-Q4_K_M.gguf $ctx $kv "$split" 2>&1 </dev/null | grep -E "ready|n_ctx|error|Error"
    got=$(curl -s -m 5 http://10.42.0.1:18090/props | python3 -c "import sys,json;print(json.load(sys.stdin)['default_generation_settings']['n_ctx'])" 2>/dev/null)
    if [ "$got" != "$ctx" ]; then echo "BO QUA: n_ctx thuc=$got khac $ctx"; continue; fi
    python3 bigctx_ladder.py 0 $f1 $f2 $f3 </dev/null
  } > $OUT/$name.log 2>&1
done <<'CFG'
c48q8 49152 q8_0 1 2 52,48 12000 29000 44000
c64q8 65536 q8_0 1 2 52,48 16000 40000 58000
c80q8 81920 q8_0 1 2 50,50 20000 49000 73000
c96q8 98304 q8_0 1 4 50,50 24000 59000 88000
CFG
touch $OUT/all.done
