#!/usr/bin/env bash
# Remove this CadFabric install (the folder it lives in, its launcher and menu entry). Settings, history and exported STEP files are kept unless --purge.
set -euo pipefail
PREFIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
[ -x "$PREFIX/venv/bin/python" ] || { echo "not a CadFabric install folder: $PREFIX" >&2; exit 1; }
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
for f in "$APPS"/cadfabric-*.desktop; do [ -f "$f" ] && grep -qx "X-CadFabric-Prefix=$PREFIX" "$f" && rm -f "$f" && echo "removed $f"; done
L="$HOME/.local/bin/cadfabric"; [ -L "$L" ] && [ "$(readlink "$L")" = "$PREFIX/venv/bin/cadfabric" ] && rm -f "$L" && echo "removed $L"
if [ $PURGE -eq 1 ]; then rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/cadfabric" "${XDG_DATA_HOME:-$HOME/.local/share}/cadfabric"; echo "removed settings, history and exports"; fi
cd /; rm -rf "$PREFIX"; echo "removed $PREFIX"
