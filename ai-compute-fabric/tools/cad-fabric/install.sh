#!/usr/bin/env bash
# Install cadfabric into its own virtualenv and run the doctor. Usage: ./install.sh [--prefix DIR] [--python python3.12]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/cadfabric"; PY=python3
while [ $# -gt 0 ]; do case "$1" in --prefix) PREFIX="$2"; shift 2;; --python) PY="$2"; shift 2;; *) echo "unknown option $1"; exit 2;; esac; done
"$PY" - <<'PYEOF' || { echo "Python 3.10 or newer is required."; exit 1; }
import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)
PYEOF
mkdir -p "$PREFIX"
"$PY" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$HERE"
mkdir -p "$HOME/.local/bin"; ln -sf "$PREFIX/venv/bin/cadfabric" "$HOME/.local/bin/cadfabric"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/cadfabric/config.json"
[ -f "$CFG" ] || "$PREFIX/venv/bin/cadfabric" init
echo; echo "Installed: $HOME/.local/bin/cadfabric   (make sure ~/.local/bin is on PATH)"
echo "Edit $CFG (url, model, key_file), then:"; "$PREFIX/venv/bin/cadfabric" doctor || echo "doctor reported problems: fix the config and re-run 'cadfabric doctor'"
