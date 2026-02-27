#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# Oz Cloud Agent Setup
#
# Creates the Oz environment, secrets, and scheduled agent tasks.
# Run this from your local machine (where oz CLI is installed).
#
# Usage:
#   export SENPI_API_KEY="..."
#   export TELEGRAM_BOT_TOKEN="..."
#   export TELEGRAM_CHAT_ID="..."
#   bash scripts/oz/setup-oz-agents.sh
# ---------------------------------------------------------------------------

STATE_REPO="${SENPI_STATE_REPO:-github.com/YOUR_USER/senpi-state}"
SKILLS_REPO="github.com/Senpi-ai/senpi-skills"

echo "=== Oz Cloud Agent Setup ==="

# --- 1. Create environment ---
echo "[1/4] Creating Oz environment..."
ENV_OUTPUT=$(oz environment create \
    --name "senpi-trader" \
    --docker-image "warpdotdev/dev-base:latest" \
    --repo "$STATE_REPO" \
    --repo "$SKILLS_REPO" \
    --setup-command "pip install requests pandas && npm i -g mcporter" \
    --output-format json 2>/dev/null || echo '{}')

ENV_ID=$(echo "$ENV_OUTPUT" | jq -r '.id // empty')

if [ -z "$ENV_ID" ]; then
    echo "Environment creation failed or already exists."
    echo "List existing environments:"
    oz environment list --output-format text
    echo ""
    read -rp "Enter existing environment ID: " ENV_ID
fi

echo "Using environment: $ENV_ID"

# --- 2. Create secrets ---
echo "[2/4] Creating Oz secrets..."

if [ -n "${SENPI_API_KEY:-}" ]; then
    oz secret create SENPI_API_KEY --team \
        --value "$SENPI_API_KEY" \
        --description "Senpi MCP authentication token" 2>/dev/null || echo "  (SENPI_API_KEY already exists)"
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    oz secret create TELEGRAM_BOT_TOKEN --team \
        --value "$TELEGRAM_BOT_TOKEN" \
        --description "Telegram bot token for trade alerts" 2>/dev/null || echo "  (TELEGRAM_BOT_TOKEN already exists)"
fi

if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    oz secret create TELEGRAM_CHAT_ID --team \
        --value "$TELEGRAM_CHAT_ID" \
        --description "Telegram chat ID for alerts" 2>/dev/null || echo "  (TELEGRAM_CHAT_ID already exists)"
fi

# --- 3. Create scheduled agents ---
echo "[3/4] Creating scheduled cloud agents..."

# Agent A: Trade Evaluator — every 15 minutes
echo "  Creating: Trade Evaluator (*/15 * * * *)"
oz schedule create --cron "*/15 * * * *" --environment "$ENV_ID" \
    --name "senpi-trade-evaluator" \
    --prompt 'You are the Senpi Trade Evaluator. Your job:

1. Pull latest state: `git pull` in the senpi-state repo.
2. Read `state/pending-entries.json` for queued signals.
3. For each non-auto-entered pending signal:
   - Run the Opportunity Scanner v6 pipeline (scripts in senpi-skills/opportunity-scanner/).
   - Apply 4-pillar scoring + hourly trend gate + hard disqualifiers from config/scanner-config.json.
   - If score >= 175 with trend alignment: open position via mcporter, create DSL state file in state/{strategyKey}/, record in memory/trade-journal.json.
   - If score < 175 or disqualified: skip and log reason.
4. For auto-entered signals (autoEntered: true): review quality. If the signal looks bad (erratic, counter-trend, low score), close the position immediately.
5. Clear processed entries from pending-entries.json.
6. Commit and push all state changes.

Use DSL-Tight profile for all entries. Read config/risk-regime.json for current regime and slot limits. Never counter-trend on hourly. Never enter assets at rank #1-10.' \
    2>/dev/null || echo "  (schedule may already exist)"

# Agent B: Regime Classifier — hourly
echo "  Creating: Regime Classifier (0 * * * *)"
oz schedule create --cron "0 * * * *" --environment "$ENV_ID" \
    --name "senpi-regime-classifier" \
    --prompt 'You are the Senpi Regime Classifier. Your job:

1. Pull latest state from senpi-state repo.
2. Fetch BTC and ETH 4h + 1h candles via mcporter (use market_get_candles or equivalent).
3. Analyze: MA slope, ATR ratio, funding rates, OI changes.
4. Classify macro regime:
   - RISK_ON: Strong trend + controlled volatility. Allow max slots, upper leverage.
   - BASELINE: Mixed signals. Standard slots and leverage.
   - RISK_OFF: Extreme chop, funding blowouts, or liquidation clusters. No new entries.
