#!/usr/bin/env bash
# Build the offline Windows bundle: dist/cadfabric-windows-<version>.zip  (run on Linux/macOS; needs python3 with pip, curl, zip)
# Contents: install.cmd, install.ps1, uninstall.ps1, python-3.12.x-embed-amd64.zip, vc_redist.x64.exe, wheelhouse/ (cp312 win_amd64 wheels + cadfabric), SHA256SUMS
# Env: WHEELHOUSE=<dir>  reuse an already downloaded wheelhouse (saves ~300 MB of download)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
PY_VER="${PY_VER:-3.12.10}"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
WORK="$(mktemp -d)"; OUT="$ROOT/dist"; B="$WORK/cadfabric-windows-$VERSION"; mkdir -p "$B/wheelhouse" "$OUT"
PIP="${PYTHON:-python3} -m pip"

echo "== cadfabric wheel"
$PIP wheel --no-deps --quiet -w "$B/wheelhouse" "$ROOT"

echo "== dependencies for Windows x64 / Python 3.12"
if [ -n "${WHEELHOUSE:-}" ]; then cp -n "$WHEELHOUSE"/*.whl "$B/wheelhouse/"; fi
$PIP download --quiet --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 -d "$B/wheelhouse" --find-links "$B/wheelhouse" cadfabric pip

echo "== desktop window (pywebview + its Windows dependencies; markers are evaluated on THIS host, so the list is explicit)"
$PIP download --quiet --only-binary=:all: --no-deps --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 -d "$B/wheelhouse" \
  pywebview pythonnet clr_loader cffi pycparser typing_extensions
$PIP wheel --quiet --no-deps -w "$B/wheelhouse" proxy_tools bottle          # source-only on PyPI, pure Python: built here into py3-none-any wheels

echo "== embeddable Python $PY_VER and the Microsoft runtime"
curl -fsSL -o "$B/python-$PY_VER-embed-amd64.zip" "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"
if [ -f "${VC_REDIST:-}" ]; then cp "$VC_REDIST" "$B/vc_redist.x64.exe"; else curl -fsSL -o "$B/vc_redist.x64.exe" https://aka.ms/vs/17/release/vc_redist.x64.exe; fi

cp "$ROOT/assets/cadfabric.ico" "$B/cadfabric.ico"; cp "$HERE/windows/install.cmd" "$HERE/windows/install.ps1" "$HERE/windows/uninstall.ps1" "$B/"
cp "$HERE/windows/README.txt" "$B/README.txt"
cp "$ROOT/docs/HUONG-DAN-CAI-DAT-WINDOWS.md" "$B/HUONG-DAN-CAI-DAT-WINDOWS.md"
( cd "$B" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
( cd "$WORK" && zip -qr -0 "$OUT/cadfabric-windows-$VERSION.zip" "cadfabric-windows-$VERSION" )
ls -la "$OUT/cadfabric-windows-$VERSION.zip"; rm -rf "$WORK"
