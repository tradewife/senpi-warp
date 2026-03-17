#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# Oz Cloud Agent Setup — ORCA Hybrid Edition
#
# Creates the Oz environment, secrets, and scheduled agent tasks.
# Now includes Arena Strategy Learner for data-driven self-improvement.
#
# Usage:
#   export SENPI_API_KEY="..."
#   export GITHUB_TOKEN="..."   # GitHub fine-grained token (Contents read/write)
#   export TELEGRAM_BOT_TOKEN="..."
#   export TELEGRAM_CHAT_ID="..."
#   export SENPI_STATE_REPO="github.com/YOUR_USER/senpi-waifu"
#   bash scripts/oz/setup-oz-agents.sh
# ---------------------------------------------------------------------------

STATE_REPO="${SENPI_STATE_REPO:-github.com/tradewife/senpi-waifu}"
SKILLS_REPO="github.com/Senpi-ai/senpi-skills"

echo "=== Oz Cloud Agent Setup (ORCA Hybrid) ==="

# --- 1. Create environment ---
echo "[1/4] Creating Oz environment..."
ENV_OUTPUT=$(oz-preview environment create \
    --name "senpi-orca-hybrid" \
    --docker-image "warpdotdev/dev-base:latest" \
    --repo "$STATE_REPO" \
    --repo "$SKILLS_REPO" \
    --setup-command "pip install requests pandas && npm i -g mcporter" \
    --output-format json 2>/dev/null || echo '{}')

ENV_ID=$(python3 - <<'PY'
import json,sys
raw = sys.stdin.read().strip()
try:
    data = json.loads(raw) if raw else {}
except json.JSONDecodeError:
    data = {}
print(data.get("id",""))
PY
<<< "$ENV_OUTPUT")

if [ -z "$ENV_ID" ]; then
    echo "Environment creation failed or already exists."
    echo "List existing environments:"
    oz-preview environment list --output-format text
    echo ""
    read -rp "Enter existing environment ID: " ENV_ID
fi

echo "Using environment: $ENV_ID"

# --- 2. Create secrets ---
echo "[2/4] Creating Oz secrets..."

if [ -n "${SENPI_API_KEY:-}" ]; then
    oz-preview secret create SENPI_API_KEY --team \
        --value "$SENPI_API_KEY" \
        --description "Senpi MCP authentication token" 2>/dev/null || echo "  (SENPI_API_KEY already exists)"
fi

if [ -n "${GITHUB_TOKEN:-}" ]; then
    oz-preview secret create GITHUB_TOKEN --team \
        --value "$GITHUB_TOKEN" \
        --description "GitHub fine-grained token for state repo push access" 2>/dev/null || echo "  (GITHUB_TOKEN already exists)"
fi

if [ -n "${GITHUB_REPO:-}" ]; then
    oz-preview secret create GITHUB_REPO --team \
        --value "${GITHUB_REPO:-tradewife/senpi-waifu}" \
        --description "GitHub repo (owner/name) for state repo" 2>/dev/null || echo "  (GITHUB_REPO already exists)"
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    oz-preview secret create TELEGRAM_BOT_TOKEN --team \
        --value "$TELEGRAM_BOT_TOKEN" \
        --description "Telegram bot token for trade alerts" 2>/dev/null || echo "  (TELEGRAM_BOT_TOKEN already exists)"
fi

if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    oz-preview secret create TELEGRAM_CHAT_ID --team \
        --value "$TELEGRAM_CHAT_ID" \
        --description "Telegram chat ID for alerts" 2>/dev/null || echo "  (TELEGRAM_CHAT_ID already exists)"
fi

# --- 3. Create scheduled agents ---
# Prompts are written to temp files using quoted heredocs (<< 'PROMPT') so that
# backticks, double quotes, and other special characters are never interpreted by bash.
echo "[3/4] Creating scheduled cloud agents..."

