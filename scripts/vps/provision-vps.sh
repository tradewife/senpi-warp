#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# VPS Provisioning Script
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

echo "=== Senpi VPS Provisioning ==="

# --- 1. System deps ---
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip nodejs npm git curl jq

# --- 2. mcporter ---
echo "[2/7] Installing mcporter..."
npm install -g mcporter

# --- 3. Directory structure ---
echo "[3/7] Creating workspace..."
mkdir -p "$SENPI_DIR"

# --- 4. Clone repos ---
echo "[4/7] Cloning repositories..."
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
echo "[5/7] Configuring mcporter..."
if [ -f "$SENPI_DIR/.env" ]; then
    source "$SENPI_DIR/.env"
    mcporter config add senpi --command npx \
        --env SENPI_AUTH_TOKEN="$SENPI_API_KEY" \
        -- mcp-remote "https://mcp.prod.senpi.ai/mcp" \
        --header "Authorization: Bearer \${SENPI_AUTH_TOKEN}"
    echo "mcporter configured with Senpi MCP"
else
    echo "WARNING: No .env file found at $SENPI_DIR/.env"
    echo "Create it with: SENPI_API_KEY=your_key_here"
    echo "Then re-run this section manually."
fi

# --- 6. Git config for auto-commits ---
echo "[6/7] Configuring git for auto-commits..."
git -C "$SENPI_DIR/senpi-state" config user.email "senpi-bot@vps"
git -C "$SENPI_DIR/senpi-state" config user.name "Senpi VPS Bot"

# --- 7. Install crontab ---
echo "[7/7] Installing cron jobs..."

CRON_CONTENT=$(cat <<'CRONTAB'
# Senpi Trading Agent — VPS Cron Jobs
# Environment
SENPI_STATE_DIR=/opt/senpi/senpi-state
SENPI_SKILLS_DIR=/opt/senpi/senpi-skills
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin

# Load secrets
BASH_ENV=/opt/senpi/.env

# Job 1: Emerging Movers Scanner (every 60 seconds)
* * * * * python3 /opt/senpi/senpi-state/scripts/vps/emerging-movers-cron.py >> /var/log/senpi/em.log 2>&1

# Job 2: DSL Combined Runner (every 3 minutes)
*/3 * * * * python3 /opt/senpi/senpi-state/scripts/vps/dsl-combined-cron.sh >> /var/log/senpi/dsl.log 2>&1

# Job 3: SM Flip Detector (every 5 minutes)
*/5 * * * * python3 /opt/senpi/senpi-state/scripts/vps/sm-flip-cron.sh >> /var/log/senpi/smflip.log 2>&1

# Job 4: Watchdog (every 5 minutes, offset by 2min)
2-57/5 * * * * python3 /opt/senpi/senpi-state/scripts/vps/watchdog-cron.sh >> /var/log/senpi/watchdog.log 2>&1

# Job 5: Health Check + git sync (every 10 minutes)
*/10 * * * * python3 /opt/senpi/senpi-state/scripts/vps/health-check-cron.sh >> /var/log/senpi/health.log 2>&1

# Risk Arbiter (every 30 seconds via two cron entries)
* * * * * python3 /opt/senpi/senpi-state/scripts/vps/risk-arbiter.py >> /var/log/senpi/arbiter.log 2>&1
* * * * * sleep 30 && python3 /opt/senpi/senpi-state/scripts/vps/risk-arbiter.py >> /var/log/senpi/arbiter.log 2>&1

# Log rotation (daily)
0 0 * * * find /var/log/senpi/ -name "*.log" -mtime +7 -delete
CRONTAB
)

mkdir -p /var/log/senpi
echo "$CRON_CONTENT" | crontab -

echo ""
echo "=== Provisioning Complete ==="
echo ""
echo "Next steps:"
echo "  1. Create /opt/senpi/.env with your secrets:"
echo "     SENPI_API_KEY=your_senpi_api_key"
echo "     TELEGRAM_BOT_TOKEN=your_bot_token"
echo "     TELEGRAM_CHAT_ID=your_chat_id"
echo ""
echo "  2. Configure mcporter (if .env wasn't ready):"
echo "     source /opt/senpi/.env && mcporter config add senpi ..."
echo ""
echo "  3. Add a strategy via the Oz setup or manually:"
echo "     Edit config/wolf-strategies.json"
echo ""
echo "  4. Fund the strategy wallet with USDC"
echo ""
echo "  5. Verify cron is running:"
echo "     crontab -l"
echo "     tail -f /var/log/senpi/em.log"
