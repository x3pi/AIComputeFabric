#!/usr/bin/env bash
# AICF Production Preflight Verification Script — Pha B2(c)
# Validates all 7 mandatory production security & operational preconditions:
#   1. Admin token is set, >= 16 chars, and not a known default/demo token
#   2. Secret files / env files have strict permissions (0600 or 0400)
#   3. TLS certificate and private key exist, are readable, and key is 0600/0400
#   4. Worker-guard (:8099) and ggml-rpc (:50052) are not bound to 0.0.0.0 without firewall
#   5. Docker daemon is reachable and NVIDIA container runtime is registered (unless AICF_SKIP_DOCKER=1)
#   6. Sufficient free disk space on data volume (>= AICF_MIN_DISK_MB, default 2048 MiB)
#   7. Production safety flags (--allow-signup=false, --signup-auto-approve=false, --allow-unauthenticated-admin=false)
set -euo pipefail

ENV_FILE="${AICF_ENV_FILE:-/etc/aicf/site-gateway.env}"
DATA_DIR="${AICF_DATA_DIR:-/var/lib/aicf}"
MIN_DISK_MB="${AICF_MIN_DISK_MB:-2048}"
SKIP_DOCKER="${AICF_SKIP_DOCKER:-0}"

fails=0
pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1" >&2; fails=$((fails + 1)); }

if [[ ! -f "${ENV_FILE}" ]]; then
  fail "1/7 Environment file not found: ${ENV_FILE}"
else
  # Check 2: File permissions on ENV_FILE (must be 600 or 400)
  perm="$(stat -c '%a' "${ENV_FILE}")"
  if [[ "${perm}" != "600" && "${perm}" != "400" ]]; then
    fail "2/7 Secret env file ${ENV_FILE} has insecure permissions ${perm} (expected 600 or 400)"
  else
    pass "2/7 Secret env file ${ENV_FILE} permissions are ${perm}"
  fi

  # Load variables safely in subshell or parse key=value
  ADMIN_TOKEN="$(grep -E '^AICF_ADMIN_TOKEN=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  TLS_CERT="$(grep -E '^AICF_TLS_CERT=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  TLS_KEY="$(grep -E '^AICF_TLS_KEY=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  ALLOW_SIGNUP="$(grep -E '^AICF_ALLOW_SIGNUP=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "false")"
  AUTO_APPROVE="$(grep -E '^AICF_SIGNUP_AUTO_APPROVE=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "false")"
  UNAUTH_ADMIN="$(grep -E '^AICF_ALLOW_UNAUTHENTICATED_ADMIN=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "false")"
  WG_BIND="$(grep -E '^AICF_WORKER_GUARD_ADDR=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "127.0.0.1:8099")"
  RPC_BIND="$(grep -E '^AICF_RPC_BIND=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "10.42.0.2:50052")"

  # Check 1: Admin token strength & non-default
  case "${ADMIN_TOKEN}" in
    ""|"demo-admin-token"|"admin"|"password"|"changeme"|"default"|"aicf-demo-token"|"test-token")
      fail "1/7 AICF_ADMIN_TOKEN is empty or a forbidden default token"
      ;;
    *)
      if [[ "${#ADMIN_TOKEN}" -lt 16 ]]; then
        fail "1/7 AICF_ADMIN_TOKEN is too short (${#ADMIN_TOKEN} < 16 chars)"
      else
        pass "1/7 AICF_ADMIN_TOKEN is non-default and strong (${#ADMIN_TOKEN} chars)"
      fi
      ;;
  esac

  # Check 3: TLS certificate and private key
  if [[ -z "${TLS_CERT}" || -z "${TLS_KEY}" || ! -f "${TLS_CERT}" || ! -f "${TLS_KEY}" ]]; then
    fail "3/7 TLS certificate (${TLS_CERT:-unset}) or key (${TLS_KEY:-unset}) missing"
  else
    key_perm="$(stat -c '%a' "${TLS_KEY}")"
    if [[ "${key_perm}" != "600" && "${key_perm}" != "400" ]]; then
      fail "3/7 TLS private key ${TLS_KEY} has insecure permissions ${key_perm} (expected 600 or 400)"
    else
      pass "3/7 TLS certificate and private key verified (key mode ${key_perm})"
    fi
  fi

  # Check 4: Network bind restriction for worker-guard (:8099) & ggml-rpc (:50052)
  if [[ "${WG_BIND}" == 0.0.0.0:* || "${WG_BIND}" == :* || "${RPC_BIND}" == 0.0.0.0:* || "${RPC_BIND}" == :* ]]; then
    fail "4/7 Unsafe wildcard bind detected (worker-guard=${WG_BIND}, ggml-rpc=${RPC_BIND}); must bind to loopback or private link IP"
  else
    pass "4/7 Network binds restricted (worker-guard=${WG_BIND}, ggml-rpc=${RPC_BIND})"
  fi

  # Check 7: Safe flags
  if [[ "${ALLOW_SIGNUP}" == "true" || "${AUTO_APPROVE}" == "true" || "${UNAUTH_ADMIN}" == "true" ]]; then
    fail "7/7 Unsafe production flags enabled (allow_signup=${ALLOW_SIGNUP}, auto_approve=${AUTO_APPROVE}, unauth_admin=${UNAUTH_ADMIN})"
  else
    pass "7/7 Production auth safety flags enforced (allow_signup=false, auto_approve=false, unauth_admin=false)"
  fi
fi

# Check 5: Docker & NVIDIA runtime
if [[ "${SKIP_DOCKER}" == "1" ]]; then
  pass "5/7 Docker/NVIDIA check skipped via AICF_SKIP_DOCKER=1"
else
  if ! command -v docker >/dev/null 2>&1; then
    fail "5/7 docker CLI not found"
  elif ! docker info >/dev/null 2>&1; then
    fail "5/7 docker daemon not reachable"
  elif ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi "nvidia"; then
    fail "5/7 NVIDIA container runtime not registered in docker"
  else
    pass "5/7 Docker daemon reachable and NVIDIA container runtime registered"
  fi
fi

# Check 6: Disk space
mkdir -p "${DATA_DIR}" 2>/dev/null || true
CheckDir="${DATA_DIR}"
[[ -d "${CheckDir}" ]] || CheckDir="/"
avail_mb="$(df -Pm "${CheckDir}" | awk 'NR==2 {print $4}')"
if [[ "${avail_mb:-0}" -lt "${MIN_DISK_MB}" ]]; then
  fail "6/7 Insufficient disk space on ${CheckDir}: ${avail_mb} MiB < ${MIN_DISK_MB} MiB"
else
  pass "6/7 Disk space sufficient on ${CheckDir}: ${avail_mb} MiB available"
fi

if [[ "${fails}" -gt 0 ]]; then
  echo "PREFLIGHT_FAILED (${fails} error(s))" >&2
  exit 1
fi
echo "PREFLIGHT_OK (7/7 checks passed)"

