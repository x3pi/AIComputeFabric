#!/usr/bin/env bash
# AICF Production Firewall Rules (UFW) — Pha B2
# Usage: sudo GATEWAY_IP=10.42.0.1 RPC_SUBNET=10.42.0.0/24 bash deploy/production/firewall/ufw-rules.sh
set -euo pipefail

GATEWAY_IP="${GATEWAY_IP:-10.42.0.1}"
RPC_SUBNET="${RPC_SUBNET:-10.42.0.0/24}"

echo "[aicf-ufw] Locking down worker-guard (8099/tcp) to loopback + ${GATEWAY_IP}..."
ufw allow from 127.0.0.1 to any port 8099 proto tcp comment "AICF worker-guard loopback"
ufw allow from "${GATEWAY_IP}" to any port 8099 proto tcp comment "AICF worker-guard from site-gateway"
ufw deny 8099/tcp comment "Block external access to worker-guard"

echo "[aicf-ufw] Locking down ggml-rpc (50052/tcp) to private direct link ${RPC_SUBNET}..."
ufw allow from "${RPC_SUBNET}" to any port 50052 proto tcp comment "AICF ggml-rpc private link"
ufw deny 50052/tcp comment "Block LAN/WAN access to unencrypted ggml-rpc"

echo "[aicf-ufw] Blocking external access to internal sidecars (18090, 18095, 18100, 18110)..."
for port in 18090 18095 18100 18110; do
  ufw allow from 127.0.0.1 to any port "${port}" proto tcp comment "AICF internal sidecar ${port}"
  ufw deny "${port}/tcp" comment "Block external access to sidecar ${port}"
done

echo "[aicf-ufw] Firewall rules applied."
