#!/usr/bin/env bash
# AICF Zero-Downtime Upgrade & Rollback Procedure — Pha B2(d)
# Usage:
#   deploy/production/upgrade_rollback.sh upgrade /path/to/new/site-gateway
#   deploy/production/upgrade_rollback.sh rollback
set -euo pipefail

ACTION="${1:-upgrade}"
NEW_BIN="${2:-}"
INSTALL_BIN="${AICF_INSTALL_BIN:-/opt/aicf/bin/site-gateway}"
BACKUP_BIN="${INSTALL_BIN}.prev"
GATEWAY_URL="${AICF_GATEWAY_URL:-http://127.0.0.1:18080}"
ADMIN_TOKEN="${AICF_ADMIN_TOKEN:-}"
DRAIN_WAIT_SEC="${AICF_DRAIN_WAIT_SEC:-5}"

set_drain() {
  local state="$1"
  if [[ -n "${ADMIN_TOKEN}" ]]; then
    curl -fsS -X POST "${GATEWAY_URL}/api/serverless/v1/admin/state" \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"draining\":${state}}" >/dev/null || true
  fi
}

wait_healthy() {
  for _ in $(seq 1 25); do
    if curl -fsS "${GATEWAY_URL}/healthz" >/dev/null 2>&1 && curl -fsS "${GATEWAY_URL}/readyz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if [[ "${ACTION}" == "upgrade" ]]; then
  if [[ -z "${NEW_BIN}" || ! -x "${NEW_BIN}" ]]; then
    echo "ERROR: Provide executable binary path: $0 upgrade /path/to/new/site-gateway" >&2
    exit 1
  fi
  echo "[1/5] Draining active Serverless & LLM requests..."
  set_drain true
  sleep "${DRAIN_WAIT_SEC}"

  echo "[2/5] Backing up current binary ${INSTALL_BIN} -> ${BACKUP_BIN}..."
  if [[ -f "${INSTALL_BIN}" ]]; then
    cp -pf "${INSTALL_BIN}" "${BACKUP_BIN}"
  fi

  echo "[3/5] Installing new binary atomically..."
  cp -pf "${NEW_BIN}" "${INSTALL_BIN}.tmp"
  mv -f "${INSTALL_BIN}.tmp" "${INSTALL_BIN}"

  echo "[4/5] Restarting aicf-site-gateway..."
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet aicf-site-gateway 2>/dev/null; then
    systemctl restart aicf-site-gateway
  fi

  echo "[5/5] Verifying /healthz and /readyz..."
  if ! wait_healthy; then
    echo "ERROR: Upgrade failed readiness check! Rolling back automatically..." >&2
    "$0" rollback
    exit 1
  fi
  set_drain false
  echo "UPGRADE_OK"
elif [[ "${ACTION}" == "rollback" ]]; then
  if [[ ! -f "${BACKUP_BIN}" ]]; then
    echo "ERROR: No backup binary found at ${BACKUP_BIN}" >&2
    exit 1
  fi
  echo "[rollback] Restoring ${BACKUP_BIN} -> ${INSTALL_BIN}..."
  cp -pf "${BACKUP_BIN}" "${INSTALL_BIN}.tmp"
  mv -f "${INSTALL_BIN}.tmp" "${INSTALL_BIN}"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet aicf-site-gateway 2>/dev/null; then
    systemctl restart aicf-site-gateway
  fi
  set_drain false
  echo "ROLLBACK_OK"
else
  echo "Usage: $0 {upgrade /path/to/bin|rollback}" >&2
  exit 1
fi

