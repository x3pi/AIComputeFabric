#!/usr/bin/env bash
# Dong bo windows-agent sang C:\wagent\src tren Windows (host ssh: ${WIN_HOST:-fios02@10.42.0.225})
set -e
cd "$(dirname "$0")/.."
tar cf - src scripts pyproject.toml | ssh "${WIN_HOST:-fios02@10.42.0.225}" "cd /d C:\\wagent & tar xf -"
