#!/usr/bin/env bash
# Build the offline Ubuntu bundle: dist/cadfabric-linux-<version>.tar.gz  (x86_64; wheels for Python 3.10 = Ubuntu 22.04 and 3.12 = Ubuntu 24.04)
# Env: WHEELHOUSE=<dir with cp310/ and cp312/ subfolders> to reuse downloads (~250 MB each)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
WORK="$(mktemp -d)"; OUT="$ROOT/dist"; B="$WORK/cadfabric-linux-$VERSION"; mkdir -p "$B/wheelhouse" "$OUT"
PYTHON="${PYTHON:-python3}"
PL=(); for g in $(seq 17 39); do PL+=(--platform "manylinux_2_${g}_x86_64"); done; PL+=(--platform manylinux2014_x86_64 --platform linux_x86_64)   # glibc 2.17 ... 2.39 (Ubuntu 24.04)
for v in 310 312; do
  D="$WORK/cp$v"; mkdir -p "$D"
  if [ -d "${WHEELHOUSE:-/nonexistent}/cp$v" ]; then cp "$WHEELHOUSE/cp$v"/*.whl "$D/"; rm -f "$D"/cadfabric-*.whl; fi
  "$PYTHON" -m pip wheel --quiet --no-deps -w "$D" "$ROOT"
  "$PYTHON" -m pip wheel --quiet --no-deps -w "$D" proxy_tools bottle      # source-only on PyPI, pure Python: built here so the resolver can use them
  "$PYTHON" -m pip download --quiet --only-binary=:all: "${PL[@]}" --python-version "${v:0:1}.${v:1}" --implementation cp --abi "cp$v" -d "$D" --find-links "$D" "cadfabric[desktop]"
  cp -n "$D"/*.whl "$B/wheelhouse/" 2>/dev/null || true
done
cp "$HERE/linux/install.sh" "$HERE/linux/uninstall.sh" "$B/"; chmod +x "$B/install.sh" "$B/uninstall.sh"
cp "$ROOT/docs/HUONG-DAN-CAI-DAT-UBUNTU.md" "$B/HUONG-DAN-CAI-DAT-UBUNTU.md"
( cd "$B" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
tar -C "$WORK" -czf "$OUT/cadfabric-linux-$VERSION.tar.gz" "cadfabric-linux-$VERSION"
ls -la "$OUT/cadfabric-linux-$VERSION.tar.gz"; rm -rf "$WORK"