# Agent A: Trade Evaluator — every 15 minutes (ORCA + KOMODO aware)
echo "  Creating: Trade Evaluator (*/15 * * * *)"
cat > /tmp/oz_trade_evaluator.txt << 'PROMPT'
Run `bash senpi-waifu/scripts/oz/agent-init.sh` first to configure mcporter and git push credentials. Then:

You are the Senpi Trade Evaluator (ORCA Hybrid Edition). Your job:

1. Run agent-init.sh (already done above), then git pull in the senpi-waifu repo.
2. Read `state/pending-entries.json` for queued signals from ORCA and KOMODO scanners.
3. For each pending signal:
   - Check signal source: "orca" signals are STALKER/STRIKER dual-mode. "komodo" signals are momentum event consensus.
   - For ORCA signals: Validate using Opportunity Scanner v6 (scripts in senpi-skills/opportunity-scanner/). Score >= 175 with hourly trend alignment passes. Apply hard disqualifiers from config/scanner-config.json.
   - For KOMODO signals: Verify momentum event consensus is still valid. 2+ quality traders on same asset/direction within 60 min.
   - For auto-entered signals (autoEntered: true): review quality. If erratic, counter-trend, or low score — close immediately.
4. Apply these HARDCODED rules (from ORCA lessons):
   - NEVER enter XYZ equities (net negative across all 22 agents)
   - Leverage MUST be 7-10x (sub-7x cannot overcome fees, >10x blows up)
   - Max 3 simultaneous positions
   - 4H trend alignment is a HARD gate — never counter-trend
   - 2-hour per-asset cooldown after any Phase 1 exit
5. For valid entries: open via mcporter with DSL High Water Mode (lockMode: pct_of_high_water).
6. Clear processed entries from pending-entries.json.
7. Read outputs/arena-state.json for insights from winning predator strategies. Prefer selectivity over frequency.
8. Commit and push all state changes.

Key lesson from 22 agents: FEWER TRADES + HIGHER CONVICTION = better performance. FOX is #1 at +13.93% with only 436 trades. Agents with 700+ trades are all negative.
PROMPT
oz-preview schedule create --cron "*/15 * * * *" --environment "$ENV_ID" \
    --name "senpi-trade-evaluator" --team \
    --prompt "$(cat /tmp/oz_trade_evaluator.txt)" \
    || echo "  WARNING: failed to create Trade Evaluator schedule (see error above)"

# Agent B: Regime Classifier — hourly
echo "  Creating: Regime Classifier (0 * * * *)"
cat > /tmp/oz_regime_classifier.txt << 'PROMPT'
Run `bash senpi-waifu/scripts/oz/agent-init.sh` first to configure mcporter and git push credentials. Then:

You are the Senpi Regime Classifier. Your job:

1. git pull in senpi-waifu repo.
2. Fetch BTC and ETH 4h + 1h candles via mcporter.
3. Analyze: MA slope, ATR ratio, funding rates, OI changes.
4. Classify macro regime:
   - RISK_ON: Strong trend + controlled volatility. Allow max 3 slots, 7-10x leverage.
   - BASELINE: Mixed signals. 2 slots max, 7-10x leverage.
   - RISK_OFF: Extreme chop, funding blowouts, or liquidation clusters. No new entries.
5. ORCA-aligned rules: maxLeverage never exceeds 10 regardless of regime. XYZ leverage always 0.
6. Update config/risk-regime.json with: riskMode, updatedAt, updatedBy="oz-regime", reason.
7. Commit and push.

Be conservative with RISK_ON — only set it when trend evidence is clear across multiple timeframes.
PROMPT
oz-preview schedule create --cron "0 * * * *" --environment "$ENV_ID" \
    --name "senpi-regime-classifier" --team \
    --prompt "$(cat /tmp/oz_regime_classifier.txt)" \
    || echo "  WARNING: failed to create Regime Classifier schedule (see error above)"

