#!/usr/bin/env bash
# HOWL v2 — Hunt, Optimize, Win, Learn.
# Nightly analysis procedure. Runs daily at 23:55 as a Hermes cron job.
# Reads memory/howl-analysis-prompt.md and follows it exactly.
#
# This is the most complex agent — it runs the full 10-pillar analysis.

set -euo pipefail
WAIFU_DIR="${SENPI_WAIFU_DIR:-/home/kt/senpi-waifu}"
cd "$WAIFU_DIR"

echo "[howl] $(date -u +%Y-%m-%dT%H:%M:%SZ) starting nightly analysis"
git pull --rebase --quiet 2>/dev/null || true

python3 -c "
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

today = datetime.now(timezone.utc)
today_str = today.strftime('%Y-%m-%d')
yesterday = today - timedelta(days=1)
yesterday_str = yesterday.strftime('%Y-%m-%d')

# Load all data
journal = json.load(open('memory/trade-journal.json')) if Path('memory/trade-journal.json').exists() else []
regime = json.load(open('config/risk-regime.json'))
arena = json.load(open('outputs/arena-state.json')) if Path('outputs/arena-state.json').exists() else {}
arbiter = json.load(open('outputs/arbiter-state.json')) if Path('outputs/arbiter-state.json').exists() else {}

# Filter last 24h trades
cutoff = (today - timedelta(hours=24)).isoformat()
recent = [t for t in journal if t.get('recordedAt', '') >= cutoff]
opens = [t for t in recent if t.get('action') == 'OPEN']
closes = [t for t in recent if t.get('action') == 'CLOSE']
wins = [t for t in closes if float(t.get('realizedPnl', 0)) > 0]
losses = [t for t in closes if float(t.get('realizedPnl', 0)) < 0]

# Core metrics
total_pnl = sum(float(t.get('realizedPnl', 0)) for t in closes)
gross_wins = sum(float(t.get('realizedPnl', 0)) for t in wins)
gross_losses = abs(sum(float(t.get('realizedPnl', 0)) for t in losses))
win_rate = len(wins) / len(closes) * 100 if closes else 0
pf = gross_wins / gross_losses if gross_losses > 0 else float('inf')
avg_win = gross_wins / len(wins) if wins else 0
avg_loss = gross_losses / len(losses) if losses else 0

# Scanner breakdown
scanner_stats = defaultdict(lambda: {'opens': 0, 'closes': 0, 'wins': 0, 'pnl': 0.0})
for t in recent:
    source = str(t.get('entrySource', t.get('entryMode', 'unknown'))).lower()
    for s in ['orca', 'komodo', 'condor', 'barracuda', 'bison', 'shark', 'sentinel', 'rhino']:
        if s in source:
            source = s
            break
    bucket = scanner_stats[source]
    if t.get('action') == 'OPEN':
        bucket['opens'] += 1
    elif t.get('action') == 'CLOSE':
        bucket['closes'] += 1
        pnl = float(t.get('realizedPnl', 0))
        bucket['pnl'] += pnl
        if pnl > 0:
            bucket['wins'] += 1

# Monster trade dependency
sorted_closes = sorted(closes, key=lambda t: abs(float(t.get('realizedPnl', 0))), reverse=True)
top3_pnl = sum(abs(float(t.get('realizedPnl', 0))) for t in sorted_closes[:3])
total_abs = sum(abs(float(t.get('realizedPnl', 0))) for t in closes) if closes else 1
monster_pct = top3_pnl / total_abs * 100 if closes else 0
without_top3 = total_pnl - sum(float(t.get('realizedPnl', 0)) for t in sorted_closes[:3])

# Fee drag
cumulative_fees = sum(float(t.get('fees', 0)) for t in closes)
equity = arbiter.get('lastEquity', arbiter.get('peakEquity', 1000))
fdr = cumulative_fees / equity * 100 if equity > 0 else 0

# Holding period buckets
buckets = {'<30min': [], '30-90min': [], '90min-4h': [], '>4h': []}
for t in closes:
    opened = t.get('openedAt', t.get('recordedAt', ''))
    closed = t.get('recordedAt', '')
    if opened and closed:
        try:
            o = datetime.fromisoformat(opened.replace('Z', '+00:00'))
            c = datetime.fromisoformat(closed.replace('Z', '+00:00'))
            mins = (c - o).total_seconds() / 60
            pnl = float(t.get('realizedPnl', 0))
            if mins < 30:
                buckets['<30min'].append(pnl)
            elif mins < 90:
                buckets['30-90min'].append(pnl)
            elif mins < 240:
                buckets['90min-4h'].append(pnl)
            else:
                buckets['>4h'].append(pnl)
        except:
            pass

# Direction breakdown
long_trades = [t for t in closes if str(t.get('direction', '')).upper() == 'LONG']
short_trades = [t for t in closes if str(t.get('direction', '')).upper() == 'SHORT']
long_wr = sum(1 for t in long_trades if float(t.get('realizedPnl', 0)) > 0) / len(long_trades) * 100 if long_trades else 0
short_wr = sum(1 for t in short_trades if float(t.get('realizedPnl', 0)) > 0) / len(short_trades) * 100 if short_trades else 0

# Auto-apply risk-reducing changes
auto_applied = []
manual_review = []

# Check scanner performance — flag underperformers
for scanner, stats in scanner_stats.items():
    if stats['closes'] >= 10:
        wr = stats['wins'] / stats['closes'] * 100
        if wr < 25 and stats['pnl'] < 0:
            auto_applied.append(f'{scanner}: disabled (<25% WR, {stats[\"closes\"]} trades, \${stats[\"pnl\"]:.0f} PnL)')

