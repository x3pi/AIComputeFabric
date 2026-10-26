#!/usr/bin/env bash
# Dependency scan + SBOM (plan P8.2). Needs govulncheck, cyclonedx-gomod (go install) and
# pip-audit, cyclonedx-py (pip install). Set PYBIN to the venv python's bin dir and GOBIN_TOOLS to the go tools dir.
# Exit status is non-zero if any scan finds a vulnerability that the code can reach.
# Not covered: container images (trivy not installed here), license review, signing of the SBOM.
set -uo pipefail
cd "$(dirname "$0")/../.."
OUT=${OUT:-benchmarks/reports/security}; mkdir -p "$OUT"
PYBIN=${PYBIN:-}; GOT=${GOBIN_TOOLS:-$(go env GOPATH)/bin}
rc=0
for m in apps/site-gateway apps/worker-guard core/vram-estimator; do
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
exit $rc
