#!/usr/bin/env bash
# CadFabric installer for Ubuntu / Debian (x86_64), current user, no root needed.
# Offline: the wheelhouse next to this script holds CadQuery and every dependency for Python 3.10 (Ubuntu 22.04) and 3.12 (Ubuntu 24.04).
# It uses the system python3 to create a private virtual environment under --prefix; nothing else on the system is touched.
#
#   ./install.sh [--prefix DIR] [--url URL] [--model NAME] [--planner off|nothink|think|codex|codex-relay] [--key-file FILE]
#                [--advisor-cli claude|codex] [--no-shortcuts] [--online]
#   --key-file   file holding the API key (read, then stored in the user's config folder; never put on a command line)
#   --online     allow pip to download from the internet when no bundled wheel fits this Python (e.g. Python 3.11)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/cadfabric-app"; URL=""; MODEL=""; PLANNER=""; KEYFILE=""; ADV=""; SHORTCUTS=1; ONLINE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;; --url) URL="$2"; shift 2;; --model) MODEL="$2"; shift 2;; --planner) PLANNER="$2"; shift 2;;
    --key-file) KEYFILE="$2"; shift 2;; --advisor-cli) ADV="$2"; shift 2;; --no-shortcuts) SHORTCUTS=0; shift;; --online) ONLINE=1; shift;;
    -h|--help) sed -n 2,11p "$0"; exit 0;; *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
step() { printf '\n== %s\n' "$1"; }
die() { echo "error: $1" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (the app is installed for one user)"
[ "$(uname -m)" = "x86_64" ] || die "this bundle is for x86_64 (CadQuery wheels are bundled for that CPU only)"
case "$PREFIX" in /*) ;; *) die "--prefix must be an absolute path";; esac

step "Python"
command -v python3 >/dev/null || die "python3 not found: sudo apt install python3 python3-venv"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python $PYV is too old (need 3.10 or newer)"
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing: sudo apt install python3-venv"
echo "python3 $PYV"
BUNDLED=0; case "$PYV" in 3.10|3.12) BUNDLED=1;; esac
if [ $BUNDLED -eq 0 ] && [ $ONLINE -eq 0 ]; then die "this bundle carries wheels for Python 3.10 and 3.12 only (you have $PYV). Re-run with --online to let pip download them."; fi

step "Stopping a running CadFabric (so the upgrade can replace its files)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/cadfabric"
if [ -f "$DATA/app.json" ]; then
  PID="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('pid',''))" "$DATA/app.json" 2>/dev/null || true)"
  if [ -n "$PID" ] && [ -r "/proc/$PID/cmdline" ] && tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q cadfabric; then kill "$PID" 2>/dev/null || true; sleep 2; echo "stopped pid $PID"; fi
fi

step "Virtual environment in $PREFIX"
mkdir -p "$PREFIX"
# --system-site-packages: lets the app window use the system GTK / WebKit (python3-gi); pip still installs our own versions of everything else into the venv
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv --system-site-packages "$PREFIX/venv"
PYBIN="$PREFIX/venv/bin/python"

step "Installing cadfabric and CadQuery"
PIPARGS=(--find-links "$HERE/wheelhouse" --upgrade --disable-pip-version-check)
[ $ONLINE -eq 1 ] || PIPARGS+=(--no-index)
"$PYBIN" -m pip install "${PIPARGS[@]}" "cadfabric[desktop]" 2>&1 | tail -3
"$PYBIN" -c "import cadfabric; print('cadfabric', cadfabric.__version__)"

step "Launchers"
mkdir -p "$HOME/.local/bin"
ln -sf "$PREFIX/venv/bin/cadfabric" "$HOME/.local/bin/cadfabric"
cp "$HERE/uninstall.sh" "$PREFIX/uninstall.sh"; chmod +x "$PREFIX/uninstall.sh"
if [ -f "$HERE/cadfabric.svg" ]; then cp "$HERE/cadfabric.svg" "$PREFIX/cadfabric.svg"; else
cat > "$PREFIX/cadfabric.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="CadFabric"><title>CadFabric</title><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#d97757"/><stop offset="1" stop-color="#b9532f"/></linearGradient></defs><rect width="64" height="64" rx="14" fill="url(#g)"/><path d="M12.08 20.50 L32.00 32.00 L32.00 55.00 L12.08 43.50 Z" fill="rgba(255,255,255,0.74)"/><path d="M32.00 32.00 L51.92 20.50 L51.92 43.50 L32.00 55.00 Z" fill="rgba(255,255,255,0.5)"/><path d="M32.00 9.00 L51.92 20.50 L32.00 32.00 L12.08 20.50 Z" fill="#ffffff"/><ellipse cx="32.00" cy="20.50" rx="10.12" ry="5.84" fill="#8f3b21"/></svg>
SVG
fi
if [ $SHORTCUTS -eq 1 ]; then
  APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"; mkdir -p "$APPS"
  NAME="CadFabric"; [ "$PREFIX" = "${XDG_DATA_HOME:-$HOME/.local/share}/cadfabric-app" ] || NAME="CadFabric - $(basename "$PREFIX")"
  DF="$APPS/cadfabric-$(printf '%s' "$PREFIX" | cksum | cut -d' ' -f1).desktop"
  printf '[Desktop Entry]\nType=Application\nName=%s\nComment=CAD design chat (local Qwen + CadQuery)\nExec=%s app\nIcon=%s\nTerminal=false\nCategories=Graphics;Engineering;\nX-CadFabric-Prefix=%s\n' "$NAME" "$PREFIX/venv/bin/cadfabric" "$PREFIX/cadfabric.svg" "$PREFIX" > "$DF"
  echo "launcher: $DF  (search 'CadFabric' in the applications menu)"
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
fi

if [ -n "$URL$MODEL$PLANNER$ADV$KEYFILE" ]; then
  step "Configuring"
  A=(configure); [ -z "$URL" ] || A+=(--url "$URL"); [ -z "$MODEL" ] || A+=(--model "$MODEL"); [ -z "$PLANNER" ] || A+=(--planner "$PLANNER"); [ -z "$ADV" ] || A+=(--advisor-cli "$ADV")
  if [ -n "$KEYFILE" ]; then [ -r "$KEYFILE" ] || die "cannot read --key-file"; A+=(--key-stdin); tr -d '\r\n' < "$KEYFILE" | "$PYBIN" -m cadfabric "${A[@]}"; else "$PYBIN" -m cadfabric "${A[@]}"; fi
fi

step "App window (optional)"
if "$PYBIN" -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('WebKit2','4.1')" 2>/dev/null; then echo "GTK + WebKit found: CadFabric opens in its own window"
else echo "No GTK/WebKit for Python: CadFabric will open in your web browser instead. For its own window run: sudo apt install python3-gi gir1.2-webkit2-4.1"; fi

step "Self-check (CadQuery kernel + sandbox)"
"$PYBIN" -m cadfabric doctor || true
printf '\nInstalled. Start CadFabric from the applications menu, or run: %s app\n' "$HOME/.local/bin/cadfabric"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: $HOME/.local/bin is not on your PATH yet (log out and in, or use the full path)";; esac
echo "Uninstall: $PREFIX/uninstall.sh"
