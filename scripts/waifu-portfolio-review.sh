#!/usr/bin/env bash
# Portfolio Review — checks risk rails, reviews open positions, writes report.
# Runs every 6 hours as a Waifu cron job.

set -euo pipefail
WAIFU_DIR="${SENPI_WAIFU_DIR:-/home/kt/senpi-waifu}"
cd "$WAIFU_DIR"

echo "[portfolio-review] $(date -u +%Y-%m-%dT%H:%M:%SZ) starting"
git pull --rebase --quiet 2>/dev/null || true

python3 -c "
import json, sys
sys.path.insert(0, '$WAIFU_DIR/scripts/lib')
from senpi_common import mcporter_call
from pathlib import Path
from datetime import datetime, timezone

# 1. Get portfolio
portfolio = mcporter_call('account_get_portfolio', {})

# 2. Read state
regime = json.load(open('config/risk-regime.json'))
arbiter = json.load(open('outputs/arbiter-state.json')) if Path('outputs/arbiter-state.json').exists() else {}
journal = json.load(open('memory/trade-journal.json')) if Path('memory/trade-journal.json').exists() else []

# 3. Count open positions
open_positions = []
state_dir = Path('state')
for strat_dir in state_dir.iterdir():
    if not strat_dir.is_dir() or strat_dir.name.startswith('.'):
        continue
    for dsl_file in strat_dir.glob('dsl-*.json'):
        dsl = json.load(open(dsl_file))
        if dsl.get('active', False):
            open_positions.append(dsl)

# 4. Compute daily PnL
today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
daily_pnl = sum(
    float(t.get('realizedPnl', 0))
    for t in journal
    if t.get('action') == 'CLOSE' and t.get('recordedAt', '').startswith(today)
)
daily_closes = sum(
    1 for t in journal
    if t.get('action') == 'CLOSE' and t.get('recordedAt', '').startswith(today)
)
daily_wins = sum(
    1 for t in journal
    if t.get('action') == 'CLOSE' and t.get('recordedAt', '').startswith(today)
    and float(t.get('realizedPnl', 0)) > 0
)

# 5. Equity tracking
equity = portfolio.get('total_balance_usd', arbiter.get('lastEquity', 0))
peak = arbiter.get('peakEquity', equity)
if equity > peak:
    peak = equity
    arbiter['peakEquity'] = peak
arbiter['lastEquity'] = equity
arbiter['lastCheckAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

day_start = arbiter.get('dayStartEquity', equity)
if arbiter.get('dayStartDate') != today:
    arbiter['dayStartDate'] = today
    arbiter['dayStartEquity'] = equity

drawdown_pct = (peak - equity) / peak * 100 if peak > 0 else 0
daily_loss_pct = (day_start - equity) / day_start * 100 if day_start > 0 else 0

# 6. Guardrail check
guardrails = regime.get('globalGuardrails', {})
alerts = []
mode = regime.get('riskMode', 'BASELINE')

if daily_loss_pct >= guardrails.get('dailyLossLimitPct', 10):
    alerts.append(f'DAILY LOSS LIMIT: {daily_loss_pct:.1f}% >= {guardrails[\"dailyLossLimitPct\"]}%')
if drawdown_pct >= guardrails.get('catastrophicDrawdownPct', 20):
    alerts.append(f'CATASTROPHIC DRAWDOWN: {drawdown_pct:.1f}%')

# 7. Dead weight detection
dead_weight = []
for pos in open_positions:
    roe = float(pos.get('currentRoe', 0) or 0)
    sm_conv = float(pos.get('entrySmConviction', 0) or 0)
    opened = pos.get('openedAt', '')
    if opened:
        try:
            opened_dt = datetime.fromisoformat(opened.replace('Z', '+00:00'))
            minutes_open = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 60
            if roe < -2 and sm_conv == 0 and minutes_open > 30:
                dead_weight.append(f'{pos.get(\"asset\", \"?\")} ({roe:.1f}% ROE, {minutes_open:.0f}min)')
        except:
            pass

# 8. Build report
report = {
    'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'regime': mode,
    'equity': round(float(equity), 2) if equity else 0,
    'peakEquity': round(float(peak), 2) if peak else 0,
    'drawdownPct': round(drawdown_pct, 2),
    'dailyPnl': round(daily_pnl, 2),
    'dailyCloses': daily_closes,
    'dailyWinRate': round(daily_wins / daily_closes * 100, 1) if daily_closes > 0 else 0,
    'openPositions': len(open_positions),
    'alerts': alerts,
    'deadWeight': dead_weight,
    'guardrails': guardrails,
}

Path('outputs/arbiter-state.json').write_text(json.dumps(arbiter, indent=2) + '\n')
Path('outputs/latest-report.json').write_text(json.dumps(report, indent=2) + '\n')

print(f'  Regime: {mode}')
print(f'  Equity: \${report[\"equity\"]:,.2f} | Peak: \${report[\"peakEquity\"]:,.2f}')
print(f'  Drawdown: {drawdown_pct:.1f}% | Daily PnL: \${daily_pnl:,.2f}')
print(f'  Open: {len(open_positions)} | Daily closes: {daily_closes} ({report[\"dailyWinRate\"]}% WR)')
if alerts:
    print(f'  ALERTS: {\" | \".join(alerts)}')
if dead_weight:
    print(f'  DEAD WEIGHT: {\", \".join(dead_weight)}')
"

git add outputs/latest-report.json outputs/arbiter-state.json 2>/dev/null
git commit -m "portfolio-review: update report" --allow-empty 2>/dev/null || true
git push 2>/dev/null || echo "[portfolio-review] git push failed (non-fatal)"

echo "[portfolio-review] $(date -u +%Y-%m-%dT%H:%M:%SZ) done"
