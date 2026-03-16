#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# VPS Provisioning Script — ORCA Hybrid Edition
#
# Run on a fresh Ubuntu 22.04+ VPS to set up the Senpi trading agent.
# Usage: bash provision-vps.sh
#
# Prerequisites:
#   - .env file at /opt/senpi/.env with secrets (see template below)
#   - SSH key with push access to your senpi-state repo
# ---------------------------------------------------------------------------

SENPI_DIR="/opt/senpi"
STATE_REPO="${SENPI_STATE_REPO:-git@github.com:YOUR_USER/senpi-state.git}"
SKILLS_REPO="https://github.com/Senpi-ai/senpi-skills.git"

echo "=== Senpi VPS Provisioning (ORCA Hybrid) ==="

# --- 1. System deps ---
echo "[1/8] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip nodejs npm git curl jq

# --- 2. mcporter (Senpi MCP client) ---
echo "[2/8] Installing mcporter..."
npm install -g mcporter

# --- 3. Directory structure ---
echo "[3/8] Creating workspace..."
mkdir -p "$SENPI_DIR"

# --- 4. Clone repos ---
echo "[4/8] Cloning repositories..."
if [ ! -d "$SENPI_DIR/senpi-state/.git" ]; then
    git clone "$STATE_REPO" "$SENPI_DIR/senpi-state"
else
    git -C "$SENPI_DIR/senpi-state" pull --rebase
fi

if [ ! -d "$SENPI_DIR/senpi-skills/.git" ]; then
    git clone --depth 1 "$SKILLS_REPO" "$SENPI_DIR/senpi-skills"
else
    git -C "$SENPI_DIR/senpi-skills" pull --rebase
fi

# --- 5. Configure mcporter with Senpi MCP ---
echo "[5/8] Configuring mcporter with Senpi MCP..."
if [ -f "$SENPI_DIR/.env" ]; then
    source "$SENPI_DIR/.env"
    mcporter config add senpi --command npx \
        --env SENPI_AUTH_TOKEN="$SENPI_API_KEY" \
        -- mcp-remote "https://mcp.prod.senpi.ai/mcp" \
        --header "Authorization: Bearer \${SENPI_AUTH_TOKEN}"
    echo "mcporter configured with Senpi MCP at https://mcp.prod.senpi.ai"
else
    echo "WARNING: No .env file found at $SENPI_DIR/.env"
    echo "Create it with: SENPI_API_KEY=your_key_here"
    echo "Then re-run: mcporter config add senpi ..."
fi

# --- 6. Git config for auto-commits ---
echo "[6/8] Configuring git for auto-commits..."
git -C "$SENPI_DIR/senpi-state" config user.email "senpi-bot@vps"
git -C "$SENPI_DIR/senpi-state" config user.name "Senpi VPS Bot"

# --- 7. Verify mcporter connection ---
echo "[7/8] Verifying MCP connection..."
if command -v mcporter &>/dev/null; then
    mcporter list 2>/dev/null && echo "  mcporter: OK" || echo "  mcporter: configured but not yet verified (check token)"
else
    echo "  WARNING: mcporter not found in PATH"
fi

# --- 8. Install crontab ---
echo "[8/8] Installing cron jobs (ORCA hybrid architecture)..."