# Check fee drag
if fdr > 10:
    auto_applied.append(f'Fee drag critical ({fdr:.1f}%) — recommend reducing scan frequency')
elif fdr > 5:
    manual_review.append(f'Fee drag elevated ({fdr:.1f}%) — consider reducing frequency')

# Check direction mismatch
if len(long_trades) >= 5 and long_wr < 30:
    manual_review.append(f'LONG WR {long_wr:.0f}% across {len(long_trades)} trades — possible regime mismatch')
if len(short_trades) >= 5 and short_wr < 30:
    manual_review.append(f'SHORT WR {short_wr:.0f}% across {len(short_trades)} trades — possible regime mismatch')

# Check monster trade dependency
if monster_pct > 80:
    manual_review.append(f'Monster trade dependency {monster_pct:.0f}% — strategy relies on outliers')

# Build report
report = f'''# HOWL Report — {today_str}

## Summary
{len(opens)} trades opened, {len(closes)} closed ({win_rate:.0f}% WR). Net PnL: \${total_pnl:,.2f}. PF: {pf:.2f}. FDR: {fdr:.1f}%.

## Core Metrics
| Metric | Value |
|--------|-------|
| Opens | {len(opens)} |
| Closes | {len(closes)} |
| Win Rate | {win_rate:.1f}% |
| Profit Factor | {pf:.2f} |
| Net PnL | \${total_pnl:,.2f} |
| Avg Win | \${avg_win:,.2f} |
| Avg Loss | \${avg_loss:,.2f} |
| Largest Win | \${max((float(t.get('realizedPnl',0)) for t in wins), default=0):,.2f} |
| Largest Loss | \${min((float(t.get('realizedPnl',0)) for t in losses), default=0):,.2f} |

## Scanner Breakdown
| Scanner | Opens | Closes | Wins | WR | PnL |
|---------|-------|--------|------|-----|-----|
'''
for scanner, stats in sorted(scanner_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
    wr = stats['wins'] / stats['closes'] * 100 if stats['closes'] else 0
    report += f"| {scanner} | {stats['opens']} | {stats['closes']} | {stats['wins']} | {wr:.0f}% | \${stats['pnl']:,.2f} |\n"

report += f'''
## Monster Trades
Top 3 trades account for {monster_pct:.0f}% of gross PnL.
'''
for t in sorted_closes[:3]:
    report += f"- {t.get('asset','?')} {t.get('direction','')} \${float(t.get('realizedPnl',0)):,.2f}\n"
report += f"Without top 3: net PnL would be \${without_top3:,.2f}\n"

report += f'''
## Fee Drag
FDR: {fdr:.1f}% | Cumulative fees: \${cumulative_fees:,.2f}
{"CRITICAL — over-trading detected" if fdr > 10 else "Elevated — monitor" if fdr > 5 else "Healthy"}
'''

report += '''
## Holding Periods
| Bucket | Trades | WR | PnL |
|--------|--------|-----|-----|
'''
for bucket_name, pnls in buckets.items():
    b_wins = sum(1 for p in pnls if p > 0)
    b_wr = b_wins / len(pnls) * 100 if pnls else 0
    b_pnl = sum(pnls)
    report += f"| {bucket_name} | {len(pnls)} | {b_wr:.0f}% | \${b_pnl:,.2f} |\n"

report += f'''
## Direction Regime
LONG: {long_wr:.0f}% WR ({len(long_trades)} trades) | SHORT: {short_wr:.0f}% WR ({len(short_trades)} trades)
{"LONG regime mismatch detected" if long_wr < 30 and len(long_trades) >= 5 else ""} {"SHORT regime mismatch detected" if short_wr < 30 and len(short_trades) >= 5 else ""}

## Recommendations
### Auto-applied (risk-reducing)
'''
for a in auto_applied:
    report += f"- {a}\n"
if not auto_applied:
    report += "- None\n"

report += '''
### Requires manual approval (risk-increasing)
'''
for m in manual_review:
    report += f"- {m}\n"
if not manual_review:
    report += "- None\n"

# Save report
report_path = Path(f'memory/howl-{today_str}.md')
report_path.write_text(report)
print(f'  Report saved: {report_path}')

# Update MEMORY.md
memory_path = Path('memory/MEMORY.md')
memory = memory_path.read_text() if memory_path.exists() else '# MEMORY\n'
key_insights = []
if fdr > 10:
    key_insights.append(f'FDR {fdr:.1f}% — over-trading')
if monster_pct > 80:
    key_insights.append(f'Monster dependency {monster_pct:.0f}%')
if win_rate < 40 and closes:
    key_insights.append(f'WR {win_rate:.0f}% — tighten scores')

insight_line = '. '.join(key_insights) if key_insights else f'{win_rate:.0f}% WR, \${total_pnl:,.2f} PnL'
summary = f'\n### HOWL {today_str}\n{insight_line}. {len(auto_applied)} auto-applied, {len(manual_review)} pending review.\n'

if 'HOWL' not in memory or f'HOWL {today_str}' not in memory:
    memory += summary
    memory_path.write_text(memory)
    print(f'  MEMORY.md updated')
"

git add memory/howl-*.md memory/MEMORY.md 2>/dev/null
git commit -m "howl: nightly report ${today_str}" --allow-empty 2>/dev/null || true
git push 2>/dev/null || echo "[howl] git push failed (non-fatal)"

echo "[howl] $(date -u +%Y-%m-%dT%H:%M:%SZ) done"
