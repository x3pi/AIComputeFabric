#!/usr/bin/env bash
# Dependency scan + SBOM (plan P8.2). Needs govulncheck, cyclonedx-gomod (go install) and
# pip-audit, cyclonedx-py (pip install). Set PYBIN to the venv python's bin dir and GOBIN_TOOLS to the go tools dir.
# Exit status is non-zero if any scan finds a vulnerability that the code can reach.
# Also runs trivy (filesystem: vulnerabilities in lock files, secrets, misconfiguration) and signs the SBOMs with cosign
# (TRIVY, COSIGN and COSIGN_KEY point at the binaries/key; signing is skipped, loudly, when no key is given).
# Not covered: container images (none are built yet), license review, a trusted signing identity (the key used so far is a
# development key generated on the build machine, not an organisational root), a transparency log.
set -uo pipefail
cd "$(dirname "$0")/../.."
OUT=${OUT:-benchmarks/reports/security}; mkdir -p "$OUT"
PYBIN=${PYBIN:-}; GOT=${GOBIN_TOOLS:-$(go env GOPATH)/bin}
rc=0
for m in apps/site-gateway apps/worker-guard core/vram-estimator apps/global-control-plane; do
  n=$(basename "$m")
  echo "== govulncheck $m"
  (cd "$m" && "$GOT/govulncheck" ./...) > "$OUT/govulncheck_$n.txt" 2>&1 || rc=1
  tail -3 "$OUT/govulncheck_$n.txt"
  (cd "$m" && GOWORK=off "$GOT/cyclonedx-gomod" mod -json -licenses -output "../../$OUT/sbom_go_$n.cdx.json" 2>/dev/null) || echo "SBOM failed for $m"
done
echo "== pip-audit"
"${PYBIN}pip-audit" -f json -o "$OUT/pip_audit.json" >/dev/null 2>&1 || true
"${PYBIN}pip-audit" 2>&1 | tail -5 | tee "$OUT/pip_audit.txt"
"${PYBIN}cyclonedx-py" environment --of JSON -o "$OUT/sbom_python_env.cdx.json" 2>/dev/null
TRIVY=${TRIVY:-trivy}
if command -v "$TRIVY" >/dev/null 2>&1 || [ -x "$TRIVY" ]; then
  echo "== trivy fs (tracked source only: .demo, .venv, third_party skipped)"
  "$TRIVY" fs --scanners vuln,secret,misconfig --skip-dirs .demo --skip-dirs .venv --skip-dirs third_party --skip-dirs node_modules \
    --exit-code 1 --severity HIGH,CRITICAL --format table -o "$OUT/trivy_fs.txt" . >/dev/null 2>&1 || { echo "trivy found HIGH/CRITICAL issues, see $OUT/trivy_fs.txt"; rc=1; }
  tail -3 "$OUT/trivy_fs.txt"
else
  echo "trivy not found: skipped"
fi
COSIGN=${COSIGN:-cosign}
if [ -n "${COSIGN_KEY:-}" ]; then
  echo "== signing SBOMs"
  for f in "$OUT"/sbom_*.json; do
    COSIGN_PASSWORD="${COSIGN_PASSWORD:-}" "$COSIGN" sign-blob --key "$COSIGN_KEY" --use-signing-config=false --new-bundle-format=false --tlog-upload=false --yes \
      --output-signature "$f.sig" "$f" >/dev/null 2>&1 || { echo "signing failed for $f"; rc=1; }
  done
else
  echo "COSIGN_KEY not set: SBOMs NOT signed"
fi
exit $rc
