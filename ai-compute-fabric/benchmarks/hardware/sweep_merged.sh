#!/usr/bin/env bash
# Sweep of merged (RPC) options. Usage: sweep_merged.sh <ctx> <out.jsonl> <tag=extra args>...   (one probe run per tag so one crash does not stop the rest)
set -u
cd "$(dirname "$0")"
Wd="$(realpath ../../../.aicf-work)"; export LD_LIBRARY_PATH="$Wd/bin"
ctx="$1"; out="$2"; shift 2
for cfg in "$@"; do
  timeout 600 python3 probe_server_cfg.py --server "$Wd/bin/llama-server" --model "${MODEL:-$Wd/Qwen3-30B-A3B-Q4_K_M.gguf}" --ctx "$ctx" --reps 5 --start-timeout 300 \
    --configs "$cfg" --out "$out" 2>&1 | tail -1 | cut -c1-200
done
