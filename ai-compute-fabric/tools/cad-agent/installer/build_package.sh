#!/usr/bin/env bash
# Builds dist/aicf-cad-agent-<version>.zip (+ .sha256) from the files in this folder. Fails if tests do not pass.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:-$(date +%Y.%m.%d)}"
python3 -m py_compile aicfconf.py sw_agent.py job_runner.py advisor.py doctor.py
python3 test_aicfconf.py >/dev/null 2>&1 || { echo "test_aicfconf failed"; exit 1; }
python3 canary_test.py >/dev/null 2>&1 || { echo "canary_test failed (data-leak filter regression)"; exit 1; }
# secrets must never ship
for f in key.txt api.key config.json audit.jsonl advisor_audit.jsonl state.json; do [ -e "$f" ] && echo "note: $f present locally, NOT packaged"; done
OUT="dist/aicf-cad-agent-$VERSION"; rm -rf "$OUT" "$OUT.zip"; mkdir -p "$OUT/installer" "$OUT/jobs"
cp aicfconf.py sw_agent.py job_runner.py advisor.py doctor.py canary_test.py test_aicfconf.py "$OUT/"
cp installer/install.ps1 installer/uninstall.ps1 installer/README.txt "$OUT/installer/"
cp jobs/*.json "$OUT/jobs/"
echo "$VERSION" > "$OUT/VERSION"
(cd "$OUT" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256)
(cd dist && zip -qr "aicf-cad-agent-$VERSION.zip" "aicf-cad-agent-$VERSION")
(cd dist && sha256sum "aicf-cad-agent-$VERSION.zip" | tee "aicf-cad-agent-$VERSION.zip.sha256")
grep -rIl "key\|secret" "$OUT" >/dev/null 2>&1 || true
if grep -rIE "[A-Za-z0-9]{32,}" "$OUT" --include=*.json --include=*.ps1 --include=*.txt >/dev/null 2>&1; then echo "WARNING: long token-like string in package, review"; fi
echo "built dist/aicf-cad-agent-$VERSION.zip"
