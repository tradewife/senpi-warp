#!/usr/bin/env bash
# Oz Agent Init — run once at the start of every agent session.
#
# Secrets are NOT available during `oz environment create --setup-command`.
# They ARE available at agent run-time as env vars. This script bridges the gap:
# configures mcporter with the live SENPI_API_KEY and sets up git push credentials.
#
# Usage (first line of every Oz agent prompt):
#   bash senpi-waifu/scripts/oz/agent-init.sh

set -euo pipefail

# --- mcporter + Senpi MCP ---
if [ -n "${SENPI_API_KEY:-}" ]; then
    mcporter config add senpi \
        --command npx \
        --env "SENPI_AUTH_TOKEN=$SENPI_API_KEY" \
        -- mcp-remote https://mcp.prod.senpi.ai/mcp \
        --header "Authorization: Bearer $SENPI_API_KEY" 2>/dev/null || true
    echo "[init] mcporter configured with Senpi MCP"
else
    echo "[init] WARNING: SENPI_API_KEY not set — mcporter will not work"
fi

# --- git push credentials (GitHub token via credential store) ---
if [ -n "${GITHUB_TOKEN:-}" ]; then
    REPO="${GITHUB_REPO:-tradewife/senpi-waifu}"
    git config --global user.email "oz-agent@warp.dev"
    git config --global user.name "Oz Agent"
    git config --global credential.helper store
    echo "https://${GITHUB_TOKEN}:x-oauth-basic@github.com" > ~/.git-credentials
    chmod 600 ~/.git-credentials
    echo "[init] git credentials configured for $REPO"
else
    echo "[init] WARNING: GITHUB_TOKEN not set — git push will fail"
fi

echo "[init] Done. senpi-waifu ready."
