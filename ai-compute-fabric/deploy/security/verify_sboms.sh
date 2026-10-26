#!/usr/bin/env bash
# Verifies every SBOM under benchmarks/reports/security against deploy/security/cosign.pub. Non-zero if any is unsigned,
# altered, or signed with another key. Needs cosign (COSIGN=/path/to/cosign).
set -uo pipefail
cd "$(dirname "$0")/../.."
COSIGN=${COSIGN:-cosign}; rc=0; n=0
for f in benchmarks/reports/security/sbom_*.json; do
  n=$((n+1))
  if [ ! -f "$f.sig" ]; then echo "UNSIGNED $f"; rc=1; continue; fi
  "$COSIGN" verify-blob --key deploy/security/cosign.pub --signature "$f.sig" --new-bundle-format=false --insecure-ignore-tlog=true "$f" >/dev/null 2>&1 \
    && echo "ok       $f" || { echo "BAD      $f"; rc=1; }
done
[ "$n" -gt 0 ] || { echo "no SBOMs found"; rc=1; }
exit $rc