CRON_CONTENT=$(cat <<'CRONTAB'
# Senpi Trading Agent — VPS Cron Jobs (ORCA Hybrid Edition)
# Architecture: ORCA scanner + KOMODO momentum + DSL HW + Risk Arbiter + Arena Monitor
SENPI_STATE_DIR=/opt/senpi/senpi-state
SENPI_SKILLS_DIR=/opt/senpi/senpi-skills
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
BASH_ENV=/opt/senpi/.env

# Job 1: ORCA Dual-Mode Scanner (every 90 seconds via two cron entries)
# STALKER mode (accumulation) + STRIKER mode (explosion) with hardcoded gates
* * * * * python3 /opt/senpi/senpi-state/scripts/vps/orca-scanner-cron.py >> /var/log/senpi/orca.log 2>&1
* * * * * sleep 30 && python3 /opt/senpi/senpi-state/scripts/vps/orca-scanner-cron.py >> /var/log/senpi/orca.log 2>&1

# Job 2: DSL Combined Runner (every 3 minutes) — now using High Water Mode
*/3 * * * * bash /opt/senpi/senpi-state/scripts/vps/dsl-combined-cron.sh >> /var/log/senpi/dsl.log 2>&1

# Job 3: SM Flip Detector (every 5 minutes)
*/5 * * * * bash /opt/senpi/senpi-state/scripts/vps/sm-flip-cron.sh >> /var/log/senpi/smflip.log 2>&1

# Job 4: Watchdog (every 5 minutes, offset by 2min)
2-57/5 * * * * bash /opt/senpi/senpi-state/scripts/vps/watchdog-cron.sh >> /var/log/senpi/watchdog.log 2>&1

# Job 5: Health Check + git sync (every 10 minutes)
*/10 * * * * bash /opt/senpi/senpi-state/scripts/vps/health-check-cron.sh >> /var/log/senpi/health.log 2>&1

# Job 6: KOMODO Momentum Event Scanner (every 5 minutes, offset by 1min)
1-56/5 * * * * python3 /opt/senpi/senpi-state/scripts/vps/komodo-scanner-cron.py >> /var/log/senpi/komodo.log 2>&1

# Job 7: Arena Monitor — tracks Senpi Predators performance (every 15 minutes)
*/15 * * * * python3 /opt/senpi/senpi-state/scripts/vps/arena-monitor.py >> /var/log/senpi/arena.log 2>&1

# Risk Arbiter (every 30 seconds) — mechanical safety, no LLM
* * * * * python3 /opt/senpi/senpi-state/scripts/vps/risk-arbiter.py >> /var/log/senpi/arbiter.log 2>&1
* * * * * sleep 30 && python3 /opt/senpi/senpi-state/scripts/vps/risk-arbiter.py >> /var/log/senpi/arbiter.log 2>&1

# Senpi Skills repo auto-update (every 6 hours)
0 */6 * * * git -C /opt/senpi/senpi-skills pull --rebase --quiet 2>/dev/null || true

# Log rotation (daily)
0 0 * * * find /var/log/senpi/ -name "*.log" -mtime +7 -delete
CRONTAB
)

mkdir -p /var/log/senpi
echo "$CRON_CONTENT" | crontab -

echo ""
echo "=== Provisioning Complete (ORCA Hybrid) ==="
echo ""
echo "VPS Cron Architecture:"
echo "  🐋 ORCA Scanner:     every 90s  (dual-mode: STALKER + STRIKER)"
echo "  🦎 KOMODO Scanner:   every 5min (momentum event consensus)"
echo "  📊 Arena Monitor:    every 15min (tracks winning strategies)"
echo "  🔒 DSL v5 HW:        every 3min (High Water infinite trailing)"
echo "  🔄 SM Flip:          every 5min (conviction collapse)"
echo "  👁 Watchdog:         every 5min (margin/liq monitoring)"
echo "  🏥 Health Check:     every 10min (state validation + git sync)"
echo "  🚨 Risk Arbiter:     every 30s  (hard safety limits)"
echo ""
echo "Next steps:"
echo "  1. Create /opt/senpi/.env with your secrets:"
echo "     SENPI_API_KEY=your_senpi_api_key"
echo "     TELEGRAM_BOT_TOKEN=your_bot_token"
echo "     TELEGRAM_CHAT_ID=your_chat_id"
echo ""
echo "  2. If .env wasn't ready, configure mcporter:"
echo "     source /opt/senpi/.env"
echo "     mcporter config add senpi --command npx \\"
echo "       --env SENPI_AUTH_TOKEN=\$SENPI_API_KEY \\"
echo "       -- mcp-remote 'https://mcp.prod.senpi.ai/mcp' \\"
echo "       --header 'Authorization: Bearer \${SENPI_AUTH_TOKEN}'"
echo ""
echo "  3. Add a strategy: edit config/wolf-strategies.json"
echo "  4. Fund the strategy wallet with USDC (min \$500)"
echo "  5. Verify: crontab -l && tail -f /var/log/senpi/orca.log"