# Agent C: Portfolio Review — every 6 hours
echo "  Creating: Portfolio Review (0 */6 * * *)"
cat > /tmp/oz_portfolio_review.txt << 'PROMPT'
Run `bash senpi-waifu/scripts/oz/agent-init.sh` first to configure mcporter and git push credentials. Then:

You are the Senpi Portfolio Reviewer. Your job:

1. git pull in senpi-waifu repo.
2. Read all DSL state files across all strategies in state/.
3. Read memory/trade-journal.json for recent trades.
4. Compute: daily realized PnL, unrealized PnL, drawdown from peak, directional exposure.
5. Check guardrails from config/risk-regime.json: 10% daily loss limit, 70% directional cap, max 3 positions.
6. Check DSL mode: all positions should be using High Water Mode (lockMode: pct_of_high_water). Flag any using legacy fixed tiers.
7. Read outputs/arena-state.json — compare our performance vs top arena predators. Note our trade frequency vs theirs.
8. Identify dead weight: positions with SM conviction 0, negative ROE, open > 30 minutes.
9. Write structured JSON report to outputs/latest-report.json.
10. Send Telegram summary.
11. Commit and push.
PROMPT
oz-preview schedule create --cron "0 */6 * * *" --environment "$ENV_ID" \
    --name "senpi-portfolio-review" --team \
    --prompt "$(cat /tmp/oz_portfolio_review.txt)" \
    || echo "  WARNING: failed to create Portfolio Review schedule (see error above)"

# Agent D: HOWL Nightly Review — daily at 23:55
echo "  Creating: HOWL Nightly (55 23 * * *)"
cat > /tmp/oz_howl.txt << 'PROMPT'
Run `bash senpi-waifu/scripts/oz/agent-init.sh` first to configure mcporter and git push credentials. Then:

You are HOWL — Hunt, Optimize, Win, Learn. Run the full nightly analysis.

1. git pull in senpi-waifu repo.
2. Read senpi-skills/wolf-howl/SKILL.md for the complete analysis procedure.
3. Gather: memory/trade-journal.json (last 24h), all DSL state files, state/orca-scan-history.json, state/komodo-events.json, config/*.json.
4. Compute ALL metrics: win rate, profit factor, fee drag ratio, holding period buckets, LONG vs SHORT, scanner source comparison (ORCA STALKER vs STRIKER vs KOMODO).
5. Read outputs/arena-state.json — compare our performance against the top Senpi Predators. What are they doing differently?
6. Identify patterns: STALKER entries vs STRIKER entries — which mode is winning? Should we adjust mode weights?
7. Auto-apply ONLY risk-reducing changes (tighten thresholds, reduce leverage). Risk increases require manual approval.
8. Save report to memory/howl-YYYY-MM-DD.md.
9. Append distilled summary to memory/MEMORY.md.
10. Send Telegram summary.
11. Commit and push.

Key data points to always report: trade count by scanner (ORCA/KOMODO), mode breakdown (STALKER/STRIKER), win rate by mode, avg holding time by mode, fee drag as % of PnL.
PROMPT
oz-preview schedule create --cron "55 23 * * *" --environment "$ENV_ID" \
    --name "senpi-howl" --team \
    --prompt "$(cat /tmp/oz_howl.txt)" \
    || echo "  WARNING: failed to create HOWL schedule (see error above)"

# Agent E: Whale Index — daily at 01:00
echo "  Creating: Whale Index (0 1 * * *)"
cat > /tmp/oz_whale_index.txt << 'PROMPT'
Run `bash senpi-waifu/scripts/oz/agent-init.sh` first to configure mcporter and git push credentials. Then:

You are the Whale Index Manager. Run daily rebalance per senpi-skills/whale-index/SKILL.md.

1. git pull in senpi-waifu repo.
2. Scan top 50 Discovery traders via mcporter: discovery_top_traders(limit=50, timeframe="30d").
3. Score: PnL rank (35%), win rate (25%), consistency (20%), hold time (10%), drawdown (10%).
4. Check existing mirror strategies for watch status (2-day watch before swaps).
5. If a trader has degraded for 2+ consecutive days AND a replacement scores 15%+ higher: swap.
6. If no mirror strategies exist yet, present top 3 candidates and create mirrors.
7. Update state with mirror strategy status.
8. Commit and push.
PROMPT
oz-preview schedule create --cron "0 1 * * *" --environment "$ENV_ID" \
    --name "senpi-whale-index" --team \
    --prompt "$(cat /tmp/oz_whale_index.txt)" \
    || echo "  WARNING: failed to create Whale Index schedule (see error above)"

# Agent F: Arena Strategy Learner — every 4 hours
echo "  Creating: Arena Strategy Learner (0 */4 * * *)"
cat > /tmp/oz_arena_learner.txt << 'PROMPT'
Run `bash senpi-waifu/scripts/oz/agent-init.sh` first to configure mcporter and git push credentials. Then:

You are the Arena Strategy Learner. You study the Senpi Predators arena and extract actionable intelligence.

1. git pull in senpi-waifu repo.
2. Read outputs/arena-state.json (written by the VPS arena-monitor every 15min).
3. Analyze the current leaderboard:
   - Which predators are profitable? What strategies do they use? (Check senpi-skills/ for their SKILL.md)
   - What trade frequency correlates with success? (Universally: fewer trades = better)
   - Are any new strategies outperforming our current approach?
4. Compare our own performance (from memory/trade-journal.json) vs the arena:
   - Our win rate vs FOX's win rate
   - Our avg trade duration vs top performers
   - Our fee drag vs theirs
   - Our STALKER vs STRIKER mode performance
5. If our KOMODO scanner is producing entries — how do they perform vs ORCA entries?
6. Generate concrete, data-driven recommendations:
   - Should we tighten entry scores? (if win rate < 50%)
   - Should we widen Phase 1 tolerance? (if most exits are early Phase 1 cuts)
   - Should we favor STALKER vs STRIKER? (based on which mode produces better trades)
   - Should we adjust leverage? (always within 7-10x)
7. Write recommendations to outputs/arena-learnings.json with confidence levels.
8. Auto-apply ONLY risk-reducing changes. Flag risk-increasing suggestions for manual review.
9. Send Telegram summary of key findings.
10. Commit and push.

NEVER increase leverage above 10x. NEVER remove XYZ ban. NEVER disable stagnation TP. These are proven rules from 22 agents.
PROMPT
oz-preview schedule create --cron "0 */4 * * *" --environment "$ENV_ID" \
    --name "senpi-arena-learner" --team \
    --prompt "$(cat /tmp/oz_arena_learner.txt)" \
    || echo "  WARNING: failed to create Arena Learner schedule (see error above)"

# --- 4. Summary ---
echo ""
echo "[4/4] Listing schedules..."
oz-preview schedule list --output-format text 2>/dev/null || echo "(list failed — check oz login)"

echo ""
echo "=== Oz Setup Complete (ORCA Hybrid) ==="
echo ""
echo "Environment ID: $ENV_ID"
echo ""
echo "Scheduled agents:"
echo "  - Trade Evaluator:       every 15 min  (ORCA + KOMODO signal validation)"
echo "  - Regime Classifier:     every hour    (BTC/ETH macro regime)"
echo "  - Portfolio Review:      every 6 hours (risk rails + reporting)"
echo "  - HOWL:                  nightly       (self-improvement + arena comparison)"
echo "  - Whale Index:           daily         (copy-trade rebalance)"
echo "  - Arena Strategy Learner: every 4 hours (study winning predators)"
echo ""
echo "Monitor runs:  oz-preview run list"
echo "Check a run:   oz-preview run get <run-id>"
echo "Manual run:    oz-preview agent run-cloud --environment $ENV_ID --prompt '...'"