5. Update config/risk-regime.json with: riskMode, updatedAt, updatedBy="oz-regime", reason.
6. Commit and push.

Be conservative with RISK_ON — only set it when trend evidence is clear across multiple timeframes.' \
    2>/dev/null || echo "  (schedule may already exist)"

# Agent C: Portfolio Review — every 6 hours
echo "  Creating: Portfolio Review (0 */6 * * *)"
oz schedule create --cron "0 */6 * * *" --environment "$ENV_ID" \
    --name "senpi-portfolio-review" \
    --prompt 'You are the Senpi Portfolio Reviewer. Your job:

1. Pull latest state.
2. Read all DSL state files across all strategies in state/.
3. Read memory/trade-journal.json for recent trades.
4. Compute: daily realized PnL, unrealized PnL, drawdown from peak, directional exposure (LONG vs SHORT notional).
5. Check guardrails from config/risk-regime.json: daily loss limit, directional cap (70%), max positions.
6. Identify dead weight: positions with SM conviction 0, negative ROE, open > 30 minutes.
7. If any guardrail is breached, update risk-regime.json appropriately.
8. Write a structured JSON report to outputs/latest-report.json.
9. Send a Telegram summary with portfolio status.
10. Commit and push.' \
    2>/dev/null || echo "  (schedule may already exist)"

# Agent D: HOWL Nightly Review — daily at 23:55
echo "  Creating: HOWL Nightly (55 23 * * *)"
oz schedule create --cron "55 23 * * *" --environment "$ENV_ID" \
    --name "senpi-howl" \
    --prompt 'You are HOWL — Hunt, Optimize, Win, Learn. Run the full v2 nightly analysis.

1. Pull latest state.
2. Read senpi-skills/wolf-howl/SKILL.md for the complete analysis procedure.
3. Gather: memory/trade-journal.json (last 24h), all DSL state files, state/scan-history.json, config/*.json.
4. Compute ALL v2 metrics: win rate, profit factor (gross AND net), fee drag ratio, holding period buckets (<30min, 30-90min, 90+min), LONG vs SHORT breakdown, monster trade dependency, rotation cost tracking.
5. Identify patterns: what worked, what failed, regime mismatches, DSL effectiveness.
6. Produce improvement suggestions at high/medium/low confidence.
7. Auto-apply ONLY risk-reducing changes to config/ (e.g., tighten thresholds, reduce leverage). Risk-increasing changes require manual approval.
8. Save full report to memory/howl-YYYY-MM-DD.md.
9. Append distilled summary to memory/MEMORY.md.
10. Send Telegram summary.
11. Commit and push.' \
    2>/dev/null || echo "  (schedule may already exist)"

# Agent E: Whale Index — daily at 01:00
echo "  Creating: Whale Index (0 1 * * *)"
oz schedule create --cron "0 1 * * *" --environment "$ENV_ID" \
    --name "senpi-whale-index" \
    --prompt 'You are the Whale Index Manager. Run daily rebalance per senpi-skills/whale-index/SKILL.md.

1. Pull latest state.
2. Scan top 50 Discovery traders via mcporter: discovery_top_traders(limit=50, timeframe="30d").
3. Score: PnL rank (35%), win rate (25%), consistency (20%), hold time (10%), drawdown (10%).
4. Check existing mirror strategies for watch status (2-day watch before swaps).
5. If a trader has degraded for 2+ consecutive days AND a replacement scores 15%+ higher: swap.
6. If no mirror strategies exist yet, present top 3 candidates and create mirrors.
7. Update state with mirror strategy status.
8. Commit and push.' \
    2>/dev/null || echo "  (schedule may already exist)"

# --- 4. Summary ---
echo ""
echo "[4/4] Listing schedules..."
oz schedule list --output-format text 2>/dev/null || echo "(list failed — check oz login)"

echo ""
echo "=== Oz Setup Complete ==="
echo ""
echo "Environment ID: $ENV_ID"
echo ""
echo "Scheduled agents:"
echo "  - Trade Evaluator:    every 15 min"
echo "  - Regime Classifier:  every hour"
echo "  - Portfolio Review:   every 6 hours"
echo "  - HOWL:               nightly at 23:55 UTC"
echo "  - Whale Index:        daily at 01:00 UTC"
echo ""
echo "Monitor runs:  oz run list"
echo "Check a run:   oz run get <run-id>"
echo "Manual run:    oz agent run-cloud --environment $ENV_ID --prompt '...'"
