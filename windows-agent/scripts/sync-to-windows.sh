#!/usr/bin/env bash
# Dong bo windows-agent sang C:\wagent (host ssh: ${WIN_HOST:-fios02@10.42.0.225}). Neu ui/dist ton tai thi day thanh C:\wagent\ui_dist.
set -e
cd "$(dirname "$0")/.."
H="${WIN_HOST:-fios02@10.42.0.225}"
tar cf - src scripts pyproject.toml | ssh "$H" "cd /d C:\\wagent & tar xf -"
if [ -f ui/dist/index.html ]; then
  ssh "$H" "rmdir /s /q C:\\wagent\\ui_dist 2>nul & mkdir C:\\wagent\\ui_dist" >/dev/null
  (cd ui/dist && tar cf - .) | ssh "$H" "cd /d C:\\wagent\\ui_dist & tar xf -"
fi
